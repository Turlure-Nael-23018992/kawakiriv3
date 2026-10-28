from __future__ import annotations

import json
import os
from dataclasses import asdict
from datetime import date, datetime, timedelta

from kawakiriv3.Identifiability.identifiabilityScore import IdentifiabilityScore
from kawakiriv3.Identifiability.scoring import (
    COMPLETENESS_WEIGHT,
    ENTROPY_WEIGHT,
    IDENTIFIABILITY_DEFAULT_DIAGNOSTIC,
    IDENTIFIABILITY_THRESHOLDS,
    UNIQUENESS_WEIGHT,
)
from kawakiriv3.Profiling.aggProfiling import aggProfiling
from kawakiriv3.Profiling.columnProfiling import ColumnProfiling


class Identifiability:
    """Scores how identifying a column is, from uniqueness + entropy +
    completeness, on top of an `aggProfiling`. Kept separate from
    `aggProfiling` itself because "how identifying is this column" is a
    downstream interpretation of raw profiling stats, not a stat in its
    own right — `FunctionalGroupEngine` and `PrimaryKeyEngine` both need
    this ranking, but neither needs (or should own) the scoring formula.
    """

    def __init__(self, profiler: aggProfiling):
        self.profiler = profiler

    def computeColumnScore(self) -> IdentifiabilityScore:
        """Scores the single column `profiler` was built for, reusing its
        already-computed `profiling` (no re-aggregation)."""
        return self.score(self.profiler.columnName, self.profiler.profiling)

    def computeTableScores(self) -> dict[str, IdentifiabilityScore]:
        """Score every column of the table. `getAggProfiling` is cached per
        column on the profiler, so calling this alongside `PrimaryKeyEngine`
        on the same table does not recompute the SQL aggregates twice."""
        return {
            columnName: self.score(columnName, self.profiler.getAggProfiling(columnName))
            for columnName in self.profiler.columns
        }

    def score(self, columnName, profiling: ColumnProfiling) -> IdentifiabilityScore:
        """score = 0.5 * uniquenessRatio + 0.3 * entropyRatio + 0.2 * completeness
        (weights from `Identifiability/scoring.py`)."""
        uniqueness_ratio = (profiling.DistinctCount / profiling.TotalCount) if profiling.TotalCount else 0.0
        completeness = 1 - profiling.Sparsity
        entropy_ratio = profiling.NormalizedEntropy

        score = (
            UNIQUENESS_WEIGHT * uniqueness_ratio
            + ENTROPY_WEIGHT * entropy_ratio
            + COMPLETENESS_WEIGHT * completeness
        )

        return IdentifiabilityScore(
            columnName=columnName,
            uniquenessRatio=uniqueness_ratio,
            completeness=completeness,
            entropyRatio=entropy_ratio,
            score=score,
            diagnostic=self.diagnose(score),
        )

    def diagnose(self, score: float) -> str:
        """Maps a numeric score to its human-readable bucket by walking
        `IDENTIFIABILITY_THRESHOLDS` in descending order and taking the
        first threshold the score clears; falls back to
        `IDENTIFIABILITY_DEFAULT_DIAGNOSTIC` below the lowest one."""
        for threshold, diagnostic in IDENTIFIABILITY_THRESHOLDS:
            if score >= threshold:
                return diagnostic

        return IDENTIFIABILITY_DEFAULT_DIAGNOSTIC

    def export(self, outputDir="output") -> IdentifiabilityScore:
        """Writes both the raw profiling stats and the computed
        `IdentifiabilityScore` for this column to
        `{outputDir}/{tableName}/{columnName}_{profiling,identifiability}.json`,
        creating the table subdirectory if needed, and returns the score."""
        columnScore = self.computeColumnScore()

        tableDir = os.path.join(outputDir, self.profiler.tableName)
        os.makedirs(tableDir, exist_ok=True)

        profilingPath = os.path.join(tableDir, f"{self.profiler.columnName}_profiling.json")
        identifiabilityPath = os.path.join(tableDir, f"{self.profiler.columnName}_identifiability.json")

        self._writeJson(profilingPath, asdict(self.profiler.profiling))
        self._writeJson(identifiabilityPath, asdict(columnScore))

        return columnScore

    def _writeJson(self, path, data):
        """Serializes `data` to `path` as pretty-printed UTF-8 JSON."""
        with open(path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2, ensure_ascii=False, default=self._jsonDefault)

    def _jsonDefault(self, value):
        """`json.dump` fallback for values it can't serialize natively
        (dates/datetimes/timedeltas from profiling stats, or anything
        else) — everything is coerced to its `str()` form."""
        if isinstance(value, (datetime, date, timedelta)):
            return str(value)
        return str(value)
