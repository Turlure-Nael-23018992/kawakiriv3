"""What the post-processing reads of the built stars, and all it reads: per
star, the fact (`<fact>__fact`), the role of each of its columns, and the
dimension each key points to -- a linked source table (`web_customers`
through `customer_id`), an extracted dimension (`<fact>__dim_product_name`)
or the calendar (`<fact>__dim_date`).

Nothing here classifies, links or extracts anything again: the catalogue is
read off the `FactModel`s, so the unification can never undo what a star
decided.

`summary()` is what the LLM is shown: names, types, roles, row counts and
null ratios -- never a real value (GDPR, see docs).
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.DuckDbManagement.duckDbUtils import _typeFamily
from kawakiriv3.FactAnalysis.columnAssessment import DEGENERATE, EXTRACTED_KEY, FOREIGN_KEY, MEASURE
from kawakiriv3.Sources.sourceConnector import quoteIdentifier as q

LINKED = "linked"        # a source table a key of the fact links to
EXTRACTED = "extracted"  # a dimension generated out of the fact
CALENDAR = "calendar"    # the fact's shared calendar

# The roles of the columns a star fact table keeps.
STAR_ROLES = (FOREIGN_KEY, EXTRACTED_KEY, MEASURE, DEGENERATE)

_INFERRED_TYPES = {"numeric": "numeric", "temporal": "date", "text": "string"}


def inferredTypeOf(sqlType):
    """The profiler's words for a DuckDB type: numeric, date, string or boolean."""
    family = _typeFamily(sqlType)
    if family in _INFERRED_TYPES:
        return _INFERRED_TYPES[family]
    return "boolean" if family.startswith("BOOL") else family.lower()


@dataclass
class StarDimension:
    """`columns`: `[(column, inferredType)]`, the key first. `nullRatios`:
    `{column: share of null rows}`."""

    table: str
    keyColumn: str
    kind: str
    columns: list
    rowCount: int
    nullRatios: dict = field(default_factory=dict)
    usedBy: list = field(default_factory=list)

    @property
    def columnNames(self):
        return [c for c, _ in self.columns]

    def typeOf(self, column):
        return dict(self.columns).get(column)


@dataclass
class StarColumn:
    """One column of a star fact table. `dimension`/`dimensionKey`: the
    dimension a key points to and the column it joins on."""

    name: str
    inferredType: str
    role: str
    additivity: Optional[str] = None
    dimension: Optional[str] = None
    dimensionKey: Optional[str] = None


@dataclass
class Star:
    """`name`: how a job or the LLM names the fact (the hinted table, or
    the assembled fact's name)."""

    name: str
    model: object
    columns: dict
    rowCount: int
    expectedGrain: Optional[str] = None

    @property
    def factTable(self):
        return self.model.starFactTable

    def assessment(self, column):
        return next(a for a in self.model.assessments if a.columnName == column)


class StarCatalog:
    """`stars`: `{name: Star}`, in build order; `dimensions`: `{table:
    StarDimension}`, one per dimension table whatever the number of stars
    using it."""

    def __init__(self, stars, dimensions):
        self.stars = stars
        self.dimensions = dimensions

    @classmethod
    def build(cls, db, facts):
        """`facts`: `[(name, FactModel, expectedGrain or None)]`."""
        stars, dimensions = {}, {}
        for name, model, expectedGrain in facts:
            extracted = {d.tableName: d for d in model.dimensions}
            columns = {}
            for a in model.assessments:
                if a.role not in STAR_ROLES:
                    continue
                column = StarColumn(name=a.columnName, inferredType=a.inferredType, role=a.role,
                                    additivity=a.additivity)
                if a.role == FOREIGN_KEY and a.linkedTo:
                    column.dimension, column.dimensionKey = a.linkedTo.rsplit(".", 1)
                    if column.dimension not in dimensions:
                        dimensions[column.dimension] = _dimension(db, column.dimension, column.dimensionKey, LINKED)
                elif a.role == EXTRACTED_KEY and a.dimensionTable in extracted:
                    generated = extracted[a.dimensionTable]
                    column.dimension, column.dimensionKey = generated.tableName, generated.keyColumn
                    if column.dimension not in dimensions:
                        dimensions[column.dimension] = _dimension(
                            db, generated.tableName, generated.keyColumn, CALENDAR if generated.isDate else EXTRACTED,
                        )
                if column.dimension and name not in dimensions[column.dimension].usedBy:
                    dimensions[column.dimension].usedBy.append(name)
                columns[a.columnName] = column
            stars[name] = Star(
                name=name, model=model, columns=columns, rowCount=db.getRowCount(model.starFactTable),
                expectedGrain=expectedGrain,
            )
        return cls(stars, dimensions)

    def starsUsing(self, dimension):
        """`[(star, column)]`: every fact column pointing to `dimension`."""
        return [
            (star, column) for star in self.stars.values()
            for column in star.columns.values() if column.dimension == dimension
        ]

    def summary(self):
        """`{"stars": [...], "dimensions": [...]}`, structure only."""
        stars = []
        for star in self.stars.values():
            columns = []
            for column in star.columns.values():
                entry = {"columnName": column.name, "inferredType": column.inferredType, "role": column.role}
                if column.additivity:
                    entry["additivity"] = column.additivity
                if column.dimension:
                    entry["dimension"] = column.dimension
                columns.append(entry)
            entry = {"fact": star.name, "rowCount": star.rowCount, "columns": columns}
            if star.expectedGrain:
                entry["expectedGrain"] = star.expectedGrain
            stars.append(entry)
        dimensions = [
            {
                "dimension": d.table, "kind": d.kind, "keyColumn": d.keyColumn, "rowCount": d.rowCount,
                "usedBy": list(d.usedBy),
                "columns": [
                    {"columnName": c, "inferredType": t, "nullRatio": d.nullRatios.get(c, 0.0)} for c, t in d.columns
                ],
            }
            for d in self.dimensions.values()
        ]
        return {"stars": stars, "dimensions": dimensions}


def _dimension(db, table, keyColumn, kind):
    columnTypes = db.getColumns(table, dataType=True)
    names = [keyColumn] + [c for c, _ in columnTypes if c != keyColumn]
    types = dict(columnTypes)
    rowCount = db.getRowCount(table)
    nullRatios = {}
    if rowCount and names:
        counts = db.query(f"SELECT {', '.join(f'COUNT({q(c)})' for c in names)} FROM {q(table)}")[0]
        nullRatios = {c: round(1 - n / rowCount, 3) for c, n in zip(names, counts)}
    return StarDimension(
        table=table, keyColumn=keyColumn, kind=kind, columns=[(c, inferredTypeOf(types[c])) for c in names],
        rowCount=rowCount, nullRatios=nullRatios,
    )
