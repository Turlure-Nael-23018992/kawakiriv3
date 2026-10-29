"""Derived tables: the union (column alignment, NULL filling, origin label,
type families, unmapped columns kept) and the dedup (union-find over real
values, first real value per identity in priority order, the correspondence
table), on two different entities -- nothing here is specific to
customers."""
import pandas as pd

from kawakiriv3.DerivedTables.derivedTableBuilder import DerivedTableBuilder
from kawakiriv3.DerivedTables.derivedTableOutcome import BUILT, SKIPPED
from kawakiriv3.DerivedTables.derivedTableSpec import DerivedTableSpec

# Two customer referentials, same people in two id spaces:
#   web 1 / magasin 10 share an email; magasin 10 / web 3 share a phone -> one identity, by transitivity
#   web 2 has a blank email, magasin 11 a NULL one: never merged on them
CLIENTS_WEB = {
    "client_id": [1, 2, 3],
    "email": ["a@x.fr", "  ", None],
    "telephone": ["0601", "0602", "0610"],
    "date_inscription": ["2024-01-01", "2024-02-01", "2024-03-01"],
}
CLIENTS_MAGASIN = {
    "code_client": ["M10", "M11"],
    "mail": ["a@x.fr", None],
    "tel": ["0610", "0699"],
    "carte_fidelite": ["F1", "F2"],
}
# Two product catalogues: the same EAN under two references, one id numeric, the other text.
PRODUITS_A = {"ref": [1, 2], "ean": ["300", "301"], "nom": ["Chaise", "Table"]}
PRODUITS_B = {"sku": ["X9", "X8"], "ean13": ["301", "302"], "designation": ["Table", "Lampe"]}


def _spec(data):
    return DerivedTableSpec.model_validate(data)


CLIENTS_UNION = {
    "name": "clients_union", "kind": "union", "sourceLabelColumn": "origine",
    "unionSources": [
        {"sourceTable": "clients_web", "sourceLabelValue": "web",
         "columnMapping": {"client_id": "client_id", "email": "email", "telephone": "telephone",
                           "date_inscription": "date_inscription"}},
        # Mapping order differs from the first source's: alignment must not depend on it.
        {"sourceTable": "clients_magasin", "sourceLabelValue": "magasin",
         "columnMapping": {"telephone": "tel", "carte_fidelite": "carte_fidelite", "email": "mail",
                           "client_id": "code_client"}},
    ],
}
CLIENTS_DEDUP = {
    "name": "clients_unifies", "kind": "dedup", "dedupSourceTable": "clients_union",
    "dedupMatchColumns": ["email", "telephone"],
}


def _load(db):
    db.loadTables({
        "clients_web": CLIENTS_WEB, "clients_magasin": CLIENTS_MAGASIN,
        "produits_a": PRODUITS_A, "produits_b": PRODUITS_B,
    })
    return db


def _rows(db, sql):
    return db.query(sql)


def testUnionAlignsColumnsByNameFillsNullAndLabelsTheOrigin(duck_db):
    db = _load(duck_db)
    outcome = DerivedTableBuilder(db).buildAll([_spec(CLIENTS_UNION)])["clients_union"]

    assert outcome.status == BUILT
    assert outcome.totalRows == 5
    assert outcome.sourceRowCounts == {"clients_web": 3, "clients_magasin": 2}
    assert db.getColumns("clients_union") == [
        "client_id", "email", "telephone", "date_inscription", "carte_fidelite", "origine",
    ]
    rows = _rows(db, 'SELECT origine, client_id, telephone, date_inscription, carte_fidelite FROM clients_union '
                     'ORDER BY origine, telephone')
    assert rows[0] == ("magasin", "M10", "0610", None, "F1")
    assert rows[2] == ("web", "1", "0601", "2024-01-01", None)


def testUnionCastsMixedTypeFamiliesToTextWithAWarning(duck_db):
    db = _load(duck_db)
    outcome = DerivedTableBuilder(db).buildAll([_spec(CLIENTS_UNION)])["clients_union"]

    assert len(outcome.typeWarnings) == 1
    assert "'client_id'" in outcome.typeWarnings[0]
    assert db.getColumnType("clients_union", "client_id") == "VARCHAR"
    assert db.getColumnType("clients_union", "telephone") == "VARCHAR"


