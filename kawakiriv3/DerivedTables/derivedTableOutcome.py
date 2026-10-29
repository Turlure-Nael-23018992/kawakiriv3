"""What building one `DerivedTableSpec` gave: row counts, how much a dedup
merged, and anything worth a human look -- an audit trail, never a gate.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

BUILT = "built"
SKIPPED = "skipped"


@dataclass
class DerivedTableOutcome:
    """`reason`: why a `SKIPPED` spec wasn't built.

    A built "union": `sourceRowCounts` (`{sourceTable: rows}`), `totalRows`,
    and `typeWarnings`, one per target column fed by source columns of
    different type families (a numeric id in one source, a text one in
    another) -- never a refusal.

    A built "dedup": `sourceRowCount` -> `resultRowCount` identities,
    `largestClusterSize`, `mergedByColumn` (`{matchColumn: merges}`: the
    drop in identities each column caused, an earlier column's merges never
    counted twice), `aggressiveMergeWarning` when one identity absorbed an
    unusually large share of the rows (see
    `AGGRESSIVE_MERGE_CLUSTER_RATIO_THRESHOLD`), `mappingTable`, every
    source row with its unified id, `survivorship`, the priority order
    an identity's attributes were taken in, in French, and `sourceColumns`,
    `{source column: [its columns per source]}` -- the origin label split
    into `present_<source>` flags, a source id into `<id>_<source>`."""

    name: str
    kind: str
    status: str
    reason: Optional[str] = None
    justification: Optional[str] = None

    sourceRowCounts: dict = field(default_factory=dict)
    totalRows: Optional[int] = None
    typeWarnings: tuple = ()

    sourceTable: Optional[str] = None
    sourceRowCount: Optional[int] = None
    resultRowCount: Optional[int] = None
    largestClusterSize: Optional[int] = None
    mergedByColumn: dict = field(default_factory=dict)
    aggressiveMergeWarning: bool = False
    idColumn: Optional[str] = None
    mappingTable: Optional[str] = None
    survivorship: Optional[str] = None
    sourceColumns: dict = field(default_factory=dict)

    @property
    def mergedRowCount(self):
        if self.sourceRowCount is None or self.resultRowCount is None:
            return None
        return self.sourceRowCount - self.resultRowCount

    def summary(self):
        if self.status == SKIPPED:
            return f"ignoree : {self.reason}"
        if self.kind == "union":
            warning = f" -- ATTENTION types heterogenes : {list(self.typeWarnings)}" if self.typeWarnings else ""
            return f"construite (union) : {self.totalRows} lignes depuis {self.sourceRowCounts}{warning}"
        warning = " -- ATTENTION fusion potentiellement trop agressive" if self.aggressiveMergeWarning else ""
        return (
            f"construite (dedup) : {self.sourceRowCount} -> {self.resultRowCount} lignes "
            f"({self.mergedRowCount} fusionnee(s), plus grand cluster={self.largestClusterSize}, "
            f"par colonne={self.mergedByColumn}, attributs : {self.survivorship}){warning}"
        )
