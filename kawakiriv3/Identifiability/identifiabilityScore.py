from dataclasses import dataclass


@dataclass
class IdentifiabilityScore:
    """Result of `Identifiability.score()` for a single column: how
    identifying that column is, broken down into its three components plus
    the weighted total and a human-readable diagnostic bucket.

    columnName: the scored column.
    uniquenessRatio: distinct values / total rows — how close the column is
        to being a key on its own.
    completeness: 1 - Sparsity — how rarely the column is null.
    entropyRatio: normalized Shannon entropy of the value distribution —
        how evenly spread (vs. dominated by a few values) the column is.
    score: the weighted sum of the three components above (see
        `Identifiability/scoring.py` for the weights).
    diagnostic: the threshold bucket `score` falls into (e.g.
        "HIGH_IDENTIFIABILITY"), from `IDENTIFIABILITY_THRESHOLDS`.
    """

    columnName: str
    uniquenessRatio: float
    completeness: float
    entropyRatio: float
    score: float
    diagnostic: str
