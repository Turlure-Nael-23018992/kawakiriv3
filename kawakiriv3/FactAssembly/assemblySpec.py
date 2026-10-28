"""What a fact to assemble is made of: an anchor table (one row of it = one
row of the fact), the measures to gather and the keys to carry onto it.
Same shape whether the caller declares it in the job (`assembledFacts`) or
the LLM hint proposes it (kawakiri-langgraph's `AssembledFactChoice`).

    {"name": "marge_lignes", "anchorTable": "lignes_commande",
     "measures": [
       {"table": "tarification_lignes", "column": "montant_ht", "additivity": "additive"},
       {"table": "expeditions", "column": "poids_kg", "additivity": "additive"},
       {"table": "commandes", "column": "frais_port", "additivity": "additive",
        "allocation": "prorata:montant_ht"}],
     "keys": [{"table": "commandes", "column": "client_id"}]}

A declaration, never a decision: `FactAssembler` checks every item against
the physical links and the grain, and refuses what would be counted wrong.
"""
from __future__ import annotations

from typing import List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

ADDITIVE = "additive"
SEMI_ADDITIVE = "semi_additive"
NON_ADDITIVE = "non_additive"

ALLOCATION_EQUAL = "egal"
ALLOCATION_PRORATA_PREFIX = "prorata:"


class MeasureSpec(BaseModel):
    """`aggregation`: how rows FINER than the grain are brought down to it
    (default: `sum` for an additive measure, refused otherwise).
    `allocation`: how a measure COARSER than the grain is spread over its
    rows, `"egal"` or `"prorata:<column of the assembled fact>"` (default:
    an additive coarser measure is refused). `alias`: the column's name in
    the assembled fact."""
    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    additivity: Literal[ADDITIVE, SEMI_ADDITIVE, NON_ADDITIVE] = ADDITIVE
    aggregation: Optional[Literal["sum", "count", "min", "max", "avg"]] = None
    allocation: Optional[str] = Field(default=None, pattern=r"^(egal|prorata:.+)$")
    alias: Optional[str] = None


class KeySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    table: str
    column: str
    alias: Optional[str] = None


class AssemblySpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=r"^[A-Za-z_][A-Za-z0-9_]*$")
    anchorTable: str
    justification: Optional[str] = None
    expectedGrain: Optional[str] = None
    measures: List[MeasureSpec] = Field(..., min_length=1)
    keys: List[KeySpec] = Field(default_factory=list)

    def renamedTables(self, tableNames):
        """A copy whose table names go through `tableNames` (`{name:
        newName}`, e.g. source table -> DuckDB table); unknown names kept."""
        rename = lambda name: tableNames.get(name, name)
        return self.model_copy(update={
            "anchorTable": rename(self.anchorTable),
            "measures": [m.model_copy(update={"table": rename(m.table)}) for m in self.measures],
            "keys": [k.model_copy(update={"table": rename(k.table)}) for k in self.keys],
        })
