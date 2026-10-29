"""Gathers the facts of several stars into one: the union of their star
facts (remapped when they were, see `keyRemapper.py`), with the origin
label column, the computed measures and the non-additive ones.

The roles are taken over from the stars, never computed again. Per column
of the gathered fact, from the columns it gathers:

    measure everywhere             -> measure (non additive if one is, or the spec says so)
    a date everywhere              -> key to a common calendar (the stars' calendars, and the days of the fact)
    degenerate everywhere          -> degenerate
    key to the same table          -> key to it (a unified dimension, a shared source table)
    extracted key in several stars -> key to a conformed dimension: the union of their dimensions, by value
    key in one fact only           -> key to that fact's dimension (empty for the other sources)
    anything else                  -> degenerate, with a warning (the stars disagree)

The origin label is a key to its own small dimension. The grain is verified
again (`GrainVerifier`) on the roles taken over; the result is a
`FactModel` built directly, without `FactAnalyzer`.
"""
from __future__ import annotations

import dataclasses
from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.DerivedTables.derivedTableBuilder import DerivedTableBuilder
from kawakiriv3.DerivedTables.derivedTableOutcome import BUILT, SKIPPED
from kawakiriv3.DerivedTables.derivedTableSpec import DerivedTableSpec
from kawakiriv3.DuckDbManagement.duckDbUtils import _typeFamily
from kawakiriv3.FactAnalysis.columnAssessment import (
    DATE, DEGENERATE, EXTRACTED_KEY, FOREIGN_KEY, KEY_CANDIDATE, MEASURE, NON_ADDITIVE, SOURCE_STAR,
    SOURCE_UNIFICATION, ColumnAssessment,
)
from kawakiriv3.FactAnalysis.dimensionExtractor import (
    DATE_KEY_COLUMN, ExtractedDimension, calendarTableName, dimensionTableName, starFactTableName,
)
from kawakiriv3.FactAnalysis.factAnalyzer import FactModel, nonGrainWarnings, withGrainVerdict
from kawakiriv3.FactAnalysis.factLinker import ForeignKeyLink
from kawakiriv3.FactAnalysis.grainVerifier import GrainVerifier
from kawakiriv3.StarUnification.starCatalog import inferredTypeOf
from kawakiriv3.Sources.sourceConnector import quoteIdentifier as q


@dataclass
class GatheredFact:
    """`sources`: `[{"fact", "label", "table", "rowCount"}]`, `table` the
    (remapped) star fact gathered."""

    name: str
    status: str
    reason: Optional[str] = None
    justification: Optional[str] = None
    table: Optional[str] = None
    labelColumn: Optional[str] = None
    sources: list = field(default_factory=list)
    rowCount: Optional[int] = None
    typeWarnings: tuple = ()


@dataclass
class _Entry:
    """One gathered fact's column under a target of the union."""

    fact: str
    column: str
    assessment: object
    model: object


def _fromStars(facts):
    return f"de l'etoile {facts[0]}" if len(facts) == 1 else f"des etoiles {', '.join(facts)}"