def testUnionOfProductCataloguesWithoutLabelColumn(duck_db):
    db = _load(duck_db)
    spec = _spec({"name": "produits_union", "kind": "union", "unionSources": [
        {"sourceTable": "produits_a", "columnMapping": {"produit_ref": "ref", "ean": "ean", "libelle": "nom"}},
        {"sourceTable": "produits_b", "columnMapping": {"produit_ref": "sku", "ean": "ean13", "libelle": "designation"}},
    ]})
    outcome = DerivedTableBuilder(db).buildAll([spec])["produits_union"]

    assert outcome.status == BUILT
    assert db.getColumns("produits_union") == ["produit_ref", "ean", "libelle"]
    assert db.getRowCount("produits_union") == 4


def testUnknownTableOrColumnSkipsThatSpecOnly(duck_db):
    db = _load(duck_db)
    outcomes = DerivedTableBuilder(db).buildAll([
        _spec({"name": "u1", "kind": "union", "unionSources": [
            {"sourceTable": "absente", "columnMapping": {"a": "a"}}]}),
        _spec({"name": "u2", "kind": "union", "unionSources": [
            {"sourceTable": "produits_a", "columnMapping": {"a": "inexistante"}}]}),
        _spec({"name": "u3", "kind": "dedup", "dedupSourceTable": "produits_a", "dedupMatchColumns": ["code"]}),
        _spec({"name": "u4", "kind": "union", "unionSources": [
            {"sourceTable": "produits_a", "columnMapping": {"ean": "ean"}}]}),
    ])

    assert [o.status for o in outcomes.values()] == [SKIPPED, SKIPPED, SKIPPED, BUILT]
    assert "absente" in outcomes["u1"].reason
    assert "inexistante" in outcomes["u2"].reason
    assert "code" in outcomes["u3"].reason
    assert "u1" not in db.getTableNames()


def testNeverOverwritesAnExistingTable(duck_db):
    db = _load(duck_db)
    outcomes = DerivedTableBuilder(db).buildAll([
        _spec({"name": "produits_a", "kind": "union", "unionSources": [
            {"sourceTable": "produits_b", "columnMapping": {"ean": "ean13"}}]}),
        _spec({"name": "produits", "kind": "union", "unionSources": [
            {"sourceTable": "produits_b", "columnMapping": {"ean": "ean13"}}]}),
        _spec({"name": "produits", "kind": "union", "unionSources": [
            {"sourceTable": "produits_a", "columnMapping": {"ean": "ean"}}]}),
    ])

    statuses = [o.status for o in outcomes.values()]
    assert statuses == [SKIPPED, BUILT, SKIPPED]
    assert db.getColumns("produits_a") == ["ref", "ean", "nom"]
    assert db.getRowCount("produits") == 2


def testLabelColumnCollidingWithAMappedColumnIsSkipped(duck_db):
    db = _load(duck_db)
    outcome = DerivedTableBuilder(db).buildAll([_spec({
        "name": "u", "kind": "union", "sourceLabelColumn": "ean", "unionSources": [
            {"sourceTable": "produits_a", "columnMapping": {"ean": "ean"}}],
    })])["u"]

    assert outcome.status == SKIPPED
    assert "origine" in outcome.reason


def testDedupMergesTransitivelyOnRealValuesOnly(duck_db):
    db = _load(duck_db)
    outcomes = DerivedTableBuilder(db).buildAll([_spec(CLIENTS_UNION), _spec(CLIENTS_DEDUP)])
    outcome = outcomes["clients_unifies"]

    # web 1 ~ magasin 10 (email), magasin 10 ~ web 3 (phone); web 2 and magasin 11 alone.
    assert outcome.status == BUILT
    assert (outcome.sourceRowCount, outcome.resultRowCount, outcome.mergedRowCount) == (5, 3, 2)
    assert outcome.mergedByColumn == {"email": 1, "telephone": 1}
    assert outcome.largestClusterSize == 3
    assert outcome.idColumn == "clients_unifies_id"
    assert db.getColumns("clients_unifies")[0] == "clients_unifies_id"

    merged = _rows(db, "SELECT email, telephone, date_inscription, carte_fidelite FROM clients_unifies "
                       "WHERE clients_unifies_id = 1")
    # First real value of each column in row order: the blank or NULL ones are skipped.
    assert merged == [("a@x.fr", "0601", "2024-01-01", "F1")]


