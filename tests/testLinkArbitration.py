"""The decision on each physical link: the deterministic stage (names, 1:1
covered both ways or one way only), the doubtful links the LLM arbitrates
in one call, the verdict cache, and the caller's last word."""
import pytest
from pydantic import ValidationError

from kawakiriv3.FactAssembly.linkGraph import (
    ACCEPTED, DECIDED_BY_CACHE, DECIDED_BY_CALLER, DECIDED_BY_LLM, DECIDED_BY_NAMES, DECIDED_BY_STATISTICS, PENDING,
    REJECTED, LinkGraph,
)
from kawakiriv3.FactAssembly.linkVerdictCache import LinkVerdictCache
from kawakiriv3.FactHint.factHintAdvisor import FactHintAdvisor, FactTableHint
from kawakiriv3.jobPayload import JobPayload
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.Schema.schemaEngine import SchemaEngine

# A JD Edwards-like receivable: every key is an integer whose name follows
# the "table prefix + field" convention, so no name ever corroborates a link.
#   RPAN8 -> F0101.ABAN8   the customer's address number  (a real link)
#   RPCO  -> F0010.CCCO    the company                    (a real link)
#   RPSFX -> F0010.CCCO    a pay item number 1..3, inside the company range by chance
F03B11 = {
    "RPDOC": list(range(1, 41)),
    "RPAN8": [1001 + (i % 20) for i in range(40)],
    "RPCO": [1 + (i % 3) for i in range(40)],
    "RPSFX": [1 + (i % 3) for i in range(1, 41)],
    "RPAG": [round(10.5 + i * 3.25, 2) for i in range(40)],
}
F0101 = {"ABAN8": list(range(1001, 1031)), "ABALPH": [f"Client {i}" for i in range(30)]}
F0010 = {"CCCO": [1, 2, 3], "CCNAME": ["Societe A", "Societe B", "Societe C"]}
JDE_TABLES = ["f03b11", "f0101", "f0010"]

# Order-line extensions with unrelated names, on non-sequential line ids:
#   detail.ref        unique, covers every line and every line is covered: a shared key
#   partiel.ref       unique, covers 70% of the lines: partially shared, doubtful
#   clients.client_id three values found among the lines: covered one way only
LINE_IDS = [17, 23, 31, 42, 58, 64, 77, 85, 93, 101]
LIGNES = {"ligne_id": LINE_IDS, "montant": [float(i) * 1.5 for i in range(1, 11)]}
DETAIL = {"ref": list(reversed(LINE_IDS)), "poids": [0.5 * i for i in range(10)]}
PARTIEL = {"ref_partielle": LINE_IDS[:7], "remise": [0.1 * i for i in range(7)]}
CLIENTS = {"client_id": [17, 23, 31], "nom": ["a", "b", "c"]}


@pytest.fixture
def jdeDb(duck_db):
    duck_db.loadTables({"f03b11": F03B11, "f0101": F0101, "f0010": F0010})
    return duck_db


def _graph(db, tables, **kwargs):
    schemaEngine = SchemaEngine(db, tables).build()
    return LinkGraph.build(schemaEngine, schemaEngine.inferAllPrimaryKeyCandidates(), **kwargs)


def _byLink(graph):
    return {d.link.describe(): d for d in graph.decisions}


def testUnrelatedNamesWithAStrongOverlapAreLeftToTheArbiter(jdeDb):
    graph = _graph(jdeDb, JDE_TABLES)

    decisions = _byLink(graph)
    assert {d.link.describe() for d in graph.pending} == {
        "f03b11.RPAN8 -> f0101.ABAN8 (N:1)", "f03b11.RPCO -> f0010.CCCO (N:1)", "f03b11.RPSFX -> f0010.CCCO (N:1)",
    }
    assert all(d.linkId for d in graph.pending)
    assert decisions["f03b11.RPAN8 -> f0101.ABAN8 (N:1)"].reason == "noms discordants"
    assert graph.links == []


def testA1To1LinkIsJudgedOnItsCoverageBothWays(duck_db):
    duck_db.loadTables({"lignes": LIGNES, "detail": DETAIL, "partiel": PARTIEL, "clients": CLIENTS})

    decisions = _byLink(_graph(duck_db, ["lignes", "detail", "partiel", "clients"]))

    shared = decisions["detail.ref -> lignes.ligne_id (1:1)"]
    assert (shared.status, shared.decidedBy, shared.link.reverseCoverage) == (ACCEPTED, DECIDED_BY_STATISTICS, 1.0)
    assert decisions["partiel.ref_partielle -> lignes.ligne_id (1:1)"].status == PENDING
    coincidence = decisions["clients.client_id -> lignes.ligne_id (1:1)"]
    assert (coincidence.status, coincidence.decidedBy) == (REJECTED, DECIDED_BY_STATISTICS)
    assert "30% dans l'autre" in coincidence.reason


