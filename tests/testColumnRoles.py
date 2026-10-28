"""Column roles imposed from outside the statistics, technical columns, the
shared calendar dimension and the "unknown member" grain."""
from kawakiriv3.FactAnalysis.columnAssessment import (
    DEGENERATE, EXTRACTED_KEY, MEASURE, ROW_IDENTIFIER, SOURCE_CALLER, SOURCE_LLM, SOURCE_NAME_PATTERN,
    TECHNICAL,
)
from kawakiriv3.FactAnalysis.columnClassifier import ColumnClassifier, looksTechnical
from kawakiriv3.FactAnalysis.columnRoles import ColumnRoles
from kawakiriv3.FactAnalysis.factAnalyzer import OK, FactAnalyzer
from kawakiriv3.FactHint.factHintAdvisor import FactHintAdvisor, FactTableHint
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.Schema.schemaEngine import SchemaEngine

# A JD Edwards-like ledger: integer keys no naming convention flags
# (RPAN8 = customer, RPCO = company), an amount, dbt load metadata, two dates
# (one of them text in dd/mm/yyyy) and a sparse optional key.
LEDGER = {
    "RPDOC": [1, 2, 3, 4, 5, 6],
    "RPAN8": [700, 701, 700, 702, 701, 700],
    "RPCO": [10, 10, 20, 20, 10, 20],
    "RPAG": [10.5, 20.0, 31.25, 5.5, 8.0, 12.0],
    "RPDIVJ": ["2024-01-05", "2024-01-05", "2024-01-06", "2024-01-07", "2024-01-07", "2024-01-08"],
    "RPDDJ": ["05/02/2024", "05/02/2024", "06/02/2024", "07/02/2024", "07/02/2024", "08/02/2024"],
    "RPMCU": [None, None, None, 571, None, 572],
    "DBT_SCD_ID": ["a", "b", "c", "d", "e", "f"],
    "LOADED_AT": ["2026-02-13", "2026-02-13", "2026-02-14", "2026-02-14", "2026-02-14", "2026-02-14"],
}


def _ledger(duck_db):
    duck_db.loadTables({"f03b11": LEDGER})
    duck_db.conn.execute("ALTER TABLE f03b11 ALTER RPDIVJ TYPE DATE")
    schemaEngine = SchemaEngine(duck_db, ["f03b11"]).build()
    return schemaEngine, schemaEngine.inferAllPrimaryKeyCandidates()


def _assessments(schemaEngine, columnRoles=None):
    return {a.columnName: a for a in ColumnClassifier(schemaEngine.profilers["f03b11"], columnRoles).classify()}


def testTechnicalNamePatterns():
    for name in ("DBT_SCD_ID", "dbt_valid_from", "LOADED_AT", "_airbyte_extracted_at", "FILE_NAME", "_sdc_batched_at"):
        assert looksTechnical(name), name
    for name in ("RPAN8", "created_by", "loaded", "montant"):
        assert not looksTechnical(name), name


def testWithoutRolesJdeIntegerKeysAreMistakenForMeasures(duck_db):
    schemaEngine, _ = _ledger(duck_db)

    byName = _assessments(schemaEngine)

    # The limitation the column roles exist for: nothing tells RPAN8 is a key.
    assert byName["RPAN8"].fallbackRole == MEASURE
    assert byName["LOADED_AT"].role == TECHNICAL and byName["LOADED_AT"].roleSource == SOURCE_NAME_PATTERN
    assert byName["DBT_SCD_ID"].role == TECHNICAL


def testCallerRolesOverrideLlmRolesColumnByColumn(duck_db):
    schemaEngine, _ = _ledger(duck_db)
    llm = ColumnRoles.fromLlm({"keyColumns": ["RPAN8", "RPCO"], "measureColumns": ["RPMCU"], "reasoning": "r"})
    caller = ColumnRoles.fromLists(keys=["RPMCU"], excluded=["RPCO"])

    byName = _assessments(schemaEngine, llm.overriddenBy(caller))

    assert (byName["RPAN8"].fallbackRole, byName["RPAN8"].roleSource) == ("key_candidate", SOURCE_LLM)
    assert not byName["RPAN8"].requiresNameHint
    assert (byName["RPMCU"].role, byName["RPMCU"].roleSource) == ("key_candidate", SOURCE_CALLER)
    assert (byName["RPCO"].role, byName["RPCO"].roleSource) == (TECHNICAL, SOURCE_CALLER)