def testCorrespondenceTableMapsEverySourceRowToItsIdentity(duck_db):
    db = _load(duck_db)
    outcome = DerivedTableBuilder(db).buildAll([_spec(CLIENTS_UNION), _spec(CLIENTS_DEDUP)])["clients_unifies"]

    assert outcome.mappingTable == "clients_unifies__correspondance"
    assert db.getColumns(outcome.mappingTable) == [*db.getColumns("clients_union"), "clients_unifies_id"]
    mapping = dict(((origin, key), unified) for origin, key, unified in _rows(
        db, 'SELECT origine, client_id, clients_unifies_id FROM clients_unifies__correspondance'))
    assert len(mapping) == 5
    assert mapping[("web", "1")] == mapping[("magasin", "M10")] == mapping[("web", "3")]
    assert len({mapping[("web", "2")], mapping[("magasin", "M11")], mapping[("web", "1")]}) == 3


def testDedupOfProductsOnEan(duck_db):
    db = _load(duck_db)
    outcomes = DerivedTableBuilder(db).buildAll([
        _spec({"name": "produits_union", "kind": "union", "sourceLabelColumn": "catalogue", "unionSources": [
            {"sourceTable": "produits_a", "sourceLabelValue": "a",
             "columnMapping": {"produit_ref": "ref", "ean": "ean", "libelle": "nom"}},
            {"sourceTable": "produits_b", "sourceLabelValue": "b",
             "columnMapping": {"produit_ref": "sku", "ean": "ean13", "libelle": "designation"}},
        ]}),
        _spec({"name": "produits_unifies", "kind": "dedup", "dedupSourceTable": "produits_union",
               "dedupMatchColumns": ["ean"], "dedupIdColumn": "produit_unifie_id"}),
    ])

    assert outcomes["produits_unifies"].resultRowCount == 3
    unified = dict(((c, r), u) for c, r, u in _rows(
        db, "SELECT catalogue, produit_ref, produit_unifie_id FROM produits_unifies__correspondance"))
    assert unified[("a", "2")] == unified[("b", "X9")]


def testAggressiveMergeIsFlaggedNeverRefused(duck_db):
    db = duck_db
    db.loadTables({"sites": {"site": [1, 2, 3, 4, 5], "pays": ["FR", "FR", "FR", "DE", "IT"]}})
    outcome = DerivedTableBuilder(db).buildAll([_spec({
        "name": "sites_unifies", "kind": "dedup", "dedupSourceTable": "sites", "dedupMatchColumns": ["pays"],
    })])["sites_unifies"]

    assert outcome.status == BUILT
    assert outcome.aggressiveMergeWarning
    assert "ATTENTION" in outcome.summary()


def testDedupIdColumnOrMappingTableCollisionIsSkipped(duck_db):
    db = _load(duck_db)
    db.loadTables({"produits_a_dedup__correspondance": {"x": [1]}})
    outcomes = DerivedTableBuilder(db).buildAll([
        _spec({"name": "pa", "kind": "dedup", "dedupSourceTable": "produits_a",
               "dedupMatchColumns": ["ean"], "dedupIdColumn": "ref"}),
        _spec({"name": "produits_a_dedup", "kind": "dedup", "dedupSourceTable": "produits_a",
               "dedupMatchColumns": ["ean"]}),
    ])

    assert "ref" in outcomes["pa"].reason
    assert "produits_a_dedup__correspondance" in outcomes["produits_a_dedup"].reason
    assert "produits_a_dedup" not in db.getTableNames()


def testUnionKeepsTheColumnsTheMappingLeavesOut(duck_db):
    # Only the EAN is mapped: every other column is kept, a name already
    # taken by a mapped target gets its source's label.
    db = _load(duck_db)
    outcome = DerivedTableBuilder(db).buildAll([_spec({
        "name": "produits_union", "kind": "union", "sourceLabelColumn": "catalogue", "unionSources": [
            {"sourceTable": "produits_a", "sourceLabelValue": "a", "columnMapping": {"ref": "ean"}},
            {"sourceTable": "produits_b", "sourceLabelValue": "b", "columnMapping": {"ref": "ean13"}}],
    })])["produits_union"]

    assert outcome.status == BUILT
    assert db.getColumns("produits_union") == ["ref", "ref_a", "nom", "sku", "designation", "catalogue"]
    assert _rows(db, "SELECT ref, nom, designation FROM produits_union WHERE catalogue = 'b' ORDER BY ref")[0] == (
        "301", None, "Table",
    )


