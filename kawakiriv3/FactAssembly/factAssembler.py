"""Builds a fact table no source table holds as is: one row per row of the
anchor table, with measures and keys gathered from other tables along the
physical links (`LinkGraph`). The fact then goes through the usual v3 chain
(`FactAnalyzer`), like any existing fact table.

The grain algebra -- what each source table is to the anchor, and what that
allows:

| relation         | path                                    | a measure                          | a key   |
|------------------|-----------------------------------------|------------------------------------|---------|
| `meme_grain`     | 1:1 links only, either direction        | copied                             | copied  |
| `plus_grossiere` | anchor -> table, through an N:1 link    | additive: spread (`allocation`)    | copied  |
|                  |                                         | non additive: repeated as is       |         |
|                  |                                         | semi additive: refused             |         |
| `plus_fine`      | table -> anchor, through an N:1 link    | aggregated (`sum` if additive)     | refused |
| `hors_perimetre` | none                                    | refused                            | refused |

Following a link maps a row to ONE row of its target, so a path from the
anchor never multiplies the anchor's rows; rows finer than the grain are
aggregated BEFORE being joined. An additive measure coarser than the grain
is never copied as is: copied onto each of an order's lines, its shipping
fee would be summed once per line.

Checks, all turned into warnings (the fact then goes to review):
- the assembled fact has exactly the anchor's row count;
- reconciliation: each additive measure sums to the same total in the
  assembled fact as in its source table -- a gap means source rows that
  reach no row of the grain (orphans), or rows counted twice;
- every refused item, and every table reached by several equally short paths.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.FactAnalysis.columnAssessment import SOURCE_CALLER
from kawakiriv3.FactAnalysis.columnRoles import ColumnRoles
from kawakiriv3.FactAssembly.assemblySpec import (
    ADDITIVE, ALLOCATION_EQUAL, ALLOCATION_PRORATA_PREFIX, NON_ADDITIVE, SEMI_ADDITIVE,
)
from kawakiriv3.Sources.sourceConnector import quoteIdentifier as q

ANCHOR = "ancre"
SAME_GRAIN = "meme_grain"
COARSER = "plus_grossiere"
FINER = "plus_fine"
UNREACHABLE = "hors_perimetre"

MEASURE_ITEM = "mesure"
KEY_ITEM = "cle"

RECONCILIATION_TOLERANCE = 0.01

_SQL_AGGREGATIONS = {"sum": "SUM", "count": "COUNT", "min": "MIN", "max": "MAX", "avg": "AVG"}


@dataclass
class SourcePlan:
    """How `table` relates to the anchor. `path`: the links followed, as
    text; `towardsAnchor`: the path goes from `table` to the anchor (its
    rows are grouped by the anchor's key) rather than from the anchor."""

    table: str
    relation: str
    path: list = field(default_factory=list)
    towardsAnchor: bool = False
    alternatives: list = field(default_factory=list)


@dataclass
class AssembledColumn:
    name: str
    sourceTable: str
    sourceColumn: str
    kind: str
    relation: str
    operation: str
    additivity: Optional[str] = None
    nullRows: int = 0


@dataclass
class RefusedItem:
    table: str
    column: str
    kind: str
    reason: str


@dataclass
class Reconciliation:
    column: str
    source: str
    sourceTotal: float
    assembledTotal: float

    @property
    def delta(self):
        return (self.assembledTotal or 0.0) - (self.sourceTotal or 0.0)

    @property
    def ok(self):
        return abs(self.delta) <= RECONCILIATION_TOLERANCE


@dataclass
class AssemblyResult:
    """`tableName`: the assembled table in DuckDB, None when nothing could be
    built (see `error`). `consumedTables`: the anchor and the tables at its
    grain or finer -- their rows are IN the fact, so they are neither link
    targets nor "finer table" warnings for it."""

    name: str
    anchorTable: str
    tableName: Optional[str] = None
    justification: Optional[str] = None
    expectedGrain: Optional[str] = None
    anchorRowCount: int = 0
    rowCount: int = 0
    sources: list = field(default_factory=list)
    columns: list = field(default_factory=list)
    refused: list = field(default_factory=list)
    reconciliations: list = field(default_factory=list)
    warnings: list = field(default_factory=list)
    consumedTables: list = field(default_factory=list)
    error: Optional[str] = None
    declaredKeys: list = field(default_factory=list)
    declaredMeasures: list = field(default_factory=list)

    def columnRoles(self, source=SOURCE_CALLER):
        """The roles the assembly itself settles: every gathered measure and
        key, plus the anchor's own columns the spec named."""
        measures = [c.name for c in self.columns if c.kind == MEASURE_ITEM] + self.declaredMeasures
        keys = [c.name for c in self.columns if c.kind == KEY_ITEM] + self.declaredKeys
        return ColumnRoles.fromLists(keys=keys, measures=measures, source=source)


def _rank(path):
    return (sum(l.nameHint for l in path), min(l.coverage for l in path))


class FactAssembler:
    """`db`: the run's duckDbUtils. `schemaEngine`: the profiled source
    tables. `linkGraph`: their `LinkGraph`. `spec`: an `AssemblySpec`."""

    def __init__(self, db, schemaEngine, linkGraph, spec):
        self.db = db
        self.schemaEngine = schemaEngine
        self.linkGraph = linkGraph
        self.spec = spec

    def assemble(self):
        spec = self.spec
        result = AssemblyResult(
            name=spec.name, anchorTable=spec.anchorTable, justification=spec.justification,
            expectedGrain=spec.expectedGrain,
        )
        if spec.anchorTable not in self.schemaEngine.profilers:
            result.error = f"table d'ancrage inconnue : {spec.anchorTable!r}"
            result.warnings.append(result.error)
            return result

        self.result = result
        self.anchorColumns = list(self.schemaEngine.profilers[spec.anchorTable].columns)
        self.usedNames = set(self.anchorColumns)
        self.numericNames = {
            c for c in self.anchorColumns
            if self.schemaEngine.profilers[spec.anchorTable].getAggProfiling(c).InferredDataType == "numeric"
        }
        self.forwardPaths = self.linkGraph.shortestPaths(spec.anchorTable)
        self.plans = {}
        self.joins = []
        self.forwardAliases = {}
        self.selects = []          # (sql over the base FROM, name)
        self.reverseItems = {}     # table -> [(aggregate sql over c0, name)]
        self.allocations = []      # (MeasureSpec, plan path, name)
        self.allocatedSelects = [] # (window sql over base, name)
        self.copiedKeys = {}       # (origin table, column) -> name, see _joinOrigin

        seen = set()
        for kind, items in ((MEASURE_ITEM, spec.measures), (KEY_ITEM, spec.keys)):
            for item in items:
                identity = (kind, item.table, item.column, getattr(item, "aggregation", None))
                if identity in seen:
                    continue
                seen.add(identity)
                self._addItem(kind, item)
        for index, (measure, path, name) in enumerate(self.allocations):
            self._addAllocation(measure, path, name, index)

        result.sources = [plan for plan, _ in self.plans.values()]
        result.consumedTables = [spec.anchorTable] + [
            plan.table for plan, _ in self.plans.values() if plan.relation in (SAME_GRAIN, FINER)
        ]
        tableName = spec.name
        if tableName in self.db.getTableNames():
            tableName = f"{spec.name}__assemble"
        try:
            self._materialize(tableName)
        except Exception as exc:
            result.error = f"assemblage impossible : {type(exc).__name__}: {exc}"
            result.warnings.append(result.error)
            return result
        result.tableName = tableName
        self._check()
        return result

    # --- planning -------------------------------------------------------

    def _plan(self, table):
        """`(SourcePlan, path)` for `table`, memoized."""
        if table in self.plans:
            return self.plans[table]
        if table == self.spec.anchorTable:
            planned = (SourcePlan(table, ANCHOR), ())
        elif table in self.forwardPaths:
            planned = self._planFromPaths(table, self.forwardPaths[table], towardsAnchor=False)
        else:
            back = self.linkGraph.shortestPaths(table).get(self.spec.anchorTable)
            planned = (
                self._planFromPaths(table, back, towardsAnchor=True) if back
                else (SourcePlan(table, UNREACHABLE), ())
            )
        self.plans[table] = planned
        plan = planned[0]
        if plan.alternatives:
            self.result.warnings.append(
                f"{table} : plusieurs chemins aussi courts vers l'ancre, retenu {' / '.join(plan.path)} "
                f"(aussi : {'; '.join(plan.alternatives)})"
            )
        return planned

    def _planFromPaths(self, table, paths, towardsAnchor):
        ranked = sorted(paths, key=_rank, reverse=True)
        path = ranked[0]
        if all(link.oneToOne for link in path):
            relation = SAME_GRAIN
        else:
            relation = FINER if towardsAnchor else COARSER
        plan = SourcePlan(
            table, relation, path=[link.describe() for link in path], towardsAnchor=towardsAnchor,
            alternatives=[" / ".join(link.describe() for link in p) for p in ranked[1:]],
        )
        return plan, path

    def _refuse(self, kind, item, reason):
        self.result.refused.append(RefusedItem(item.table, item.column, kind, reason))
        self.result.warnings.append(f"{item.table}.{item.column} ({kind}) refusee : {reason}")

    def _outputName(self, item, default):
        name = getattr(item, "alias", None) or default
        if name in self.usedNames:
            name = f"{item.table}_{name}"
        base, n = name, 2
        while name in self.usedNames:
            name, n = f"{base}_{n}", n + 1
        self.usedNames.add(name)
        return name

    def _addItem(self, kind, item):
        if item.table not in self.schemaEngine.profilers:
            return self._refuse(kind, item, "table inconnue")
        if item.column not in self.schemaEngine.profilers[item.table].columns:
            return self._refuse(kind, item, "colonne inconnue dans cette table")
        plan, path = self._plan(item.table)

        if plan.relation == ANCHOR:
            (self.result.declaredMeasures if kind == MEASURE_ITEM else self.result.declaredKeys).append(item.column)
            return None
        if plan.relation == UNREACHABLE:
            return self._refuse(
                kind, item, f"aucun chemin de liens physiques entre {item.table} et {self.spec.anchorTable} : "
                "un autre grain, ou un autre processus",
            )
        if kind == KEY_ITEM:
            if plan.relation == FINER:
                return self._refuse(
                    kind, item, f"{item.table} est plus fine que le grain : plusieurs valeurs par ligne de "
                    f"{self.spec.anchorTable}",
                )
            return self._addCopy(kind, item, plan, path, "copiee")
        return self._addMeasure(item, plan, path)

    def _addMeasure(self, measure, plan, path):
        if plan.relation == SAME_GRAIN:
            return self._addCopy(MEASURE_ITEM, measure, plan, path, "copiee (meme grain)")

        if plan.relation == FINER:
            aggregation = measure.aggregation or ("sum" if measure.additivity == ADDITIVE else None)
            if aggregation is None:
                return self._refuse(
                    MEASURE_ITEM, measure, f"mesure {measure.additivity} d'une table plus fine que le grain : "
                    "precise une agregation (min, max, avg, count)",
                )
            if aggregation == "sum" and measure.additivity != ADDITIVE:
                return self._refuse(
                    MEASURE_ITEM, measure, f"une somme d'une mesure {measure.additivity} serait fausse",
                )
            name = self._outputName(measure, measure.column if aggregation == "sum" else f"{aggregation}_{measure.column}")
            sql = f"{_SQL_AGGREGATIONS[aggregation]}(c0.{q(measure.column)})"
            if aggregation in ("sum", "count"):
                # No finer row for a row of the grain: nothing happened, zero.
                sql = f"COALESCE({sql}, 0)"
            self.reverseItems.setdefault(measure.table, []).append((sql, name))
            self.numericNames.add(name)
            return self._record(name, measure, MEASURE_ITEM, plan, f"{aggregation} par ligne du grain")

        # Coarser than the grain.
        if measure.additivity == NON_ADDITIVE:
            return self._addCopy(MEASURE_ITEM, measure, plan, path, "repetee sur chaque ligne (non additive)")
        if measure.additivity == SEMI_ADDITIVE:
            return self._refuse(
                MEASURE_ITEM, measure, "mesure semi-additive d'une table plus grossiere que le grain : "
                "recopiee sur chaque ligne, elle serait comptee plusieurs fois",
            )
        if not measure.allocation:
            return self._refuse(
                MEASURE_ITEM, measure, f"{measure.table} est plus grossiere que le grain : recopiee sur chaque "
                f"ligne de {self.spec.anchorTable}, la mesure serait comptee plusieurs fois ; precise une "
                "allocation (egal, prorata:<mesure>)",
            )
        name = self._outputName(measure, measure.column)
        self.allocations.append((measure, path, name))
        return None

    def _joinOrigin(self, table, column, path):
        """The column a forward-path value really comes from: a target key
        of the path holds the same value as the column that joined it
        (`commandes.commande_id` is `lignes.commande_id`), down to the
        anchor when the whole path is made of such keys."""
        while path and (table, column) == (path[-1].targetTable, path[-1].targetColumn):
            table, column = path[-1].sourceTable, path[-1].sourceColumn
            path = path[:-1]
        return (table, column), path

    def _addCopy(self, kind, item, plan, path, operation):
        origin = None
        if kind == KEY_ITEM and not plan.towardsAnchor:
            origin, originPath = self._joinOrigin(item.table, item.column, path)
            if origin[0] == self.spec.anchorTable:
                self.result.declaredKeys.append(origin[1])
                return None
            if origin in self.copiedKeys:
                return None
        name = self._outputName(item, item.column)
        if origin:
            self.copiedKeys[origin] = name
        if plan.towardsAnchor:
            self.reverseItems.setdefault(item.table, []).append((f"ANY_VALUE(c0.{q(item.column)})", name))
        elif origin:
            self.selects.append((f"{self._forwardAlias(originPath)}.{q(origin[1])}", name))
        else:
            self.selects.append((f"{self._forwardAlias(path)}.{q(item.column)}", name))
        if kind == MEASURE_ITEM:
            self.numericNames.add(name)
        return self._record(name, item, kind, plan, operation)

    def _addAllocation(self, measure, path, name, index):
        allocation = measure.allocation
        weight = None
        if allocation.startswith(ALLOCATION_PRORATA_PREFIX):
            weight = allocation[len(ALLOCATION_PRORATA_PREFIX):]
            if weight not in self.numericNames:
                self.usedNames.discard(name)
                return self._refuse(
                    MEASURE_ITEM, measure, f"repartition au prorata de {weight!r}, qui n'est pas une colonne "
                    "numerique du fait assemble",
                )
        alias = self._forwardAlias(path)
        parentKey, parentValue = f"__p{index}", f"__m{index}"
        self.selects.append((f"{alias}.{q(path[-1].targetColumn)}", parentKey))
        self.selects.append((f"{alias}.{q(measure.column)}", parentValue))
        partition = f"OVER (PARTITION BY {q(parentKey)})"
        share = f"{q(parentValue)} / COUNT(*) {partition}"
        if weight:
            weightSql = f"COALESCE({q(weight)}, 0)"
            share = (
                f"CASE WHEN SUM({weightSql}) {partition} = 0 THEN {share} "
                f"ELSE {q(parentValue)} * {weightSql} / SUM({weightSql}) {partition} END"
            )
        self.allocatedSelects.append((share, name))
        self.numericNames.add(name)
        operation = f"repartie au prorata de {weight}" if weight else "repartie a parts egales"
        self._record(name, measure, MEASURE_ITEM, self.plans[measure.table][0], operation)

    def _record(self, name, item, kind, plan, operation):
        self.result.columns.append(AssembledColumn(
            name=name, sourceTable=item.table, sourceColumn=item.column, kind=kind, relation=plan.relation,
            operation=operation, additivity=getattr(item, "additivity", None),
        ))

    # --- SQL --------------------------------------------------------------

    def _forwardAlias(self, path):
        alias = "a"
        for i, link in enumerate(path):
            prefix = tuple(path[:i + 1])
            if prefix not in self.forwardAliases:
                joined = f"j{len(self.forwardAliases) + 1}"
                self.joins.append(
                    f"LEFT JOIN {q(link.targetTable)} AS {joined} "
                    f"ON {alias}.{q(link.sourceColumn)} = {joined}.{q(link.targetColumn)}"
                )
                self.forwardAliases[prefix] = joined
            alias = self.forwardAliases[prefix]
        return alias

    def _reverseJoin(self, table, items, index):
        """Groups `table`'s rows by the anchor key its path leads to, then
        joins the result onto the anchor -- one row per row of the grain."""
        path = self.plans[table][1]
        fromSql, current = f"{q(table)} AS c0", "c0"
        for i, link in enumerate(path[:-1]):
            joined = f"c{i + 1}"
            fromSql += (
                f" LEFT JOIN {q(link.targetTable)} AS {joined} "
                f"ON {current}.{q(link.sourceColumn)} = {joined}.{q(link.targetColumn)}"
            )
            current = joined
        keySql = f"{current}.{q(path[-1].sourceColumn)}"
        alias = f"s{index}"
        values = ", ".join(f"{sql} AS {q(f'__v{i}')}" for i, (sql, _) in enumerate(items))
        self.joins.append(
            f"LEFT JOIN (SELECT {keySql} AS __k, {values} FROM {fromSql} WHERE {keySql} IS NOT NULL "
            f"GROUP BY {keySql}) AS {alias} ON a.{q(path[-1].targetColumn)} = {alias}.__k"
        )
        for i, (sql, name) in enumerate(items):
            # Aggregates already default to 0 inside; a grain row with no
            # finer row at all gets 0 too.
            value = f"{alias}.{q(f'__v{i}')}"
            if sql.startswith("COALESCE("):
                value = f"COALESCE({value}, 0)"
            self.selects.append((value, name))

    def _materialize(self, tableName):
        for index, (table, items) in enumerate(self.reverseItems.items(), start=1):
            self._reverseJoin(table, items, index)
        baseColumns = [f"a.{q(c)} AS {q(c)}" for c in self.anchorColumns] + [
            f"{sql} AS {q(name)}" for sql, name in self.selects
        ]
        visible = self.anchorColumns + [name for _, name in self.selects if not name.startswith("__")]
        outerColumns = [q(c) for c in visible] + [f"{sql} AS {q(name)}" for sql, name in self.allocatedSelects]
        self.sql = (
            f"CREATE OR REPLACE TABLE {q(tableName)} AS "
            f"WITH base AS (SELECT {', '.join(baseColumns)} FROM {q(self.spec.anchorTable)} AS a "
            f"{' '.join(self.joins)}) "
            f"SELECT {', '.join(outerColumns)} FROM base"
        )
        self.db.conn.execute(self.sql)

    # --- checks -----------------------------------------------------------

    def _scalar(self, sql):
        return self.db.conn.execute(sql).fetchone()[0]

    def _check(self):
        result = self.result
        result.anchorRowCount = self.db.getRowCount(self.spec.anchorTable)
        result.rowCount = self.db.getRowCount(result.tableName)
        if result.rowCount != result.anchorRowCount:
            result.warnings.append(
                f"{result.tableName} a {result.rowCount} lignes pour {result.anchorRowCount} dans "
                f"{self.spec.anchorTable} : une jointure a demultiplie les lignes"
            )
        for column in result.columns:
            column.nullRows = self._scalar(
                f"SELECT COUNT(*) - COUNT({q(column.name)}) FROM {q(result.tableName)}"
            )
            if column.kind != MEASURE_ITEM:
                continue
            counted = column.operation.startswith("count")
            if column.additivity != ADDITIVE and not counted:
                continue
            sourceSql = f"COUNT({q(column.sourceColumn)})" if counted else f"SUM({q(column.sourceColumn)})"
            reconciliation = Reconciliation(
                column=column.name, source=f"{column.sourceTable}.{column.sourceColumn}",
                sourceTotal=float(self._scalar(f"SELECT {sourceSql} FROM {q(column.sourceTable)}") or 0.0),
                assembledTotal=float(
                    self._scalar(f"SELECT SUM({q(column.name)}) FROM {q(result.tableName)}") or 0.0
                ),
            )
            result.reconciliations.append(reconciliation)
            if not reconciliation.ok:
                share = abs(reconciliation.delta) / abs(reconciliation.sourceTotal) if reconciliation.sourceTotal else 1.0
                cause = (
                    f"des lignes de {column.sourceTable} ne se rattachent a aucune ligne de {self.spec.anchorTable}"
                    if reconciliation.delta < 0 else "des lignes sont comptees plusieurs fois"
                )
                result.warnings.append(
                    f"reconciliation de {column.name} : {reconciliation.assembledTotal:.2f} dans le fait "
                    f"assemble contre {reconciliation.sourceTotal:.2f} dans {reconciliation.source} (ecart "
                    f"{reconciliation.delta:+.2f}, {share:.2%}) : {cause}"
                )
