"""What a derived table is made of: a table no source holds as is. The
engine of the post-processing (see `StarUnification/`), which writes these
specs itself from a `UnificationSpec` -- never read from a job or the LLM.

Two kinds, whatever the entity (customers, products, suppliers, sites...):

    {"name": "produits_union", "kind": "union", "sourceLabelColumn": "catalogue",
     "unionSources": [
       {"sourceTable": "produits_a", "sourceLabelValue": "a",
        "columnMapping": {"produit_ref": "ref", "ean": "code_ean", "libelle": "nom"}},
       {"sourceTable": "produits_b", "sourceLabelValue": "b",
        "columnMapping": {"produit_ref": "sku", "ean": "ean13", "libelle": "designation"}}]}
    {"name": "produits_unifies", "kind": "dedup", "dedupSourceTable": "produits_union",
     "dedupMatchColumns": ["ean", "libelle"], "dedupIdColumn": "produit_unifie_id",
     "dedupPriorityColumn": "date_modif", "dedupSourcePriority": ["b", "a"]}

- "union": the rows of several tables under one set of columns
  (`columnMapping`: target column -> source column, only the columns that
  mean the same thing in several sources), plus an optional label column
  telling which source each row comes from. With `keepUnmappedColumns`
  (default), every column a source has that the mapping doesn't use is kept
  too (see `withAllColumns`): the union carries all the attributes of its
  sources. A union of facts may add `computedMeasures` (a product of its
  columns: `montant = quantite x prix_unitaire`) and name its
  `nonAdditiveMeasures` (a unit price: never summed);
- "dedup": one row per identity of `dedupSourceTable` (a loaded table or an
  earlier "union"): two rows are the same identity as soon as they share a
  real value on any of `dedupMatchColumns`. Each attribute of an identity
  comes from its priority row -- the most recent on `dedupPriorityColumn`,
  then the first source of `dedupSourcePriority` (label values of the
  union read), then row order --, an empty value filled from the next row in
  that order. Over a union with an origin label, the identity says which
  sources it is in (`present_<source>`), and each of
  `dedupSourceIdColumns` (the column a source's rows are referenced by: a
  card number, a web id) gets one column per source. Also writes
  `<name>__correspondance`, every source row with
  its unified id -- the table a fact's original keys are resolved through.

A declaration, never a decision: `DerivedTableBuilder` checks every table and
column name, and never overwrites an existing table.
"""
from __future__ import annotations

from typing import Dict, List, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field

UNION = "union"
DEDUP = "dedup"

MAPPING_TABLE_SUFFIX = "__correspondance"

_TABLE_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"


class UnionSource(BaseModel):
    """One table contributing to a "union". `columnMapping` never has to
    cover the columns only the other sources have: those are NULL here.
    `sourceLabelColumn` is accepted here too, not just on the spec (an LLM
    naturally puts it next to `sourceLabelValue`); the source's own wins."""
    model_config = ConfigDict(extra="forbid")

    sourceTable: str
    columnMapping: Dict[str, str]
    sourceLabelValue: Optional[str] = None
    sourceLabelColumn: Optional[str] = None


class ComputedMeasure(BaseModel):
    """A measure no source holds: the product of `multiply`, columns of the
    union (e.g. `{"name": "montant", "multiply": ["quantite", "prix_unitaire"]}`)."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=_TABLE_NAME_PATTERN)
    multiply: List[str] = Field(..., min_length=2)


class DerivedTableSpec(BaseModel):
    """`dedupIdColumn`: the unified id's column (default `<name>_id`), a
    synthetic 1, 2, 3... -- the key the facts are resolved to."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=_TABLE_NAME_PATTERN)
    kind: Literal[UNION, DEDUP]
    justification: Optional[str] = None

    unionSources: List[UnionSource] = Field(default_factory=list)
    sourceLabelColumn: Optional[str] = None
    keepUnmappedColumns: bool = True
    computedMeasures: List[ComputedMeasure] = Field(default_factory=list)
    nonAdditiveMeasures: List[str] = Field(default_factory=list)

    dedupSourceTable: Optional[str] = None
    dedupMatchColumns: List[str] = Field(default_factory=list)
    dedupIdColumn: Optional[str] = None
    dedupPriorityColumn: Optional[str] = None
    dedupSourcePriority: List[str] = Field(default_factory=list)
    dedupSourceIdColumns: List[str] = Field(default_factory=list)

    @property
    def idColumn(self):
        return self.dedupIdColumn or f"{self.name}_id"

    @property
    def mappingTableName(self):
        return f"{self.name}{MAPPING_TABLE_SUFFIX}"

    def labelColumnFor(self, source):
        return source.sourceLabelColumn or self.sourceLabelColumn

    def withAllColumns(self, columnsByTable):
        """A "union" with `keepUnmappedColumns`: a copy whose mapping also
        carries every column of `columnsByTable[source]` the mapping doesn't
        use, under its own name -- shared by the sources that have it
        unmapped -- or, when that name is already a mapped target or the
        label column, as `<column>_<source label>`. Anything else: as is."""
        if self.kind != UNION or not self.keepUnmappedColumns:
            return self
        taken = {t for s in self.unionSources for t in s.columnMapping}
        taken |= {self.labelColumnFor(s) for s in self.unionSources} - {None}
        sources = []
        for source in self.unionSources:
            mapping = dict(source.columnMapping)
            used = set(mapping.values())
            for column in columnsByTable.get(source.sourceTable, []):
                if column in used:
                    continue
                target = column if column not in taken else f"{column}_{source.sourceLabelValue or source.sourceTable}"
                if target not in mapping:
                    mapping[target] = column
            sources.append(source.model_copy(update={"columnMapping": mapping}))
        return self.model_copy(update={"unionSources": sources})