def testUnionWithoutKeepUnmappedColumnsKeepsOnlyTheMapping(duck_db):
    db = _load(duck_db)
    DerivedTableBuilder(db).buildAll([_spec({
        "name": "produits_union", "kind": "union", "keepUnmappedColumns": False, "unionSources": [
            {"sourceTable": "produits_a", "columnMapping": {"ean": "ean"}},
            {"sourceTable": "produits_b", "columnMapping": {"ean": "ean13"}}],
    })])

    assert db.getColumns("produits_union") == ["ean"]


def testUnmappedColumnsSharingANameShareOneColumn():
    spec = _spec({"name": "u", "kind": "union", "unionSources": [
        {"sourceTable": "crm", "columnMapping": {"email": "email"}},
        {"sourceTable": "web", "columnMapping": {"email": "mail"}}]})

    completed = spec.withAllColumns({"crm": ["id", "email", "nom"], "web": ["uid", "mail", "nom"]})

    assert [s.columnMapping for s in completed.unionSources] == [
        {"email": "email", "id": "id", "nom": "nom"}, {"email": "mail", "uid": "uid", "nom": "nom"},
    ]


# Same two people in a CRM and a web shop, each source more recent for one of them.
FICHES_CRM = {"id": [1, 2], "email": ["a@x.fr", "b@x.fr"], "nom": ["Dupont", "Durand"],
              "maj": pd.to_datetime(["2024-01-01", "2024-06-01"])}
FICHES_WEB = {"uid": [7, 8], "mail": ["a@x.fr", "b@x.fr"], "nom": ["DUPONT J.", None],
              "maj": pd.to_datetime(["2024-03-01", "2024-01-01"])}
FICHES_UNION = {"name": "fiches_union", "kind": "union", "sourceLabelColumn": "origine", "unionSources": [
    {"sourceTable": "fiches_crm", "sourceLabelValue": "crm", "columnMapping": {"email": "email"}},
    {"sourceTable": "fiches_web", "sourceLabelValue": "web", "columnMapping": {"email": "mail"}}]}


def _dedupFiches(db, **priority):
    db.loadTables({"fiches_crm": FICHES_CRM, "fiches_web": FICHES_WEB})
    outcomes = DerivedTableBuilder(db).buildAll([_spec(FICHES_UNION), _spec({
        "name": "personnes", "kind": "dedup", "dedupSourceTable": "fiches_union", "dedupMatchColumns": ["email"],
        "dedupIdColumn": "personne_id", **priority})])
    return outcomes["personnes"]


def testDedupTakesTheMostRecentRowFirst(duck_db):
    outcome = _dedupFiches(duck_db, dedupPriorityColumn="maj", dedupSourcePriority=["crm", "web"])

    assert outcome.status == BUILT
    assert outcome.survivorship == "la plus recente selon maj, puis source prioritaire crm > web, puis ordre des lignes"
    # The winning row shows in its modification date.
    assert _rows(duck_db, "SELECT email, nom, CAST(maj AS DATE)::VARCHAR FROM personnes ORDER BY email") == [
        ("a@x.fr", "DUPONT J.", "2024-03-01"), ("b@x.fr", "Durand", "2024-06-01"),
    ]


def testDedupFollowsTheSourcePriorityAndFillsAnEmptyValueFromTheNextSource(duck_db):
    outcome = _dedupFiches(duck_db, dedupSourcePriority=["web", "crm"])

    assert outcome.status == BUILT
    # b@x.fr has no name on the web: taken from the CRM.
    assert _rows(duck_db, "SELECT email, nom, CAST(maj AS DATE)::VARCHAR FROM personnes ORDER BY email") == [
        ("a@x.fr", "DUPONT J.", "2024-03-01"), ("b@x.fr", "Durand", "2024-01-01"),
    ]
    # Both people are in both sources: the origin label becomes one flag per source.
    assert _rows(duck_db, "SELECT present_crm, present_web FROM personnes") == [(True, True), (True, True)]
    # The correspondence table keeps every source row, without the ranking columns.
    assert duck_db.getColumns("personnes__correspondance") == ["email", "id", "nom", "maj", "uid", "origine", "personne_id"]
    assert duck_db.getRowCount("personnes__correspondance") == 4


