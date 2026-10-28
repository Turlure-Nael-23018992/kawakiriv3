"""Data structure holding the full profiling result for a single column, as
produced by aggProfiling."""

from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, List, Optional

from kawakiriv3.Profiling.topValue import TopValue


@dataclass
class ColumnProfiling:
    """Full profiling result for a single column, produced by `aggProfiling`.

    The `General` fields are always populated regardless of the column's
    inferred type; the `Numérique`/`Texte`/`Date` groups below are mutually
    exclusive and only one is filled in (via `dataclasses.replace`), matching
    `InferredDataType` — the others stay at their `None` default so a caller
    can check `field is None` rather than needing to branch on
    `InferredDataType` first. See `aggProfiling.getAggProfiling` for how each
    group is computed.
    """

    # Général
    TotalCount: int
    NullCount: int
    NullRate: float
    Sparsity: float
    DistinctCount: int
    DistinctRate: float
    Mode: Any
    ModeFrequency: float
    TopNValues: List[TopValue]
    DuplicateCount: int
    IsConstant: bool
    InferredDataType: str
    TypeConsistency: float
    MemorySize: int
    SampleValues: List[Any]
    ShannonEntropy: float
    NormalizedEntropy: float

    # Numérique
    Min: Optional[float] = None
    Max: Optional[float] = None
    Sum: Optional[float] = None
    Mean: Optional[float] = None
    Median: Optional[float] = None
    StandardDeviation: Optional[float] = None
    Variance: Optional[float] = None
    P1: Optional[float] = None
    P5: Optional[float] = None
    P25: Optional[float] = None
    P75: Optional[float] = None
    P95: Optional[float] = None
    P99: Optional[float] = None
    IQR: Optional[float] = None
    Skewness: Optional[float] = None
    Kurtosis: Optional[float] = None
    ZeroCount: Optional[int] = None
    NegativeCount: Optional[int] = None
    OutlierCount: Optional[int] = None

    # Texte
    MinLength: Optional[int] = None
    MaxLength: Optional[int] = None
    AvgLength: Optional[float] = None
    EmptyOrBlankCount: Optional[int] = None
    LeadingTrailingWhitespaceCount: Optional[int] = None
    CaseInconsistencyCount: Optional[int] = None
    NonAsciiCharCount: Optional[int] = None
    PatternMatchRate: Optional[float] = None

    # Date
    MinDate: Optional[datetime] = None
    MaxDate: Optional[datetime] = None
    DateRangeSpan: Optional[timedelta] = None
    Granularity: Optional[str] = None
    FutureDateCount: Optional[int] = None
    InvalidDateFormatCount: Optional[int] = None
