"""The physical link graph and the fact assembler: the grain algebra (same
grain, coarser, finer, unreachable), allocation, reconciliation, and the
assembled fact going through the usual v3 chain."""
import pytest

from kawakiriv3.FactAnalysis.columnAssessment import (
    DEGENERATE, EXTRACTED_KEY, FOREIGN_KEY, MEASURE, SOURCE_CALLER, SOURCE_LLM,
)
from kawakiriv3.FactAssembly.assemblySpec import AssemblySpec
from kawakiriv3.FactAssembly.factAssembler import COARSER, FINER, SAME_GRAIN, UNREACHABLE, FactAssembler
from kawakiriv3.FactAssembly.linkGraph import LinkGraph
from kawakiriv3.FactHint.factHintAdvisor import FactHintAdvisor
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.Schema.schemaEngine import SchemaEngine

# Order lines (the anchor) and their measures spread over:
#   tarifs    same grain, 1:1, line 8 has no price  -> copied, one null
#   colis     finer: 0..4 parcels per line, plus one orphan (ligne_id 99)
#   commandes coarser: one shipping fee per order, over its lines
#   stocks    another process, linked to nothing the lines reach
LIGNES = {
    "ligne_id": [1, 2, 3, 4, 5, 6, 7, 8],
    "commande_id": [100, 100, 101, 101, 101, 102, 103, 103],
    "produit_code": ["A", "B", "A", "C", "B", "C", "A", "B"],
    "quantite": [1, 2, 3, 1, 2, 4, 1, 1],
    "bonus": [0, 0, 0, 0, 0, 0, 0, 0],
}
TARIFS = {
    "ligne_id": [7, 1, 2, 3, 4, 5, 6],
    "montant_ht": [10.0, 10.0, 20.0, 30.0, 10.0, 20.0, 40.0],
    "prix_unitaire": [10.0, 10.0, 10.0, 10.0, 10.0, 10.0, 10.0],
}
COLIS_LIGNES = [1, 1, 1, 2, 2, 3, 3, 3, 3, 4, 5, 5, 5, 6, 6, 6, 7, 7, 7, 99]
COLIS = {
    "colis_id": [f"K{i:02d}" for i in range(len(COLIS_LIGNES))],
    "ligne_id": COLIS_LIGNES,
    "cout": [1.5] * 19 + [5.0],
    "transporteur": ["poste", "express"] * 10,
}
COMMANDES = {
    "commande_id": [100, 101, 102, 103],
    "client_id": [1, 2, 1, 3],
    "canal": ["web", "web", "magasin", "web"],
    "frais_port": [6.0, 9.0, 0.0, 3.0],
    "taux_change": [1.0, 1.1, 1.0, 0.9],
    "solde_client": [50.0, 20.0, 50.0, 70.0],
}
CLIENTS = {"client_id": [1, 2, 3], "nom": ["Alpha", "Beta", "Gamma"]}
PRODUITS = {"produit_code": ["A", "B", "C"], "libelle": ["Agrafes", "Bloc", "Crayon"]}
STOCKS = {
    "produit_code": ["A", "A", "B", "B", "C", "C"],
    "jour": ["2025-01-01", "2025-01-02"] * 3,
    "stock": [5, 6, 7, 8, 9, 3],
}
TABLES = ["lignes", "tarifs", "colis", "commandes", "clients", "produits", "stocks"]


@pytest.fixture
def ordersDb(duck_db):
    duck_db.loadTables({
        "lignes": LIGNES, "tarifs": TARIFS, "colis": COLIS, "commandes": COMMANDES,
        "clients": CLIENTS, "produits": PRODUITS, "stocks": STOCKS,
    })
    return duck_db


def _engine(db):
    schemaEngine = SchemaEngine(db, TABLES).build()
    keys = schemaEngine.inferAllPrimaryKeyCandidates()
    return schemaEngine, LinkGraph.build(schemaEngine, keys)