def testTwoDenseSequencesOfTheSameLengthAreDoubtfulNotShared(duck_db):
    duck_db.loadTables({
        "clients": {"client_id": list(range(1, 11)), "nom": list("abcdefghij")},
        "produits": {"produit_id": list(range(10, 0, -1)), "libelle": list("klmnopqrst")},
    })

    decision = _byLink(_graph(duck_db, ["clients", "produits"]))["clients.client_id -> produits.produit_id (1:1)"]

    assert decision.status == PENDING and "plage d'entiers dense" in decision.reason


def testAGenericNameSharedByTwoTablesProvesNothing(duck_db):
    duck_db.loadTables({
        "commandes": {"id": [3, 7, 14, 20], "client_id": [3, 3, 7, 8], "m": [1.5, 2.5, 3.5, 4.5]},
        "clients": {"id": [3, 7, 8, 14, 20], "nom": list("abcde")},
    })

    decisions = _byLink(_graph(duck_db, ["commandes", "clients"]))

    assert decisions["commandes.client_id -> clients.id (N:1)"].decidedBy == DECIDED_BY_NAMES  # names the table
    # Both tables call their key "id": the 1:1 link is judged on statistics alone.
    generic = decisions["commandes.id -> clients.id (1:1)"]
    assert not generic.link.nameHint
    assert (generic.status, generic.reason) == (PENDING, "noms discordants, cle partiellement partagee")


def testTheCallerHasTheLastWord(jdeDb):
    graph = _graph(
        jdeDb, JDE_TABLES, declaredLinks=["f03b11.RPAN8 -> f0101.ABAN8"], forbiddenLinks=["f03b11.RPCO->f0010.CCCO"],
    )

    decisions = _byLink(graph)
    assert (decisions["f03b11.RPAN8 -> f0101.ABAN8 (N:1)"].status, decisions["f03b11.RPAN8 -> f0101.ABAN8 (N:1)"].decidedBy) \
        == (ACCEPTED, DECIDED_BY_CALLER)
    assert decisions["f03b11.RPCO -> f0010.CCCO (N:1)"].status == REJECTED
    assert [d.link.sourceColumn for d in graph.pending] == ["RPSFX"]


def testADeclaredLinkIsKeptEvenBelowTheOverlapThreshold(jdeDb):
    jdeDb.conn.execute("UPDATE f03b11 SET RPAN8 = 9999 WHERE RPDOC <= 20")  # half the customers unknown

    graph = _graph(jdeDb, JDE_TABLES, declaredLinks=["f03b11.RPAN8 -> f0101.ABAN8"])

    link = _byLink(graph)["f03b11.RPAN8 -> f0101.ABAN8 (N:1)"]
    assert link.status == ACCEPTED and link.link.coverage == pytest.approx(0.5)


def testCorroboratingNamesWithAWeakOverlapAreDoubtful(duck_db):
    duck_db.loadTables({
        "ventes": {"client_id": [1, 1, 2, 3, 4, 5, 6, 7, 8, 99], "m": [1.5] * 10},
        "clients": {"client_id": list(range(1, 11)), "nom": list("abcdefghij")},
    })

    decision = _byLink(_graph(duck_db, ["ventes", "clients"]))["ventes.client_id -> clients.client_id (N:1)"]

    assert (decision.status, decision.reason) == (PENDING, "noms concordants mais couverture faible")


# --- arbitration in the pipeline ----------------------------------------------

def _verdictsFor(candidateLinks, accepted):
    return {
        link["linkId"]: (f'{link["sourceTable"]}.{link["sourceColumn"]}' in accepted, "verdict de test")
        for link in candidateLinks
    }


class _ArbiterAdvisor:
    """Accepts RPAN8 and RPCO, refuses everything else; designates f03b11."""

    lastError = None
    lastReasoning = None
    lastTokenUsage = None
    rejectedTableNames = []
    lastColumnRolesByTable = {}
    lastColumnRolesErrors = {}
    rejectedColumnNames = {}
    lastAssembledFacts = []
    rejectedAssembledFacts = []
    lastLinkError = None
    lastLinkTokenUsage = {"inputTokens": 40, "outputTokens": 9}

    def __init__(self):
        self.linkCalls = []
        self.physicalLinks = None

    def arbitrateLinks(self, tablesSummary, candidateLinks, businessNeed=None, contextType=None):
        self.linkCalls.append(candidateLinks)
        return _verdictsFor(candidateLinks, {"f03b11.RPAN8", "f03b11.RPCO"})

    def proposeFactTables(self, tablesSummary, businessNeed=None, contextType=None, factTables=None,
                          classifyColumns=True, physicalLinks=None):
        self.physicalLinks = physicalLinks
        return [FactTableHint("f03b11", "le registre des creances")]


