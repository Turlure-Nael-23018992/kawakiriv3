"""SQL-pushdown column profiler: computes per-column statistics (counts,
quantiles, entropy, type inference, ...) directly in DuckDb rather than by
materializing columns into Python."""

from collections import Counter
from dataclasses import replace
from datetime import datetime
import math

from kawakiriv3.Profiling.columnProfiling import ColumnProfiling
from kawakiriv3.Profiling.topValue import TopValue


class _Color:
    """ANSI escape codes used by `beautyPrint` for terminal output."""

    RESET = "\033[0m"
    BOLD = "\033[1m"
    CYAN = "\033[36m"
    YELLOW = "\033[33m"
    GREEN = "\033[32m"
    MAGENTA = "\033[35m"
    GREY = "\033[90m"

# Date formats tried in order when parsing VARCHAR values as dates (see
# _parseDate); order matters since some formats can ambiguously match others.
DATE_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d",
    "%d/%m/%Y",
    "%m/%d/%Y",
    "%Y/%m/%d",
)

# DuckDb SQL type names classified as integer vs. floating/decimal, used by
# _category and isContinuousNumeric to tell a discrete count/id apart from a
# continuous measure.
_INTEGER_TYPES = {
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT",
    "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
}
_FLOAT_TYPE_PREFIXES = ("DOUBLE", "FLOAT", "REAL", "DECIMAL")

# Max dependent columns tested per `testFunctionalDependencies` query. Each
# dependent adds a MIN and a MAX value per determinant group to the hash
# aggregate's state, so on a near-unique determinant an unbounded batch on a
# wide table would hold that many values per row in memory.
_FD_BATCH_SIZE = 32

# `testFunctionalDependencies` first tries to refute every candidate on a
# subset of about this many rows (see its docstring), and only re-checks the
# survivors on the whole table. Tables of fewer than twice this many rows
# skip that first pass: the subset would be more than half the table.
_FD_SAMPLE_TARGET_ROWS = 10_000

# The subset keeps whole determinant groups (rows hashed on the determinant)
# only if that leaves at least this many groups per sampled fraction of the
# table; a determinant with fewer, bigger groups (a status, a country code)
# is sampled by row instead, which still leaves plenty of rows per group.
_FD_MIN_SAMPLED_GROUPS = 100


