"""Roles a column of the fact table can take, and the per-column record the
classifier -> linker -> extractor chain fills in."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# --- roles assigned by ColumnClassifier, before any link is tested ---
EMPTY = "empty"                     # no non-null value
CONSTANT = "constant"               # a single distinct value: carries nothing
MEASURE = "measure"                 # continuous numeric: stays on the fact, aggregated
ROW_IDENTIFIER = "row_identifier"   # unique per row of the fact: never part of the grain
KEY_CANDIDATE = "key_candidate"     # discrete, repeating value: a dimension key, linked or extracted
DATE = "date"                       # date/timestamp: a date dimension key
DESCRIPTIVE = "descriptive"         # near-unique text (label, comment): stays on the fact, out of the grain
TECHNICAL = "technical"             # load/lineage metadata (dbt, ingestion timestamps, source file): excluded

# Who settled a column's role (`ColumnAssessment.roleSource`).
SOURCE_STATISTICS = "statistiques"
SOURCE_CALLER = "appelant"
SOURCE_LLM = "LLM"
SOURCE_NAME_PATTERN = "motif de nom"
SOURCE_STAR = "etoile"               # taken over from a built star, see StarUnification/
SOURCE_UNIFICATION = "unification"  # a key remapped to a unified dimension, a computed measure

# --- final roles, once linking and extraction ran ---
FOREIGN_KEY = "foreign_key"         # linked to an existing table's key
EXTRACTED_KEY = "extracted_key"     # no table to link to: becomes the key of a generated dimension
MOVED_ATTRIBUTE = "moved_attribute" # functionally determined by an extracted key: moved into its dimension
DEGENERATE = "degenerate"           # stays on the fact, outside the grain (row identifier, descriptive text)

# How a measure aggregates (`ColumnAssessment.additivity`), same words as FactAssembly's AssemblySpec.
ADDITIVE = "additive"
NON_ADDITIVE = "non_additive"  # a unit price, a rate, a balance: never summed

# Roles whose column takes part in the grain once resolved.
GRAIN_ROLES = (FOREIGN_KEY, EXTRACTED_KEY)


@dataclass
class ColumnAssessment:
    """One column of the analysed fact table.

    role: the classifier's verdict (see the constants above), later
        overwritten by the final role once linking/extraction ran.
    fallbackRole: the role the column ends with when no link is found --
        e.g. an integer named like a quantity is a key candidate only if it
        links somewhere, a measure otherwise.
    requiresNameHint: a link is only accepted when the names corroborate it
        (`SourceJoinCandidate.nameHint`). Set for row identifiers and for
        integers not named like a key: their values overlap any small
        integer id range by pure coincidence.
    reason: why the column got its current role, in French (shown in the
        report).
    """

    columnName: str
    inferredType: str
    sqlType: str
    totalCount: int
    nullCount: int
    distinctCount: int
    role: str
    fallbackRole: str
    requiresNameHint: bool
    reason: str
    linkedTo: Optional[str] = None       # "table.column" once linked
    dimensionTable: Optional[str] = None # the generated dimension it was extracted to / moved into
    initialRole: Optional[str] = None    # the classifier's role, kept when `role` is overwritten
    roleSource: str = SOURCE_STATISTICS  # who settled `initialRole` (see the SOURCE_* constants)
    additivity: Optional[str] = None     # NON_ADDITIVE when the caller or the LLM said so; a measure is additive otherwise

    def __post_init__(self):
        if self.initialRole is None:
            self.initialRole = self.role

    @property
    def isLinkable(self):
        """Whether the linker should test this column at all."""
        return self.role in (ROW_IDENTIFIER, KEY_CANDIDATE, DATE, DESCRIPTIVE)