def _assemble(db, measures, keys=()):
    schemaEngine, graph = _engine(db)
    spec = AssemblySpec.model_validate(
        {"name": "fait", "anchorTable": "lignes", "measures": measures, "keys": list(keys)}
    )
    return FactAssembler(db, schemaEngine, graph, spec).assemble()


def _column(db, column, table="fait"):
    return [row[0] for row in db.query(f'SELECT "{column}" FROM "{table}" ORDER BY ligne_id')]


# --- link graph -------------------------------------------------------------

def testLinkGraphKeepsCorroboratedLinksWithTheirCardinality(ordersDb):
    _, graph = _engine(ordersDb)

    links = {l.describe() for l in graph.links}
    assert "lignes.commande_id -> commandes.commande_id (N:1)" in links
    assert "tarifs.ligne_id -> lignes.ligne_id (1:1)" in links
    assert "colis.ligne_id -> lignes.ligne_id (N:1)" in links
    assert "commandes.client_id -> clients.client_id (N:1)" in links
    # client_id 1..3 also falls inside ligne_id 1..8: two keys overlapping by chance.
    assert not [l for l in graph.links if l.sourceColumn == "client_id" and l.targetTable != "clients"]
    assert not [l for l in graph.links if l.sourceColumn in ("quantite", "stock")]


def testShortestPathsFollowLinksFromTheirSource(ordersDb):
    _, graph = _engine(ordersDb)

    paths = graph.shortestPaths("lignes")

    assert [l.describe() for l in paths["clients"][0]] == [
        "lignes.commande_id -> commandes.commande_id (N:1)", "commandes.client_id -> clients.client_id (N:1)",
    ]
    assert "colis" not in paths and "stocks" not in paths
    assert "lignes" in graph.shortestPaths("colis")


# --- grain algebra ------------------------------------------------------------

def testEachTableGetsItsRelationToTheGrain(ordersDb):
    result = _assemble(ordersDb, [
        {"table": "tarifs", "column": "montant_ht"},
        {"table": "colis", "column": "cout"},
        {"table": "commandes", "column": "frais_port", "allocation": "egal"},
        {"table": "stocks", "column": "stock", "additivity": "semi_additive"},
    ])

    relations = {plan.table: plan.relation for plan in result.sources}
    assert relations == {"tarifs": SAME_GRAIN, "colis": FINER, "commandes": COARSER, "stocks": UNREACHABLE}
    assert result.rowCount == result.anchorRowCount == 8
    assert sorted(result.consumedTables) == ["colis", "lignes", "tarifs"]
    assert [r.column for r in result.refused] == ["stock"]


def testSameGrainMeasuresAreCopiedAndAMissingRowStaysNull(ordersDb):
    result = _assemble(ordersDb, [{"table": "tarifs", "column": "montant_ht"}])

    assert _column(ordersDb, "montant_ht") == [10.0, 20.0, 30.0, 10.0, 20.0, 40.0, 10.0, None]
    assert result.columns[0].nullRows == 1
    assert result.reconciliations[0].ok


def testFinerRowsAreAggregatedBeforeTheJoinAndOrphansShowInTheReconciliation(ordersDb):
    result = _assemble(ordersDb, [
        {"table": "colis", "column": "cout"},
        {"table": "colis", "column": "colis_id", "aggregation": "count", "alias": "nb_colis"},
    ])

    assert _column(ordersDb, "nb_colis") == [3, 2, 4, 1, 3, 3, 3, 0]
    assert _column(ordersDb, "cout") == [4.5, 3.0, 6.0, 1.5, 4.5, 4.5, 4.5, 0.0]
    byColumn = {r.column: r for r in result.reconciliations}
    assert (byColumn["cout"].sourceTotal, byColumn["cout"].assembledTotal) == (33.5, 28.5)
    assert byColumn["nb_colis"].delta == -1 and not byColumn["nb_colis"].ok
    assert any("ne se rattachent a aucune ligne de lignes" in w for w in result.warnings)


def testACoarserAdditiveMeasureIsRefusedWithoutAllocation(ordersDb):
    result = _assemble(ordersDb, [{"table": "commandes", "column": "frais_port"}])

    assert [r.column for r in result.refused] == ["frais_port"]
    assert "comptee plusieurs fois" in result.refused[0].reason
    assert "frais_port" not in ordersDb.getColumns("fait")


