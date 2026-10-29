"""What the post-processing unifies, in the words of the built stars. Same
shape whether the job declares it (`unification`) or the LLM proposes it
(kawakiri-langgraph's `POST /propose-unifications`):

    {"dimensions": [{
       "name": "clients_unifies", "idColumn": "client_unifie_id",
       "sources": [
         {"dimension": "loyalty_cards", "label": "magasin",
          "columnMapping": {"email": "client_email", "telephone": "client_phone"}},
         {"dimension": "web_customers", "label": "web",
          "columnMapping": {"email": "email", "telephone": "phone"}}],
       "matchColumns": ["email", "telephone"],
       "priorityColumn": null, "sourcePriority": ["web", "magasin"]}],
     "facts": [{
       "name": "ventes_unifiees", "labelColumn": "canal",
       "sources": [
         {"fact": "web_sales", "label": "web", "columnMapping": {"quantite": "quantity"}},
         {"fact": "shop_sales", "label": "magasin", "columnMapping": {"quantite": "qty"}}],
       "computedMeasures": [{"name": "montant", "multiply": ["quantite", "prix_unitaire"]}],
       "nonAdditiveMeasures": ["prix_unitaire"]}]}

- a unified dimension: the dimensions of the stars that describe the same
  entity (a linked source table, an extracted dimension), one per source,
  each with its origin label. `columnMapping` (target -> the dimension's
  column) only aligns the attributes that mean the same thing; every other
  column is kept as is (see `unionTargets`). Each source's key -- the
  column its facts join on, known from the star -- lands under one target
  (`sourceKeyTarget`), split per source by the dedup (`<key>_<label>`).
  Rows sharing a real value on any of `matchColumns` (union targets) are
  one identity; its attributes come from its most recent row on
  `priorityColumn`, then from the first source of `sourcePriority`;
- gathered facts: the star facts of several stars stacked under one set of
  columns (`columnMapping`: target -> the star fact's column), with the
  origin label column, optional `computedMeasures` and `nonAdditiveMeasures`.

`errorsAgainst` only checks names of the catalogue: dimensions, facts, and
columns of the star tables.
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, Field

from kawakiriv3.DerivedTables.derivedTableSpec import ComputedMeasure, DerivedTableSpec
from kawakiriv3.StarUnification.starCatalog import CALENDAR

_NAME_PATTERN = r"^[A-Za-z_][A-Za-z0-9_]*$"

UNION_TABLE_SUFFIX = "__union"
DEFAULT_SOURCE_KEY_TARGET = "cle_source"

_ORDERABLE_TYPES = ("date", "numeric")


class DimensionSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimension: str
    label: str = Field(..., min_length=1)
    columnMapping: Dict[str, str] = Field(default_factory=dict)


class UnifiedDimensionSpec(BaseModel):
    """`idColumn`: the unified id (default `<name>_id`), a synthetic 1, 2,
    3... -- what the facts' keys are remapped to. `labelColumn`: the origin
    column of the intermediate union."""
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=_NAME_PATTERN)
    idColumn: Optional[str] = Field(None, pattern=_NAME_PATTERN)
    sources: List[DimensionSource] = Field(..., min_length=1)
    matchColumns: List[str] = Field(..., min_length=1)
    priorityColumn: Optional[str] = None
    sourcePriority: List[str] = Field(default_factory=list)
    labelColumn: str = Field("origine", pattern=_NAME_PATTERN)
    justification: Optional[str] = None

    @property
    def unifiedIdColumn(self):
        return self.idColumn or f"{self.name}_id"

    @property
    def unionTable(self):
        return f"{self.name}{UNION_TABLE_SUFFIX}"

    def sourceKeyTarget(self, catalog):
        """The union target every source's key lands under: the one a
        mapping gives it, else the keys' shared name, else `cle_source`."""
        mapped = {
            target for source in self.sources if source.dimension in catalog.dimensions
            for target, column in source.columnMapping.items()
            if column == catalog.dimensions[source.dimension].keyColumn
        }
        if mapped:
            return sorted(mapped)[0]
        keys = {catalog.dimensions[s.dimension].keyColumn for s in self.sources if s.dimension in catalog.dimensions}
        return keys.pop() if len(keys) == 1 else DEFAULT_SOURCE_KEY_TARGET

    def toDerivedSpecs(self, catalog):
        """`(union, dedup)` `DerivedTableSpec`s for `DerivedTableBuilder`:
        every column of each dimension kept, each key under
        `sourceKeyTarget`, kept per source by the dedup."""
        keyTarget = self.sourceKeyTarget(catalog)
        sources = []
        for source in self.sources:
            mapping = dict(source.columnMapping)
            mapping.setdefault(keyTarget, catalog.dimensions[source.dimension].keyColumn)
            sources.append({"sourceTable": source.dimension, "sourceLabelValue": source.label,
                            "columnMapping": mapping})
        union = DerivedTableSpec(
            name=self.unionTable, kind="union", sourceLabelColumn=self.labelColumn, unionSources=sources,
            justification=self.justification,
        ).withAllColumns({name: d.columnNames for name, d in catalog.dimensions.items()})
        dedup = DerivedTableSpec(
            name=self.name, kind="dedup", dedupSourceTable=union.name, dedupMatchColumns=self.matchColumns,
            dedupIdColumn=self.unifiedIdColumn, dedupPriorityColumn=self.priorityColumn,
            dedupSourcePriority=self.sourcePriority, dedupSourceIdColumns=[keyTarget],
            justification=self.justification,
        )
        return union, dedup


