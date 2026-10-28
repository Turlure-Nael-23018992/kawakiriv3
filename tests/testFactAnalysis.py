from kawakiriv3.FactAnalysis.columnAssessment import (
    CONSTANT, DATE, DEGENERATE, EXTRACTED_KEY, FOREIGN_KEY, KEY_CANDIDATE, MEASURE, MOVED_ATTRIBUTE, ROW_IDENTIFIER,
)
from kawakiriv3.FactAnalysis.columnClassifier import ColumnClassifier, looksLikeKeyName
from kawakiriv3.FactAnalysis.dimensionExtractor import DimensionExtractor
from kawakiriv3.FactAnalysis.factAnalyzer import NEEDS_REVIEW, OK, REJECTED, FactAnalyzer
from kawakiriv3.FactAnalysis.factLinker import FactLinker
from kawakiriv3.Schema.schemaEngine import SchemaEngine


def _schema(db):
    schemaEngine = SchemaEngine(db, db.getTableNames()).build()
    return schemaEngine, schemaEngine.inferAllPrimaryKeyCandidates()


def _roles(assessments):
    return {a.columnName: a.role for a in assessments}


def testLooksLikeKeyNameCoversEnglishAndFrenchConventions():
    for name in ("customer_id", "id_client", "code_produit", "productKey", "ref_commande", "id"):
        assert looksLikeKeyName(name), name
    for name in ("quantite", "montant", "valid", "idee"):
        assert not looksLikeKeyName(name), name


def testClassifierSortsTheFactColumns(sales_db):
    schemaEngine, _ = _schema(sales_db)

    roles = _roles(ColumnClassifier(schemaEngine.profilers["ventes"]).classify())

    assert roles == {
        "vente_id": ROW_IDENTIFIER, "date_vente": DATE, "client_id": KEY_CANDIDATE,
        "code_produit": KEY_CANDIDATE, "libelle_produit": KEY_CANDIDATE, "categorie": KEY_CANDIDATE,
        "canal": KEY_CANDIDATE, "quantite": KEY_CANDIDATE, "montant": MEASURE, "statut": CONSTANT,
    }


def testLinkerLinksTheRealForeignKeyButNeverAnUnnamedIntegerOverlap(sales_db):
    schemaEngine, keys = _schema(sales_db)
    assessments = ColumnClassifier(schemaEngine.profilers["ventes"]).classify()

    links = FactLinker(schemaEngine, keys, "ventes", assessments).link()

    # quantite's 1..6 values all exist in clients.client_id: only the name
    # requirement keeps it from being linked.
    assert [(l.sourceColumn, l.target) for l in links] == [("client_id", "clients.client_id")]
    assert _roles(assessments)["client_id"] == FOREIGN_KEY


def testExtractorGroupsFunctionalDependenciesIntoOneDimension(sales_db):
    schemaEngine, keys = _schema(sales_db)
    profiler = schemaEngine.profilers["ventes"]
    assessments = ColumnClassifier(profiler).classify()
    FactLinker(schemaEngine, keys, "ventes", assessments).link()

    starFact, dimensions = DimensionExtractor(sales_db, profiler, "ventes", assessments).extract()

    byKey = {d.keyColumn: d for d in dimensions}
    assert set(byKey) == {"date_key", "code_produit", "canal"}
    assert sorted(byKey["code_produit"].attributeColumns) == ["categorie", "libelle_produit"]
    assert byKey["code_produit"].rowCount == 4
    assert byKey["date_key"].isDate and byKey["date_key"].roleColumns == ["date_vente"]
    roles = _roles(assessments)
    assert roles["code_produit"] == EXTRACTED_KEY
    assert roles["libelle_produit"] == MOVED_ATTRIBUTE
    assert roles["quantite"] == MEASURE
    assert roles["vente_id"] == DEGENERATE
    assert sales_db.getColumns(starFact) == [
        "vente_id", "date_vente", "client_id", "code_produit", "canal", "quantite", "montant",
    ]
    assert sales_db.query("SELECT * FROM \"ventes__dim_code_produit\" WHERE code_produit = 'P03'") == [
        ("P03", "Deco", "Lampe"),
    ]


def testAnalyzerBuildsAnOkStarWhenTheGrainIsUnique(sales_db):
    schemaEngine, keys = _schema(sales_db)

    model = FactAnalyzer(sales_db, schemaEngine, keys, "ventes").analyze()

    assert model.verdict == OK, model.warnings
    assert model.grain.grainColumns == ["date_vente", "client_id", "code_produit", "canal"]
    assert model.grain.isUnique
    assert model.measures == ["quantite", "montant"]


def testNonUniqueGrainNeedsReview(duck_db):
    duck_db.loadTables({"tickets": {
        "num_ticket": ["T1", "T1", "T2", "T3"],
        "produit": ["A", "A", "B", "A"],
        "prix": [1.5, 1.5, 2.0, 1.5],
    }})
    schemaEngine, keys = _schema(duck_db)

    model = FactAnalyzer(duck_db, schemaEngine, keys, "tickets").analyze()

    assert model.verdict == NEEDS_REVIEW
    assert not model.grain.isUnique
    assert model.grain.duplicateRows == 1
    assert any("non unique" in w for w in model.warnings)


def testAFactWithNoDimensionKeyIsRejected(duck_db):
    duck_db.loadTables({"mesures": {"id": [1, 2, 3], "valeur": [1.5, 2.5, 3.5]}})
    schemaEngine, keys = _schema(duck_db)

    model = FactAnalyzer(duck_db, schemaEngine, keys, "mesures").analyze()

    assert model.verdict == REJECTED


def testHeaderLinesPatternWarnsThatTheGrainIsProbablyFiner(duck_db):
    duck_db.loadTables({
        "commandes": {"commande_id": [1, 2, 3], "client": ["a", "b", "a"], "total": [10.0, 20.0, 5.0]},
        "lignes_commande": {
            "ligne_id": [11, 12, 13, 14, 15, 16], "commande_id": [1, 1, 2, 2, 3, 3],
            "produit": ["x", "y", "x", "z", "y", "z"], "prix": [4.0, 6.0, 8.0, 12.0, 2.5, 2.5],
        },
    })
    schemaEngine, keys = _schema(duck_db)

    model = FactAnalyzer(duck_db, schemaEngine, keys, "commandes").analyze()

    assert model.verdict == NEEDS_REVIEW
    assert any("lignes_commande.commande_id" in w for w in model.warnings)