def testProrataAllocationSpreadsTheParentValueAndReconciles(ordersDb):
    result = _assemble(ordersDb, [
        {"table": "tarifs", "column": "montant_ht"},
        {"table": "commandes", "column": "frais_port", "allocation": "prorata:montant_ht"},
    ])

    # Order 100: 6.0 over 10/20; order 103: line 8 has no price, line 7 takes it all.
    assert _column(ordersDb, "frais_port") == pytest.approx([2.0, 4.0, 4.5, 1.5, 3.0, 0.0, 3.0, 0.0])
    assert all(r.ok for r in result.reconciliations)


def testEqualAllocationAndAllZeroWeightsFallBackToEqualShares(ordersDb):
    _assemble(ordersDb, [{"table": "commandes", "column": "frais_port", "allocation": "egal"}])
    equal = _column(ordersDb, "frais_port")
    result = _assemble(ordersDb, [{"table": "commandes", "column": "frais_port", "allocation": "prorata:bonus"}])

    assert equal == pytest.approx([3.0, 3.0, 3.0, 3.0, 3.0, 0.0, 1.5, 1.5])
    assert _column(ordersDb, "frais_port") == pytest.approx(equal)
    assert all(r.ok for r in result.reconciliations)


def testProrataOnAColumnOutsideTheFactIsRefused(ordersDb):
    result = _assemble(ordersDb, [{"table": "commandes", "column": "frais_port", "allocation": "prorata:poids"}])

    assert "pas une colonne numerique du fait assemble" in result.refused[0].reason


def testCoarserNonAdditiveIsRepeatedButSemiAdditiveIsRefused(ordersDb):
    result = _assemble(ordersDb, [
        {"table": "commandes", "column": "taux_change", "additivity": "non_additive"},
        {"table": "commandes", "column": "solde_client", "additivity": "semi_additive"},
    ])

    assert _column(ordersDb, "taux_change") == [1.0, 1.0, 1.1, 1.1, 1.1, 1.0, 0.9, 0.9]
    assert [r.column for r in result.refused] == ["solde_client"]
    assert result.reconciliations == []


def testFinerNonAdditiveNeedsAnExplicitAggregation(ordersDb):
    result = _assemble(ordersDb, [
        {"table": "colis", "column": "cout", "additivity": "non_additive"},
        {"table": "colis", "column": "cout", "additivity": "non_additive", "aggregation": "sum"},
        {"table": "colis", "column": "cout", "additivity": "non_additive", "aggregation": "max"},
    ])

    assert [r.reason.split(":")[0] for r in result.refused] == [
        "mesure non_additive d'une table plus fine que le grain ", "une somme d'une mesure non_additive serait fausse",
    ]
    assert _column(ordersDb, "max_cout")[0] == 1.5


def testKeysComeFromCoarserTablesNeverFromFinerOnes(ordersDb):
    result = _assemble(
        ordersDb, [{"table": "tarifs", "column": "montant_ht"}],
        keys=[{"table": "commandes", "column": "client_id"}, {"table": "colis", "column": "transporteur"},
              {"table": "lignes", "column": "produit_code"}],
    )

    assert _column(ordersDb, "client_id") == [1, 1, 2, 2, 2, 1, 3, 3]
    assert [r.column for r in result.refused] == ["transporteur"]
    roles = result.columnRoles(source=SOURCE_LLM)
    assert roles.roleOf("client_id") == ("key", SOURCE_LLM)
    assert roles.roleOf("produit_code") == ("key", SOURCE_LLM)
    assert roles.roleOf("montant_ht") == ("measure", SOURCE_LLM)


