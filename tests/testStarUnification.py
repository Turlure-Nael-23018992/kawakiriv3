"""The post-processing on built stars: the catalogue read off the stars, the
spec checked against it, the unified dimensions, the keys remapped to them
and the facts gathered -- every role taken over from the stars, never
computed again."""
import json

import pytest

from kawakiriv3.FactAnalysis.columnAssessment import (
    DEGENERATE, EXTRACTED_KEY, FOREIGN_KEY, MEASURE, NON_ADDITIVE, SOURCE_STAR, SOURCE_UNIFICATION,
)
from kawakiriv3.FactAnalysis.columnRoles import ColumnRoles
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.StarUnification.starCatalog import CALENDAR, EXTRACTED, LINKED, StarCatalog
from kawakiriv3.StarUnification.unificationSpec import UnificationSpec

TABLES = ["clients_web", "clients_mag", "ventes_web", "ventes_mag"]

# Customers: a@x is web 1 and M3, b@x is web 2 and M1; web 4's blank email
# never merges. Five people.
DATA = {
    "clients_web": {"client_id": [1, 2, 3, 4], "email": ["a@x", "b@x", "c@x", ""],
                    "ville": ["Lyon", "Pau", "Nice", "Caen"]},
    "clients_mag": {"code_client": ["M1", "M2", "M3"], "mail": ["b@x", "d@x", "a@x"], "nom": ["Bob", "Dan", "Ana"]},
    "ventes_web": {
        "vente_id": [101, 102, 103, 104, 105, 106, 107, 108],
        "client_id": [1, 2, 3, 4, 1, 2, 3, 1],
        "produit": ["P1", "P2", "P1", "P3", "P2", "P1", "P3", "P2"],
        "date_vente": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-03",
                       "2024-01-03", "2024-01-04", "2024-01-05", "2024-01-05"],
        "quantite": [1, 2, 3, 1, 2, 3, 1, 2],
        "prix": [10.0, 20.0, 10.0, 30.0, 20.0, 10.0, 30.0, 20.0],
    },
    "ventes_mag": {
        "ticket": ["T1", "T1", "T2", "T3", "T4", "T4"],
        "ligne": [1, 2, 1, 1, 1, 2],
        "code_client": ["M1", "M1", "M2", "M3", "M1", "M1"],
        "article": ["A1", "A2", "A1", "A3", "A2", "A1"],
        "date_ticket": ["2024-01-01", "2024-01-01", "2024-01-06", "2024-01-06", "2024-01-07", "2024-01-07"],
        "qty": [1, 1, 2, 1, 3, 1],
        "prix_unitaire": [5.0, 7.0, 5.0, 9.0, 7.0, 5.0],
        "mode_paiement": ["cb", "cb", "esp", "cb", "esp", "esp"],
    },
}
ROLES = {
    "ventes_web": ColumnRoles.fromLists(keys=["client_id", "produit"], measures=["quantite"], nonAdditive=["prix"]),
    "ventes_mag": ColumnRoles.fromLists(
        keys=["code_client", "article", "mode_paiement"], measures=["qty"], nonAdditive=["prix_unitaire"],
        degenerate=["ticket", "ligne"],
    ),
}
LINKS = ["ventes_web.client_id -> clients_web.client_id", "ventes_mag.code_client -> clients_mag.code_client"]

CUSTOMERS = {
    "name": "clients_unifies", "idColumn": "client_unifie_id",
    "sources": [
        {"dimension": "clients_web", "label": "web", "columnMapping": {"client_ref": "client_id", "email": "email"}},
        {"dimension": "clients_mag", "label": "mag", "columnMapping": {"client_ref": "code_client", "email": "mail"}},
    ],
    "matchColumns": ["email"], "sourcePriority": ["mag", "web"],
}
SALES = {
    "name": "ventes", "labelColumn": "canal",
    "sources": [
        {"fact": "ventes_web", "label": "web", "columnMapping": {
            "quantite": "quantite", "prix": "prix", "date": "date_vente", "produit": "produit", "ref": "vente_id"}},
        {"fact": "ventes_mag", "label": "mag", "columnMapping": {
            "quantite": "qty", "prix": "prix_unitaire", "date": "date_ticket", "produit": "article", "ref": "ticket"}},
    ],
    "computedMeasures": [{"name": "montant", "multiply": ["quantite", "prix"]}],
}


