"""Materializes `DerivedTableSpec`s as DuckDB tables, in order, so a "dedup"
can read an earlier "union". Ported from kawakiriv2's `derivedTableBuilder`.

- "union": a `UNION ALL` built from the whitelisted column mapping, never
  from LLM-written SQL. Branches are aligned by position, so every branch
  lists the same target columns in the same order (`NULL` for one a source
  doesn't have): two different columns can never silently end up under one
  alias. A target column fed by different type families is cast to VARCHAR
  in every branch, with a `typeWarnings` entry. With `keepUnmappedColumns`,
  the mapping is first completed with every column it leaves out
  (`DerivedTableSpec.withAllColumns`).
- "dedup": union-find identity resolution -- two rows are one identity as
  soon as they share a real value (not NULL, not NaN, not blank text) on any
  match column. Each column of an identity takes its first real value in
  priority order: the most recent `dedupPriorityColumn` first, then the
  sources in `dedupSourcePriority` order (on the label column of the union
  deduplicated), then row order. Over a union with an origin label, an
  identity says which sources it is in (`present_<source>`, instead of the
  label of its priority row alone), and each of `dedupSourceIdColumns` (a
  card number, a web id) gets one column per source (`<column>_<source>`)
  instead of the priority row's value alone. Rows
  never leave DuckDB: only integer row ids reach Python (see
  `_clusterRows`). Also writes
  `<name>__correspondance`: every source row with its unified id, what a
  fact's original keys are resolved through.

Every table and column a spec names is checked against what exists so far,
and a spec never overwrites an existing table: anything wrong skips that spec
alone (`SKIPPED`, with its reason), never the run.
"""
from __future__ import annotations

import re
from array import array

import numpy as np
import pandas as pd

from kawakiriv3.DerivedTables.derivedTableOutcome import BUILT, SKIPPED, DerivedTableOutcome
from kawakiriv3.DerivedTables.derivedTableSpec import DEDUP, UNION
from kawakiriv3.DuckDbManagement.duckDbUtils import _typeFamily
from kawakiriv3.Sources.sourceConnector import quoteIdentifier as q

# One identity absorbing more than this share of a dedup's rows sets
# `aggressiveMergeWarning` (a customer table shouldn't have one "person"
# that is 30 % of all rows): a smell test, never a refusal.
AGGRESSIVE_MERGE_CLUSTER_RATIO_THRESHOLD = 0.2

_TEXT_SQL_TYPES = frozenset({"VARCHAR", "TEXT", "STRING", "CHAR", "BPCHAR"})
_FLOAT_SQL_TYPES = frozenset({"FLOAT", "DOUBLE", "REAL"})

# How many (row, first row with the same value) pairs `_clusterRows` turns
# into Python ints at a time: bounds the union loop's memory, whatever the
# number of duplicate rows.
_EDGE_CHUNK_SIZE = 1 << 20


