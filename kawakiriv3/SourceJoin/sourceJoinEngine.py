"""Statistically infers FK-style join candidates between the tables of a
SchemaEngine by testing column value overlap against primary-key candidates."""

import re

import duckdb

from kawakiriv3.SourceJoin.sourceJoinCandidate import SourceJoinCandidate

JOIN_SUCCESS_THRESHOLD = 0.95  # high bar: below this, a match ratio is too likely to be coincidental overlap rather than a real FK relationship
TABLE_PREFIXES = ("dim_", "fact_")
SNAKE_CASE_KEY_SUFFIX = re.compile(r"_(id|fk|key|pk)$", re.IGNORECASE)
CAMEL_CASE_KEY_SUFFIX = re.compile(r"(?<=[a-z])(Id|Fk|Key|Pk)$")

# Same `InferredDataType` category ("date") can cover both a native DATE/
# TIMESTAMP column and a VARCHAR column whose values merely *look* like
# dates (see aggProfiling._stringOrDateCategory) -- DuckDb refuses to
# compare those two physical types directly in an IN clause. When one side
# is textual and the other a native date type, both sides are cast to
# VARCHAR for the match test (see _needsTextCast/_countMatchesForColumn below).
_DATE_SQL_PREFIXES = ("DATE", "TIMESTAMP")
_TEXT_SQL_PREFIXES = ("VARCHAR", "TEXT", "STRING", "CHAR")


def _needsTextCast(sourceSqlType, targetSqlType):
    source = sourceSqlType.upper()
    target = targetSqlType.upper()
    sourceIsDate = source.startswith(_DATE_SQL_PREFIXES)
    targetIsDate = target.startswith(_DATE_SQL_PREFIXES)
    sourceIsText = source.startswith(_TEXT_SQL_PREFIXES)
    targetIsText = target.startswith(_TEXT_SQL_PREFIXES)

    return (sourceIsDate and targetIsText) or (sourceIsText and targetIsDate)


def _concept(name):
    """Normalize a column name to its "business concept": strip a trailing
    key-ish suffix (_id/_fk/_key/_pk or Id/Fk/Key/Pk), lowercase, and
    de-pluralize. Used only as a naming *hint*, never as a hard filter — see
    the note on `nameHint` below."""
    stripped = CAMEL_CASE_KEY_SUFFIX.sub("", name)
    stripped = SNAKE_CASE_KEY_SUFFIX.sub("", stripped)
    stripped = stripped.strip("_").lower()

    if stripped.endswith("ies"):
        return stripped[:-3] + "y"
    if stripped.endswith("s") and not stripped.endswith("ss"):
        return stripped[:-1]

    return stripped


def _tableConcept(tableName):
    """Like `_concept`, but for a table name: strips a `dim_`/`fact_` prefix first so e.g. `dim_customers` and `customers` normalize the same."""
    lowered = tableName.lower()
    for prefix in TABLE_PREFIXES:
        if lowered.startswith(prefix):
            lowered = lowered[len(prefix):]
            break

    return _concept(lowered)


def _quote(identifier):
    """Wraps an identifier in double quotes for safe use in generated SQL."""
    return f'"{identifier}"'


