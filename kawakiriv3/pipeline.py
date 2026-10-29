"""`KawakiriV3Pipeline`: fact-first star reconstruction.

    source (CSV, ClickHouse, ...) -> DuckDB -> profiling -> physical links
    -> LLM hint: which table(s) the business need makes the fact table(s) --
       existing, or to assemble from measures spread over several tables --,
       per existing fact table the column roles the statistics can't settle,
       and whether the need asks for a unification
    -> per fact to assemble: gather its measures and keys onto its grain
    -> per fact table: classify columns -> link -> extract dimensions -> verify grain
    -> post-processing, only when the job declares a `unification` or the
       hint says the need asks for one (the LLM then describes it from the
       built stars): unified dimensions (union + dedup of the stars'
       dimensions describing one entity), the facts' keys to them replaced
       by the unified id, then facts of several stars gathered into one

The stars are always built first, each source on its own, and the
post-processing reads only the stars and the roles they decided: no new
classification, no new link, no role computed again -- it can't break a
star.

Unlike v2, the roles of the tables are never inferred from the topology of a
relationship graph: the fact table is given (by the caller, or by the LLM hint
read against the business need), and everything else is decided from the
fact's own columns outward.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.DerivedTables.derivedTableOutcome import BUILT
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
from kawakiriv3.StarUnification.dimensionUnifier import DimensionUnifier
from kawakiriv3.StarUnification.factGatherer import FactGatherer
from kawakiriv3.StarUnification.keyRemapper import KeyRemapper
from kawakiriv3.StarUnification.starCatalog import StarCatalog


@dataclass
class _FactRun:
    """One fact a star is built for. `name`: how a job or the LLM refers to
    it (the hinted table, or the assembled fact's name); `table`: the DuckDB
    table analysed (the hinted table, or the assembled one); `origins`:
    `{column: (table, column)}` each of its columns comes from; `consumed`:
    the tables whose rows are in it; `headers`: the coarser tables an
    assembled fact copied its header columns from (see `AssemblyResult`).
    Neither are link targets for it."""

    name: str
    table: str
    hint: Optional[str]
    roles: Optional[ColumnRoles]
    origins: dict
    consumed: list
    expectedGrain: Optional[str] = None
    assembly: Optional[object] = None
    headers: list = field(default_factory=list)
    model: Optional[object] = None


class KawakiriV3Pipeline:
    """`db`: a duckDbUtils. `factHintAdvisor`: anything exposing
    `proposeFactTables(tablesSummary, businessNeed, contextType, factTables=,
    classifyColumns=)` plus `lastError`/`lastReasoning`/`lastTokenUsage`/
    `rejectedTableNames`/`lastColumnRolesByTable`/`lastColumnRolesErrors`/
    `rejectedColumnNames`/`lastAssembledFacts`/`rejectedAssembledFacts`/
    `lastUnificationRequested`, `arbitrateLinks(tablesSummary,
    candidateLinks, businessNeed, contextType)` plus
    `lastLinkError`/`lastLinkTokenUsage`, and `proposeUnifications(
    catalogSummary, businessNeed, contextType)` plus
    `lastUnificationError`/`lastUnificationReasoning`/
    `lastUnificationTokenUsage`/`rejectedUnifications` (default:
    `FactHintAdvisor`, built only when a hint is actually needed).
    `linkVerdictCache`: a `LinkVerdictCache` (default: in memory, for this
    pipeline's runs)."""

    def __init__(self, db, factHintAdvisor=None, linkVerdictCache=None):
        self.db = db
        self.factHintAdvisor = factHintAdvisor
        self.linkVerdictCache = linkVerdictCache or LinkVerdictCache()

    def ingest(self, source, tables=None, rowFilters=None):
        """Copies `tables` (`{sourceTable: [columns] or None}`, None = every
        table, every column) from `source` into DuckDB, keeping only the
        rows matching `rowFilters.get(sourceTable)` when given (a SQL
        boolean expression -- see `JobPayload.rowFilters`). Returns
        `{sourceTable: duckDbTable}`."""
        tables = tables if tables is not None else {name: None for name in source.listTables()}
        rowFilters = rowFilters or {}
        loaded = {}
        for sourceTable, columns in tables.items():
            loaded[sourceTable] = source.loadInto(
                self.db, sourceTable, columns, rowFilter=rowFilters.get(sourceTable),
            )
        duplicates = {t for t in loaded.values() if list(loaded.values()).count(t) > 1}
        if duplicates:
            raise ValueError(f"plusieurs tables sources chargees sous le meme nom DuckDB : {sorted(duplicates)}")
        return loaded

    def run(
        self, tableNames, businessNeed=None, contextType=None, factTables=None, tableDescriptions=None,
        columnRoles=None, columnHint=True, assembledFacts=None, declaredLinks=None, forbiddenLinks=None,
        linkHint=True, unification=None,
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
        always reused). `unification`: optional `UnificationSpec` imposed by
        the caller, applied by the post-processing once the stars are built
        (see `_postProcess`) -- the LLM then isn't asked. It names the
        stars' dimensions, and their facts as the job or the hint names
        them.
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
            "unification": {
                "source": None, "requested": False, "reasoning": None, "error": None, "tokenUsage": None,
                "rejected": [], "dimensions": [], "remappedKeys": [], "facts": [],
            },
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
            "replacedModels": [],
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
                results["unification"]["requested"] = bool(getattr(advisor, "lastUnificationRequested", False))
            results["factHintError"] = advisor.lastError
            results["columnRolesByTable"] = dict(advisor.lastColumnRolesByTable)
            results["columnRolesErrors"] = dict(advisor.lastColumnRolesErrors)
            results["rejectedColumnNames"] = dict(advisor.rejectedColumnNames)
            results["tokenUsage"] = advisor.lastTokenUsage
        results["factTableHints"] = hints

        runs = []
        for hint in hints:
            roles = ColumnRoles.fromLlm(results["columnRolesByTable"].get(hint.tableName))
            if hint.tableName in callerRoles:
                roles = roles.overriddenBy(callerRoles[hint.tableName])
            runs.append(_FactRun(
                name=hint.tableName, table=hint.tableName, hint=hint.justification, roles=roles,
                origins={c: (hint.tableName, c) for c in schemaEngine.profilers[hint.tableName].columns},
                consumed=[hint.tableName], expectedGrain=hint.expectedGrain,
            ))

        for spec in assemblies:
            assembly = FactAssembler(self.db, schemaEngine, linkGraph, spec).assemble()
            if assembly.tableName is None:
                results["rejectedAssembledFacts"].append({"fact": spec.model_dump(), "error": assembly.error})
                continue
            schemaEngine = self._withTables(schemaEngine, primaryKeyCandidatesByTable, [assembly.tableName])
            roles = assembly.columnRoles(source=assemblySource)
            if spec.name in callerRoles or assembly.tableName in callerRoles:
                roles = roles.overriddenBy(callerRoles.get(spec.name) or callerRoles[assembly.tableName])
            origins = {c: (assembly.anchorTable, c) for c in schemaEngine.profilers[assembly.anchorTable].columns}
            origins.update({c.name: (c.sourceTable, c.sourceColumn) for c in assembly.columns})
            runs.append(_FactRun(
                name=spec.name, table=assembly.tableName, hint=spec.justification, roles=roles, origins=origins,
                consumed=list(assembly.consumedTables), expectedGrain=spec.expectedGrain, assembly=assembly,
                headers=list(assembly.headerTables),
            ))

        for run in runs:
            run.model = FactAnalyzer(
                self.db, schemaEngine, primaryKeyCandidatesByTable, run.table, hint=run.hint,
                columnRoles=run.roles, assembly=run.assembly,
                linkTargets=[t for t in tableNames if t not in run.consumed and t not in run.headers],
                linkVerdicts=linkGraph.verdictsFor(run.origins),
            ).analyze()

        replacements = self._postProcess(runs, unification, businessNeed, contextType, results)
        models = [model for run in runs for model in replacements.get(run.name, [run.model])]
        results["models"] = list({id(model): model for model in models}.values())
        results["replacedModels"] = [run.model for run in runs if run.name in replacements]
        return results

    # --- post-processing --------------------------------------------------------

    def _postProcess(self, runs, unification, businessNeed, contextType, results):
        """Unifies what the job declares (`unification`) or, when the hint
        said the need asks for it, what the LLM describes from the built
        stars -- nothing otherwise. Four steps, each reading only the stars:

        1. the catalogue of the stars (`StarCatalog`), the LLM's input;
        2. the unified dimensions (`DimensionUnifier`);
        3. in each fact, the keys to a folded dimension replaced by the
           unified id (`KeyRemapper`);
        4. the gathered facts (`FactGatherer`).

        Fills `results["unification"]`; returns `{fact name: [the model
        replacing its star]}` -- its remapped copy, or the gathered fact it
        is in (the star's tables stay in DuckDB)."""
        outcome = results["unification"]
        spec = unification
        if (spec is None or spec.isEmpty) and not outcome["requested"]:
            return {}
        catalog = StarCatalog.build(self.db, [(run.name, run.model, run.expectedGrain) for run in runs])
        if spec is not None and not spec.isEmpty:
            outcome["source"] = "caller"
        else:
            advisor = self._resolveFactHintAdvisor()
            spec = advisor.proposeUnifications(catalog.summary(), businessNeed, contextType)
            outcome.update(
                source="llm", reasoning=advisor.lastUnificationReasoning, error=advisor.lastUnificationError,
                tokenUsage=advisor.lastUnificationTokenUsage, rejected=list(advisor.rejectedUnifications),
            )
        if spec is None or spec.isEmpty:
            return {}

        errors = spec.errorsAgainst(catalog)
        unifier = DimensionUnifier(self.db, catalog)
        unified = [unifier.unify(d, errors.get(d.name)) for d in spec.dimensions]
        built = [u for u in unified if u.status == BUILT]
        for u in built:
            results["tableColumns"][u.name] = u.columns
        outcome["dimensions"] = [dataclasses.asdict(u) for u in unified]

        remapper = KeyRemapper(self.db, catalog, built)
        models, replacements = {}, {}
        for star in catalog.stars.values():
            remapped = remapper.remap(star)
            models[star.name] = remapped or star.model
            if remapped is not None:
                replacements[star.name] = [remapped]
                outcome["remappedKeys"] += [dataclasses.asdict(k) for k in remapped.remappedKeys]

        gatherer = FactGatherer(self.db, catalog, models)
        for factSpec in spec.facts:
            gathered, model = gatherer.gather(factSpec, errors.get(factSpec.name))
            outcome["facts"].append(dataclasses.asdict(gathered))
            if model is not None:
                for name in model.gatheredFacts:
                    replacements[name] = [model]
        return replacements

    def _withTables(self, schemaEngine, primaryKeyCandidatesByTable, tables):
        """`schemaEngine` extended with `tables` (profiled), their key
        candidates added to `primaryKeyCandidatesByTable`."""
        tables = [t for t in tables if t not in schemaEngine.tableNames]
        if not tables:
            return schemaEngine
        schemaEngine = SchemaEngine(self.db, [*schemaEngine.tableNames, *tables], previous=schemaEngine).build()
        for table in tables:
            primaryKeyCandidatesByTable[table] = schemaEngine.primaryKeyEngines[table].inferSourceKeyCandidates()
        return schemaEngine

    # --- LLM helpers ------------------------------------------------------------

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