def testThePipelineArbitratesTheDoubtfulLinksInOneCallBeforeTheFactHint(jdeDb):
    advisor = _ArbiterAdvisor()

    results = KawakiriV3Pipeline(jdeDb, factHintAdvisor=advisor).run(JDE_TABLES, businessNeed="creances")

    assert len(advisor.linkCalls) == 1 and len(advisor.linkCalls[0]) == 3
    accepted = {(l["sourceColumn"], l["targetTable"]) for l in advisor.physicalLinks}
    assert accepted == {("RPAN8", "f0101"), ("RPCO", "f0010")}  # the fact hint only sees accepted links
    byLink = {d["link"]: d for d in results["linkDecisions"]}
    assert byLink["f03b11.RPSFX -> f0010.CCCO (N:1)"]["status"] == REJECTED
    assert byLink["f03b11.RPAN8 -> f0101.ABAN8 (N:1)"]["decidedBy"] == DECIDED_BY_LLM
    assert results["linkTokenUsage"] == {"inputTokens": 40, "outputTokens": 9}
    # The star follows the verdicts: no naming convention links RPAN8, the LLM did.
    model = results["models"][0]
    assert sorted(l.target for l in model.links) == ["f0010.CCCO", "f0101.ABAN8"]
    assert "lien accepte par : LLM" in {a.columnName: a for a in model.assessments}["RPAN8"].reason
    assert "RPSFX" not in [l.sourceColumn for l in model.links]


def testTheSameDatasetReusesTheCachedVerdictsWithoutACall(jdeDb, tmp_path):
    cachePath = tmp_path / "verdicts.json"
    first = _ArbiterAdvisor()
    KawakiriV3Pipeline(jdeDb, factHintAdvisor=first, linkVerdictCache=LinkVerdictCache(cachePath)).run(JDE_TABLES)
    second = _ArbiterAdvisor()

    results = KawakiriV3Pipeline(
        jdeDb, factHintAdvisor=second, linkVerdictCache=LinkVerdictCache(cachePath),
    ).run(JDE_TABLES)

    assert second.linkCalls == []
    assert {d["decidedBy"] for d in results["linkDecisions"] if d["link"].startswith("f03b11.RP")} == {DECIDED_BY_CACHE}
    assert {(l["sourceColumn"], l["targetTable"]) for l in results["physicalLinks"]} == {("RPAN8", "f0101"), ("RPCO", "f0010")}


def testARunWithoutAnyLlmCallRejectsTheDoubtfulLinks(jdeDb):
    advisor = _ArbiterAdvisor()

    results = KawakiriV3Pipeline(jdeDb, factHintAdvisor=advisor).run(
        JDE_TABLES, factTables=["f03b11"], columnHint=False,
    )

    assert advisor.linkCalls == []
    assert results["physicalLinks"] == []
    doubtful = [d for d in results["linkDecisions"] if d["linkId"]]
    assert len(doubtful) == 3 and all("arbitrage LLM desactive" in d["reason"] for d in doubtful)


def testAFailedArbitrationRejectsByCaution(jdeDb):
    advisor = _ArbiterAdvisor()
    advisor.arbitrateLinks = lambda *a, **k: {}
    advisor.lastLinkError = "boom"

    results = KawakiriV3Pipeline(jdeDb, factHintAdvisor=advisor).run(JDE_TABLES)

    assert results["linkArbitrationError"] == "boom"
    assert all(d["status"] == REJECTED for d in results["linkDecisions"])


# --- HTTP client and job ------------------------------------------------------

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
        self.posted = (url, json)
        return _Response(self.body)


def testFactHintAdvisorKeepsOnlyVerdictsOnAskedLinks():
    session = _Session({"verdicts": [
        {"linkId": "L1", "accepted": True, "justification": "AN8"},
        {"linkId": "L7", "accepted": True, "justification": "jamais demande"},
        {"linkId": "L2", "accepted": "oui", "justification": "mal forme"},
    ], "tokenUsage": {"inputTokens": 3, "outputTokens": 1}})
    advisor = FactHintAdvisor(baseUrl="http://advisor", session=session)

    verdicts = advisor.arbitrateLinks([], [{"linkId": "L1"}, {"linkId": "L2"}], "creances", "JDE")

    assert verdicts == {"L1": (True, "AN8")}
    assert session.posted[0] == "http://advisor/arbitrate-links"
    assert advisor.lastLinkTokenUsage == {"inputTokens": 3, "outputTokens": 1}


def testFactHintAdvisorArbitrationNeverRaises():
    class _Down:
        def post(self, *a, **k):
            raise ConnectionError("refused")

    advisor = FactHintAdvisor(baseUrl="http://advisor", session=_Down())

    assert advisor.arbitrateLinks([], [{"linkId": "L1"}]) == {}
    assert "ConnectionError" in advisor.lastLinkError


def testJobRejectsAMalformedLink():
    with pytest.raises(ValidationError, match="lien mal forme"):
        JobPayload.model_validate({"source": {"kind": "csv", "path": "x"}, "declaredLinks": ["f03b11.RPAN8 f0101"]})