class SourceJoinEngine:
    """Infers likely FK-style joins between tables of an already-built
    `SchemaEngine`, by testing every (source column) x (single-column primary
    key candidate) pair of matching inferred type and keeping the ones whose
    match ratio clears `JOIN_SUCCESS_THRESHOLD`."""

    def __init__(
        self, schemaEngine, primaryKeyCandidatesByTable, declaredForeignKeys=None,
        sourceColumnsByTable=None, targetTables=None,
    ):
        """Binds the engine to an already-profiled `schemaEngine` and the
        primary-key candidates found for each of its tables.
        `declaredForeignKeys`: optional `((sourceTable, sourceColumn,
        targetTable, targetColumn), ...)` the source system declared --
        always returned as candidates, see `_declaredCandidates`.

        `sourceColumnsByTable`: optional `{tableName: columnNames}` -- only
        these columns are tested as join sources (v3 only ever links OUT of
        a known fact table, so testing every column of every table would be
        wasted SQL). `None` tests every column of every table, as in v2.
        `targetTables`: optional, the only tables whose key candidates may
        be join targets. `None` allows every table."""
        if not schemaEngine.profilers:
            raise ValueError("SourceJoinEngine: aucune table dans le schemaEngine, rien a joindre.")

        self.schemaEngine = schemaEngine
        self.primaryKeyCandidatesByTable = primaryKeyCandidatesByTable
        self.sourceColumnsByTable = (
            {table: tuple(columns) for table, columns in sourceColumnsByTable.items()}
            if sourceColumnsByTable is not None else None
        )
        self.targetTables = set(targetTables) if targetTables is not None else None
        self.declaredForeignKeys = tuple(
            foreignKey for foreignKey in (declaredForeignKeys or ())
            if self._hasColumn(foreignKey[0], foreignKey[1]) and self._hasColumn(foreignKey[2], foreignKey[3])
        )
        # All profilers share the same DuckDb connection (one table per
        # profiler, same in-process database), so any of them exposes it.
        self.conn = next(iter(schemaEngine.profilers.values())).conn

    def inferSourceJoins(self, threshold=JOIN_SUCCESS_THRESHOLD):
        """For every column of every table, test it against every candidate
        target key of the same inferred type and keep matches above
        `threshold`. Results are sorted by `nameHint` first, then by ratio —
        `nameHint` is a sort signal, not a filter (see `_concept`'s
        docstring): a hard filter would silently drop a real relationship
        whose column names don't happen to match (e.g. `user_id` ->
        `customer_id`).

        All of a column's applicable target keys are tested in a single
        query (see `_countMatchesForColumn`) instead of one query per
        (source column, target key) pair -- besides fewer round trips, this
        lets DuckDb scan `sourceTable` once per column for every candidate
        target instead of once per candidate.

        A declared foreign key is never subject to `threshold` nor to the
        target being a key candidate: it is always returned, first, with
        its real ratio (see `_declaredCandidates`). The same pair is not
        tested statistically a second time; every other target of the same
        source column still is."""
        targetKeys = self._collectSingleColumnTargetKeys()
        declaredPairs = set(self.declaredForeignKeys)
        candidates = self._declaredCandidates()

        for sourceTable, sourceColumns in self._sourceColumns():
            profiler = self.schemaEngine.profilers[sourceTable]
            for sourceColumn in sourceColumns:
                profiling = profiler.getAggProfiling(sourceColumn)

                if profiling.TotalCount == 0 or profiling.DistinctCount <= 1:
                    continue  # empty or constant column: a 100% match here wouldn't prove anything

                sourceConcept = _concept(sourceColumn)
                sourceType = profiling.InferredDataType

                applicableTargets = [
                    (targetTable, targetColumn, targetSqlType)
                    for targetTable, targetColumn, targetType, targetSqlType in targetKeys
                    if targetType == sourceType
                    and not (sourceTable == targetTable and sourceColumn == targetColumn)
                ]
                if not applicableTargets:
                    continue

                matchedCounts, sourceNonNull = self._countMatchesForColumn(
                    sourceTable, sourceColumn, profiler.columnTypes[sourceColumn], applicableTargets,
                )
                if sourceNonNull == 0:
                    continue

                for (targetTable, targetColumn, _), matched in zip(applicableTargets, matchedCounts):
                    if (sourceTable, sourceColumn, targetTable, targetColumn) in declaredPairs:
                        continue
                    ratio = matched / sourceNonNull
                    if ratio >= threshold:
                        nameHint = sourceConcept == _concept(targetColumn) or sourceConcept == _tableConcept(targetTable)

                        candidates.append(
                            SourceJoinCandidate(
                                sourceTable=sourceTable,
                                sourceColumn=sourceColumn,
                                targetTable=targetTable,
                                targetColumn=targetColumn,
                                joinSuccessRatio=ratio,
                                matchedRows=matched,
                                sourceNonNullRows=sourceNonNull,
                                nameHint=nameHint,
                            )
                        )

        return sorted(candidates, key=lambda c: (c.declared, c.nameHint, c.joinSuccessRatio), reverse=True)

    def _sourceColumns(self):
        """`(tableName, columnNames)` pairs to test as join sources -- see
        `sourceColumnsByTable` in `__init__`."""
        if self.sourceColumnsByTable is None:
            return [(table, profiler.columns) for table, profiler in self.schemaEngine.profilers.items()]
        return [
            (table, [c for c in columns if c in self.schemaEngine.profilers[table].columns])
            for table, columns in self.sourceColumnsByTable.items()
            if table in self.schemaEngine.profilers
        ]

    def _hasColumn(self, tableName, columnName):
        profiler = self.schemaEngine.profilers.get(tableName)
        return profiler is not None and columnName in profiler.columns

    def _declaredCandidates(self):
        """One `declared=True` candidate per declared foreign key, its
        ratio measured on the real data. A source column with no non-null
        value has nothing orphaned: ratio 1.0. When the two columns' types
        can't be compared directly, both sides are compared as text."""
        candidates = []
        for sourceTable, sourceColumn, targetTable, targetColumn in self.declaredForeignKeys:
            sourceProfiler = self.schemaEngine.profilers[sourceTable]
            targetProfiler = self.schemaEngine.profilers[targetTable]
            target = (targetTable, targetColumn, targetProfiler.columnTypes[targetColumn])
            sourceSqlType = sourceProfiler.columnTypes[sourceColumn]
            try:
                matchedCounts, sourceNonNull = self._countMatchesForColumn(
                    sourceTable, sourceColumn, sourceSqlType, [target],
                )
            except duckdb.Error:
                matchedCounts, sourceNonNull = self._countMatchesForColumn(
                    sourceTable, sourceColumn, sourceSqlType, [target], forceTextCast=True,
                )
            matched = matchedCounts[0]
            candidates.append(
                SourceJoinCandidate(
                    sourceTable=sourceTable,
                    sourceColumn=sourceColumn,
                    targetTable=targetTable,
                    targetColumn=targetColumn,
                    joinSuccessRatio=matched / sourceNonNull if sourceNonNull else 1.0,
                    matchedRows=matched,
                    sourceNonNullRows=sourceNonNull,
                    nameHint=_concept(sourceColumn) in (_concept(targetColumn), _tableConcept(targetTable)),
                    declared=True,
                )
            )
        return candidates

    def _collectSingleColumnTargetKeys(self):
        """Only single-column ("simple") primary-key candidates can be join
        targets here: the match test compares one source column against one
        target column, so composite keys (e.g. `(order_id, line_number)`)
        are never candidate targets. Carries each target's actual DuckDb
        column type alongside its `InferredDataType` category, needed by
        `_needsTextCast` in `_countMatchesForColumn`."""
        targetKeys = []

        for tableName, candidates in self.primaryKeyCandidatesByTable.items():
            if self.targetTables is not None and tableName not in self.targetTables:
                continue
            profiler = self.schemaEngine.profilers[tableName]

            for candidate in candidates:
                if candidate.isComposite:
                    continue

                columnName = candidate.columnNames[0]
                columnType = profiler.getAggProfiling(columnName).InferredDataType
                targetKeys.append((tableName, columnName, columnType, profiler.columnTypes[columnName]))

        return targetKeys

    def _countMatchesForColumn(self, sourceTable, sourceColumn, sourceSqlType, targetKeys, forceTextCast=False):
        """SQL semi-join, batched: how many non-null source values exist in
        each candidate target column, computed as one `COUNT(*) FILTER(...)`
        per target key within a single query. DuckDb hash-joins each filter
        internally and scans `sourceTable` only once for the whole batch --
        no Python set is built and no row ever crosses into Python for this
        check, same as before, just no longer once per (source column,
        target key) pair.

        `sourceSqlType`/the target's own physical type (last element of
        each `targetKeys` entry) are the columns' actual DuckDb types (not
        `InferredDataType`, which can call both a native DATE column and a
        date-like VARCHAR column "date"). When the two physical types mix a
        native date type with text, DuckDb can't compare them directly in
        the IN clause -- both sides are cast to VARCHAR for that pair only,
        matching the raw-value comparison already used for two text columns
        (see `_needsTextCast`).

        Returns `(matchedCounts, sourceNonNull)` where `matchedCounts[i]`
        lines up with `targetKeys[i]`."""
        sourceCol = _quote(sourceColumn)

        filters = []
        for targetTable, targetColumn, targetSqlType in targetKeys:
            targetCol = _quote(targetColumn)

            if forceTextCast or _needsTextCast(sourceSqlType, targetSqlType):
                sourceExpr = f"CAST({sourceCol} AS VARCHAR)"
                targetExpr = f"CAST({targetCol} AS VARCHAR)"
            else:
                sourceExpr = sourceCol
                targetExpr = targetCol

            filters.append(
                f"COUNT(*) FILTER (WHERE {sourceExpr} IN ("
                f"SELECT {targetExpr} FROM {_quote(targetTable)} WHERE {targetCol} IS NOT NULL"
                f"))"
            )

        selectClause = ",\n                ".join(filters + [f"COUNT({sourceCol})"])
        row = self.conn.execute(f"""
            SELECT
                {selectClause}
            FROM {_quote(sourceTable)}
        """).fetchone()

        matchedCounts, sourceNonNull = list(row[:-1]), row[-1]
        return matchedCounts, sourceNonNull
