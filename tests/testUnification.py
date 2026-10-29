"""The post-processing when the LLM drives it, on a Postgres-like source
(loyalty cards in store, customers on the web): asked only when the fact
hint says the business need wants a unification, shown the built stars
(never a real value), and its answer applied on the stars -- a fact it
doesn't touch keeps its star as is."""
import json

from kawakiriv3.FactAnalysis.columnAssessment import FOREIGN_KEY
from kawakiriv3.FactHint.factHintAdvisor import FactHintAdvisor, FactTableHint
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.StarUnification.unificationSpec import UnificationSpec

TABLES = ["loyalty_cards", "web_customers", "shop_sales", "web_sales", "stock_moves"]

ROLES = {
    "shop_sales": {"keyColumns": ["loyalty_card_number", "item_name"], "measureColumns": ["qty"],
                   "nonAdditiveColumns": ["unit_price"], "technicalColumns": []},
    "web_sales": {"keyColumns": ["customer_id", "product_name"], "measureColumns": ["quantity"],
                  "nonAdditiveColumns": ["price"], "technicalColumns": []},
    "stock_moves": {"keyColumns": ["warehouse"], "measureColumns": ["moved"], "technicalColumns": []},
}

SPEC = {
    "dimensions": [{
        "name": "clients_unifies", "idColumn": "client_unifie_id",
        "sources": [
            {"dimension": "loyalty_cards", "label": "magasin",
             "columnMapping": {"email": "client_email", "nom": "client_nom"}},
            {"dimension": "web_customers", "label": "web", "columnMapping": {"email": "email", "nom": "last_name"}}],
        "matchColumns": ["email"], "sourcePriority": ["web", "magasin"],
    }],
    "facts": [{
        "name": "ventes_unifiees", "labelColumn": "canal",
        "sources": [
            {"fact": "web_sales", "label": "web",
             "columnMapping": {"quantite": "quantity", "prix_unitaire": "price", "produit": "product_name"}},
            {"fact": "shop_sales", "label": "magasin",
             "columnMapping": {"quantite": "qty", "prix_unitaire": "unit_price", "produit": "item_name"}}],
        "computedMeasures": [{"name": "montant", "multiply": ["quantite", "prix_unitaire"]}],
    }],
}

SECRETS = ("ana@x", "bob@x", "Martin", "Durand", "Chaise", "Lampe", "C-001", "Lyon")


def _db(db):
    db.loadTables({
        "loyalty_cards": {"card_number": ["C-001", "C-002", "C-003"], "client_email": ["ana@x", "bob@x", "cyd@x"],
                          "client_nom": ["Martin", "Durand", "Petit"]},
        "web_customers": {"id": [1, 2, 3, 4], "email": ["bob@x", "dan@x", "ana@x", "eve@x"],
                          "last_name": ["Durand", "Leroy", "Martin", "Roux"], "city": ["Lyon", "Pau", "Nice", "Caen"]},
        "shop_sales": {"line_id": list(range(1, 10)),
                       "loyalty_card_number": ["C-001", "C-002", "C-003"] * 3,
                       "item_name": ["Chaise", "Lampe", "Table"] * 3,
                       "qty": [1, 2, 3, 1, 2, 3, 1, 2, 3], "unit_price": [5.0, 9.0, 12.0] * 3},
        "web_sales": {"id": list(range(100, 108)), "customer_id": [1, 2, 3, 4] * 2,
                      "product_name": ["Chaise", "Bureau"] * 4,
                      "quantity": [1, 1, 2, 2, 3, 3, 1, 1], "price": [5.0, 20.0] * 4},
        "stock_moves": {"move_id": list(range(1, 7)), "warehouse": ["Lyon", "Pau"] * 3,
                        "moved": [10.5, 3.5, 7.0, 1.0, 2.0, 4.0]},
    })
    return db


class _Advisor:
    lastError = None
    lastReasoning = "trois faits"
    lastTokenUsage = None
    rejectedTableNames = []
    lastColumnRolesErrors = {}
    rejectedColumnNames = {}
    lastAssembledFacts = []
    rejectedAssembledFacts = []
    lastLinkError = None
    lastLinkTokenUsage = None
    lastUnificationError = None
    lastUnificationReasoning = "clients unifies sur l'email, donc ventes reunies"
    lastUnificationTokenUsage = {"inputTokens": 5, "outputTokens": 5}
    rejectedUnifications = []
    lastColumnRolesByTable = ROLES

    def __init__(self, requested):
        self.lastUnificationRequested = requested
        self.unificationCalls = []

    def proposeFactTables(self, *args, **kwargs):
        return [FactTableHint(tableName=t, justification="un fait") for t in ("shop_sales", "web_sales", "stock_moves")]

    def arbitrateLinks(self, tablesSummary, candidateLinks, businessNeed=None, contextType=None):
        # Only the two customer keys are real links.
        real = {("shop_sales", "loyalty_card_number", "loyalty_cards"), ("web_sales", "customer_id", "web_customers")}
        return {
            link["linkId"]: ((link["sourceTable"], link["sourceColumn"], link["targetTable"]) in real, "")
            for link in candidateLinks
        }

    def proposeUnifications(self, catalogSummary, businessNeed=None, contextType=None):
        self.unificationCalls.append(catalogSummary)
        return UnificationSpec.model_validate(SPEC)


