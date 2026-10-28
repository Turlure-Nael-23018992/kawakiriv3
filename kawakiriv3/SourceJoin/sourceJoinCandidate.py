"""Data model for a single statistically-inferred FK-style join candidate."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class SourceJoinCandidate:
    """One inferred FK-style relationship, as produced by `SourceJoinEngine`:
    `sourceColumn` on `sourceTable` matches values found in `targetColumn`,
    a candidate primary key on `targetTable`. Purely a statistical finding —
    it says nothing yet about whether the relationship is structurally sound
    (that's `StructureValidator`) or semantically believable (that's
    `SemanticHomogeneityValidator`); this dataclass just carries the raw
    evidence downstream.

    sourceTable / sourceColumn: the "many" side being tested.
    targetTable / targetColumn: the primary-key candidate being matched
        against.
    joinSuccessRatio: matchedRows / sourceNonNullRows — the fraction of
        non-null source values found in the target column.
    matchedRows: count of source rows whose value exists in the target.
    sourceNonNullRows: count of non-null source rows (the ratio's
        denominator) — excludes nulls so a sparsely-populated column isn't
        unfairly penalized for the rows it doesn't even have an opinion on.
    nameHint: True if the source/target column or table names share the
        same normalized "business concept" (see `_concept` in
        `sourceJoinEngine.py`). This is a *signal* used for sorting and for
        `SemanticHomogeneityValidator`'s warning, never a hard filter — a
        real relationship can have unrelated column names (e.g.
        `event_log.user_id -> dim_customers.customer_id`).
    flattenedFromKey: set only by `StarFlattener` on the direct edge it
        synthesizes when copying a linking column onto the fact table --
        the `relationshipEvidence.relationshipKey(...)` of the original,
        already-evidenced indirect edge it replaces (e.g. flattening
        `intermediate.col -> outer.col` onto `fact.col -> outer.col` sets
        this to the former). `RelationshipEvidenceValidator` falls back to
        looking up evidence under this key when the new edge's own key has
        none -- the flattened join is exactly as trustworthy as the edge it
        was copied from, it just isn't itself a `SourceJoinEngine` finding.
        `None` for every edge that isn't a flattening result.
    declared: True for a foreign key the source system declared
        (`isForeignKey`): added whatever its `joinSuccessRatio`, which is
        still measured, never used to decide.
    """

    sourceTable: str
    sourceColumn: str
    targetTable: str
    targetColumn: str
    joinSuccessRatio: float
    matchedRows: int
    sourceNonNullRows: int
    nameHint: bool
    flattenedFromKey: Optional[str] = None
    declared: bool = False
