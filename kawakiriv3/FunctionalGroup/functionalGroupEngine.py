from kawakiriv3.FunctionalGroup.functionalGroupCandidate import FunctionalGroupCandidate

DETERMINANT_MAX_NULL_RATE = 50.0
DETERMINANT_MAX_UNIQUENESS_RATIO = 0.95


class FunctionalGroupEngine:
    """Detects functional dependencies (determinant -> dependents) within a
    single table: if every value of column A always maps to the same value
    of column B (A -> B), B carries no information beyond A and the two
    columns belong to the same "functional group" rather than being
    independent — useful later to decide which columns together form one
    logical dimension instead of being treated as isolated columns.
    """

    def __init__(self, profiler, identifiability):
        """`profiler`: the table's profiling engine, used both for
        `testFunctionalDependencies` (the actual A -> B check) and for column
        metadata (`getAggProfiling`, `isContinuousNumeric`). `identifiability`:
        supplies the score used to order determinant candidates."""
        self.profiler = profiler
        self.identifiability = identifiability

    def inferFunctionalGroups(self):
        """Greedy grouping: try determinant candidates from the strongest
        identifiability score down, assign every column it functionally
        determines to its group, and remove all of them from the race so no
        column is claimed by two groups. Columns that never determine
        anything (and are never determined) end up as their own singleton
        group, so every column of the table appears in exactly one group."""
        scores = self.identifiability.computeTableScores()
        determinants = self._candidateDeterminants(scores)

        assigned = set()
        groups = []

        for determinantColumn in determinants:
            if determinantColumn in assigned:
                continue

            candidateDependents = [
                columnName
                for columnName in self.profiler.columns
                if columnName != determinantColumn
                and columnName not in assigned
                and self._isEligibleDependent(columnName)
            ]
            dependents = self.profiler.testFunctionalDependencies(determinantColumn, candidateDependents)

            if not dependents:
                continue

            assigned.add(determinantColumn)
            assigned.update(dependents)
            groups.append(
                FunctionalGroupCandidate(
                    determinantColumn=determinantColumn,
                    dependentColumns=tuple(dependents),
                    isSingleton=False,
                )
            )

        for columnName in self.profiler.columns:
            if columnName in assigned:
                continue

            assigned.add(columnName)
            groups.append(
                FunctionalGroupCandidate(
                    determinantColumn=columnName,
                    dependentColumns=(),
                    isSingleton=True,
                )
            )

        return groups

    def _isEligibleDependent(self, columnName):
        """A constant column (`DistinctCount <= 1`) is excluded from ever
        being claimed as a dependent: `testFunctionalDependencies`' SQL test
        (`MIN(dependent) <> MAX(dependent)` per determinant value) can never
        find a violation against a column that only ever holds one value, so *any* determinant
        would trivially "determine" it — a false positive with no real
        business meaning, not an actual relationship. A constant column ends
        up its own singleton group instead, which is the honest outcome:
        it carries no differentiating information regardless of what it's
        grouped with.

        A column that is itself a valid simple-key candidate of this table
        (`isSimpleKeyCandidate`) is excluded the same way: a determinant
        with a uniqueness ratio just under `DETERMINANT_MAX_UNIQUENESS_RATIO`
        (e.g. mostly-unique but with a handful of nulls or coincidental
        repeats) can still pass the functional-dependency test against the
        table's real identifier by small-sample coincidence -- extracting it
        would strip the table's own grain key out of its base table for no
        real normalization gain, leaving the base table with no candidate
        key at all. The column stays its own singleton group instead, so it
        remains behind on the base table where `PrimaryKeyEngine` can find
        it."""
        profiling = self.profiler.getAggProfiling(columnName)
        if profiling.DistinctCount <= 1:
            return False
        return not self.profiler.isSimpleKeyCandidate(columnName)

    def _candidateDeterminants(self, scores):
        """A column can act as a determinant if it repeats enough to group
        something (DistinctCount > 1, uniquenessRatio below
        DETERMINANT_MAX_UNIQUENESS_RATIO — a primary-key-like column would
        trivially "determine" every other column with exactly one row per
        value, which proves nothing), is mostly non-null, and isn't a
        continuous numeric measure (reusing `isContinuousNumeric`, the same
        exclusion `PrimaryKeyEngine` applies to key candidates). Ordered by
        descending identifiability score, so the greedy assignment favors
        the most informative determinant when several would work."""
        candidates = []

        for columnName in self.profiler.columns:
            if self.profiler.isContinuousNumeric(columnName):
                continue

            profiling = self.profiler.getAggProfiling(columnName)
            if profiling.TotalCount == 0 or profiling.DistinctCount <= 1:
                continue
            if profiling.NullRate > DETERMINANT_MAX_NULL_RATE:
                continue
            if (profiling.DistinctCount / profiling.TotalCount) >= DETERMINANT_MAX_UNIQUENESS_RATIO:
                continue

            candidates.append(columnName)

        return sorted(candidates, key=lambda columnName: scores[columnName].score, reverse=True)