def testUniquenessKeepsTheLastWordOverADesignatedKey(duck_db):
    schemaEngine, _ = _ledger(duck_db)

    byName = _assessments(schemaEngine, ColumnRoles.fromLlm({"keyColumns": ["RPDOC"]}))

    assert byName["RPDOC"].role == ROW_IDENTIFIER
    assert "designee cle (LLM) mais unique par ligne" in byName["RPDOC"].reason


def testAMeasureDesignationOnANonNumericColumnIsIgnored(duck_db):
    schemaEngine, _ = _ledger(duck_db)

    byName = _assessments(schemaEngine, ColumnRoles.fromLists(measures=["DBT_SCD_ID", "RPAN8"]))

    assert byName["RPAN8"].role == MEASURE
    assert byName["DBT_SCD_ID"].role == ROW_IDENTIFIER  # an explicit role disables the name pattern


def testLedgerWithRolesSharesOneCalendarAndCountsNullKeysAsUnknown(duck_db):
    schemaEngine, keys = _ledger(duck_db)
    roles = ColumnRoles.fromLlm({"keyColumns": ["RPAN8", "RPCO", "RPMCU"], "reasoning": "cles JDE"})

    model = FactAnalyzer(duck_db, schemaEngine, keys, "f03b11", columnRoles=roles).analyze()

    calendar = model.dimensions[0]
    assert calendar.tableName == "f03b11__dim_date"
    assert sorted(calendar.roleColumns) == ["RPDDJ", "RPDIVJ"]
    # Both dates' days in one calendar, the dd/mm/yyyy text parsed with its own format.
    assert calendar.rowCount == 8
    assert duck_db.query('SELECT annee, mois FROM "f03b11__dim_date" WHERE date_key = DATE \'2024-02-05\'') == [(2024, 2)]
    byName = {a.columnName: a for a in model.assessments}
    assert byName["RPDDJ"].role == EXTRACTED_KEY and byName["RPDDJ"].dimensionTable == "f03b11__dim_date"
    assert model.technicalColumns == ["DBT_SCD_ID", "LOADED_AT"]
    assert "LOADED_AT" not in duck_db.getColumns(model.starFactTable)
    # RPMCU is null on 4 rows: those rows carry the "unknown" member, the grain stays unique.
    assert model.grain.unknownKeyRows == 4
    assert model.grain.isUnique
    assert model.verdict == OK, model.warnings
    assert model.columnRolesReasoning == "cles JDE"
    assert byName["RPDOC"].role == DEGENERATE


def testTwoRowsWithTheSameUnknownKeyAreADuplicate(duck_db):
    duck_db.loadTables({"faits": {"k": ["a", "b", "b", None, None], "m": [1.5, 2.5, 3.5, 4.5, 5.5]}})
    schemaEngine = SchemaEngine(duck_db, ["faits"]).build()

    model = FactAnalyzer(duck_db, schemaEngine, schemaEngine.inferAllPrimaryKeyCandidates(), "faits").analyze()

    assert model.grain.grainColumns == ["k"]
    assert model.grain.duplicateRows == 2  # "b" twice, and the unknown member twice
    assert model.grain.unknownKeyRows == 2


class _RolesAdvisor:
    lastError = None
    lastReasoning = None
    lastTokenUsage = None
    rejectedTableNames = []
    lastColumnRolesErrors = {}
    rejectedColumnNames = {}
    lastAssembledFacts = []
    rejectedAssembledFacts = []

    def __init__(self):
        self.lastColumnRolesByTable = {}
        self.calls = []

    def proposeFactTables(self, tablesSummary, businessNeed=None, contextType=None, factTables=None,
                          classifyColumns=True, physicalLinks=None):
        self.calls.append((factTables, classifyColumns))
        self.lastColumnRolesByTable = {"f03b11": {"keyColumns": ["RPAN8", "RPCO"], "reasoning": "r"}}
        return [FactTableHint("f03b11", "le registre")] if not factTables else []


