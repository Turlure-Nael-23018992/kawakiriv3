"""Data structure for one entry of a column's top-N most-frequent-values ranking."""

from dataclasses import dataclass
from typing import Any


@dataclass
class TopValue:
    """One entry of a column's most-frequent-values ranking (see
    `aggProfiling._topNValues`): the raw `value`, how many rows carry it
    (`count`), and its share of the column's non-null rows (`percentage`)."""

    value: Any
    count: int
    percentage: float
