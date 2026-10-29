"""Builds the unified dimensions: for each `UnifiedDimensionSpec`, the union
of the stars' dimension tables (every column kept, each with its origin
label, each source's key under one target), then the dedup of that union on
its match columns, transitively (`DerivedTableBuilder` does both).

The unified table says which sources an identity is in (`present_<source>`)
and keeps each source's key (`<key>_<source>`); its attributes come from
the priority record (most recent on the priority column, then the priority
source). `<name>__correspondance` relates every source row, through its
origin label and its key, to its unified id: what the facts' keys are
remapped through (see `keyRemapper.py`).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.DerivedTables.derivedTableBuilder import DerivedTableBuilder
from kawakiriv3.DerivedTables.derivedTableOutcome import BUILT, SKIPPED
from kawakiriv3.StarUnification.starCatalog import inferredTypeOf


@dataclass
class UnifiedDimension:
    """`sources`: `[{"dimension", "label", "keyColumn", "rowCount"}]`.
    `keyTarget`: the column of the correspondence holding each source's
    key, next to `labelColumn`. `columns`: `[(column, inferredType)]` of the
    built table. The rest: see `DerivedTableOutcome` for a dedup."""

    name: str
    status: str
    reason: Optional[str] = None
    justification: Optional[str] = None
    idColumn: Optional[str] = None
    unionTable: Optional[str] = None
    mappingTable: Optional[str] = None
    labelColumn: Optional[str] = None
    keyTarget: Optional[str] = None
    sources: list = field(default_factory=list)
    matchColumns: list = field(default_factory=list)
    sourceRowCount: Optional[int] = None
    resultRowCount: Optional[int] = None
    largestClusterSize: Optional[int] = None
    mergedByColumn: dict = field(default_factory=dict)
    aggressiveMergeWarning: bool = False
    survivorship: Optional[str] = None
    sourceColumns: dict = field(default_factory=dict)
    typeWarnings: tuple = ()
    columns: list = field(default_factory=list)

    def labelOf(self, dimension):
        return next((s["label"] for s in self.sources if s["dimension"] == dimension), None)


class DimensionUnifier:
    def __init__(self, db, catalog):
        self.db = db
        self.catalog = catalog

    def unify(self, spec, errors=None):
        """One `UnifiedDimension`, `SKIPPED` with its reason when `errors`
        (from `UnificationSpec.errorsAgainst`) or the builder refuse it."""
        result = UnifiedDimension(
            name=spec.name, status=SKIPPED, justification=spec.justification, idColumn=spec.unifiedIdColumn,
            labelColumn=spec.labelColumn, matchColumns=list(spec.matchColumns),
            sources=[
                {"dimension": s.dimension, "label": s.label,
                 "keyColumn": self.catalog.dimensions[s.dimension].keyColumn if s.dimension in self.catalog.dimensions else None,
                 "rowCount": self.catalog.dimensions[s.dimension].rowCount if s.dimension in self.catalog.dimensions else None}
                for s in spec.sources
            ],
        )
        if errors:
            result.reason = "spec invalide : " + " ; ".join(errors)
            return result
        union, dedup = spec.toDerivedSpecs(self.catalog)
        outcomes = DerivedTableBuilder(self.db).buildAll([union, dedup])
        unionOutcome, dedupOutcome = outcomes[union.name], outcomes.get(dedup.name)
        if unionOutcome.status != BUILT:
            result.reason = f"union des dimensions : {unionOutcome.reason}"
            return result
        result.unionTable, result.typeWarnings = union.name, unionOutcome.typeWarnings
        if dedupOutcome is None or dedupOutcome.status != BUILT:
            result.reason = f"dedoublonnage : {dedupOutcome.reason if dedupOutcome else 'non construit'}"
            return result
        result.status = BUILT
        result.keyTarget = spec.sourceKeyTarget(self.catalog)
        for name in ("mappingTable", "sourceRowCount", "resultRowCount", "largestClusterSize", "mergedByColumn",
                     "aggressiveMergeWarning", "survivorship", "sourceColumns"):
            setattr(result, name, getattr(dedupOutcome, name))
        result.columns = [(c, inferredTypeOf(t)) for c, t in self.db.getColumns(spec.name, dataType=True)]
        return result
