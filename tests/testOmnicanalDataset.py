"""Regression on the full `examples/omnicanal` dataset (regenerated into a
temporary directory), with the unification of
examples/job_omnicanal_impose.json: the two channels' stars built first,
then two unified dimensions (customers and products) built from the stars'
dimensions, each fact's keys replaced by the unified ids, and the two facts
gathered into one."""
import importlib.util
import json
from pathlib import Path

import pytest

from kawakiriv3.FactAnalysis.columnAssessment import FOREIGN_KEY, MEASURE
from kawakiriv3.jobPayload import JobPayload
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.Sources import CsvSource

_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR = _ROOT / "scripts" / "generate_omnicanal_dataset.py"
_JOB = _ROOT / "examples" / "job_omnicanal_impose.json"


def _unification():
    return JobPayload.model_validate(json.loads(_JOB.read_text(encoding="utf-8"))).unification


@pytest.fixture
def omnicanalDb(tmp_path, duck_db):
    spec = importlib.util.spec_from_file_location("generate_omnicanal_dataset", _GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.generate(tmp_path)
    pipeline = KawakiriV3Pipeline(duck_db)
    loaded = pipeline.ingest(CsvSource(tmp_path))
    return pipeline, list(loaded.values()), generator


def _run(omnicanalDb):
    pipeline, tables, _ = omnicanalDb
    return pipeline.run(tables, factTables=["ventes_web", "ventes_boutique"], columnHint=False, unification=_unification())


def _models(results, replaced=False):
    return {model.factTable: model for model in results["replacedModels" if replaced else "models"]}


def testStarsComeFirstThenTheirDimensionsAreUnified(omnicanalDb):
    results = _run(omnicanalDb)
    dimensions = {d["name"]: d for d in results["unification"]["dimensions"]}
    generator = omnicanalDb[2]

    people = generator.N_WEB_ONLY + generator.N_STORE_ONLY + generator.N_SHARED_BY_EMAIL + generator.N_SHARED_BY_PHONE
    assert dimensions["clients_unifies"]["resultRowCount"] == people
    assert dimensions["clients_unifies"]["mergedByColumn"] == {
        "email": generator.N_SHARED_BY_EMAIL, "telephone": generator.N_SHARED_BY_PHONE,
    }
    assert [s["dimension"] for s in dimensions["clients_unifies"]["sources"]] == ["clients_web", "clients_magasin"]
    assert dimensions["produits_unifies"]["resultRowCount"] == (
        generator.N_WEB_ONLY_PRODUCTS + generator.N_STORE_ONLY_PRODUCTS + generator.N_SHARED_PRODUCTS
    )
    assert results["unification"]["source"] == "caller"
    # The unified dimensions never took part in link detection.
    linkedTables = {part.split(".")[0] for d in results["linkDecisions"] for part in d["link"].split(" -> ")}
    assert not linkedTables & {"clients_unifies", "produits_unifies"}
    # The gathered fact replaces the two channels' stars.
    assert sorted(_models(results)) == ["ventes_unifiees"]
    assert sorted(m.starFactTable for m in results["replacedModels"]) == ["ventes_boutique__fact", "ventes_web__fact"]


def testEachChannelsKeysAreReplacedByTheUnifiedIds(omnicanalDb):
    results = _run(omnicanalDb)
    generator = omnicanalDb[2]
    keys = {(k["fact"], k["column"]): k for k in results["unification"]["remappedKeys"]}

    assert sorted((fact, column, k["newColumn"]) for (fact, column), k in keys.items()) == [
        ("ventes_boutique", "code_article", "produit_unifie_id"), ("ventes_boutique", "code_client", "client_unifie_id"),
        ("ventes_web", "client_id", "client_unifie_id"), ("ventes_web", "sku", "produit_unifie_id"),
    ]
    assert keys[("ventes_boutique", "code_client")]["orphanRows"] == generator.N_ORPHAN_TICKETS
    assert keys[("ventes_boutique", "code_client")]["resolvedRows"] == (
        generator.N_STORE_SALES - generator.N_ANONYMOUS_TICKETS - generator.N_ORPHAN_TICKETS
    )
    gathered = _models(results)["ventes_unifiees"]
    assert any("3 ligne(s) sans correspondance dans clients_unifies" in w for w in gathered.warnings)


def testBothChannelsAreGatheredOnTheUnifiedIds(omnicanalDb, duck_db):
    results = _run(omnicanalDb)
    generator = omnicanalDb[2]
    model = _models(results)["ventes_unifiees"]
    columns = set(duck_db.getColumns("ventes_unifiees__fact"))

    assert model.gatheredFacts == ["ventes_web", "ventes_boutique"]
    assert duck_db.getRowCount("ventes_unifiees__fact") == generator.N_WEB_SALES + generator.N_STORE_SALES
    assert {"client_unifie_id", "produit_unifie_id", "canal"} <= columns
    assert not columns & {"client_id", "code_client", "sku", "code_article"}
    assert sorted(link.target for link in model.links) == [
        "clients_unifies.client_unifie_id", "produits_unifies.produit_unifie_id",
    ]
    assert {a.columnName for a in model.assessments if a.role == MEASURE} == {"quantite", "montant"}
    assert {a.columnName for a in model.assessments if a.role == FOREIGN_KEY} == {"client_unifie_id", "produit_unifie_id"}
    # Nothing lost or counted twice.
    assert duck_db.query('SELECT ROUND(SUM(montant), 2) FROM "ventes_unifiees__fact"') == duck_db.query(
        "SELECT ROUND((SELECT SUM(montant) FROM ventes_web) + (SELECT SUM(montant_ttc) FROM ventes_boutique), 2)"
    )


def testSameCustomerOnBothChannelsGetsOneUnifiedId(omnicanalDb, duck_db):
    _run(omnicanalDb)

    shared = duck_db.query("""
        SELECT COUNT(DISTINCT client_unifie_id) FROM ventes_unifiees__fact
        WHERE client_unifie_id IN (SELECT client_unifie_id FROM ventes_unifiees__fact WHERE canal = 'web')
          AND client_unifie_id IN (SELECT client_unifie_id FROM ventes_unifiees__fact WHERE canal = 'boutique')
    """)[0][0]
    assert shared > 0
    assert duck_db.query("""
        SELECT COUNT(*) FROM (
            SELECT f.code_client FROM ventes_boutique__fact f
            JOIN ventes_boutique__fact_unifie u ON u.ticket_id = f.ticket_id
            WHERE u.client_unifie_id IS NOT NULL
            GROUP BY f.code_client HAVING COUNT(DISTINCT u.client_unifie_id) > 1)
    """) == [(0,)]


def testNoUnificationWithoutSpecOrRequest(omnicanalDb):
    pipeline, tables, _ = omnicanalDb
    results = pipeline.run(tables, factTables=["ventes_web"], columnHint=False)
    model = results["models"][0]

    assert model.factTable == "ventes_web" and model.remappedKeys == []
    assert results["unification"]["source"] is None and results["unification"]["dimensions"] == []