def testAKeyThatIsAJoinColumnIsNotCopiedTwice(ordersDb):
    result = _assemble(
        ordersDb, [{"table": "tarifs", "column": "montant_ht"}],
        keys=[{"table": "commandes", "column": "commande_id"}, {"table": "clients", "column": "client_id"},
              {"table": "commandes", "column": "client_id"}, {"table": "produits", "column": "produit_code"}],
    )

    # commandes.commande_id IS lignes.commande_id, clients.client_id IS commandes.client_id.
    assert [c.name for c in result.columns if c.kind == "cle"] == ["client_id"]
    assert sorted(result.declaredKeys) == ["commande_id", "produit_code"]
    assert _column(ordersDb, "client_id") == [1, 1, 2, 2, 2, 1, 3, 3]
    assert not [c for c in ordersDb.getColumns("fait") if c.endswith(("_commande_id", "_client_id", "_produit_code"))]


def testAColumnNameAlreadyInTheFactIsPrefixedWithItsTable(ordersDb):
    result = _assemble(ordersDb, [{"table": "commandes", "column": "commande_id", "additivity": "non_additive"}])

    assert result.columns[0].name == "commandes_commande_id"


def testUnknownTablesAndColumnsAreRefusedNotRaised(ordersDb):
    result = _assemble(ordersDb, [{"table": "tarifs", "column": "absente"}, {"table": "inconnue", "column": "x"}])

    assert [r.reason for r in result.refused] == ["colonne inconnue dans cette table", "table inconnue"]
    assert result.tableName == "fait"


# --- pipeline -----------------------------------------------------------------

SPEC = {
    "name": "marge", "anchorTable": "lignes", "justification": "une ligne = un produit commande",
    "measures": [
        {"table": "lignes", "column": "quantite"},
        {"table": "tarifs", "column": "montant_ht"},
        {"table": "colis", "column": "cout"},
        {"table": "commandes", "column": "frais_port", "allocation": "prorata:montant_ht"},
    ],
    "keys": [{"table": "commandes", "column": "client_id"}],
}


class _NoCallAdvisor:
    def proposeFactTables(self, *args, **kwargs):
        raise AssertionError("aucun appel LLM attendu")


def testImposedAssembledFactRunsTheV3ChainWithoutLlm(ordersDb):
    results = KawakiriV3Pipeline(ordersDb, factHintAdvisor=_NoCallAdvisor()).run(
        TABLES, assembledFacts=[AssemblySpec.model_validate(SPEC)],
    )

    model = results["models"][0]
    assert results["factTableSource"] == "caller"
    assert model.factTable == "marge" and model.starFactTable == "marge__fact"
    # commandes is the lines' header: no dimension, its number is degenerate.
    assert sorted(l.target for l in model.links) == ["clients.client_id", "produits.produit_code"]
    assert model.assembly.headerTables == ["commandes"]
    byName = {a.columnName: a for a in model.assessments}
    assert byName["commande_id"].role == DEGENERATE
    assert byName["montant_ht"].role == MEASURE and byName["montant_ht"].roleSource == SOURCE_CALLER
    assert byName["ligne_id"].role != FOREIGN_KEY  # never linked back to the tables folded into the fact
    assert not [w for w in model.warnings if "le grain demande est peut-etre" in w]
    assert any(w.startswith("reconciliation de cout") for w in model.warnings)  # the orphan parcel
    assert model.grain.isUnique
    assert results["physicalLinks"]


class _AssemblingAdvisor:
    lastError = None
    lastReasoning = "les montants sont repartis"
    lastTokenUsage = None
    rejectedTableNames = []
    lastColumnRolesByTable = {}
    lastColumnRolesErrors = {}
    rejectedColumnNames = {}
    rejectedAssembledFacts = []

    def __init__(self):
        self.lastAssembledFacts = [AssemblySpec.model_validate(SPEC)]
        self.physicalLinks = None

    def proposeFactTables(self, tablesSummary, businessNeed=None, contextType=None, factTables=None,
                          classifyColumns=True, physicalLinks=None):
        self.physicalLinks = physicalLinks
        return []

    lastLinkError = None
    lastLinkTokenUsage = None

    def arbitrateLinks(self, tablesSummary, candidateLinks, businessNeed=None, contextType=None):
        return {}