class DerivedTableBuilder:
    def __init__(self, db):
        self.db = db
        self._labelColumns = {}  # built union -> its origin label column (None without one)

    def buildAll(self, specs):
        """`{spec name: DerivedTableOutcome}`, in spec order, one per spec
        whether built or skipped."""
        columnTypesByTable = {
            table: dict(self.db.getColumns(table, dataType=True)) for table in self.db.getTableNames()
        }
        outcomes = {}
        self._labelColumns = {}
        for spec in specs:
            if spec.name in outcomes:
                outcomes[f"{spec.name}#{len(outcomes)}"] = self._skip(spec, "nom deja utilise par une autre spec")
                continue
            try:
                outcome = self._build(spec, columnTypesByTable)
            except Exception as exc:
                outcome = self._skip(spec, f"echec de construction : {type(exc).__name__}: {exc}")
            outcomes[spec.name] = outcome
            if outcome.status == BUILT:
                if spec.kind == UNION:
                    self._labelColumns[spec.name] = next(
                        iter({spec.labelColumnFor(s) for s in spec.unionSources} - {None}), None,
                    )
                for table in (spec.name, outcome.mappingTable):
                    if table:
                        columnTypesByTable[table] = dict(self.db.getColumns(table, dataType=True))
        return outcomes

    def _build(self, spec, columnTypesByTable):
        taken = [t for t in (spec.name, spec.mappingTableName if spec.kind == DEDUP else None)
                 if t and t in columnTypesByTable]
        if taken:
            return self._skip(spec, f"une table de ce nom existe deja : {taken}")
        if spec.kind == UNION:
            return self._buildUnion(spec, columnTypesByTable)
        return self._buildDedup(spec, columnTypesByTable)

    def _skip(self, spec, reason):
        return DerivedTableOutcome(
            name=spec.name, kind=spec.kind, status=SKIPPED, reason=reason, justification=spec.justification,
        )

    def _buildUnion(self, spec, columnTypesByTable):
        if not spec.unionSources:
            return self._skip(spec, "aucune source fournie")
        spec = spec.withAllColumns(columnTypesByTable)

        targetColumns = []
        for source in spec.unionSources:
            columnTypes = columnTypesByTable.get(source.sourceTable)
            if columnTypes is None:
                return self._skip(spec, f"table source inconnue : {source.sourceTable}")
            if not source.columnMapping:
                return self._skip(spec, f"aucun mapping de colonnes pour {source.sourceTable}")
            unknown = [c for c in source.columnMapping.values() if c not in columnTypes]
            if unknown:
                return self._skip(spec, f"colonnes inconnues sur {source.sourceTable} : {unknown}")
            targetColumns += [c for c in source.columnMapping if c not in targetColumns]

        labelColumns = {spec.labelColumnFor(s) for s in spec.unionSources} - {None}
        if len(labelColumns) > 1:
            return self._skip(spec, f"plusieurs colonnes d'origine differentes : {sorted(labelColumns)}")
        labelColumn = next(iter(labelColumns), None)
        if labelColumn in targetColumns:
            return self._skip(spec, f"la colonne d'origine {labelColumn!r} est aussi une colonne mappee")

        familiesByTarget = {}
        for source in spec.unionSources:
            columnTypes = columnTypesByTable[source.sourceTable]
            for target, column in source.columnMapping.items():
                familiesByTarget.setdefault(target, set()).add(_typeFamily(columnTypes[column]))
        castToText = {target for target, families in familiesByTarget.items() if len(families) > 1}

        selects = []
        for source in spec.unionSources:
            parts = []
            for target in targetColumns:
                column = source.columnMapping.get(target)
                if column is None:
                    parts.append(f"NULL AS {q(target)}")
                elif target in castToText:
                    parts.append(f"CAST({q(column)} AS VARCHAR) AS {q(target)}")
                else:
                    parts.append(f"{q(column)} AS {q(target)}")
            if labelColumn:
                label = (source.sourceLabelValue or source.sourceTable).replace("'", "''")
                parts.append(f"'{label}' AS {q(labelColumn)}")
            selects.append(f"SELECT {', '.join(parts)} FROM {q(source.sourceTable)}")
        columns = targetColumns + ([labelColumn] if labelColumn else [])
        computed = []
        for measure in spec.computedMeasures:
            unknown = [c for c in measure.multiply if c not in columns]
            if measure.name in columns or unknown:
                return self._skip(spec, f"mesure calculee {measure.name!r} : nom deja pris ou colonnes inconnues {unknown}")
            notNumeric = [c for c in measure.multiply if familiesByTarget.get(c) != {"numeric"}]
            if notNumeric:
                return self._skip(spec, f"mesure calculee {measure.name!r} : colonnes non numeriques {notNumeric}")
            computed.append(f"{' * '.join(q(c) for c in measure.multiply)} AS {q(measure.name)}")
            columns.append(measure.name)
        union = " UNION ALL ".join(selects)
        if computed:
            union = f"SELECT *, {', '.join(computed)} FROM ({union})"
        self.db.conn.execute(f"CREATE TABLE {q(spec.name)} AS {union}")

        return DerivedTableOutcome(
            name=spec.name, kind=UNION, status=BUILT, justification=spec.justification,
            sourceRowCounts={s.sourceTable: self.db.getRowCount(s.sourceTable) for s in spec.unionSources},
            totalRows=self.db.getRowCount(spec.name),
            typeWarnings=tuple(
                f"colonne {target!r} : types heterogenes entre sources ({sorted(familiesByTarget[target])}), "
                "convertie en texte"
                for target in targetColumns if target in castToText
            ),
        )

    def _buildDedup(self, spec, columnTypesByTable):
        sourceTable = spec.dedupSourceTable
        columnTypes = columnTypesByTable.get(sourceTable)
        if columnTypes is None:
            return self._skip(spec, f"table source inconnue : {sourceTable}")
        if not spec.dedupMatchColumns:
            return self._skip(spec, "aucune colonne de rapprochement fournie")
        unknown = [c for c in spec.dedupMatchColumns if c not in columnTypes]
        if unknown:
            return self._skip(spec, f"colonnes de rapprochement inconnues : {unknown}")
        if spec.idColumn in columnTypes:
            return self._skip(spec, f"la colonne d'identifiant {spec.idColumn!r} existe deja dans {sourceTable}")
        if any(column.lower() == "rowid" for column in columnTypes):
            # DuckDB's row-id pseudo-column is what the clustering keys on; a
            # real column of that name would shadow it.
            return self._skip(spec, "une colonne \"rowid\" masquerait l'identifiant de ligne interne de DuckDB")
        sourceRowCount = self.db.getRowCount(sourceTable)
        if sourceRowCount == 0:
            return self._skip(spec, f"{sourceTable} est vide")
        priority = self._priorityOrder(spec, columnTypes)
        if isinstance(priority, str):
            return self._skip(spec, priority)
        priorityOrder, survivorship = priority

        labelColumn = self._labelColumns.get(sourceTable)
        perSource = self._perSourceColumns(spec, sourceTable, labelColumn, columnTypes)

        rowids, clusterIds, mergedByColumn = _clusterRows(
            self.db.conn, sourceTable, spec.dedupMatchColumns, columnTypes,
        )
        clusterSizes = np.bincount(clusterIds)
        unifiedId = f'c."_dedup_cluster" + 1 AS {q(spec.idColumn)}'
        identityParts = [unifiedId]
        for name, sqlType in columnTypes.items():
            if name == labelColumn:
                identityParts += [
                    f"bool_or(CAST(s.{q(name)} AS VARCHAR) = {_sqlText(label)}) AS {q(column)}"
                    for label, column in perSource["presence"].items()
                ]
            elif name in perSource["ids"]:
                identityParts += [
                    f"{_firstPresentSql(name, sqlType, f'CAST(s.{q(labelColumn)} AS VARCHAR) = {_sqlText(label)}')} "
                    f"AS {q(column)}"
                    for label, column in perSource["ids"][name].items()
                ]
            else:
                identityParts.append(f"{_firstPresentSql(name, sqlType)} AS {q(name)}")
        ranked = (
            f'(SELECT *, rowid AS "_dedup_source_rowid", '
            f'row_number() OVER (ORDER BY {priorityOrder}) AS "_dedup_rank" FROM {q(sourceTable)})'
        )
        joined = f'FROM {ranked} s JOIN _dedup_clusters c ON s."_dedup_source_rowid" = c."_dedup_rowid"'
        self.db.conn.register("_dedup_clusters", pd.DataFrame({"_dedup_rowid": rowids, "_dedup_cluster": clusterIds}))
        try:
            self.db.conn.execute(
                f"CREATE TABLE {q(spec.name)} AS SELECT {', '.join(identityParts)} {joined} "
                f'GROUP BY c."_dedup_cluster" ORDER BY c."_dedup_cluster"'
            )
            self.db.conn.execute(
                f'CREATE TABLE {q(spec.mappingTableName)} AS SELECT s.* EXCLUDE ("_dedup_source_rowid", '
                f'"_dedup_rank"), {unifiedId} {joined} ORDER BY s."_dedup_source_rowid"'
            )
        finally:
            self.db.conn.unregister("_dedup_clusters")

        largestClusterSize = int(clusterSizes.max())
        return DerivedTableOutcome(
            name=spec.name, kind=DEDUP, status=BUILT, justification=spec.justification,
            sourceTable=sourceTable, sourceRowCount=sourceRowCount, resultRowCount=len(clusterSizes),
            largestClusterSize=largestClusterSize, mergedByColumn=mergedByColumn,
            aggressiveMergeWarning=largestClusterSize / sourceRowCount > AGGRESSIVE_MERGE_CLUSTER_RATIO_THRESHOLD,
            idColumn=spec.idColumn, mappingTable=spec.mappingTableName, survivorship=survivorship,
            sourceColumns={**({labelColumn: list(perSource["presence"].values())} if perSource["presence"] else {}),
                           **{name: list(columns.values()) for name, columns in perSource["ids"].items()}},
        )

    def _perSourceColumns(self, spec, sourceTable, labelColumn, columnTypes):
        """`{"presence": {label: present_<label>}, "ids": {column: {label:
        <column>_<label>}}}` for a dedup over a union with an origin label;
        empty otherwise; ids from `dedupSourceIdColumns`."""
        result = {"presence": {}, "ids": {}}
        if labelColumn is None:
            return result
        labels = [row[0] for row in self.db.query(
            f"SELECT CAST({q(labelColumn)} AS VARCHAR) AS label FROM {q(sourceTable)} "
            f"WHERE {q(labelColumn)} IS NOT NULL GROUP BY 1 ORDER BY MIN(rowid)"
        )]
        taken = set(columnTypes)
        result["presence"] = {label: _freeName(f"present_{_identifier(label)}", taken) for label in labels}
        for name in columnTypes:
            if name in spec.dedupSourceIdColumns and name != labelColumn:
                result["ids"][name] = {label: _freeName(f"{name}_{_identifier(label)}", taken) for label in labels}
        return result

    def _priorityOrder(self, spec, columnTypes):
        """`(SQL ORDER BY ranking the source rows, best first; its French
        description)`, or the reason the spec can't be built."""
        parts, described = [], []
        column = spec.dedupPriorityColumn
        if column:
            if column not in columnTypes:
                return f"colonne de priorite inconnue : {column!r}"
            if _typeFamily(columnTypes[column]) not in ("temporal", "numeric"):
                return f"colonne de priorite {column!r} ni date ni nombre ({columnTypes[column]})"
            parts.append(f"{q(column)} DESC NULLS LAST")
            described.append(f"la plus recente selon {column}")
        if spec.dedupSourcePriority:
            labelColumn = self._labelColumns.get(spec.dedupSourceTable)
            if labelColumn is None:
                return f"priorite par source impossible : {spec.dedupSourceTable} n'est pas une union avec colonne d'origine"
            labels = {row[0] for row in self.db.query(
                f"SELECT DISTINCT CAST({q(labelColumn)} AS VARCHAR) FROM {q(spec.dedupSourceTable)}"
            )}
            unknown = [v for v in spec.dedupSourcePriority if v not in labels]
            if unknown:
                return f"sources de priorite inconnues dans {labelColumn} : {unknown}"
            cases = " ".join(
                f"WHEN '{v.replace(chr(39), chr(39) * 2)}' THEN {i}" for i, v in enumerate(spec.dedupSourcePriority)
            )
            parts.append(f"CASE CAST({q(labelColumn)} AS VARCHAR) {cases} ELSE {len(spec.dedupSourcePriority)} END")
            described.append(f"source prioritaire {' > '.join(spec.dedupSourcePriority)}")
        parts.append("rowid")
        described.append("ordre des lignes")
        return ", ".join(parts), ", puis ".join(described)


