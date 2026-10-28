# Weights applied in Identifiability.score() (must sum to 1.0). Uniqueness
# dominates because a near-unique column is the strongest identifiability
# signal on its own; entropy and completeness are secondary corrections.
UNIQUENESS_WEIGHT = 0.5
ENTROPY_WEIGHT = 0.3
COMPLETENESS_WEIGHT = 0.2

# Score thresholds (descending order) mapped to their diagnostic label. The
# first threshold cleared (score >= threshold) determines the diagnostic;
# below the lowest threshold, IDENTIFIABILITY_DEFAULT_DIAGNOSTIC applies.
IDENTIFIABILITY_THRESHOLDS = (
    (0.85, "HIGH_IDENTIFIABILITY"),
    (0.50, "MEDIUM_IDENTIFIABILITY"),
    (0.20, "LOW_IDENTIFIABILITY"),
)
IDENTIFIABILITY_DEFAULT_DIAGNOSTIC = "VERY_LOW_IDENTIFIABILITY"