def testDedupWithoutPriorityKeepsRowOrder(duck_db):
    outcome = _dedupFiches(duck_db)

    assert outcome.survivorship == "ordre des lignes"
    assert _rows(duck_db, "SELECT nom FROM personnes ORDER BY email") == [("Dupont",), ("Durand",)]


def testDedupWithAnUnusablePriorityIsSkipped(duck_db):
    _load(duck_db)
    duck_db.loadTables({"fiches_crm": FICHES_CRM, "fiches_web": FICHES_WEB})
    dedup = {"kind": "dedup", "dedupSourceTable": "fiches_union", "dedupMatchColumns": ["email"]}
    outcomes = DerivedTableBuilder(duck_db).buildAll([
        _spec(FICHES_UNION),
        _spec({**dedup, "name": "d1", "dedupPriorityColumn": "date_modif"}),
        _spec({**dedup, "name": "d2", "dedupPriorityColumn": "nom"}),
        _spec({**dedup, "name": "d3", "dedupSourcePriority": ["erp"]}),
        _spec({"name": "d4", "kind": "dedup", "dedupSourceTable": "produits_a", "dedupMatchColumns": ["ean"],
               "dedupSourcePriority": ["a"]}),
    ])

    assert [outcomes[n].status for n in ("d1", "d2", "d3", "d4")] == [SKIPPED] * 4
    assert "inconnue" in outcomes["d1"].reason
    assert "ni date ni nombre" in outcomes["d2"].reason
    assert "['erp']" in outcomes["d3"].reason
    assert "colonne d'origine" in outcomes["d4"].reason


def testDedupKeepsEachSourceIdAndWhichSourcesAnIdentityIsIn(duck_db):
    # web 1 / magasin M10 / web 3 are one person, web 2 and magasin M11 alone.
    db = _load(duck_db)
    outcome = DerivedTableBuilder(db).buildAll([
        _spec(CLIENTS_UNION), _spec({**CLIENTS_DEDUP, "dedupSourceIdColumns": ["client_id"]}),
    ])["clients_unifies"]

    assert outcome.sourceColumns == {"origine": ["present_web", "present_magasin"],
                                     "client_id": ["client_id_web", "client_id_magasin"]}
    columns = db.getColumns("clients_unifies")
    assert "origine" not in columns and "client_id" not in columns
    assert _rows(db, "SELECT client_id_web, client_id_magasin, present_web, present_magasin FROM clients_unifies "
                     "ORDER BY clients_unifies_id") == [
        ("1", "M10", True, True), ("2", None, True, False), (None, "M11", False, True),
    ]


def testUnionComputesAMeasureFromItsColumns(duck_db):
    duck_db.loadTables({"ventes_a": {"qte": [1, 2], "prix": [10.0, 5.5]}, "ventes_b": {"quantity": [3], "price": [2.0]}})
    outcomes = DerivedTableBuilder(duck_db).buildAll([_spec({
        "name": "ventes", "kind": "union", "sourceLabelColumn": "canal",
        "computedMeasures": [{"name": "montant", "multiply": ["quantite", "prix_unitaire"]}],
        "unionSources": [
            {"sourceTable": "ventes_a", "sourceLabelValue": "a", "columnMapping": {"quantite": "qte", "prix_unitaire": "prix"}},
            {"sourceTable": "ventes_b", "sourceLabelValue": "b",
             "columnMapping": {"quantite": "quantity", "prix_unitaire": "price"}}],
    }), _spec({
        "name": "ventes_bis", "kind": "union", "sourceLabelColumn": "canal",
        "computedMeasures": [{"name": "montant", "multiply": ["quantite", "canal"]}],
        "unionSources": [
            {"sourceTable": "ventes_a", "sourceLabelValue": "a", "columnMapping": {"quantite": "qte"}},
            {"sourceTable": "ventes_b", "sourceLabelValue": "b", "columnMapping": {"quantite": "quantity"}}],
    })])

    assert outcomes["ventes"].status == BUILT
    assert _rows(duck_db, "SELECT canal, montant FROM ventes ORDER BY montant") == [("b", 6.0), ("a", 10.0), ("a", 11.0)]
    assert outcomes["ventes_bis"].status == SKIPPED and "non numeriques" in outcomes["ventes_bis"].reason