def testTheLlmCanProposeAnAssembledFactInsteadOfATable(ordersDb):
    advisor = _AssemblingAdvisor()

    results = KawakiriV3Pipeline(ordersDb, factHintAdvisor=advisor).run(TABLES, businessNeed="marge")

    assert results["factTableSource"] == "llm_hint"
    assert {"sourceTable": "colis", "sourceColumn": "ligne_id", "targetTable": "lignes", "targetColumn": "ligne_id",
            "cardinality": "N:1", "coverage": 0.95, "nameHint": True} in advisor.physicalLinks
    model = results["models"][0]
    assert model.assembly.anchorTable == "lignes"
    assert {a.columnName: a.roleSource for a in model.assessments}["frais_port"] == SOURCE_LLM


class _Response:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _Session:
    def __init__(self, body):
        self.body = body
        self.posted = None

    def post(self, url, json=None, timeout=None):
        self.posted = json
        return _Response(self.body)


def testFactHintAdvisorKeepsValidAssembledFactsAndRejectsTheOthers():
    session = _Session({
        "factTables": [], "reasoning": "r",
        "assembledFacts": [
            SPEC,
            {**SPEC, "name": "autre", "measures": [{"table": "fantome", "column": "x", "additivity": "additive"}]},
            {**SPEC, "name": "sans mesure", "measures": []},
        ],
    })
    advisor = FactHintAdvisor(baseUrl="http://advisor", session=session)
    summary = [{"tableName": t, "columns": []} for t in TABLES]

    hints = advisor.proposeFactTables(summary, "marge", physicalLinks=[{"sourceTable": "colis"}])

    assert hints == [] and advisor.lastError is None
    assert [spec.name for spec in advisor.lastAssembledFacts] == ["marge"]
    assert "tables inconnues : ['fantome']" in advisor.rejectedAssembledFacts[0]["error"]
    assert len(advisor.rejectedAssembledFacts) == 2
    assert session.posted["physicalLinks"] == [{"sourceTable": "colis"}]


def testAHeaderTableIsFoldedIntoItsLinesFactNotLinked(duck_db):
    # Ticket lines with their ticket header (card, day, payment): the same
    # article scanned twice on ticket 1 -- a line grain, not a duplicate.
    duck_db.loadTables({
        "tickets": {"transaction_id": [1, 2, 3, 4], "carte": ["C1", "C2", "C1", "C3"],
                    "date_ticket": ["2024-01-01", "2024-01-01", "2024-01-02", "2024-01-03"],
                    "paiement": ["cb", "esp", "cb", "cb"]},
        "lignes": {"line_id": [1, 2, 3, 4, 5, 6, 7, 8], "transaction_id": [1, 1, 1, 2, 3, 3, 4, 4],
                   "article": ["A", "A", "B", "A", "B", "C", "A", "B"],
                   "qty": [1.5, 1.5, 2.0, 1.0, 3.0, 1.0, 2.0, 1.0]},
    })
    duck_db.conn.execute("ALTER TABLE tickets ALTER date_ticket TYPE DATE")
    spec = AssemblySpec.model_validate({
        "name": "ventes", "anchorTable": "lignes", "measures": [{"table": "lignes", "column": "qty"}],
        "keys": [{"table": "tickets", "column": c} for c in ("carte", "date_ticket", "paiement")],
    })

    results = KawakiriV3Pipeline(duck_db, factHintAdvisor=_NoCallAdvisor()).run(
        ["tickets", "lignes"], assembledFacts=[spec],
    )
    model = results["models"][0]
    byName = {a.columnName: a for a in model.assessments}

    assert model.assembly.headerTables == ["tickets"] and model.assembly.documentColumns == ["transaction_id"]
    assert model.links == []
    assert byName["transaction_id"].role == DEGENERATE
    assert (byName["date_ticket"].role, byName["date_ticket"].dimensionTable) == (EXTRACTED_KEY, "ventes__dim_date")
    assert "transaction_id" not in model.grain.grainColumns
    assert model.grain.repeatedWithin == "transaction_id"
    assert model.notes and model.notes[0].startswith("grain a la ligne")
    assert model.verdict == "ok", model.warnings

