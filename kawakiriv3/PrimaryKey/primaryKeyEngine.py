"""Engine that infers primary-key candidates for a single table: simple
single-column keys first, falling back to composite keys (2 columns, then
3, ... up to `MAX_COMPOSITE_SIZE`)."""

from itertools import combinations

# Weights for the confidence score: raw uniqueness dominates, identifiability
# (how "id-like" the column looks) is a secondary signal. See _buildCandidate.
UNIQUENESS_WEIGHT = 0.7
IDENTIFIABILITY_WEIGHT = 0.3
MAX_COMPOSITE_SIZE = 12

from kawakiriv3.PrimaryKey.primaryKeyCandidate import primaryKeyCandidate


class PrimaryKeyEngine:
    """Infers primary-key candidates (simple, then composite) for a single
    table, from an `aggProfiling` + its `Identifiability` scores."""

    def __init__(self, profiler, identifiability, knownKeyColumns=None, declaredKeyColumns=None):
        """`profiler`: the table's `aggProfiling`, used for both the simple
        key null/distinct check and the composite `testColumnsUnique` SQL
        pushdown. `identifiability`: the table's `Identifiability`, whose
        per-column scores feed into `confidence` alongside raw uniqueness.

        `knownKeyColumns`: set when the table's key is already known by
        construction rather than something to search for -- e.g. a
        `LogicalTableEngine` extracted table (`{tableName}__{determinant}`),
        whose determinant is guaranteed unique and non-null by the
        `SELECT DISTINCT ... WHERE determinant IS NOT NULL` that built it.
        When set, the statistical search below is skipped entirely and only
        that column is confirmed as the key: without this, any other column
        that happens to be unique on the extracted (deduplicated, usually
        much smaller) table -- a plain integer measure isn't excluded from
        key search the way a float/decimal is, see `_keyableColumns` -- can
        surface as a second, spurious key candidate purely by coincidence,
        and then attract equally spurious joins from unrelated tables.

        `declaredKeyColumns`: the key the source system declared
        (`isPrimaryKey`, simple or composite). Imposed, never verified away:
        it always comes first, at confidence 1.0, even when the data
        contradicts it (`primaryKeyCandidate.contradictedByData`). The simple
        key search still runs on every other column -- a column not
        declared can still be a key (e.g. a unique `email`); the composite
        search is skipped, the table's key being known."""
        self.profiler = profiler
        self.identifiability = identifiability
        self.knownKeyColumns = tuple(knownKeyColumns) if knownKeyColumns else None
        self.declaredKeyColumns = tuple(declaredKeyColumns) if declaredKeyColumns else None

    def inferSourceKeyCandidates(self):
        """Simple single-column candidates first; only look for composite
        (2-column, then 3-column, ... up to `MAX_COMPOSITE_SIZE`)
        candidates if none was found, since a table with
        a valid simple key never needs a composite one."""
        scores = self.identifiability.computeTableScores()

        if self.knownKeyColumns is not None:
            return self._knownKeyCandidates(scores)

        if self.declaredKeyColumns is not None:
            return self._declaredKeyCandidates(scores)

        simpleCandidates = self._simpleCandidates(scores)
        if simpleCandidates:
            return sorted(simpleCandidates, key=lambda c: c.confidence, reverse=True)

        compositeCandidates = self._compositeCandidates(scores)
        return sorted(compositeCandidates, key=lambda c: c.confidence, reverse=True)

    def _declaredKeyCandidates(self, scores):
        """The declared key first, then every other simple key found."""
        totalCount, nullCount, distinctCount = self.profiler.countKeyViolations(self.declaredKeyColumns)
        declared = self._buildCandidate(
            columnNames=self.declaredKeyColumns,
            totalCount=totalCount,
            nullCount=nullCount,
            distinctCount=distinctCount,
            scores=scores,
            reason="declared_key: cle primaire declaree par la source",
        )
        declared.confidence = 1.0
        declared.declared = True

        others = [
            candidate for candidate in self._simpleCandidates(scores)
            if candidate.columnNames != self.declaredKeyColumns
        ]
        return [declared] + sorted(others, key=lambda c: c.confidence, reverse=True)

    def _knownKeyCandidates(self, scores):
        """Restricts the search to `knownKeyColumns` (always a single column
        in practice -- `LogicalTableEngine` only ever extracts on one
        determinant) instead of every column of the table -- but still
        *verifies* it (`isSimpleKeyCandidate`) rather than assuming it.

        The determinant is *expected* to be unique and non-null on an
        extracted table (`SELECT DISTINCT ... WHERE determinant IS NOT
        NULL`), but that only actually holds if every dependent column took
        exactly one value -- including null-vs-non-null -- per determinant
        value: `FunctionalGroupEngine.testFunctionalDependency` only compares
        non-null pairs, so a dependent that's null for some rows and set for
        others sharing the same determinant can still pass that test, and
        the `SELECT DISTINCT` then produces more than one row for that
        determinant value once the dependents are included. When that
        happens, no candidate is returned here -- the same conclusion the
        generic statistical search would have reached -- rather than
        asserting a key that doesn't actually hold on the real data."""
        columnName = self.knownKeyColumns[0]
        if not self.profiler.isSimpleKeyCandidate(columnName):
            return []

        profiling = self.profiler.getAggProfiling(columnName)
        return [
            self._buildCandidate(
                columnNames=self.knownKeyColumns,
                totalCount=profiling.TotalCount,
                nullCount=profiling.NullCount,
                distinctCount=profiling.DistinctCount,
                scores=scores,
                reason="simple_key: cle de construction (determinant de l'extraction)",
            )
        ]

    def _simpleCandidates(self, scores):
        """A column is a simple-key candidate if it has no null and its
        distinct count equals its row count (already computed once by
        `getAggProfiling`, and cached — no extra SQL here)."""
        candidates = []

        for columnName in self._keyableColumns():
            profiling = self.profiler.getAggProfiling(columnName)

            if profiling.TotalCount == 0:
                continue
            if profiling.NullCount != 0:
                continue
            if profiling.DistinctCount != profiling.TotalCount:
                continue

            candidates.append(
                self._buildCandidate(
                    columnNames=(columnName,),
                    totalCount=profiling.TotalCount,
                    nullCount=profiling.NullCount,
                    distinctCount=profiling.DistinctCount,
                    scores=scores,
                    reason="simple_key: colonne unique et sans valeur nulle",
                )
            )

        return candidates

    def _compositeCandidates(self, scores, maxSize=MAX_COMPOSITE_SIZE):
        """Try every column combination of increasing size (2, then 3, ...)
        and keep the smallest size that yields at least one unique tuple.
        Each combination is a single `testColumnsUnique` SQL query (a
        `COUNT(DISTINCT (...))`), not a full-table Python scan — this used
        to be the main hot path before the DuckDb migration.

        `keyableColumns` excludes any column with at least one null value
        -- a composite key requires zero nulls across ALL its columns, on
        EVERY row, so a column that already has a null on its own can
        never be part of a valid one. This is free: `NullCount` was
        already computed and cached by `getAggProfiling` during
        `_simpleCandidates`'s own pass just before this runs, so the
        filter costs no extra SQL -- it only shrinks the combinations
        `testColumnsUnique` actually has to test, which matters more as
        `maxSize` grows (`MAX_COMPOSITE_SIZE` can go well past 3)."""
        keyableColumns = [
            columnName for columnName in self._keyableColumns()
            if self.profiler.getAggProfiling(columnName).NullCount == 0
        ]

        for size in range(2, maxSize + 1):
            candidatesAtSize = []

            for columnNames in combinations(keyableColumns, size):
                totalCount, nullCount, distinctCount = self.profiler.testColumnsUnique(columnNames)

                if totalCount == 0 or nullCount != 0 or distinctCount != totalCount:
                    continue

                candidatesAtSize.append(
                    self._buildCandidate(
                        columnNames=columnNames,
                        totalCount=totalCount,
                        nullCount=nullCount,
                        distinctCount=distinctCount,
                        scores=scores,
                        reason=f"composite_key: {size} colonnes nécessaires pour l'unicité",
                    )
                )

            if candidatesAtSize:
                return candidatesAtSize

        return []

    def _keyableColumns(self):
        """All columns except continuous numerics (floats/decimals) — see
        `aggProfiling.isContinuousNumeric`."""
        return [columnName for columnName in self.profiler.columns if not self.profiler.isContinuousNumeric(columnName)]

    def _buildCandidate(self, columnNames, totalCount, nullCount, distinctCount, scores, reason):
        """confidence = 0.7 * uniquenessRatio + 0.3 * mean(identifiabilityScore)
        — these weights are local to key confidence and distinct from the
        0.5/0.3/0.2 weights used inside the identifiability score itself."""
        uniquenessRatio = distinctCount / totalCount if totalCount else 0.0
        meanIdentifiabilityScore = sum(
            scores[columnName].score for columnName in columnNames
        ) / len(columnNames)
        confidence = (
            UNIQUENESS_WEIGHT * uniquenessRatio
            + IDENTIFIABILITY_WEIGHT * meanIdentifiabilityScore
        )

        return primaryKeyCandidate(
            columnNames=columnNames,
            isComposite=len(columnNames) > 1,
            totalCount=totalCount,
            nullCount=nullCount,
            distinctCount=distinctCount,
            uniquenessRatio=uniquenessRatio,
            identifiabilityScore=meanIdentifiabilityScore,
            confidence=confidence,
            reason=reason,
        )