def _run(db, requested):
    advisor = _Advisor(requested)
    pipeline = KawakiriV3Pipeline(_db(db), factHintAdvisor=advisor)
    results = pipeline.run(TABLES, businessNeed="ventes par client unique, magasin et web")
    return advisor, results, {model.factTable: model for model in results["models"]}


def testNoRequestNoUnificationCall(duck_db):
    advisor, results, models = _run(duck_db, requested=False)

    assert advisor.unificationCalls == []
    assert results["unification"]["source"] is None and results["unification"]["dimensions"] == []
    assert sorted(models) == ["shop_sales", "stock_moves", "web_sales"]
    assert results["replacedModels"] == []


def testTheLlmSeesTheStarsWithoutAnyRealValue(duck_db):
    advisor, results, _ = _run(duck_db, requested=True)

    summary = advisor.unificationCalls[0]
    assert sorted(s["fact"] for s in summary["stars"]) == ["shop_sales", "stock_moves", "web_sales"]
    shop = next(s for s in summary["stars"] if s["fact"] == "shop_sales")
    card = next(c for c in shop["columns"] if c["columnName"] == "loyalty_card_number")
    assert (card["role"], card["dimension"]) == (FOREIGN_KEY, "loyalty_cards")
    dimensions = {d["dimension"]: d for d in summary["dimensions"]}
    assert dimensions["loyalty_cards"]["keyColumn"] == "card_number"
    assert [c["columnName"] for c in dimensions["web_customers"]["columns"]] == ["id", "email", "last_name", "city"]
    text = json.dumps(summary)
    assert not [secret for secret in SECRETS if secret in text]
    assert results["unification"]["source"] == "llm"
    assert results["unification"]["reasoning"].startswith("clients unifies")


def testTheUnificationIsAppliedOnTheStars(duck_db):
    _, results, models = _run(duck_db, requested=True)

    assert sorted(models) == ["stock_moves", "ventes_unifiees"]
    sales = models["ventes_unifiees"]
    assert [link.target for link in sales.links] == ["clients_unifies.client_unifie_id"]
    assert sales.measures == ["quantite", "prix_unitaire", "montant"]
    assert sales.nonAdditiveMeasures == ["prix_unitaire"]
    # ana@x and bob@x are on both channels: 5 people.
    assert duck_db.getRowCount("clients_unifies") == 5
    assert duck_db.query(
        "SELECT COUNT(DISTINCT client_unifie_id) FROM ventes_unifiees__fact WHERE canal = 'magasin' "
        "AND client_unifie_id IN (SELECT client_unifie_id FROM ventes_unifiees__fact WHERE canal = 'web')"
    ) == [(2,)]
    assert duck_db.getRowCount("ventes_unifiees__fact") == 17


def testAFactTheUnificationDoesntTouchKeepsItsStar(duck_db):
    stars = KawakiriV3Pipeline(_db(duck_db), factHintAdvisor=_Advisor(False)).run(TABLES)
    before = next(m for m in stars["models"] if m.factTable == "stock_moves")
    duck_db.dropAllTables()
    _, results, models = _run(duck_db, requested=True)
    after = models["stock_moves"]

    assert after.starFactTable == before.starFactTable == "stock_moves__fact"
    describe = lambda model: (
        [(a.columnName, a.role, a.roleSource, a.linkedTo, a.dimensionTable) for a in model.assessments],
        [link.target for link in model.links], [d.tableName for d in model.dimensions],
        model.grain.grainColumns, model.verdict,
    )
    assert describe(after) == describe(before)
    assert "stock_moves" not in {m.factTable for m in results["replacedModels"]}


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
        self.sent = None

    def post(self, url, json=None, timeout=None):
        self.sent = json
        return _Response(self.body)


def testAdvisorKeepsValidSpecsAndReportsTheOthers():
    session = _Session({
        "dimensions": [SPEC["dimensions"][0], {"name": "bad name", "sources": []}],
        "facts": SPEC["facts"], "reasoning": "r", "tokenUsage": {"inputTokens": 1, "outputTokens": 1},
    })
    advisor = FactHintAdvisor(baseUrl="http://x", session=session)

    spec = advisor.proposeUnifications({"stars": [], "dimensions": []}, businessNeed="b")

    assert [d.name for d in spec.dimensions] == ["clients_unifies"]
    assert [f.name for f in spec.facts] == ["ventes_unifiees"]
    assert len(advisor.rejectedUnifications) == 1 and advisor.lastUnificationReasoning == "r"
    assert session.sent == {"stars": [], "dimensions": [], "businessNeed": "b", "contextType": None}


def testAdvisorReadsTheUnificationFlag():
    advisor = FactHintAdvisor(baseUrl="http://x", session=_Session({
        "factTables": [{"tableName": "t", "justification": "x"}], "unificationRequested": True,
    }))

    advisor.proposeFactTables([{"tableName": "t", "columns": []}])

    assert advisor.lastUnificationRequested is True
