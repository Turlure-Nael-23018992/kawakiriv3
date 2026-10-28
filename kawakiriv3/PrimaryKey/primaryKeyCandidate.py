"""Data structure describing a single candidate primary key (simple or
composite) produced by PrimaryKeyEngine."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class primaryKeyCandidate:
    """One candidate primary key for a table, produced by `PrimaryKeyEngine`
    (simple single-column or composite multi-column). Carries both the raw
    uniqueness evidence and the derived confidence score so a caller can
    either trust `confidence`/`reason` outright or re-inspect the counts
    behind it.

    columnNames: the column(s) forming the candidate key, in the order they
        were tested (a single-element tuple for a simple key).
    isComposite: True when `len(columnNames) > 1`.
    totalCount: row count of the table at candidate-evaluation time.
    nullCount: rows where at least one of `columnNames` is null — a real
        candidate always has this at 0 (null-containing tuples are excluded
        before a candidate is built).
    distinctCount: distinct `columnNames` tuples — equal to `totalCount` for
        a genuine candidate.
    uniquenessRatio: `distinctCount / totalCount`.
    identifiabilityScore: mean `Identifiability` score across `columnNames`,
        i.e. how "identifier-like" the column(s) look independent of
        uniqueness (naming, cardinality shape, etc.).
    confidence: `0.7 * uniquenessRatio + 0.3 * identifiabilityScore` — see
        `PrimaryKeyEngine._buildCandidate` for the weighting rationale.
    reason: short human-readable explanation of why this tuple qualified
        (simple vs. composite, and at what composite size).
    declared: True when the source system declared this key
        (`isPrimaryKey`) -- imposed rather than found, kept even when the
        counts above contradict it (see `contradictedByData`).
    """

    columnNames: tuple[str, ...]
    isComposite: bool
    totalCount: int
    nullCount: int
    distinctCount: int
    uniquenessRatio: float
    identifiabilityScore: float
    confidence: float
    reason: str
    declared: bool = False

    @property
    def contradictedByData(self):
        """True when the real data breaks this key: a null, or fewer
        distinct tuples than rows. Only ever possible on a declared key --
        a found one is built from these exact counts."""
        return self.nullCount != 0 or self.distinctCount != self.totalCount

    def dataQualityIssue(self):
        """Human-readable description of how the data contradicts this
        key, or None."""
        if not self.contradictedByData:
            return None
        issues = []
        duplicates = self.totalCount - self.nullCount - self.distinctCount
        if duplicates > 0:
            issues.append(f"{duplicates} doublon(s)")
        if self.nullCount:
            issues.append(f"{self.nullCount} ligne(s) avec une valeur nulle")
        return (
            f"cle primaire declaree {self.columnNames} contredite par les donnees : "
            f"{', '.join(issues)} sur {self.totalCount} lignes"
        )