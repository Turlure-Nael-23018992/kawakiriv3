from __future__ import annotations

from dataclasses import dataclass


@dataclass
class FunctionalGroupCandidate:
    """One functional dependency group detected within a table by
    `FunctionalGroupEngine`: either a real group (a determinant column that
    functionally determines one or more dependent columns) or a singleton
    (a column that determines nothing and is determined by nothing, grouped
    with itself so every column ends up in exactly one group).

    determinantColumn: the column whose value fixes the value of every
        column in `dependentColumns` (A -> B). For a singleton, this is
        just the lone column itself.
    dependentColumns: the columns functionally determined by
        `determinantColumn`. Empty for a singleton group.
    isSingleton: True when this group has no dependents — the column
        carries no redundant information worth extracting.
    """

    determinantColumn: str
    dependentColumns: tuple[str, ...]
    isSingleton: bool
