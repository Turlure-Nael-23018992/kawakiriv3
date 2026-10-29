"""Regression on the full `examples/marge` dataset (regenerated into a
temporary directory), assembled with `examples/job_marge_impose.json`'s own
spec: every case documented in examples/marge/README.md."""
import importlib.util
import json
from pathlib import Path

import pytest

from kawakiriv3.FactAnalysis.factAnalyzer import NEEDS_REVIEW
from kawakiriv3.FactAssembly.assemblySpec import AssemblySpec
from kawakiriv3.FactAssembly.factAssembler import COARSER, FINER, SAME_GRAIN
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.Sources import CsvSource

_ROOT = Path(__file__).resolve().parents[1]
_GENERATOR = _ROOT / "scripts" / "generate_marge_dataset.py"
_JOB = _ROOT / "examples" / "job_marge_impose.json"


@pytest.fixture(scope="module")
def margeResults(tmp_path_factory):
    from kawakiriv3.DuckDbManagement.duckDbManager import duckDbManager
    from kawakiriv3.DuckDbManagement.duckDbUtils import duckDbUtils

    spec = importlib.util.spec_from_file_location("generate_marge_dataset", _GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    folder = tmp_path_factory.mktemp("marge")
    generator.generate(folder)

    manager = duckDbManager(":memory:")
    db = duckDbUtils(manager.conn)
    pipeline = KawakiriV3Pipeline(db)
    loaded = pipeline.ingest(CsvSource(folder))
    assembledFacts = [
        AssemblySpec.model_validate(fact) for fact in json.loads(_JOB.read_text(encoding="utf-8"))["assembledFacts"]
    ]
    results = pipeline.run(list(loaded.values()), assembledFacts=assembledFacts)
    yield db, results
    manager.close()


def testEverySourceTableGetsItsRelationToTheOrderLine(margeResults):
    _, results = margeResults
    assembly = results["models"][0].assembly

    assert {p.table: p.relation for p in assembly.sources} == {
        "lignes_commande": "ancre", "tarification_lignes": SAME_GRAIN, "couts_achat": SAME_GRAIN,
        "expeditions": FINER, "commandes": COARSER,
    }
    assert assembly.rowCount == assembly.anchorRowCount == 7403
    assert assembly.refused == []
    # 208 order lines have no purchase cost (3%): left null, never invented.
    assert {c.name: c.nullRows for c in assembly.columns}["cout_achat"] == 208


def testAmountsReconcileAndTheSixOrphanParcelsAreReported(margeResults):
    _, results = margeResults
    reconciliations = {r.column: r for r in results["models"][0].assembly.reconciliations}

    for column in ("montant_ht", "montant_tva", "cout_achat", "frais_port"):
        assert reconciliations[column].ok, column
    assert reconciliations["nb_colis"].delta == -6
    assert not reconciliations["cout_transport"].ok and reconciliations["cout_transport"].delta < 0
    assert not reconciliations["poids_kg"].ok


def testTheAssembledFactBuildsTheExpectedStar(margeResults):
    db, results = margeResults
    model = results["models"][0]

    assert model.verdict == NEEDS_REVIEW  # only because of the orphan parcels
    assert all(w.startswith("reconciliation de ") for w in model.warnings), model.warnings
    # commandes is the lines' header: its number stays in the fact, degenerate.
    assert sorted(l.target for l in model.links) == ["clients.client_id", "produits.produit_code"]
    assert "commande_id" in model.degenerateColumns
    assert sorted(d.tableName for d in model.dimensions) == ["marge_lignes__dim_canal", "marge_lignes__dim_date"]
    assert model.measures == [
        "quantite", "montant_ht", "montant_tva", "prix_unitaire", "taux_remise", "cout_achat",
        "cout_transport", "poids_kg", "nb_colis", "frais_port",
    ]
    assert model.grain.isUnique
    assert db.getRowCount(model.starFactTable) == 7403


def testStocksAndClientsAreNeverFoldedIntoTheFact(margeResults):
    _, results = margeResults
    consumed = results["models"][0].assembly.consumedTables

    assert "stocks_journaliers" not in consumed and "clients" not in consumed
    assert {"sourceTable": "stocks_journaliers", "sourceColumn": "produit_code", "targetTable": "produits",
            "targetColumn": "produit_code", "cardinality": "N:1", "coverage": 1.0,
            "nameHint": True} in results["physicalLinks"]
