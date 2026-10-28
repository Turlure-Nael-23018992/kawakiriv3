"""`KawakiriV3Pipeline`: fact-first star reconstruction.

    source (CSV, ClickHouse, ...) -> DuckDB -> profiling -> physical links
    -> LLM hint: which table(s) the business need makes the fact table(s) --
       existing, or to assemble from measures spread over several tables --
       and, per existing fact table, the column roles the statistics can't settle
    -> per fact to assemble: gather its measures and keys onto its grain
    -> per fact table: classify columns -> link -> extract dimensions -> verify grain

Unlike v2, the roles of the tables are never inferred from the topology of a
relationship graph: the fact table is given (by the caller, or by the LLM hint
read against the business need), and everything else is decided from the
fact's own columns outward.
"""
from __future__ import annotations

from kawakiriv3.FactAnalysis.columnAssessment import SOURCE_CALLER, SOURCE_LLM
from kawakiriv3.FactAnalysis.columnRoles import ColumnRoles
from kawakiriv3.FactAnalysis.factAnalyzer import FactAnalyzer
from kawakiriv3.FactAssembly.factAssembler import FactAssembler
from kawakiriv3.FactAssembly.linkGraph import DECIDED_BY_CACHE, DECIDED_BY_LLM, LinkGraph
from kawakiriv3.FactAssembly.linkVerdictCache import LinkVerdictCache
from kawakiriv3.FactHint.factHintAdvisor import FactHintAdvisor, FactTableHint
from kawakiriv3.FactHint.tablesSummary import buildTablesSummary
from kawakiriv3.Schema.schemaEngine import SchemaEngine
from kawakiriv3.Sources.sourceConnector import duckDbTableName


