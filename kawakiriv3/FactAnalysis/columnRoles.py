"""Column roles imposed on the classifier from outside the statistics: the
caller's own declarations (job file) and the LLM's column hint
(`classify_fact_columns` in kawakiri-langgraph).

Precedence, per column: caller > LLM > technical name pattern > statistics.
The statistics still keep the last word on uniqueness: a column unique on
every row stays a row identifier whoever calls it a key.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kawakiriv3.FactAnalysis.columnAssessment import SOURCE_CALLER, SOURCE_LLM

KEY = "key"
MEASURE = "measure"
EXCLUDED = "excluded"


@dataclass
class ColumnRoles:
    """`{columnName: (role, source)}` with role in KEY/MEASURE/EXCLUDED."""

    byColumn: dict = field(default_factory=dict)
    reasoning: str = None

    def roleOf(self, columnName):
        """`(role, source)` or `(None, None)`."""
        return self.byColumn.get(columnName, (None, None))

    @classmethod
    def fromLists(cls, keys=(), measures=(), excluded=(), source=SOURCE_CALLER, reasoning=None):
        byColumn = {}
        for role, columns in ((KEY, keys), (MEASURE, measures), (EXCLUDED, excluded)):
            for column in columns or ():
                byColumn[column] = (role, source)
        return cls(byColumn=byColumn, reasoning=reasoning)

    @classmethod
    def fromLlm(cls, roles):
        """From one `columnRolesByTable` entry of the hint response."""
        roles = roles or {}
        return cls.fromLists(
            roles.get("keyColumns"), roles.get("measureColumns"), roles.get("technicalColumns"),
            source=SOURCE_LLM, reasoning=roles.get("reasoning"),
        )

    def overriddenBy(self, other):
        """A copy where every column `other` names takes `other`'s role."""
        return ColumnRoles(byColumn={**self.byColumn, **other.byColumn}, reasoning=self.reasoning)