@pytest.fixture
def db(duck_db):
    duck_db.loadTables(DATA)
    for table, column in (("ventes_web", "date_vente"), ("ventes_mag", "date_ticket")):
        duck_db.conn.execute(f"ALTER TABLE {table} ALTER {column} TYPE DATE")
    return duck_db


def _run(db, dimensions=(), facts=()):
    unification = UnificationSpec.model_validate({"dimensions": list(dimensions), "facts": list(facts)})
    results = KawakiriV3Pipeline(db).run(
        TABLES, factTables=["ventes_web", "ventes_mag"], columnHint=False, columnRoles=ROLES, declaredLinks=LINKS,
        unification=unification,
    )
    return results, {model.factTable: model for model in results["models"]}


def _stars(db):
    results = KawakiriV3Pipeline(db).run(
        TABLES, factTables=["ventes_web", "ventes_mag"], columnHint=False, columnRoles=ROLES, declaredLinks=LINKS,
    )
    return results, StarCatalog.build(db, [(m.factTable, m, None) for m in results["models"]])


def _roles(model):
    return {a.columnName: a for a in model.assessments}


# --- catalogue ----------------------------------------------------------------

def testTheCatalogueReadsEachKeysDimensionOffTheStar(db):
    _, catalog = _stars(db)

    assert {name: d.kind for name, d in catalog.dimensions.items()} == {
        "clients_web": LINKED, "clients_mag": LINKED, "ventes_web__dim_produit": EXTRACTED,
        "ventes_web__dim_date": CALENDAR, "ventes_mag__dim_article": EXTRACTED,
        "ventes_mag__dim_mode_paiement": EXTRACTED, "ventes_mag__dim_date": CALENDAR,
    }
    assert catalog.dimensions["clients_mag"].keyColumn == "code_client"
    assert catalog.dimensions["clients_mag"].columnNames == ["code_client", "mail", "nom"]
    assert catalog.dimensions["clients_web"].usedBy == ["ventes_web"]
    web = catalog.stars["ventes_web"].columns
    assert (web["client_id"].role, web["client_id"].dimension) == (FOREIGN_KEY, "clients_web")
    assert web["prix"].additivity == NON_ADDITIVE


def testTheSummaryCarriesNoRealValue(db):
    _, catalog = _stars(db)
    text = json.dumps(catalog.summary())

    for value in ("a@x", "Bob", "Lyon", "P1", "A1", "cb", "T1", "2024-01-01"):
        assert value not in text
    dimension = next(d for d in catalog.summary()["dimensions"] if d["dimension"] == "clients_web")
    assert dimension["columns"][1] == {"columnName": "email", "inferredType": "string", "nullRatio": 0.0}


# --- spec ---------------------------------------------------------------------

@pytest.mark.parametrize("change, message", [
    (lambda d: d["sources"][0].update(dimension="clients"), "n'est une dimension d'aucune etoile"),
    (lambda d: d["sources"][0].update(dimension="ventes_web__dim_date"), "est un calendrier"),
    (lambda d: d["sources"][0]["columnMapping"].update(email="courriel"), "colonnes inconnues dans la dimension"),
    (lambda d: d.update(matchColumns=["telephone"]), "colonnes de rapprochement inconnues"),
    (lambda d: d.update(priorityColumn="ville"), "ni date ni nombre"),
    (lambda d: d.update(sourcePriority=["boutique"]), "sources de priorite inconnues"),
    (lambda d: d["sources"][1].update(label="web"), "libelles de source en double"),
    (lambda d: d.update(idColumn="email"), "est deja une colonne"),
])
def testADimensionSpecOnlyNamesWhatTheStarsHave(db, change, message):
    _, catalog = _stars(db)
    dimension = json.loads(json.dumps(CUSTOMERS))
    change(dimension)

    errors = UnificationSpec.model_validate({"dimensions": [dimension]}).errorsAgainst(catalog)

    assert any(message in e for e in errors["clients_unifies"]), errors