class FactGatherer:
    """`models`: `{star name: FactModel}`, the remapped copy of a star when
    its keys were remapped, the star's own model otherwise."""

    def __init__(self, db, catalog, models):
        self.db = db
        self.catalog = catalog
        self.models = models

    def gather(self, spec, errors=None):
        """`(GatheredFact, FactModel or None)`."""
        outcome = GatheredFact(
            name=spec.name, status=SKIPPED, justification=spec.justification, labelColumn=spec.labelColumn,
            sources=[{"fact": s.fact, "label": s.label} for s in spec.sources],
        )
        if errors:
            outcome.reason = "spec invalide : " + " ; ".join(errors)
            return outcome, None
        models = [self.models[s.fact] for s in spec.sources]
        union = self._unionSpec(spec, models)
        built = DerivedTableBuilder(self.db).buildAll([union])[union.name]
        if built.status != BUILT:
            outcome.reason = built.reason
            return outcome, None
        outcome.status, outcome.table, outcome.typeWarnings = BUILT, union.name, built.typeWarnings
        outcome.rowCount = self.db.getRowCount(union.name)
        for source, model in zip(outcome.sources, models):
            source.update(table=model.starFactTable, rowCount=self.db.getRowCount(model.starFactTable))
        return outcome, self._model(spec, union, models, built)

    def _unionSpec(self, spec, models):
        """The union of the star facts, the mapping read through each star's
        remapped keys; a target every source feeds with the same unified id
        takes that id's name."""
        mappings = []
        for source, model in zip(spec.sources, models):
            renamed = {key.column: key.newColumn for key in model.remappedKeys}
            mappings.append({target: renamed.get(column, column) for target, column in source.columnMapping.items()})
        targets = list(dict.fromkeys(t for mapping in mappings for t in mapping))
        remappedIds = {key.newColumn for model in models for key in model.remappedKeys}
        renamedTargets = {}
        for target in targets:
            columns = {mapping[target] for mapping in mappings if target in mapping}
            if len(columns) == 1 and columns <= remappedIds:
                name = columns.pop()
                if name not in targets and name not in renamedTargets.values():
                    renamedTargets[target] = name
        union = DerivedTableSpec(
            name=starFactTableName(spec.name), kind="union", sourceLabelColumn=spec.labelColumn,
            justification=spec.justification, computedMeasures=spec.computedMeasures,
            nonAdditiveMeasures=spec.nonAdditiveMeasures,
            unionSources=[
                {"sourceTable": model.starFactTable, "sourceLabelValue": source.label,
                 "columnMapping": {renamedTargets.get(t, t): c for t, c in mapping.items()}}
                for source, model, mapping in zip(spec.sources, models, mappings)
            ],
        )
        return union.withAllColumns({model.starFactTable: self.db.getColumns(model.starFactTable) for model in models})

    def _model(self, spec, union, models, built):
        table = union.name
        facts = [s.fact for s in spec.sources]
        entriesByTarget = {}
        for source, branch, model in zip(spec.sources, union.unionSources, models):
            byName = {a.columnName: a for a in model.assessments}
            for target, column in branch.columnMapping.items():
                entriesByTarget.setdefault(target, []).append(_Entry(source.fact, column, byName[column], model))

        columnTypes = dict(self.db.getColumns(table, dataType=True))
        computed = {m.name: m for m in union.computedMeasures}
        nonAdditive = set(spec.nonAdditiveMeasures)
        warnings = list(built.typeWarnings)
        for model, fact in zip(models, facts):
            warnings += [f"{fact} : {w}" for w in nonGrainWarnings(model)]

        decided, dates, conformed, single = {}, [], {}, {}
        for target, entries in entriesByTarget.items():
            role, detail = self._roleOf(target, entries)
            if role == "conflict":
                warnings.append(detail)
            decided[target] = (role, entries, detail)
            if role == "date":
                dates.append(target)
            elif role == "conformed":
                conformed[target] = entries
            elif role == "single_extracted":
                single[target] = entries[0]

        dimensions = []
        calendar = None
        if dates:
            calendar = self._calendar(spec.name, table, dates, entriesByTarget)
            dimensions.append(calendar)
        for target, entries in conformed.items():
            dimensions.append(self._conformedDimension(spec.name, target, entries, columnTypes[target]))
        for target, entry in single.items():
            source = next(d for d in entry.model.dimensions if d.tableName == entry.assessment.dimensionTable)
            dimensions.append(dataclasses.replace(source, roleColumns=[target]))
        labelDimension = self._labelDimension(spec.name, table, spec.labelColumn)
        dimensions.append(labelDimension)
        dimensionByTarget = {column: d for d in dimensions for column in d.roleColumns}

        counts = self._counts(table, list(columnTypes))
        assessments, links = [], []
        for column, sqlType in columnTypes.items():
            total, nonNull, distinct = counts[column]
            base = dict(
                columnName=column, sqlType=sqlType, totalCount=total, nullCount=total - nonNull,
                distinctCount=distinct, requiresNameHint=False,
            )
            if column == spec.labelColumn:
                assessments.append(ColumnAssessment(
                    **base, inferredType=inferredTypeOf(sqlType), role=EXTRACTED_KEY, fallbackRole=EXTRACTED_KEY,
                    initialRole=KEY_CANDIDATE, roleSource=SOURCE_UNIFICATION, dimensionTable=labelDimension.tableName,
                    reason=f"colonne d'origine ({', '.join(s.label for s in spec.sources)}) : extraite dans "
                           f"{labelDimension.tableName}",
                ))
                continue
            if column in computed:
                additivity = NON_ADDITIVE if column in nonAdditive else None
                assessments.append(ColumnAssessment(
                    **base, inferredType="numeric", role=MEASURE, fallbackRole=MEASURE, roleSource=SOURCE_UNIFICATION,
                    additivity=additivity, reason=f"calculee : {' x '.join(computed[column].multiply)}",
                ))
                continue
            role, entries, detail = decided[column]
            first = entries[0].assessment
            fromFacts = [e.fact for e in entries]
            onlySome = f" (seulement dans {', '.join(fromFacts)}, vide pour les autres sources)"
            types = {e.assessment.inferredType for e in entries}
            assessment = ColumnAssessment(
                **base, inferredType=types.pop() if len(types) == 1 else inferredTypeOf(sqlType), role=DEGENERATE,
                fallbackRole=DEGENERATE, initialRole=first.initialRole, roleSource=SOURCE_STAR,
                reason=f"repris {_fromStars(fromFacts)}",
            )
            if role == MEASURE:
                assessment.role = MEASURE
                if column in nonAdditive or any(e.assessment.additivity == NON_ADDITIVE for e in entries):
                    assessment.additivity = NON_ADDITIVE
                assessment.reason += " : mesure" + (" non additive" if assessment.additivity else "")
            elif role == "date":
                assessment.role, assessment.dimensionTable = EXTRACTED_KEY, calendar.tableName
                assessment.reason += f" : date, jointe au calendrier commun {calendar.tableName}"
            elif role == FOREIGN_KEY:
                targetTable, targetColumn = first.linkedTo.rsplit(".", 1)
                assessment.role, assessment.linkedTo = FOREIGN_KEY, first.linkedTo
                assessment.reason += f" : cle vers {first.linkedTo}" + (
                    onlySome if len(entries) < len(models) else ""
                )
                sourceLinks = [
                    link for e in entries for link in e.model.links if link.sourceColumn == e.column
                ]
                links.append(ForeignKeyLink(
                    sourceColumn=column, targetTable=targetTable, targetColumn=targetColumn,
                    joinSuccessRatio=min((link.joinSuccessRatio for link in sourceLinks), default=0.0),
                    nameHint=any(link.nameHint for link in sourceLinks), ambiguous=False,
                ))
            elif role in ("conformed", "single_extracted"):
                dimension = dimensionByTarget[column]
                assessment.role, assessment.dimensionTable = EXTRACTED_KEY, dimension.tableName
                assessment.reason += (
                    f" : cle vers la dimension conforme {dimension.tableName}, union de {detail}"
                    if role == "conformed" else
                    f" : cle vers {dimension.tableName}{onlySome}"
                )
            elif role == DEGENERATE:
                assessment.reason += " : degeneree, hors grain"
            else:
                assessment.reason += " : roles divergents, gardee dans le fait comme reference, hors grain"
            assessments.append(assessment)

        model = FactModel(
            factTable=spec.name, starFactTable=table, hint=spec.justification, assessments=assessments,
            links=links, dimensions=dimensions, grain=None, warnings=warnings,
            remappedKeys=[key for model in models for key in model.remappedKeys], gatheredFacts=facts,
        )
        return withGrainVerdict(model, GrainVerifier(self.db, table, assessments).verify())

    def _roleOf(self, target, entries):
        """`(role, detail)`: MEASURE, "date", DEGENERATE, FOREIGN_KEY,
        "conformed" (detail: the dimensions it gathers), "single_extracted"
        or "conflict" (detail: the warning)."""
        assessments = [e.assessment for e in entries]
        roles = {a.role for a in assessments}
        if roles == {MEASURE}:
            return MEASURE, None
        if all(a.initialRole == DATE for a in assessments):
            # A date is an axis of the calendar, whatever a star linked it to.
            return "date", None
        if roles == {DEGENERATE}:
            return DEGENERATE, None
        if roles == {FOREIGN_KEY} and len({a.linkedTo for a in assessments}) == 1:
            return FOREIGN_KEY, None
        if roles == {EXTRACTED_KEY}:
            if len(entries) == 1:
                return "single_extracted", None
            return "conformed", ", ".join(a.dimensionTable for a in assessments)
        described = ", ".join(
            f"{e.fact}.{e.column} {e.assessment.role}" + (f" -> {e.assessment.linkedTo}" if e.assessment.linkedTo else "")
            for e in entries
        )
        return "conflict", f"{target} : les etoiles ne s'accordent pas ({described}) : gardee comme reference, hors grain"

    def _calendar(self, name, table, dates, entriesByTarget):
        """`<name>__dim_date`: the days of the stars' calendars the gathered
        dates join, and the days of the gathered fact's own dates (a date a
        star linked to a table has no calendar of its own)."""
        tableName = calendarTableName(name)
        calendars = sorted({
            e.assessment.dimensionTable for target in dates for e in entriesByTarget[target]
            if e.assessment.role == EXTRACTED_KEY and e.assessment.dimensionTable
        })
        days = [f"SELECT {DATE_KEY_COLUMN} AS d FROM {q(c)}" for c in calendars]
        days += [f"SELECT TRY_CAST({q(column)} AS DATE) AS d FROM {q(table)}" for column in dates]
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {q(tableName)} AS "
            f"SELECT DISTINCT d AS {DATE_KEY_COLUMN}, year(d) AS annee, quarter(d) AS trimestre, month(d) AS mois, "
            f"day(d) AS jour, isodow(d) AS jour_semaine FROM ({' UNION '.join(days)}) WHERE d IS NOT NULL ORDER BY d"
        )
        return ExtractedDimension(
            tableName=tableName, keyColumn=DATE_KEY_COLUMN,
            attributeColumns=["annee", "trimestre", "mois", "jour", "jour_semaine"],
            rowCount=self.db.getRowCount(tableName), isDate=True, roleColumns=list(dates),
        )

    def _conformedDimension(self, name, target, entries, keyType):
        """`<name>__dim_<target>`: the union of the stars' extracted
        dimensions, one row per key value, an attribute shared by several
        taking its largest value (as an extracted dimension does)."""
        tableName = dimensionTableName(name, target)
        sources = [next(d for d in e.model.dimensions if d.tableName == e.assessment.dimensionTable) for e in entries]
        attributeTypes = {}
        for source in sources:
            types = dict(self.db.getColumns(source.tableName, dataType=True))
            for column in source.attributeColumns:
                if column != target:
                    attributeTypes.setdefault(column, set()).add(_typeFamily(types[column]))
        attributes = list(attributeTypes)
        selects = []
        for source in sources:
            parts = [f"CAST({q(source.keyColumn)} AS {keyType}) AS {q(target)}"]
            for column in attributes:
                if column not in source.attributeColumns:
                    parts.append(f"NULL AS {q(column)}")
                elif len(attributeTypes[column]) > 1:
                    parts.append(f"CAST({q(column)} AS VARCHAR) AS {q(column)}")
                else:
                    parts.append(q(column))
            selects.append(f"SELECT {', '.join(parts)} FROM {q(source.tableName)}")
        attributesSql = "".join(f", MAX({q(c)}) AS {q(c)}" for c in attributes)
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {q(tableName)} AS SELECT {q(target)}{attributesSql} "
            f"FROM ({' UNION ALL '.join(selects)}) WHERE {q(target)} IS NOT NULL GROUP BY {q(target)} ORDER BY {q(target)}"
        )
        return ExtractedDimension(
            tableName=tableName, keyColumn=target, attributeColumns=attributes,
            rowCount=self.db.getRowCount(tableName), roleColumns=[target],
        )

    def _labelDimension(self, name, table, labelColumn):
        tableName = dimensionTableName(name, labelColumn)
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {q(tableName)} AS SELECT DISTINCT {q(labelColumn)} FROM {q(table)} "
            f"ORDER BY {q(labelColumn)}"
        )
        return ExtractedDimension(
            tableName=tableName, keyColumn=labelColumn, rowCount=self.db.getRowCount(tableName),
            roleColumns=[labelColumn],
        )

    def _counts(self, table, columns):
        """`{column: (rows, non-null rows, distinct values)}` in one scan."""
        parts = [f"COUNT({q(c)}), COUNT(DISTINCT {q(c)})" for c in columns]
        row = self.db.query(f"SELECT COUNT(*), {', '.join(parts)} FROM {q(table)}")[0]
        return {c: (row[0], row[1 + 2 * i], row[2 + 2 * i]) for i, c in enumerate(columns)}
