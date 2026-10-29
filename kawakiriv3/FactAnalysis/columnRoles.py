"""Column roles imposed on the classifier from outside the statistics: the
caller's own declarations (job file) and the LLM's column hint
(`classify_fact_columns` in kawakiri-langgraph).

Precedence, per column: caller > LLM > technical name pattern > statistics.
The statistics still keep the last word on uniqueness: a column unique on
every row stays a row identifier whoever calls it a key.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kawakiriv3.FactAnalysis.columnAssessment import NON_ADDITIVE, SOURCE_CALLER, SOURCE_LLM

KEY = "key"
MEASURE = "measure"
EXCLUDED = "excluded"
DEGENERATE = "degenerate"  # stays on the fact, outside the grain: never linked, never a dimension


@dataclass
class ColumnRoles:
    """`{columnName: (role, source)}` with role in KEY/MEASURE/EXCLUDED/DEGENERATE.
    `additivity`: `{measure: NON_ADDITIVE}` for the measures never to be
    summed (a unit price, a rate); any other measure is additive."""

    byColumn: dict = field(default_factory=dict)
    reasoning: str = None
    additivity: dict = field(default_factory=dict)

    def roleOf(self, columnName):
        """`(role, source)` or `(None, None)`."""
        return self.byColumn.get(columnName, (None, None))

    @classmethod
    def fromLists(
        cls, keys=(), measures=(), excluded=(), source=SOURCE_CALLER, reasoning=None, degenerate=(), nonAdditive=(),
    ):
        """`nonAdditive`: measures never to be summed (measures even when
        not also listed in `measures`)."""
        byColumn = {}
        for role, columns in (
            (KEY, keys), (MEASURE, measures), (MEASURE, nonAdditive), (EXCLUDED, excluded), (DEGENERATE, degenerate),
        ):
            for column in columns or ():
                byColumn[column] = (role, source)
        additivity = {column: NON_ADDITIVE for column in nonAdditive or () if byColumn[column][0] == MEASURE}
        return cls(byColumn=byColumn, reasoning=reasoning, additivity=additivity)

    @classmethod
    def fromLlm(cls, roles):
        """From one `columnRolesByTable` entry of the hint response."""
        roles = roles or {}
        return cls.fromLists(
            roles.get("keyColumns"), roles.get("measureColumns"), roles.get("technicalColumns"),
            source=SOURCE_LLM, reasoning=roles.get("reasoning"), nonAdditive=roles.get("nonAdditiveColumns"),
        )

    def overriddenBy(self, other):
        """A copy where every column `other` names takes `other`'s role."""
        additivity = {c: a for c, a in self.additivity.items() if c not in other.byColumn}
        return ColumnRoles(
            byColumn={**self.byColumn, **other.byColumn}, reasoning=self.reasoning,
            additivity={**additivity, **other.additivity},
        )
