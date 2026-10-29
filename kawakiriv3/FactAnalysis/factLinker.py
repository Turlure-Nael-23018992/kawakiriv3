"""Second step: links each linkable column of the fact table to an existing
table's key, testing ONLY the fact's own columns as join sources (never every
pair of tables, as v2 did -- the fact table is known).

Reuses `SourceJoinEngine` (value overlap >= 95% against simple key
candidates), restricted with `sourceColumnsByTable`/`targetTables`.

`linkVerdicts`: the decisions of the run's physical link graph on this
fact's columns (`FactAssembly/linkGraph.py`) -- a link it accepted is
linked even when the names don't corroborate it (JD Edwards' `RPAN8 ->
F0101.ABAN8`, accepted by the LLM or the caller), a link the LLM or the
caller rejected is never linked.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kawakiriv3.FactAnalysis.columnAssessment import FOREIGN_KEY
from kawakiriv3.SourceJoin.sourceJoinEngine import SourceJoinEngine


@dataclass
class ForeignKeyLink:
    """The link retained for one fact column.

    alternatives: every other target that also cleared the overlap
        threshold, as "table.column" -- the link is ambiguous when one of
        them is exactly as good (same name corroboration, same ratio).
    """

    sourceColumn: str
    targetTable: str
    targetColumn: str
    joinSuccessRatio: float
    nameHint: bool
    ambiguous: bool
    alternatives: list = field(default_factory=list)

    @property
    def target(self):
        return f"{self.targetTable}.{self.targetColumn}"


def _rank(candidate, targetConfidence):
    return (candidate.nameHint, round(candidate.joinSuccessRatio, 6), targetConfidence)


class FactLinker:
    """`schemaEngine`/`primaryKeyCandidatesByTable`: the run's source tables
    and their key candidates. `targetTables`: tables the fact may link to
    (default: every table, the fact itself included -- a self-referencing
    column like `facture_origine_id` can then link to the fact's own key;
    `SourceJoinEngine` never tests a column against itself, and a candidate
    still needs the same name hint or an explicit `linkVerdicts` approval as
    any other target, see `link()`)."""

    def __init__(
        self, schemaEngine, primaryKeyCandidatesByTable, factTable, assessments, targetTables=None, linkVerdicts=None,
    ):
        """`linkVerdicts`: `{column: {"accepted": {(table, column): decidedBy},
        "vetoed": {(table, column), ...}}}`."""
        self.linkVerdicts = linkVerdicts or {}
        self.schemaEngine = schemaEngine
        self.primaryKeyCandidatesByTable = primaryKeyCandidatesByTable
        self.factTable = factTable
        self.assessments = assessments
        self.targetTables = list(targetTables if targetTables is not None else schemaEngine.tableNames)

    def link(self):
        """Returns `[ForeignKeyLink, ...]` and sets `role = FOREIGN_KEY` /
        `linkedTo` on each linked assessment (in place). A column whose
        assessment `requiresNameHint` is only linked through a candidate
        whose names corroborate the relation."""
        linkable = [a for a in self.assessments if a.isLinkable]
        if not linkable or not self.targetTables:
            return []

        engine = SourceJoinEngine(
            self.schemaEngine, self.primaryKeyCandidatesByTable,
            sourceColumnsByTable={self.factTable: [a.columnName for a in linkable]},
            targetTables=self.targetTables,
        )
        candidatesByColumn = {}
        for candidate in engine.inferSourceJoins():
            if candidate.sourceTable == candidate.targetTable:
                # Same safeguard as `LinkGraph.build`: a real self-reference
                # is nullable (most rows have nothing to point at); a
                # non-nullable column matching the fact's own (typically
                # dense, sequential) key is coincidence, not a relation --
                # and this engine, unlike LinkGraph's, has no deterministic
                # stage of its own to catch it.
                profiling = self.schemaEngine.profilers[candidate.sourceTable].getAggProfiling(candidate.sourceColumn)
                if profiling.NullCount == 0:
                    continue
            candidatesByColumn.setdefault(candidate.sourceColumn, []).append(candidate)

        links = []
        for assessment in linkable:
            verdicts = self.linkVerdicts.get(assessment.columnName) or {}
            approved = verdicts.get("accepted") or {}
            vetoed = verdicts.get("vetoed") or set()
            candidates = [
                c for c in candidatesByColumn.get(assessment.columnName) or []
                if (c.targetTable, c.targetColumn) not in vetoed
            ]
            if assessment.requiresNameHint:
                candidates = [c for c in candidates if c.nameHint or (c.targetTable, c.targetColumn) in approved]
            if approved:
                # The graph settled this column: only its accepted targets compete.
                candidates = [c for c in candidates if (c.targetTable, c.targetColumn) in approved] or candidates
            if not candidates:
                continue

            ranked = sorted(candidates, key=lambda c: _rank(c, self._targetConfidence(c)), reverse=True)
            best = ranked[0]
            bestRank = _rank(best, self._targetConfidence(best))
            ambiguous = any(_rank(c, self._targetConfidence(c))[:2] == bestRank[:2] for c in ranked[1:])

            links.append(ForeignKeyLink(
                sourceColumn=assessment.columnName,
                targetTable=best.targetTable,
                targetColumn=best.targetColumn,
                joinSuccessRatio=best.joinSuccessRatio,
                nameHint=best.nameHint,
                ambiguous=ambiguous,
                alternatives=[f"{c.targetTable}.{c.targetColumn}" for c in ranked[1:]],
            ))
            assessment.role = FOREIGN_KEY
            assessment.linkedTo = f"{best.targetTable}.{best.targetColumn}"
            decidedBy = approved.get((best.targetTable, best.targetColumn))
            assessment.reason = (
                f"{best.joinSuccessRatio:.0%} des valeurs retrouvees dans {best.targetTable}.{best.targetColumn}"
                + (" (noms concordants)" if best.nameHint else " (sans concordance de nom)")
                + (f", lien accepte par : {decidedBy}" if decidedBy and not best.nameHint else "")
            )

        return links

    def _targetConfidence(self, candidate):
        for keyCandidate in self.primaryKeyCandidatesByTable.get(candidate.targetTable) or []:
            if keyCandidate.columnNames == (candidate.targetColumn,):
                return keyCandidate.confidence
        return 0.0
