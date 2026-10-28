"""Fourth step: checks the hypothesis the whole approach rests on -- once
the row identifiers are set aside, the fact's dimension keys (linked or
extracted) together identify a row.

A null key is the dimension's "unknown" member (Kimball), not a defect of the
grain: `(client=NULL, date=d)` is one tuple like any other, and two such rows
on the same day ARE a duplicate. The rows carrying an unknown key are counted
apart, as information.

A transaction fact can legitimately break uniqueness (two identical sales on
the same day, same customer, same product): the verdict says so with counts
instead of assuming it, and looks for the degenerate column (an order number,
a ticket reference) that would make the grain unique.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kawakiriv3.FactAnalysis.columnAssessment import DEGENERATE, DESCRIPTIVE, GRAIN_ROLES
from kawakiriv3.Sources.sourceConnector import quoteIdentifier


@dataclass
class GrainVerdict:
    """totalRows/distinctTuples: counts on the rewritten fact, a null key
    counting as the "unknown" member.
    unknownKeyRows: rows with at least one unknown (null) key -- information,
        never a reason for the grain not to be unique.
    completingColumns: degenerate columns that, added to the grain, make it
        unique -- only searched when the grain alone isn't."""

    grainColumns: list
    totalRows: int
    distinctTuples: int
    unknownKeyRows: int = 0
    completingColumns: list = field(default_factory=list)

    @property
    def duplicateRows(self):
        return self.totalRows - self.distinctTuples

    @property
    def isUnique(self):
        return bool(self.grainColumns) and self.duplicateRows == 0


class GrainVerifier:
    def __init__(self, db, starFactTable, assessments):
        self.db = db
        self.starFactTable = starFactTable
        self.assessments = assessments

    def verify(self):
        grainColumns = [a.columnName for a in self.assessments if a.role in GRAIN_ROLES]
        if not grainColumns:
            totalRows = self.db.getRowCount(self.starFactTable)
            return GrainVerdict(grainColumns=[], totalRows=totalRows, distinctTuples=0)

        totalRows, distinctTuples, unknownKeyRows = self._counts(grainColumns)
        verdict = GrainVerdict(grainColumns, totalRows, distinctTuples, unknownKeyRows)
        if not verdict.isUnique:
            verdict.completingColumns = [
                column for column in self._degenerateColumns()
                if self._counts(grainColumns + [column])[1] == totalRows
            ]
        return verdict

    def _degenerateColumns(self):
        """Descriptive text (a reference, a ticket number) first, row
        identifiers last -- a row identifier always makes the grain unique,
        so it is the least informative suggestion."""
        degenerate = [a for a in self.assessments if a.role == DEGENERATE]
        return [a.columnName for a in sorted(degenerate, key=lambda a: a.initialRole != DESCRIPTIVE)]

    def _counts(self, columns):
        """`(totalRows, distinctTuples, unknownKeyRows)` -- a `ROW(...)`
        struct is never null itself, so `COUNT(DISTINCT ROW(...))` counts a
        null field as a value. (A bare `(a)` is not a struct: with a single
        grain column it would silently drop the nulls.)"""
        cols = [quoteIdentifier(c) for c in columns]
        nullCond = " OR ".join(f"{c} IS NULL" for c in cols)
        return self.db.conn.execute(
            f"SELECT COUNT(*), COUNT(DISTINCT ROW({', '.join(cols)})), COUNT(*) FILTER (WHERE {nullCond}) "
            f"FROM {quoteIdentifier(self.starFactTable)}"
        ).fetchone()