class FactSource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    fact: str
    label: str = Field(..., min_length=1)
    columnMapping: Dict[str, str] = Field(default_factory=dict)


class GatheredFactSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str = Field(..., pattern=_NAME_PATTERN)
    labelColumn: str = Field("origine", pattern=_NAME_PATTERN)
    sources: List[FactSource] = Field(..., min_length=2)
    computedMeasures: List[ComputedMeasure] = Field(default_factory=list)
    nonAdditiveMeasures: List[str] = Field(default_factory=list)
    justification: Optional[str] = None


class UnificationSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dimensions: List[UnifiedDimensionSpec] = Field(default_factory=list)
    facts: List[GatheredFactSpec] = Field(default_factory=list)

    @property
    def isEmpty(self):
        return not self.dimensions and not self.facts

    def renamed(self, tableNames):
        """A copy whose dimension and fact names go through `tableNames`
        (`{name: newName}`, e.g. source table -> DuckDB table)."""
        rename = lambda name: tableNames.get(name, name)
        return self.model_copy(update={
            "dimensions": [d.model_copy(update={"sources": [
                s.model_copy(update={"dimension": rename(s.dimension)}) for s in d.sources
            ]}) for d in self.dimensions],
            "facts": [f.model_copy(update={"sources": [
                s.model_copy(update={"fact": rename(s.fact)}) for s in f.sources
            ]}) for f in self.facts],
        })

    def errorsAgainst(self, catalog):
        """`{spec name: [errors]}`, only the specs with an error."""
        errors = {}
        taken = set(catalog.dimensions) | set(catalog.stars)
        unified = {}
        for spec in self.dimensions:
            found = _dimensionErrors(spec, catalog, taken, unified)
            if found:
                errors[spec.name] = found
            taken.add(spec.name)
        gathered = {}
        for spec in self.facts:
            found = _factErrors(spec, catalog, taken, gathered)
            if found:
                errors[spec.name] = found
            taken.add(spec.name)
        return errors


def unionTargets(sources, columnsBySource, labelColumn):
    """`{target: {label: source column}}`: what a union of `sources`
    (`[(label, columnMapping)]`) carries -- its mapped targets, plus every
    column of a source the mapping leaves out, under its own name or, when
    that name is a mapped target or the label column, `<column>_<label>`
    (the rule of `DerivedTableSpec.withAllColumns`)."""
    taken = {t for _, mapping in sources for t in mapping} | {labelColumn}
    targets = {}
    for label, mapping in sources:
        used = set(mapping.values())
        full = dict(mapping)
        for column in columnsBySource.get(label, []):
            if column not in used:
                full.setdefault(column if column not in taken else f"{column}_{label}", column)
        for target, column in full.items():
            targets.setdefault(target, {})[label] = column
    return targets


def _labelErrors(labels):
    duplicates = sorted({label for label in labels if labels.count(label) > 1})
    return [f"libelles de source en double : {duplicates}"] if duplicates else []