class KawakiriV3Pipeline:
    """`db`: a duckDbUtils. `factHintAdvisor`: anything exposing
    `proposeFactTables(tablesSummary, businessNeed, contextType, factTables=,
    classifyColumns=)` plus `lastError`/`lastReasoning`/`lastTokenUsage`/
    `rejectedTableNames`/`lastColumnRolesByTable`/`lastColumnRolesErrors`/
    `rejectedColumnNames`/`lastAssembledFacts`/`rejectedAssembledFacts`, and
    `arbitrateLinks(tablesSummary, candidateLinks, businessNeed, contextType)`
    plus `lastLinkError`/`lastLinkTokenUsage` (default: `FactHintAdvisor`,
    built only when a hint is actually needed). `linkVerdictCache`: a
    `LinkVerdictCache` (default: in memory, for this pipeline's runs)."""

    def __init__(self, db, factHintAdvisor=None, linkVerdictCache=None):
        self.db = db
        self.factHintAdvisor = factHintAdvisor
        self.linkVerdictCache = linkVerdictCache or LinkVerdictCache()

    def ingest(self, source, tables=None):
        """Copies `tables` (`{sourceTable: [columns] or None}`, None = every
        table, every column) from `source` into DuckDB. Returns
        `{sourceTable: duckDbTable}`."""
        tables = tables if tables is not None else {name: None for name in source.listTables()}
        loaded = {}
        for sourceTable, columns in tables.items():
            loaded[sourceTable] = source.loadInto(self.db, sourceTable, columns)
        duplicates = {t for t in loaded.values() if list(loaded.values()).count(t) > 1}
        if duplicates:
            raise ValueError(f"plusieurs tables sources chargees sous le meme nom DuckDB : {sorted(duplicates)}")
        return loaded

    def run(
        self, tableNames, businessNeed=None, contextType=None, factTables=None, tableDescriptions=None,
        columnRoles=None, columnHint=True, assembledFacts=None, declaredLinks=None, forbiddenLinks=None,
        linkHint=True,
    ):
        """`tableNames`: the DuckDB tables to analyse. `factTables`: optional
        fact tables imposed by the caller -- the LLM then doesn't choose them.
        `columnRoles`: optional `{table: ColumnRoles}` declared by the caller,
        overriding the LLM's column hint column by column. `columnHint`:
        whether to ask the LLM for the column roles; with imposed fact tables
        and `columnHint=False`, no LLM call is made at all. `assembledFacts`:
        optional `[AssemblySpec, ...]` imposed by the caller -- facts to build
        from several tables; imposed alone, the LLM doesn't choose either.
        `declaredLinks`/`forbiddenLinks`: `"table.column -> table.column"`
        physical links the caller imposes or forbids, over every other
        decision. `linkHint`: whether the LLM arbitrates the doubtful links
        (never in a run that makes no other LLM call; a cached verdict is
        always reused).
        Returns a results dict (see `report.resultsToJson` for its shape)."""
        tableNames = list(tableNames)
        schemaEngine = SchemaEngine(self.db, tableNames).build()
        primaryKeyCandidatesByTable = schemaEngine.inferAllPrimaryKeyCandidates()
        linkGraph = LinkGraph.build(schemaEngine, primaryKeyCandidatesByTable, declaredLinks, forbiddenLinks)
        tablesSummary = buildTablesSummary(schemaEngine, tableDescriptions)
        callerRoles = columnRoles or {}
        imposed = self._imposedHints(factTables, tableNames)
        assemblies = list(assembledFacts or [])
        callerChose = bool(imposed or assemblies)
        usesLlm = not callerChose or (bool(imposed) and columnHint)

        results = {
            "tables": list(tableNames),
            "businessNeed": businessNeed,
            "contextType": contextType,
            "factTableSource": None,
            "factTableHints": [],
            "factHintError": None,
            "factHintReasoning": None,
            "rejectedHintTables": [],
            "columnRolesByTable": {},
            "columnRolesErrors": {},
            "rejectedColumnNames": {},
            "physicalLinks": [],
            "linkDecisions": [],
            "linkArbitrationError": None,
            "linkTokenUsage": None,
            "rejectedAssembledFacts": [],
            "tokenUsage": None,
            "models": [],
            "tableColumns": {
                table: [(c, schemaEngine.profilers[table].getAggProfiling(c).InferredDataType)
                        for c in schemaEngine.profilers[table].columns]
                for table in tableNames
            },
        }

        self._arbitrateLinks(linkGraph, tablesSummary, businessNeed, contextType, linkHint and usesLlm, results)
        results["physicalLinks"] = linkGraph.toSummary()
        results["linkDecisions"] = linkGraph.decisionsSummary()

        assemblySource = SOURCE_CALLER
        hints = imposed
        results["factTableSource"] = "caller" if callerChose else "llm_hint"

        if usesLlm:
            advisor = self._resolveFactHintAdvisor()
            proposed = advisor.proposeFactTables(
                tablesSummary, businessNeed, contextType,
                factTables=[h.tableName for h in imposed] or None, classifyColumns=columnHint,
                physicalLinks=results["physicalLinks"],
            )
            if not callerChose:
                hints = proposed
                assemblies = list(advisor.lastAssembledFacts)
                assemblySource = SOURCE_LLM
                results["factHintReasoning"] = advisor.lastReasoning
                results["rejectedHintTables"] = list(advisor.rejectedTableNames)
                results["rejectedAssembledFacts"] = list(advisor.rejectedAssembledFacts)
            results["factHintError"] = advisor.lastError
            results["columnRolesByTable"] = dict(advisor.lastColumnRolesByTable)
            results["columnRolesErrors"] = dict(advisor.lastColumnRolesErrors)
            results["rejectedColumnNames"] = dict(advisor.rejectedColumnNames)
            results["tokenUsage"] = advisor.lastTokenUsage
        results["factTableHints"] = hints

        for hint in hints:
            roles = ColumnRoles.fromLlm(results["columnRolesByTable"].get(hint.tableName))
            if hint.tableName in callerRoles:
                roles = roles.overriddenBy(callerRoles[hint.tableName])
            origins = {c: (hint.tableName, c) for c in schemaEngine.profilers[hint.tableName].columns}
            results["models"].append(FactAnalyzer(
                self.db, schemaEngine, primaryKeyCandidatesByTable, hint.tableName,
                hint=hint.justification, columnRoles=roles, linkVerdicts=linkGraph.verdictsFor(origins),
            ).analyze())

        for spec in assemblies:
            assembly = FactAssembler(self.db, schemaEngine, linkGraph, spec).assemble()
            if assembly.tableName is None:
                results["rejectedAssembledFacts"].append({"fact": spec.model_dump(), "error": assembly.error})
                continue
            schemaEngine = SchemaEngine(
                self.db, [*schemaEngine.tableNames, assembly.tableName], previous=schemaEngine,
            ).build()
            primaryKeyCandidatesByTable[assembly.tableName] = (
                schemaEngine.primaryKeyEngines[assembly.tableName].inferSourceKeyCandidates()
            )
            roles = assembly.columnRoles(source=assemblySource)
            if spec.name in callerRoles or assembly.tableName in callerRoles:
                roles = roles.overriddenBy(callerRoles.get(spec.name) or callerRoles[assembly.tableName])
            origins = {c: (assembly.anchorTable, c) for c in schemaEngine.profilers[assembly.anchorTable].columns}
            origins.update({c.name: (c.sourceTable, c.sourceColumn) for c in assembly.columns})
            results["models"].append(FactAnalyzer(
                self.db, schemaEngine, primaryKeyCandidatesByTable, assembly.tableName,
                hint=spec.justification, columnRoles=roles, assembly=assembly,
                linkTargets=[t for t in tableNames if t not in assembly.consumedTables],
                linkVerdicts=linkGraph.verdictsFor(origins),
            ).analyze())

        return results

    def _arbitrateLinks(self, linkGraph, tablesSummary, businessNeed, contextType, arbitrate, results):
        """Resolves the doubtful links: cached verdicts first, then one LLM
        call for the rest when `arbitrate`; whatever is left is rejected."""
        if not linkGraph.pending:
            return
        linkGraph.resolve(self.linkVerdictCache.lookup(linkGraph.pending), DECIDED_BY_CACHE)
        if linkGraph.pending and arbitrate:
            advisor = self._resolveFactHintAdvisor()
            pending = linkGraph.pending
            verdicts = advisor.arbitrateLinks(tablesSummary, linkGraph.pendingSummary(), businessNeed, contextType)
            results["linkArbitrationError"] = advisor.lastLinkError
            results["linkTokenUsage"] = advisor.lastLinkTokenUsage
            if verdicts:
                self.linkVerdictCache.store(pending, verdicts)
                linkGraph.resolve(verdicts, DECIDED_BY_LLM)
        if linkGraph.pending:
            linkGraph.rejectPending(
                "non arbitre par le LLM, refuse par prudence" if arbitrate else "arbitrage LLM desactive pour ce run"
            )

    def _imposedHints(self, factTables, tableNames):
        hints = []
        for name in factTables or []:
            tableName = name if name in tableNames else duckDbTableName(name)
            if tableName not in tableNames:
                raise ValueError(f"table de faits imposee inconnue : {name!r}")
            hints.append(FactTableHint(tableName=tableName, justification="imposee par l'appelant"))
        return hints

    def _resolveFactHintAdvisor(self):
        if self.factHintAdvisor is None:
            self.factHintAdvisor = FactHintAdvisor()
        return self.factHintAdvisor