def testPipelineAsksTheColumnRolesForImposedFactsAndAppliesCallerOverrides(duck_db):
    duck_db.loadTables({"f03b11": LEDGER})
    advisor = _RolesAdvisor()

    results = KawakiriV3Pipeline(duck_db, factHintAdvisor=advisor).run(
        ["f03b11"], factTables=["f03b11"], columnRoles={"f03b11": ColumnRoles.fromLists(measures=["RPCO"])},
    )

    assert advisor.calls == [(["f03b11"], True)]
    byName = {a.columnName: a for a in results["models"][0].assessments}
    assert byName["RPAN8"].roleSource == SOURCE_LLM and byName["RPAN8"].role == EXTRACTED_KEY
    assert byName["RPCO"].roleSource == SOURCE_CALLER and byName["RPCO"].role == MEASURE


def testImposedFactsWithoutColumnHintMakeNoLlmCall(duck_db):
    duck_db.loadTables({"f03b11": LEDGER})
    advisor = _RolesAdvisor()

    KawakiriV3Pipeline(duck_db, factHintAdvisor=advisor).run(["f03b11"], factTables=["f03b11"], columnHint=False)

    assert advisor.calls == []


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


def testFactHintAdvisorKeepsOnlyKnownColumnsAndForwardsImposedFacts():
    session = _Session({
        "factTables": [{"tableName": "f03b11", "justification": "imposee par l'appelant"}],
        "columnRolesByTable": {
            "f03b11": {"reasoning": "r", "keyColumns": ["RPAN8", "RPXX"], "measureColumns": [], "technicalColumns": []},
            "inconnue": {"keyColumns": ["a"]},
        },
        "columnRolesErrors": {"autre": "boom"},
    })
    advisor = FactHintAdvisor(baseUrl="http://advisor", session=session)
    tablesSummary = [{"tableName": "f03b11", "columns": [{"columnName": "RPAN8"}, {"columnName": "RPAG"}]}]

    advisor.proposeFactTables(tablesSummary, factTables=["f03b11"])

    assert session.posted["factTables"] == ["f03b11"] and session.posted["classifyColumns"] is True
    assert advisor.lastColumnRolesByTable == {
        "f03b11": {"reasoning": "r", "keyColumns": ["RPAN8"], "measureColumns": [], "technicalColumns": []},
    }
    assert advisor.rejectedColumnNames == {"f03b11": ["RPXX"]}
    assert advisor.lastColumnRolesErrors == {"autre": "boom"}


def testAUniqueTimeZoneTimestampIsAnEventDateJoinedToTheCalendar(duck_db):
    duck_db.loadTables({"appels": {
        "user_id": [1, 1, 2, 2, 1],
        "creation_date": ["2026-10-02 14:29:26.447+00", "2026-10-02 15:01:02.1+00", "2026-10-03 09:00:00+00",
                          "2026-10-03 09:00:01+00", "2026-10-04 23:59:59+00"],
        "input_tokens": [6607, 7057, 7615, 5742, 6001],
    }})
    duck_db.conn.execute("ALTER TABLE appels ALTER creation_date TYPE TIMESTAMPTZ")
    schemaEngine = SchemaEngine(duck_db, ["appels"]).build()

    model = FactAnalyzer(duck_db, schemaEngine, schemaEngine.inferAllPrimaryKeyCandidates(), "appels").analyze()

    byName = {a.columnName: a for a in model.assessments}
    assert byName["creation_date"].role == EXTRACTED_KEY
    assert byName["creation_date"].dimensionTable == "appels__dim_date"
    assert "creation_date" in model.grain.grainColumns
    # One row per day (in the session's time zone), even with a single date column.
    calendar = model.dimensions[0]
    assert calendar.rowCount == len({row[0] for row in duck_db.query('SELECT date_key FROM "appels__dim_date"')})
    assert calendar.rowCount < 5