@pytest.mark.parametrize("change, message", [
    (lambda f: f["sources"][0].update(fact="ventes"), "n'est le fait d'aucune etoile"),
    (lambda f: f["sources"][0]["columnMapping"].update(quantite="libelle"), "colonnes inconnues dans le fait"),
    (lambda f: f["computedMeasures"][0].update(multiply=["quantite", "produit"]), "colonnes non numeriques"),
    (lambda f: f.update(nonAdditiveMeasures=["remise"]), "mesures non additives inconnues"),
    (lambda f: f.update(labelColumn="prix"), "colonne d'origine"),
])
def testAFactSpecOnlyNamesColumnsOfTheStarFacts(db, change, message):
    _, catalog = _stars(db)
    fact = json.loads(json.dumps(SALES))
    change(fact)

    errors = UnificationSpec.model_validate({"facts": [fact]}).errorsAgainst(catalog)

    assert any(message in e for e in errors["ventes"]), errors


def testAnInvalidSpecIsSkippedWithItsReason(db):
    results, models = _run(db, dimensions=[{**CUSTOMERS, "matchColumns": ["telephone"]}])

    outcome = results["unification"]["dimensions"][0]
    assert outcome["status"] == "skipped" and "telephone" in outcome["reason"]
    assert results["unification"]["remappedKeys"] == []
    assert sorted(models) == ["ventes_mag", "ventes_web"]


# --- unified dimensions -------------------------------------------------------

def testTheUnifiedDimensionKeepsEveryAttributeAndEachSourcesKey(db):
    results, _ = _run(db, dimensions=[CUSTOMERS])
    outcome = results["unification"]["dimensions"][0]

    assert (outcome["status"], outcome["sourceRowCount"], outcome["resultRowCount"]) == ("built", 7, 5)
    assert outcome["mergedByColumn"] == {"email": 2}
    assert outcome["sourceColumns"] == {
        "origine": ["present_web", "present_mag"], "client_ref": ["client_ref_web", "client_ref_mag"],
    }
    assert {"client_unifie_id", "email", "ville", "nom", "present_web", "present_mag"} <= set(
        db.getColumns("clients_unifies")
    )
    # b@x: the store's record wins (sourcePriority), the web fills what it lacks.
    assert db.query(
        "SELECT client_ref_web, client_ref_mag, nom, ville FROM clients_unifies WHERE email = 'b@x'"
    ) == [("2", "M1", "Bob", "Pau")]
    assert results["tableColumns"]["clients_unifies"][0] == ("client_unifie_id", "numeric")


# --- remapped keys ------------------------------------------------------------

def testKeysAreReplacedInPlaceRowsAndOrderKept(db):
    results, models = _run(db, dimensions=[CUSTOMERS])
    web = models["ventes_web"]

    assert web.starFactTable == "ventes_web__fact_unifie"
    assert db.getColumns("ventes_web__fact_unifie") == [
        c if c != "client_id" else "client_unifie_id" for c in db.getColumns("ventes_web__fact")
    ]
    assert [r[0] for r in db.query("SELECT vente_id FROM ventes_web__fact_unifie")] == DATA["ventes_web"]["vente_id"]
    key = _roles(web)["client_unifie_id"]
    assert (key.role, key.linkedTo, key.roleSource) == (FOREIGN_KEY, "clients_unifies.client_unifie_id", SOURCE_UNIFICATION)
    assert [l.target for l in web.links] == ["clients_unifies.client_unifie_id"]
    assert [k.newColumn for k in web.remappedKeys] == ["client_unifie_id"]
    # Same person on both channels, same id; the replaced stars are kept apart.
    assert db.query("""
        SELECT DISTINCT w.client_unifie_id = m.client_unifie_id FROM ventes_web__fact_unifie w
        JOIN ventes_web__fact o ON o.vente_id = w.vente_id, ventes_mag__fact_unifie m
        JOIN ventes_mag__fact p ON p.ticket = m.ticket AND p.ligne = m.ligne
        WHERE o.client_id = 2 AND p.code_client = 'M1'
    """) == [(True,)]
    assert sorted(m.starFactTable for m in results["replacedModels"]) == ["ventes_mag__fact", "ventes_web__fact"]
    assert {k["column"]: k["orphanRows"] for k in results["unification"]["remappedKeys"]} == {
        "client_id": 0, "code_client": 0,
    }