def _dimensionErrors(spec, catalog, taken, unified):
    errors = []
    if spec.name in taken:
        errors.append(f"le nom {spec.name!r} est deja celui d'une dimension, d'un fait ou d'une spec precedente")
    errors += _labelErrors([s.label for s in spec.sources])
    columnsByLabel = {}
    for source in spec.sources:
        dimension = catalog.dimensions.get(source.dimension)
        if dimension is None:
            errors.append(f"{source.dimension!r} n'est une dimension d'aucune etoile")
            continue
        if dimension.kind == CALENDAR:
            errors.append(f"{source.dimension!r} est un calendrier : les calendriers sont reunis avec les faits")
            continue
        if source.dimension in unified:
            errors.append(f"{source.dimension!r} est deja une source de {unified[source.dimension]}")
        unified[source.dimension] = spec.name
        unknown = sorted(set(source.columnMapping.values()) - set(dimension.columnNames))
        if unknown:
            errors.append(f"colonnes inconnues dans la dimension {source.dimension} : {unknown}")
        columnsByLabel[source.label] = dimension.columnNames
    if errors:
        return errors

    keyTargets = {
        target for s in spec.sources for target, column in s.columnMapping.items()
        if column == catalog.dimensions[s.dimension].keyColumn
    }
    if len(keyTargets) > 1:
        errors.append(f"les cles des sources sont mappees sous plusieurs noms : {sorted(keyTargets)}")
    keyTarget = spec.sourceKeyTarget(catalog)
    mappings = []
    for s in spec.sources:
        mapping = dict(s.columnMapping)
        mapping.setdefault(keyTarget, catalog.dimensions[s.dimension].keyColumn)
        mappings.append((s.label, mapping))
    targets = unionTargets(mappings, columnsByLabel, spec.labelColumn)
    if spec.labelColumn in targets:
        errors.append(f"la colonne d'origine {spec.labelColumn!r} est aussi une colonne de la dimension")
    unknown = sorted(set(spec.matchColumns) - set(targets))
    if unknown:
        errors.append(f"colonnes de rapprochement inconnues : {unknown} ; colonnes possibles : {sorted(targets)}")
    if spec.unifiedIdColumn in targets:
        errors.append(f"l'identifiant unifie {spec.unifiedIdColumn!r} est deja une colonne des dimensions")
    if spec.priorityColumn:
        if spec.priorityColumn not in targets:
            errors.append(f"colonne de priorite inconnue : {spec.priorityColumn!r}")
        else:
            types = {
                catalog.dimensions[s.dimension].typeOf(targets[spec.priorityColumn][s.label])
                for s in spec.sources if s.label in targets[spec.priorityColumn]
            }
            if not types <= set(_ORDERABLE_TYPES):
                errors.append(f"colonne de priorite {spec.priorityColumn!r} ni date ni nombre ({sorted(types)})")
    unknown = sorted(set(spec.sourcePriority) - {s.label for s in spec.sources})
    if unknown:
        errors.append(f"sources de priorite inconnues : {unknown}")
    return errors


def _factErrors(spec, catalog, taken, gathered):
    errors = []
    if spec.name in taken:
        errors.append(f"le nom {spec.name!r} est deja celui d'une dimension, d'un fait ou d'une spec precedente")
    errors += _labelErrors([s.label for s in spec.sources])
    columnsByLabel = {}
    for source in spec.sources:
        star = catalog.stars.get(source.fact)
        if star is None:
            errors.append(f"{source.fact!r} n'est le fait d'aucune etoile")
            continue
        if source.fact in gathered:
            errors.append(f"{source.fact!r} est deja reuni dans {gathered[source.fact]}")
        gathered[source.fact] = spec.name
        unknown = sorted(set(source.columnMapping.values()) - set(star.columns))
        if unknown:
            errors.append(f"colonnes inconnues dans le fait de l'etoile {source.fact} : {unknown}")
        columnsByLabel[source.label] = list(star.columns)
    if errors:
        return errors

    targets = unionTargets([(s.label, s.columnMapping) for s in spec.sources], columnsByLabel, spec.labelColumn)
    if spec.labelColumn in targets:
        errors.append(f"la colonne d'origine {spec.labelColumn!r} est aussi une colonne des faits")
    available = set(targets)
    starOf = {s.label: catalog.stars[s.fact] for s in spec.sources}
    for measure in spec.computedMeasures:
        unknown = sorted(set(measure.multiply) - available)
        if measure.name in available or unknown:
            errors.append(f"mesure calculee {measure.name!r} : nom deja pris ou colonnes inconnues {unknown}")
            continue
        notNumeric = sorted(
            column for column in measure.multiply
            if any(starOf[label].columns[c].inferredType != "numeric" for label, c in targets[column].items())
        )
        if notNumeric:
            errors.append(f"mesure calculee {measure.name!r} : colonnes non numeriques {notNumeric}")
        available.add(measure.name)
    unknown = sorted(set(spec.nonAdditiveMeasures) - available)
    if unknown:
        errors.append(f"mesures non additives inconnues : {unknown}")
    return errors