class aggProfiling:
    """Column-level profiling computed in SQL by DuckDb.

    Every aggregate (counts, quantiles, entropy, skewness, ...) is pushed
    down to DuckDb's vectorized engine instead of being computed by looping
    over Python lists. The only things that stay in Python are: parsing
    ambiguous date formats for VARCHAR columns coming from a JSON import
    (tested on a bounded sample, see `_stringOrDateCategory`), and assembling
    the final `ColumnProfiling` dataclass.

    Note on `MemorySize`: this is redefined compared to the pre-DuckDb
    version. Instead of summing `sys.getsizeof()` per Python value (which
    assumes the whole column is already loaded in RAM), it sums the UTF-8
    byte size of the values as stored in the database (`encode()` +
    `octet_length()`). It is no longer a Python object size but an estimate
    of the column's actual on-disk/wire weight — the metric that stays
    meaningful once columns are never fully materialized client-side.
    """

    def __init__(self, db, tableName, columnName=None):
        """db: a duckDbUtils-like object exposing `.conn` (DuckDb connection)
        and `.getColumns(table, dataType=True)`. If `columnName` is given,
        `self.profiling` is eagerly computed for that column."""
        self.db = db
        self.conn = db.conn
        self.tableName = tableName
        self.columnTypes = dict(db.getColumns(tableName, dataType=True))
        self.columns = list(self.columnTypes.keys())
        self.columnName = columnName
        self._cache = {}  # (columnName, pattern, topN, sampleSize) -> ColumnProfiling
        self._dominantDateFormat = {}  # columnName -> the DATE_FORMATS entry
        # that matched most often on _stringOrDateCategory's sample, reused
        # by _dateProfilingFromStrings to vectorize the common case in SQL.
        self._rowCount = None  # lazily set by _totalRowCount
        self.profiling = self.getAggProfiling(columnName) if columnName else None

    def reuseCacheFrom(self, previousProfiler):
        """Seeds this (fresh) instance's cache from `previousProfiler` --
        an `aggProfiling` built earlier for the SAME table, e.g. before a
        derived table was materialized or a star-flattening hop ran. The
        pipeline rebuilds a whole new `SchemaEngine` (and therefore a new
        `aggProfiling` per table) after either of those, which otherwise
        throws away every profiling result already paid for, even for
        tables/columns the rebuild never actually touched.

        Only copies entries for a column whose declared SQL type is
        unchanged -- the one case where a previously computed
        `ColumnProfiling` is still guaranteed correct. A genuinely new
        column (e.g. one `StarFlattener` just copied onto a fact table) or
        one whose type changed is left uncached, computed normally on
        first access. `previousProfiler=None` (nothing to reuse from, e.g.
        a brand-new derived table) or a different table name is a no-op."""
        if previousProfiler is None or previousProfiler.tableName != self.tableName:
            return
        for cacheKey, value in previousProfiler._cache.items():
            columnName = cacheKey[0]
            if self.columnTypes.get(columnName) == previousProfiler.columnTypes.get(columnName):
                self._cache[cacheKey] = value

    def getAggProfiling(self, columnName, pattern=None, topN=5, sampleSize=10):
        """Compute (or return the cached) `ColumnProfiling` for one column.

        Results are cached per call signature so that callers which need the
        same profiling multiple times (e.g. `Identifiability` then
        `PrimaryKeyEngine` on the same column) only pay the SQL cost once.
        """
        cacheKey = (columnName, pattern, topN, sampleSize)
        if cacheKey in self._cache:
            return self._cache[cacheKey]

        sqlType = self.columnTypes[columnName]
        category = self._category(columnName, sqlType)

        general = self._generalStats(columnName)
        topNValues = self._topNValues(columnName, general["total_count"], topN)
        sampleValues = self._sampleValues(columnName, sampleSize)

        distinctCount = general["distinct_count"]
        normalizedEntropy = (
            general["shannon_entropy"] / math.log2(distinctCount) if distinctCount > 1 else 0.0
        )

        profiling = ColumnProfiling(
            TotalCount=general["total_count"],
            NullCount=general["null_count"],
            NullRate=general["null_rate"],
            Sparsity=general["sparsity"],
            DistinctCount=distinctCount,
            DistinctRate=general["distinct_rate"],
            Mode=topNValues[0].value if topNValues else None,
            ModeFrequency=topNValues[0].percentage if topNValues else 0.0,
            TopNValues=topNValues,
            DuplicateCount=general["non_null_count"] - distinctCount,
            IsConstant=distinctCount == 1,
            InferredDataType=category,
            TypeConsistency=self._typeConsistency(columnName, category, sqlType, general["non_null_count"]),
            MemorySize=general["memory_size"],
            SampleValues=sampleValues,
            ShannonEntropy=general["shannon_entropy"],
            NormalizedEntropy=normalizedEntropy,
        )

        if category == "numeric":
            profiling = replace(profiling, **self._numericProfiling(columnName))
        elif category == "string":
            profiling = replace(profiling, **self._stringProfiling(columnName, general["non_null_count"], pattern))
        elif category == "date":
            profiling = replace(profiling, **self._dateProfiling(columnName, sqlType))

        self._cache[cacheKey] = profiling
        return profiling

    def isContinuousNumeric(self, columnName):
        """True for float/decimal columns (DOUBLE, DECIMAL, ...), False for
        integer/boolean/string/date columns. Used to exclude continuous
        measures (price, percentage, ...) from primary-key candidate search,
        since a business identifier is almost never a continuous value."""
        sqlType = self.columnTypes[columnName].upper()
        return sqlType.startswith(_FLOAT_TYPE_PREFIXES)

    def isSimpleKeyCandidate(self, columnName):
        """True if this column alone, on this table, would already qualify
        as a `PrimaryKeyEngine` simple key (not a continuous numeric,
        no null, `DistinctCount == TotalCount`) -- the same single-column
        test `PrimaryKeyEngine._simpleCandidates` runs, exposed here so a
        column already identified as its table's own key never gets
        swallowed as a *dependent* of some other column's functional group
        (`FunctionalGroupEngine._isEligibleDependent`): a table's real
        identifier must stay behind on the base table, not be moved into a
        satellite dimension just because it happened to be listed among a
        determinant's dependent columns."""
        if self.isContinuousNumeric(columnName):
            return False
        profiling = self.getAggProfiling(columnName)
        return profiling.TotalCount > 0 and profiling.NullCount == 0 and profiling.DistinctCount == profiling.TotalCount

    def testColumnsUnique(self, columnNames):
        """Single-query uniqueness test for a tuple of columns: returns
        (totalCount, nullCount, distinctCount) via `COUNT(DISTINCT (...))`.
        Used by `PrimaryKeyEngine` to test composite-key candidates without
        ever pulling the underlying rows into Python."""
        table = self._quotedTable()
        cols = [self._quotedCol(c) for c in columnNames]
        colsCsv = ", ".join(cols)
        nullCond = " OR ".join(f"{c} IS NULL" for c in cols)

        totalCount, nullCount, distinctCount = self.conn.execute(f"""
            SELECT
                COUNT(*),
                COUNT(*) FILTER (WHERE {nullCond}),
                COUNT(DISTINCT ({colsCsv}))
            FROM {table}
        """).fetchone()

        return totalCount, nullCount, distinctCount

    def countKeyViolations(self, columnNames):
        """Like `testColumnsUnique`, but `distinctCount` only counts tuples
        with no null -- so `totalCount - nullCount - distinctCount` is
        exactly the number of duplicate rows. Used to measure how far a
        declared key is from holding on the real data."""
        table = self._quotedTable()
        cols = [self._quotedCol(c) for c in columnNames]
        colsCsv = ", ".join(cols)
        nullCond = " OR ".join(f"{c} IS NULL" for c in cols)

        totalCount, nullCount, distinctCount = self.conn.execute(f"""
            SELECT
                COUNT(*),
                COUNT(*) FILTER (WHERE {nullCond}),
                COUNT(DISTINCT ({colsCsv})) FILTER (WHERE NOT ({nullCond}))
            FROM {table}
        """).fetchone()

        return totalCount, nullCount, distinctCount

    def testFunctionalDependency(self, determinantColumn, dependentColumn):
        """True if `determinantColumn -> dependentColumn` holds: every
        non-null determinant value maps to at most one distinct non-null
        dependent value. Single-pair convenience over
        `testFunctionalDependencies`."""
        return bool(self.testFunctionalDependencies(determinantColumn, [dependentColumn]))

    def testFunctionalDependencies(self, determinantColumn, dependentColumns):
        """The subset of `dependentColumns` (input order kept) that
        `determinantColumn` functionally determines, in two passes.

        1. Refutation on a subset of about `_FD_SAMPLE_TARGET_ROWS` rows.
           A violation found among a subset of the rows is a violation of
           the whole table, so every candidate refuted here is discarded
           for good; on a table where most columns are unrelated (the usual
           case) that leaves few or no survivors. The subset keeps whole
           determinant groups (`hash(determinant) % k = 0`) so it contains
           the pairs of rows that could contradict each other, or, for a
           determinant with too few groups for that (a status, a country
           code), a random row sample.
        2. Verification of the survivors on the whole table -- a sample can
           only refute, never confirm, so the result is exactly what one
           full-table test per dependent would return.

        Each pass tests its candidates with `_dependentsWithoutViolation`:
        one table scan per `_FD_BATCH_SIZE` dependents instead of one per
        dependent."""
        candidates = list(dependentColumns)

        samplePredicate = self._functionalDependencySample(determinantColumn) if candidates else None
        if samplePredicate is not None:
            candidates = self._dependentsWithoutViolation(determinantColumn, candidates, samplePredicate)

        return self._dependentsWithoutViolation(determinantColumn, candidates)

    def _functionalDependencySample(self, determinantColumn):
        """SQL predicate selecting the subset of rows on which
        `testFunctionalDependencies` tries to refute candidates, or None if
        the table is too small for a subset to be worth an extra pass (see
        `_FD_SAMPLE_TARGET_ROWS`)."""
        modulo = self._totalRowCount() // _FD_SAMPLE_TARGET_ROWS
        if modulo < 2:
            return None

        if self.getAggProfiling(determinantColumn).DistinctCount >= _FD_MIN_SAMPLED_GROUPS * modulo:
            return f"hash({self._quotedCol(determinantColumn)}) % {modulo} = 0"
        return f"random() < {1 / modulo}"

    def _totalRowCount(self):
        """Row count of this profiler's table, queried once."""
        if self._rowCount is None:
            self._rowCount = self.conn.execute(f"SELECT COUNT(*) FROM {self._quotedTable()}").fetchone()[0]
        return self._rowCount

    def _dependentsWithoutViolation(self, determinantColumn, dependentColumns, rowPredicate=None):
        """The subset of `dependentColumns` for which no violation of
        `determinantColumn -> dependent` exists among the rows matching
        `rowPredicate` (all rows if None).

        Per determinant value, a dependent has at most one distinct non-null
        value exactly when `MIN(dependent) = MAX(dependent)` (both ignore
        NULLs, matching the "non-null determinant, non-null dependent"
        semantics). Unlike the equivalent `HAVING COUNT(DISTINCT dependent) > 1`,
        MIN/MAX are streaming aggregates: DuckDb keeps two values per group
        instead of building one distinct-value hash table per dependent, so
        batching several dependents into the same GROUP BY actually shares
        the work rather than just the scan. A determinant value whose
        dependents are all NULL yields `NULL <> NULL`, which `bool_or`
        ignores -- no violation, as before."""
        determinant = self._quotedCol(determinantColumn)
        table = self._quotedTable()
        rowFilter = f"AND {rowPredicate}" if rowPredicate else ""
        determined = []

        for start in range(0, len(dependentColumns), _FD_BATCH_SIZE):
            batch = dependentColumns[start:start + _FD_BATCH_SIZE]
            perGroup = ", ".join(
                f"MIN({self._quotedCol(c)}) AS min_{i}, MAX({self._quotedCol(c)}) AS max_{i}"
                for i, c in enumerate(batch)
            )
            anyViolation = ", ".join(f"bool_or(min_{i} <> max_{i})" for i in range(len(batch)))

            violated = self.conn.execute(f"""
                SELECT {anyViolation}
                FROM (
                    SELECT {perGroup}
                    FROM {table}
                    WHERE {determinant} IS NOT NULL {rowFilter}
                    GROUP BY {determinant}
                )
            """).fetchone()

            determined.extend(column for column, isViolated in zip(batch, violated) if not isViolated)

        return determined

    def _quotedCol(self, columnName):
        """Wrap a column name in double quotes for safe use in generated SQL."""
        return f'"{columnName}"'

    def _quotedTable(self, tableName=None):
        """Wrap a table name (defaulting to this profiler's table) in double quotes."""
        return f'"{tableName or self.tableName}"'

    def _category(self, columnName, sqlType):
        """Map a DuckDb column type to one of bool/numeric/date/string.
        For VARCHAR-typed columns (e.g. dates that arrived as JSON strings),
        fall back to a value-sample heuristic since the SQL type alone can't
        tell us it's actually a date."""
        upperType = sqlType.upper()

        if upperType == "BOOLEAN":
            return "bool"
        if upperType in _INTEGER_TYPES or upperType.startswith(_FLOAT_TYPE_PREFIXES):
            return "numeric"
        if upperType == "DATE" or upperType.startswith("TIMESTAMP"):
            return "date"
        if upperType.startswith("VARCHAR") or upperType in ("TEXT", "STRING"):
            return self._stringOrDateCategory(columnName)

        return "string"

    def _stringOrDateCategory(self, columnName):
        """Sample up to 200 non-null values and try to parse them as dates.
        If over 90% parse successfully, classify the column as `date` even
        though it is physically stored as VARCHAR.

        Also records the single `DATE_FORMATS` entry that matched most
        often on this same sample, in `self._dominantDateFormat` --
        `_dateProfilingFromStrings` reuses it to vectorize that one format
        in SQL for the whole column instead of parsing every value in
        Python (see its own docstring)."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()

        sample = self.conn.execute(
            f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL LIMIT 200"
        ).fetchall()
        values = [row[0] for row in sample]

        if not values:
            return "string"

        formatCounts = Counter()
        parsedAsDate = 0
        for v in values:
            parsed, fmt = self._parseDateWithFormat(v)
            if parsed is not None:
                parsedAsDate += 1
                formatCounts[fmt] += 1

        if parsedAsDate / len(values) > 0.9:
            if formatCounts:
                self._dominantDateFormat[columnName] = formatCounts.most_common(1)[0][0]
            return "date"

        return "string"

    def _typeConsistency(self, columnName, category, sqlType, nonNullCount):
        """Percentage of non-null values matching the inferred type.
        Always 100 for columns whose DuckDb type already guarantees it
        (numeric/bool/date columns, or string columns kept as `string`).
        Only VARCHAR columns reclassified as `date` need an actual check —
        capped at a 2000-row sample rather than a full scan, since this is a
        best-effort heuristic on an edge case (JSON-imported date strings),
        not a hard type guarantee."""
        if nonNullCount == 0:
            return 100.0
        if not sqlType.upper().startswith(("VARCHAR", "TEXT", "STRING")):
            return 100.0
        if category != "date":
            return 100.0

        col = self._quotedCol(columnName)
        table = self._quotedTable()

        sample = self.conn.execute(
            f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL LIMIT 2000"
        ).fetchall()
        consistent = sum(1 for (v,) in sample if self._parseDate(v) is not None)

        return consistent / len(sample) * 100 if sample else 100.0

    def _generalStats(self, columnName):
        """Total/null/distinct counts, memory size and Shannon entropy, in
        two passes: one full-table scan for total/null/memory, and one
        grouped scan (on the small distinct-values result set, not the full
        table) for distinct count + entropy, using a window function to get
        the non-null total without a second subquery."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()

        totalCount, nonNullCount, memorySize = self.conn.execute(f"""
            SELECT
                COUNT(*),
                COUNT({col}),
                COALESCE(SUM(octet_length(encode(CAST({col} AS VARCHAR)))), 0)
            FROM {table}
        """).fetchone()

        # Shannon entropy = -SUM(p_i * log2(p_i)) over the non-null value
        # distribution; computed on the (small) grouped counts, not per row.
        distinctCount, shannonEntropy = self.conn.execute(f"""
            WITH counts AS (
                SELECT {col} AS v, COUNT(*) AS cnt
                FROM {table}
                WHERE {col} IS NOT NULL
                GROUP BY {col}
            ),
            withTotal AS (
                SELECT cnt, SUM(cnt) OVER () AS total_non_null FROM counts
            )
            SELECT
                COUNT(*),
                COALESCE(SUM(-(cnt::DOUBLE / total_non_null) * LOG2(cnt::DOUBLE / total_non_null)), 0.0)
            FROM withTotal
        """).fetchone()

        nullCount = totalCount - nonNullCount

        return {
            "total_count": totalCount,
            "null_count": nullCount,
            "non_null_count": nonNullCount,
            "null_rate": (nullCount / totalCount * 100) if totalCount else 0.0,
            "sparsity": (nullCount / totalCount) if totalCount else 0.0,
            "distinct_count": distinctCount,
            "distinct_rate": (distinctCount / totalCount) if totalCount else 0.0,
            "shannon_entropy": shannonEntropy,
            "memory_size": memorySize,
        }

    def _topNValues(self, columnName, totalCount, topN):
        """Top `topN` most frequent non-null values via GROUP BY + ORDER BY
        + LIMIT — DuckDb only ever returns `topN` rows, no full materialization."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()

        rows = self.conn.execute(f"""
            SELECT {col}, COUNT(*) AS cnt
            FROM {table}
            WHERE {col} IS NOT NULL
            GROUP BY {col}
            ORDER BY cnt DESC
            LIMIT {int(topN)}
        """).fetchall()

        return [
            TopValue(value=value, count=count, percentage=(count / totalCount * 100) if totalCount else 0.0)
            for value, count in rows
        ]

    def _sampleValues(self, columnName, sampleSize):
        """Random sample of non-null values via `ORDER BY random() LIMIT n`."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()

        rows = self.conn.execute(f"""
            SELECT {col} FROM {table}
            WHERE {col} IS NOT NULL
            ORDER BY random()
            LIMIT {int(sampleSize)}
        """).fetchall()

        return [row[0] for row in rows]

    def _numericProfiling(self, columnName):
        """Min/max/sum/mean/median/stddev/variance, quantiles, skewness and
        kurtosis via DuckDb's native aggregate functions (population
        variants, matching the previous `statistics.pstdev`/`pvariance`
        semantics), then the IQR-based outlier count as a second, targeted
        query using the bounds computed from the first result."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()

        (
            minV, maxV, sumV, meanV, medianV, stddevV, varianceV,
            p1, p5, p25, p75, p95, p99, skewness, kurtosis,
            zeroCount, negativeCount,
        ) = self.conn.execute(f"""
            SELECT
                MIN({col}), MAX({col}), SUM({col}), AVG({col}),
                MEDIAN({col}), STDDEV_POP({col}), VAR_POP({col}),
                QUANTILE_CONT({col}, 0.01), QUANTILE_CONT({col}, 0.05),
                QUANTILE_CONT({col}, 0.25), QUANTILE_CONT({col}, 0.75),
                QUANTILE_CONT({col}, 0.95), QUANTILE_CONT({col}, 0.99),
                SKEWNESS({col}), KURTOSIS({col}),
                COUNT(*) FILTER (WHERE {col} = 0),
                COUNT(*) FILTER (WHERE {col} < 0)
            FROM {table}
            WHERE {col} IS NOT NULL
        """).fetchone()

        iqr = (p75 - p25) if p75 is not None and p25 is not None else None
        outlierCount = None
        if iqr is not None:
            lowerBound = p25 - 1.5 * iqr
            upperBound = p75 + 1.5 * iqr
            outlierCount = self.conn.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE {col} IS NOT NULL AND ({col} < ? OR {col} > ?)
                """,
                [lowerBound, upperBound],
            ).fetchone()[0]

        return {
            "Min": minV, "Max": maxV, "Mean": meanV, "Median": medianV,
            "StandardDeviation": stddevV, "Variance": varianceV,
            "P1": p1, "P5": p5, "P25": p25, "P75": p75, "P95": p95, "P99": p99,
            "IQR": iqr, "Skewness": skewness, "Kurtosis": kurtosis,
            "ZeroCount": zeroCount, "NegativeCount": negativeCount,
            "OutlierCount": outlierCount, "Sum": sumV,
        }

    def _stringProfiling(self, columnName, nonNullCount, pattern=None):
        """Length stats, blank/whitespace/non-ASCII counts (all in one
        aggregate query), plus a case-inconsistency count (values that only
        differ by case, via GROUP BY lower(value) HAVING > 1 distinct raw
        value) and an optional regex pattern match rate."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()
        castCol = f"CAST({col} AS VARCHAR)"

        minLen, maxLen, avgLen, emptyCount, whitespaceCount, nonAsciiCount = self.conn.execute(f"""
            SELECT
                MIN(LENGTH({castCol})), MAX(LENGTH({castCol})), AVG(LENGTH({castCol})),
                COUNT(*) FILTER (WHERE TRIM({castCol}) = ''),
                COUNT(*) FILTER (WHERE {castCol} <> TRIM({castCol})),
                COUNT(*) FILTER (WHERE octet_length(encode({castCol})) <> LENGTH({castCol}))
            FROM {table}
            WHERE {col} IS NOT NULL
        """).fetchone()

        caseInconsistencyCount = self.conn.execute(f"""
            SELECT COUNT(*) FROM (
                SELECT LOWER({castCol}) AS lowered
                FROM {table}
                WHERE {col} IS NOT NULL
                GROUP BY lowered
                HAVING COUNT(DISTINCT {castCol}) > 1
            )
        """).fetchone()[0]

        profile = {
            "MinLength": minLen, "MaxLength": maxLen, "AvgLength": avgLen,
            "EmptyOrBlankCount": emptyCount,
            "LeadingTrailingWhitespaceCount": whitespaceCount,
            "CaseInconsistencyCount": caseInconsistencyCount,
            "NonAsciiCharCount": nonAsciiCount,
        }

        if pattern is not None:
            matchCount = self.conn.execute(
                f"""
                SELECT COUNT(*) FROM {table}
                WHERE {col} IS NOT NULL AND regexp_matches({castCol}, ?)
                """,
                [pattern],
            ).fetchone()[0]
            profile["PatternMatchRate"] = (matchCount / nonNullCount * 100) if nonNullCount else 0.0
        else:
            profile["PatternMatchRate"] = None

        return profile

    def _dateProfiling(self, columnName, sqlType):
        """Min/max/range/granularity/future-date count for a natively typed
        DATE/TIMESTAMP column, computed in SQL. Delegates to
        `_dateProfilingFromStrings` for VARCHAR columns, since those need the
        per-value multi-format Python parser instead."""
        if sqlType.upper().startswith(("VARCHAR", "TEXT", "STRING")):
            return self._dateProfilingFromStrings(columnName)

        col = self._quotedCol(columnName)
        table = self._quotedTable()
        isDateOnly = sqlType.upper() == "DATE"
        # Through TIMESTAMP first: DuckDB can't cast a TIMESTAMP WITH TIME ZONE to TIME directly.
        timeExpr = "FALSE" if isDateOnly else f"({col}::TIMESTAMP::TIME <> TIME '00:00:00')"

        minDate, maxDate, hasTimeCount = self.conn.execute(f"""
            SELECT MIN({col}), MAX({col}), COUNT(*) FILTER (WHERE {timeExpr})
            FROM {table}
        """).fetchone()

        futureDateCount = self.conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE {col} > now()"
        ).fetchone()[0]

        return {
            "MinDate": minDate,
            "MaxDate": maxDate,
            "DateRangeSpan": (maxDate - minDate) if minDate is not None and maxDate is not None else None,
            "Granularity": "timestamp" if hasTimeCount else "jour",
            "FutureDateCount": futureDateCount,
            "InvalidDateFormatCount": 0,
        }

    def _dateProfilingFromStrings(self, columnName):
        """Same date stats as `_dateProfiling`, but for a VARCHAR column
        holding date-like strings (JSON round-trips lose native date types).

        Vectorizes the common case: `_stringOrDateCategory` already
        recorded, on its own sample, which single `DATE_FORMATS` entry
        matches most values of this column (`self._dominantDateFormat`).
        One `TRY_STRPTIME(...)` pass pushed down to DuckDb parses the WHOLE
        column against that one format -- only the residual it leaves NULL
        (a minority: an alternate format, or a genuinely invalid value) is
        pulled into Python and tried against the REMAINING formats one by
        one, instead of looping over every value as before.

        Falls back to `_dateProfilingFromStringsFullScan` (the original,
        fully-Python behaviour) if no dominant format was recorded --
        purely defensive: `_category` always runs `_stringOrDateCategory`
        first for a column classified `date`, and that always sets one
        whenever its sample wasn't empty."""
        dominantFormat = self._dominantDateFormat.get(columnName)
        if dominantFormat is None:
            return self._dateProfilingFromStringsFullScan(columnName)

        col = self._quotedCol(columnName)
        table = self._quotedTable()
        parsedExpr = f"TRY_STRPTIME({col}, '{dominantFormat}')"

        minDate, maxDate, hasTimeCount, futureDateCount, matchedCount, totalNonNull = self.conn.execute(f"""
            SELECT
                MIN({parsedExpr}),
                MAX({parsedExpr}),
                COUNT(*) FILTER (WHERE {parsedExpr}::TIME <> TIME '00:00:00'),
                COUNT(*) FILTER (WHERE {parsedExpr} > now()),
                COUNT({parsedExpr}),
                COUNT(*)
            FROM {table}
            WHERE {col} IS NOT NULL
        """).fetchone()
        hasTime = hasTimeCount > 0
        residualCount = totalNonNull - matchedCount

        remainingFormats = [fmt for fmt in DATE_FORMATS if fmt != dominantFormat]
        if residualCount == 0 or not remainingFormats:
            return {
                "MinDate": minDate, "MaxDate": maxDate,
                "DateRangeSpan": (maxDate - minDate) if minDate is not None and maxDate is not None else None,
                "Granularity": "timestamp" if hasTime else "jour",
                "FutureDateCount": futureDateCount,
                "InvalidDateFormatCount": residualCount,
            }

        residualRows = self.conn.execute(
            f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL AND {parsedExpr} IS NULL"
        ).fetchall()

        residualParsedDates = []
        invalidCount = 0
        for (v,) in residualRows:
            parsed = self._parseDate(v, formats=remainingFormats)
            if parsed is not None:
                residualParsedDates.append(parsed)
            else:
                invalidCount += 1

        if residualParsedDates:
            residualMin, residualMax = min(residualParsedDates), max(residualParsedDates)
            minDate = min(minDate, residualMin) if minDate is not None else residualMin
            maxDate = max(maxDate, residualMax) if maxDate is not None else residualMax
            hasTime = hasTime or any(d.time() != datetime.min.time() for d in residualParsedDates)
            futureDateCount += sum(1 for d in residualParsedDates if d > datetime.now())

        return {
            "MinDate": minDate, "MaxDate": maxDate,
            "DateRangeSpan": (maxDate - minDate) if minDate is not None and maxDate is not None else None,
            "Granularity": "timestamp" if hasTime else "jour",
            "FutureDateCount": futureDateCount,
            "InvalidDateFormatCount": invalidCount,
        }

    def _dateProfilingFromStringsFullScan(self, columnName):
        """The original, fully-Python implementation of
        `_dateProfilingFromStrings`: pulls the full column into Python and
        tries every one of `DATE_FORMATS` per value. Kept as the defensive
        fallback for when no dominant format is known (see caller)."""
        col = self._quotedCol(columnName)
        table = self._quotedTable()

        rows = self.conn.execute(f"SELECT {col} FROM {table} WHERE {col} IS NOT NULL").fetchall()
        parsedDates = []
        invalidCount = 0

        for (v,) in rows:
            parsed = self._parseDate(v)
            if parsed is not None:
                parsedDates.append(parsed)
            else:
                invalidCount += 1

        if not parsedDates:
            return {
                "MinDate": None, "MaxDate": None, "DateRangeSpan": None,
                "Granularity": None, "FutureDateCount": 0,
                "InvalidDateFormatCount": invalidCount,
            }

        minDate = min(parsedDates)
        maxDate = max(parsedDates)
        hasTime = any(d.time() != datetime.min.time() for d in parsedDates)

        return {
            "MinDate": minDate, "MaxDate": maxDate,
            "DateRangeSpan": maxDate - minDate,
            "Granularity": "timestamp" if hasTime else "jour",
            "FutureDateCount": sum(1 for d in parsedDates if d > datetime.now()),
            "InvalidDateFormatCount": invalidCount,
        }

    def _parseDateWithFormat(self, value, formats=DATE_FORMATS):
        """Try each of `formats` (`DATE_FORMATS` by default) in turn;
        return `(parsedDatetime, matchedFormat)`, or `(None, None)` if none
        match. Non-string values never parse (they're either already
        dates, handled elsewhere, or genuinely not date-like)."""
        if not isinstance(value, str):
            return None, None

        for fmt in formats:
            try:
                return datetime.strptime(value, fmt), fmt
            except ValueError:
                continue

        return None, None

    def _parseDate(self, value, formats=DATE_FORMATS):
        """Convenience wrapper over `_parseDateWithFormat` for callers that
        only care about the parsed value, not which format matched."""
        return self._parseDateWithFormat(value, formats=formats)[0]

    def beautyPrint(self, profiling, columnName=None):
        """Pretty-print a `ColumnProfiling` to the terminal with ANSI colors,
        showing only the section relevant to `InferredDataType`."""
        general_keys = [
            "TotalCount", "NullCount", "NullRate", "Sparsity", "DistinctCount", "DistinctRate",
            "Mode", "ModeFrequency", "DuplicateCount", "IsConstant",
            "InferredDataType", "TypeConsistency", "MemorySize",
            "ShannonEntropy", "NormalizedEntropy",
        ]
        numeric_keys = [
            "Min", "Max", "Sum", "Mean", "Median", "StandardDeviation", "Variance",
            "P1", "P5", "P25", "P75", "P95", "P99", "IQR", "Skewness", "Kurtosis",
            "ZeroCount", "NegativeCount", "OutlierCount",
        ]
        string_keys = [
            "MinLength", "MaxLength", "AvgLength", "EmptyOrBlankCount",
            "LeadingTrailingWhitespaceCount", "CaseInconsistencyCount",
            "NonAsciiCharCount", "PatternMatchRate",
        ]
        date_keys = [
            "MinDate", "MaxDate", "DateRangeSpan", "Granularity",
            "FutureDateCount", "InvalidDateFormatCount",
        ]

        title = f"Profiling de la colonne '{columnName}'" if columnName else f"Profiling de la table '{self.tableName}'"
        width = max(60, len(title) + 4)

        print(_Color.CYAN + "=" * width + _Color.RESET)
        print(_Color.BOLD + _Color.CYAN + title + _Color.RESET)
        print(_Color.CYAN + "=" * width + _Color.RESET)

        self._printSection("Général", profiling, general_keys)

        inferred_data_type = profiling.InferredDataType
        if inferred_data_type == "numeric":
            self._printSection("Numérique", profiling, numeric_keys)
        elif inferred_data_type == "string":
            self._printSection("Texte", profiling, string_keys)
        elif inferred_data_type == "date":
            self._printSection("Date", profiling, date_keys)

        top_n_values = profiling.TopNValues
        if top_n_values:
            print(f"\n{_Color.BOLD}{_Color.YELLOW}--- Top valeurs ---{_Color.RESET}")
            for entry in top_n_values:
                print(f"  {_Color.MAGENTA}{entry.value!r}{_Color.RESET} : {_Color.GREEN}{entry.count}{_Color.RESET} ({_Color.GREEN}{entry.percentage:.2f}%{_Color.RESET})")

        sample_values = profiling.SampleValues
        if sample_values:
            print(f"\n{_Color.BOLD}{_Color.YELLOW}--- Échantillon de valeurs ---{_Color.RESET}")
            print("  " + ", ".join(f"{_Color.MAGENTA}{v!r}{_Color.RESET}" for v in sample_values))

        print(_Color.CYAN + "=" * width + _Color.RESET)

    def _printSection(self, title, profiling, keys):
        """Print one titled section of `beautyPrint`, skipping any key whose value is None."""
        print(f"\n{_Color.BOLD}{_Color.YELLOW}--- {title} ---{_Color.RESET}")
        for key in keys:
            value = getattr(profiling, key, None)
            if value is None:
                continue
            if isinstance(value, float):
                print(f"  {_Color.GREY}{key}{_Color.RESET}: {_Color.GREEN}{value:.4f}{_Color.RESET}")
            else:
                print(f"  {_Color.GREY}{key}{_Color.RESET}: {_Color.GREEN}{value}{_Color.RESET}")