def _baseSqlType(sqlType):
    return sqlType.upper().split("(")[0].strip()


def _presentSql(ref, sqlType):
    """SQL predicate: `ref` holds a real value -- not NULL, not NaN (floats),
    not blank once trimmed (text): an export writing "unknown" as '' must
    never merge every unknown into one identity. The one definition shared
    by the clustering and the per-identity aggregation."""
    predicate = f"{ref} IS NOT NULL"
    baseType = _baseSqlType(sqlType)
    if baseType in _FLOAT_SQL_TYPES:
        predicate += f" AND NOT isnan({ref})"
    elif baseType in _TEXT_SQL_TYPES:
        predicate += f" AND length(trim({ref})) > 0"
    return predicate


def _sqlText(value):
    return "'" + str(value).replace("'", "''") + "'"


def _identifier(value):
    return re.sub(r"\W+", "_", str(value)).strip("_").lower() or "source"


def _freeName(name, taken):
    while name in taken:
        name += "_"
    taken.add(name)
    return name


def _firstPresentSql(columnName, sqlType, condition=None):
    """The aggregate collapsing one column over an identity's rows (source
    aliased `s`, ranked by `_dedup_rank`): its first real value in priority
    order, among the rows matching `condition` when given; for text with no
    real value, the first non-NULL one (an all-blank identity stays blank,
    never becomes NULL)."""
    ref = f"s.{q(columnName)}"
    restrict = f" AND {condition}" if condition else ""
    firstPresent = f'arg_min({ref}, s."_dedup_rank") FILTER (WHERE {_presentSql(ref, sqlType)}{restrict})'
    if _baseSqlType(sqlType) not in _TEXT_SQL_TYPES:
        return firstPresent
    return f'coalesce({firstPresent}, arg_min({ref}, s."_dedup_rank") FILTER (WHERE {ref} IS NOT NULL{restrict}))'