def testEverythingButTheRemappedKeyIsTakenOverFromTheStar(db):
    stars, _ = _stars(db)
    star = next(m for m in stars["models"] if m.factTable == "ventes_web")
    _, models = _run(db, dimensions=[CUSTOMERS])
    copy = models["ventes_web"]

    untouched = lambda model: [
        (a.columnName, a.role, a.roleSource, a.additivity, a.dimensionTable)
        for a in model.assessments if a.columnName not in ("client_id", "client_unifie_id")
    ]
    assert untouched(copy) == untouched(star)
    assert [d.tableName for d in copy.dimensions] == [d.tableName for d in star.dimensions]


# --- gathered facts -----------------------------------------------------------

def testGatheredRolesAreTakenOverFromTheStars(db):
    results, models = _run(db, dimensions=[CUSTOMERS], facts=[SALES])
    model = models["ventes"]
    roles = _roles(model)

    assert sorted(models) == ["ventes"] and model.gatheredFacts == ["ventes_web", "ventes_mag"]
    assert db.getRowCount("ventes__fact") == 14
    # measure everywhere, non additive in the stars, computed
    assert roles["quantite"].role == MEASURE and roles["quantite"].additivity is None
    assert (roles["prix"].role, roles["prix"].additivity) == (MEASURE, NON_ADDITIVE)
    assert (roles["montant"].role, roles["montant"].roleSource) == (MEASURE, SOURCE_UNIFICATION)
    assert roles["montant"].reason == "calculee : quantite x prix"
    # a date: the common calendar, every day of both stars
    assert (roles["date"].role, roles["date"].dimensionTable) == (EXTRACTED_KEY, "ventes__dim_date")
    assert db.getRowCount("ventes__dim_date") == 7
    # the same unified dimension, under the unified id's name
    assert (roles["client_unifie_id"].role, roles["client_unifie_id"].linkedTo) == (
        FOREIGN_KEY, "clients_unifies.client_unifie_id",
    )
    # extracted keys aligned: a conformed dimension, by value
    assert roles["produit"].dimensionTable == "ventes__dim_produit"
    assert [r[0] for r in db.query("SELECT produit FROM ventes__dim_produit")] == ["A1", "A2", "A3", "P1", "P2", "P3"]
    # a key of one fact only: that fact's dimension, empty for the other channel
    assert roles["mode_paiement"].dimensionTable == "ventes_mag__dim_mode_paiement"
    assert db.query("SELECT COUNT(*) FROM ventes__fact WHERE canal = 'web' AND mode_paiement IS NULL") == [(8,)]
    # degenerate stays degenerate
    assert roles["ref"].role == DEGENERATE and roles["ligne"].role == DEGENERATE
    assert roles["canal"].dimensionTable == "ventes__dim_canal"
    assert roles["ref"].roleSource == SOURCE_STAR and roles["ref"].reason.startswith(
        "repris des etoiles ventes_web, ventes_mag"
    )
    assert sorted(model.grain.grainColumns) == ["canal", "client_unifie_id", "date", "mode_paiement", "produit"]
    assert [d.tableName for d in model.dimensions] == [
        "ventes__dim_date", "ventes__dim_produit", "ventes_mag__dim_mode_paiement", "ventes__dim_canal",
    ]
    assert sorted(m.factTable for m in results["replacedModels"]) == ["ventes_mag", "ventes_web"]
    assert results["unification"]["facts"][0]["status"] == "built"


def testNothingIsLostOrCountedTwice(db):
    _run(db, dimensions=[CUSTOMERS], facts=[SALES])

    assert db.query("SELECT SUM(quantite), SUM(montant) FROM ventes__fact") == db.query(
        "SELECT (SELECT SUM(quantite) FROM ventes_web) + (SELECT SUM(qty) FROM ventes_mag), "
        "(SELECT SUM(quantite * prix) FROM ventes_web) + (SELECT SUM(qty * prix_unitaire) FROM ventes_mag)"
    )


def testKeysTheStarsLinkedToDifferentTablesStayAReference(db):
    # Customers not unified: each channel's key points to its own table.
    sales = json.loads(json.dumps(SALES))
    sales["sources"][0]["columnMapping"]["client"] = "client_id"
    sales["sources"][1]["columnMapping"]["client"] = "code_client"
    _, models = _run(db, facts=[sales])
    model = models["ventes"]

    assert _roles(model)["client"].role == DEGENERATE
    assert any("client : les etoiles ne s'accordent pas" in w for w in model.warnings)
    assert "client" not in model.grain.grainColumns
