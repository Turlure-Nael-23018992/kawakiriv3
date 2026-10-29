import json

import pytest

from kawakiriv3.FactHint.factHintAdvisor import FactHintAdvisor, FactTableHint
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.report import exportResults
from kawakiriv3.Sources import CsvSource


class _FakeAdvisor:
    def __init__(self, hints, error=None, columnRoles=None, assembled=None):
        self.hints = hints
        self.lastError = error
        self.lastReasoning = "raisonnement"
        self.lastTokenUsage = {"inputTokens": 5, "outputTokens": 2}
        self.rejectedTableNames = []
        self.lastColumnRolesByTable = columnRoles or {}
        self.lastColumnRolesErrors = {}
        self.rejectedColumnNames = {}
        self.lastAssembledFacts = assembled or []
        self.rejectedAssembledFacts = []
        self.lastPhysicalLinks = None
        self.lastLinkError = None
        self.lastLinkTokenUsage = None
        self.calls = []

    def proposeFactTables(self, tablesSummary, businessNeed=None, contextType=None, factTables=None,
                          classifyColumns=True, physicalLinks=None):
        self.calls.append((tablesSummary, businessNeed, contextType, factTables, classifyColumns))
        self.lastPhysicalLinks = physicalLinks
        return self.hints if not factTables else []

    def arbitrateLinks(self, tablesSummary, candidateLinks, businessNeed=None, contextType=None):
        self.linkCalls = getattr(self, "linkCalls", []) + [candidateLinks]
        return {}


def testPipelineAnalysesTheTableTheHintDesignates(sales_db, tmp_path):
    advisor = _FakeAdvisor([FactTableHint("ventes", "porte le chiffre d'affaires")])
    pipeline = KawakiriV3Pipeline(sales_db, factHintAdvisor=advisor)

    results = pipeline.run(["clients", "ventes"], businessNeed="CA par client")

    assert results["factTableSource"] == "llm_hint"
    assert [m.factTable for m in results["models"]] == ["ventes"]
    assert results["models"][0].hint == "porte le chiffre d'affaires"
    tablesSummary, businessNeed, _, _, _ = advisor.calls[0]
    assert businessNeed == "CA par client"
    # Structure and statistics only: no real value ever reaches the LLM.
    column = tablesSummary[1]["columns"][0]
    assert set(column) == {"columnName", "inferredType", "nullRate", "distinctCount", "distinctRate", "uniqueInTable"}

    jsonPath, reportPath = exportResults(results, tmp_path)
    assert json.loads(jsonPath.read_text(encoding="utf-8"))["models"][0]["verdict"] == "ok"
    report = reportPath.read_text(encoding="utf-8")
    assert "erDiagram" in report
    # A linked source table is drawn with its key and columns, not as an empty box.
    # Namespaced by its fact table so the same source table drawn in another
    # diagram of the same report never shares its entity name (see
    # report._linkedEntityName).
    assert '    "ventes.clients" {\n        numeric client_id PK\n        string nom\n    }' in report


def testImposedFactTablesSkipTheHint(sales_db):
    advisor = _FakeAdvisor([])
    pipeline = KawakiriV3Pipeline(sales_db, factHintAdvisor=advisor)

    results = pipeline.run(["clients", "ventes"], factTables=["ventes"], columnHint=False)

    assert results["factTableSource"] == "caller"
    assert advisor.calls == []
    assert len(results["models"]) == 1


def testAFailedHintProducesNoModelAndKeepsTheError(sales_db):
    pipeline = KawakiriV3Pipeline(sales_db, factHintAdvisor=_FakeAdvisor([], error="service injoignable"))

    results = pipeline.run(["clients", "ventes"])

    assert results["models"] == []
    assert results["factHintError"] == "service injoignable"


def testUnknownImposedFactTableFailsFast(sales_db):
    with pytest.raises(ValueError, match="inconnue"):
        KawakiriV3Pipeline(sales_db).run(["clients", "ventes"], factTables=["retours"])


def testIngestFromCsvThenRun(tmp_path, duck_db):
    (tmp_path / "clients.csv").write_text("client_id,nom\n1,a\n2,b\n3,c\n", encoding="utf-8")
    (tmp_path / "ventes.csv").write_text(
        "vente_id,client_id,canal,montant\n1,1,web,9.5\n2,2,web,3.25\n3,1,shop,4.75\n4,3,shop,1.5\n",
        encoding="utf-8",
    )
    pipeline = KawakiriV3Pipeline(duck_db)

    loaded = pipeline.ingest(CsvSource(tmp_path))
    results = pipeline.run(list(loaded.values()), factTables=["ventes"], columnHint=False)

    model = results["models"][0]
    assert [l.target for l in model.links] == ["clients.client_id"]
    assert [d.keyColumn for d in model.dimensions] == ["canal"]


class _FakeResponse:
    def __init__(self, body):
        self.body = body

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class _FakeSession:
    def __init__(self, body=None, error=None):
        self.body = body
        self.error = error
        self.posted = None

    def post(self, url, json=None, timeout=None):
        if self.error:
            raise self.error
        self.posted = (url, json)
        return _FakeResponse(self.body)


def testFactHintAdvisorDropsTableNamesThisRunDoesNotHave():
    session = _FakeSession({"factTables": [
        {"tableName": "commandes", "justification": "x"}, {"tableName": "ventes", "justification": "y"},
    ], "reasoning": "r", "tokenUsage": {"inputTokens": 1, "outputTokens": 1}})
    advisor = FactHintAdvisor(baseUrl="http://advisor", session=session)

    hints = advisor.proposeFactTables([{"tableName": "ventes"}], businessNeed="CA")

    assert [h.tableName for h in hints] == ["ventes"]
    assert advisor.rejectedTableNames == ["commandes"]
    assert session.posted[0] == "http://advisor/propose-fact-tables"


def testFactHintAdvisorNeverRaisesOnNetworkFailure():
    advisor = FactHintAdvisor(baseUrl="http://advisor", session=_FakeSession(error=ConnectionError("refused")))

    assert advisor.proposeFactTables([{"tableName": "ventes"}]) == []
    assert "refused" in advisor.lastError