def _duplicateEdges(conn, table, column, sqlType):
    """One `(dup_rowid, first_rowid)` pair per row whose real value on
    `column` already occurred on an earlier row: a window computed by DuckDB,
    returned as two integer arrays -- no row content reaches Python."""
    ref = q(column)
    return conn.execute(f"""
        SELECT rowid AS dup_rowid, first_rowid FROM (
            SELECT rowid, MIN(rowid) OVER (PARTITION BY {ref}) AS first_rowid
            FROM {q(table)}
            WHERE {_presentSql(ref, sqlType)}
        )
        WHERE rowid <> first_rowid
    """).fetchnumpy()


def _clusterRows(conn, table, matchColumns, columnTypes):
    """Union-find over `table`'s rows (by DuckDB `rowid`): rows share a
    cluster as soon as they agree on a real value of any match column.
    Returns `(rowids, clusterIds, mergedByColumn)`: every rowid (ascending),
    its 0-based cluster (numbered by first appearance), and the drop in
    clusters each column caused. Memory: O(rows) machine integers, plus one
    column's duplicate edges at a time."""
    rowids = np.asarray(conn.execute(f"SELECT rowid FROM {q(table)} ORDER BY rowid").fetchnumpy()["rowid"])
    parent = array("q", range(len(rowids)))
    mergedByColumn = {column: 0 for column in matchColumns}

    for column in matchColumns:
        edges = _duplicateEdges(conn, table, column, columnTypes[column])
        sources = np.searchsorted(rowids, edges["dup_rowid"])
        targets = np.searchsorted(rowids, edges["first_rowid"])
        for start in range(0, len(sources), _EDGE_CHUNK_SIZE):
            chunk = slice(start, start + _EDGE_CHUNK_SIZE)
            for i, j in zip(sources[chunk].tolist(), targets[chunk].tolist()):
                while parent[i] != i:  # find the root, halving the path as we go
                    parent[i] = parent[parent[i]]
                    i = parent[i]
                while parent[j] != j:
                    parent[j] = parent[parent[j]]
                    j = parent[j]
                if i != j:
                    parent[i] = j
                    mergedByColumn[column] += 1

    roots = np.frombuffer(parent, dtype=np.int64).copy()
    while True:
        flattened = roots[roots]
        if np.array_equal(flattened, roots):
            break
        roots = flattened
    clusterIds, _ = pd.factorize(roots)
    return rowids, clusterIds, mergedByColumn
