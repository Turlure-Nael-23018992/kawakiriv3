"""Third step: every key candidate of the fact that links to no existing
table is extracted out of the fact into a generated dimension.

Columns are grouped by functional dependency first, so `code_produit ->
libelle_produit, categorie` becomes ONE dimension keyed on `code_produit`
(the two attributes move out of the fact), instead of one dimension per
column. Determinants are tried from the most granular down (most distinct
values first), so a more granular key absorbs what it determines -- the
dimension comes out denormalized, as a star expects.

Every unlinked date of the fact (invoice date, due date, GL date...) shares
ONE calendar dimension, `<fact>__dim_date`, instead of one dimension per
date column (Kimball's role-playing date): the union of their days, with
year/quarter/month/day/weekday. A timestamp joins it through its date part.

Writes to DuckDB: the generated dimensions and the rewritten fact
`<fact>__fact` (every column but the moved attributes and the empty,
constant and technical ones). The source table itself is never modified.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from kawakiriv3.FactAnalysis.columnAssessment import (
    CONSTANT, DATE, DEGENERATE, DESCRIPTIVE, EMPTY, EXTRACTED_KEY, FOREIGN_KEY, KEY_CANDIDATE, MEASURE,
    MOVED_ATTRIBUTE, ROW_IDENTIFIER, SOURCE_LLM, SOURCE_STATISTICS, TECHNICAL,
)
from kawakiriv3.FactAnalysis.columnClassifier import looksLikeKeyName
from kawakiriv3.Sources.sourceConnector import quoteIdentifier

DROPPED_ROLES = (EMPTY, CONSTANT, TECHNICAL)

DATE_KEY_COLUMN = "date_key"
CALENDAR_ATTRIBUTES = ("annee", "trimestre", "mois", "jour", "jour_semaine")

# A key candidate that is one of these, applied to a date already in the
# fact, adds nothing of its own: `semaine_iso` next to an order date is just
# `week(date_commande)` under another name -- see `_calendarFunctionFor`.
_CALENDAR_FUNCTIONS = (
    ("annee", "year({d})"), ("trimestre", "quarter({d})"), ("mois", "month({d})"), ("jour", "day({d})"),
    ("jour_semaine", "isodow({d})"), ("semaine", "week({d})"),
)


@dataclass
class ExtractedDimension:
    """roleColumns: the fact columns joining this dimension -- one for a
    plain dimension (its own key), every date of the fact for the shared
    calendar."""

    tableName: str
    keyColumn: str
    attributeColumns: list = field(default_factory=list)
    rowCount: int = 0
    isDate: bool = False
    roleColumns: list = field(default_factory=list)


def starFactTableName(factTable):
    return f"{factTable}__fact"


def dimensionTableName(factTable, keyColumn):
    return f"{factTable}__dim_{keyColumn}"


def calendarTableName(factTable):
    return f"{factTable}__dim_date"


class DimensionExtractor:
    """`db`: the run's duckDbUtils. `profiler`: the fact's `aggProfiling`.
    `assessments`: the fact's columns, after `FactLinker.link()`."""

    def __init__(self, db, profiler, factTable, assessments):
        self.db = db
        self.profiler = profiler
        self.factTable = factTable
        self.assessments = assessments
        self.byName = {a.columnName: a for a in assessments}

    def extract(self):
        """Resolves every unlinked column's final role, materializes the
        generated dimensions and the rewritten fact. Returns
        `(starFactTable, [ExtractedDimension, ...])`."""
        self._resolveUnlinked()

        dates = [a for a in self.assessments if a.role == DATE]
        # A measure is also checked: the LLM's column hint can call a
        # calendar-derived column (`semaine_iso`) a measure instead of a key
        # -- summed, that's as meaningless as giving it its own dimension.
        # The caller's own declaration is never second-guessed this way.
        foldable = [
            a for a in self.assessments
            if a.role == KEY_CANDIDATE or (a.role == MEASURE and a.roleSource == SOURCE_LLM)
        ]
        calendarDerived = {}
        for candidate in foldable:
            match = self._calendarFunctionFor(candidate, dates) if dates else None
            if match:
                calendarDerived[candidate.columnName] = match
        keys = [a for a in self.assessments if a.role == KEY_CANDIDATE and a.columnName not in calendarDerived]
        # Dates stay grain keys of their own, never an attribute of another key;
        # so does a key the caller or the LLM designated -- a functional
        # dependency found on the data (nulls ignored) can be coincidental.
        movable = {
            a.columnName for a in self.assessments
            if a.columnName not in calendarDerived
            and ((a.role == KEY_CANDIDATE and a.roleSource == SOURCE_STATISTICS)
                 or (a.role == DEGENERATE and a.initialRole == DESCRIPTIVE))
        }
        ordered = sorted(keys, key=lambda a: (-a.distinctCount, not looksLikeKeyName(a.columnName), a.columnName))

        assigned = set()
        groups = []
        for key in ordered:
            if key.columnName in assigned:
                continue
            candidates = [
                name for name in movable
                if name != key.columnName and name not in assigned
                and self.byName[name].distinctCount <= key.distinctCount
            ]
            dependents = self.profiler.testFunctionalDependencies(key.columnName, sorted(candidates)) if candidates else []
            assigned.add(key.columnName)
            assigned.update(dependents)
            groups.append((key, [self.byName[name] for name in dependents]))

        dimensions = [self._materializeDimension(key, dependents) for key, dependents in groups]
        if dates:
            dimensions.insert(0, self._materializeCalendar(dates, calendarDerived))
        starFactTable = self._materializeFact()
        return starFactTable, dimensions

    def _resolveUnlinked(self):
        for a in self.assessments:
            if a.role == FOREIGN_KEY or a.role in DROPPED_ROLES or a.role == MEASURE:
                continue
            if a.role in (ROW_IDENTIFIER, DESCRIPTIVE):
                a.role = DEGENERATE
            elif a.role == KEY_CANDIDATE and a.fallbackRole == MEASURE:
                a.role = MEASURE
                a.reason = "entier sans lien corrobore par le nom vers une autre table : mesure"

    def _dateExpression(self, assessment):
        """The column as a DATE. A VARCHAR date is parsed with the format the
        profiler found dominant on it (`jj/mm/aaaa`...), else cast."""
        column = quoteIdentifier(assessment.columnName)
        if assessment.sqlType.upper().startswith(("DATE", "TIMESTAMP")):
            return f"CAST({column} AS DATE)"
        dominantFormat = self.profiler._dominantDateFormat.get(assessment.columnName)
        if dominantFormat:
            return f"CAST(try_strptime({column}, '{dominantFormat}') AS DATE)"
        return f"TRY_CAST({column} AS DATE)"

    def _calendarFunctionFor(self, candidate, dates):
        """`(dateAssessment, sqlTemplate)` when `candidate` equals one of
        `_CALENDAR_FUNCTIONS` applied to one of the fact's own dates on
        every row where both are non-null (at least one such row) -- a
        deterministic dependency found on the data, same coincidence caveat
        as `testFunctionalDependencies`. `None` when no such function
        matches any date."""
        if candidate.inferredType != "numeric":
            return None  # a calendar function always returns an integer
        candidateColumn = quoteIdentifier(candidate.columnName)
        for dateAssessment in dates:
            dateExpr = self._dateExpression(dateAssessment)
            for _, template in _CALENDAR_FUNCTIONS:
                function = template.format(d=dateExpr)
                # IS DISTINCT FROM, not !=: NULL != x is NULL (dropped by
                # FILTER, not counted), which would make a column that
                # fails TRY_CAST on every row look like a match.
                mismatches, comparable = self.db.conn.execute(
                    f"SELECT count(*) FILTER (WHERE TRY_CAST({candidateColumn} AS BIGINT) IS DISTINCT FROM "
                    f"{function}), count(*) "
                    f"FROM {quoteIdentifier(self.factTable)} "
                    f"WHERE {candidateColumn} IS NOT NULL AND {dateExpr} IS NOT NULL"
                ).fetchone()
                if comparable and not mismatches:
                    return dateAssessment, template
        return None

    def _materializeCalendar(self, dates, calendarDerived=None):
        calendarDerived = calendarDerived or {}
        tableName = calendarTableName(self.factTable)
        union = " UNION ".join(
            f"SELECT {self._dateExpression(a)} AS d FROM {quoteIdentifier(self.factTable)}" for a in dates
        )
        derivedSql = "".join(
            f", {template.format(d='d')} AS {quoteIdentifier(columnName)}"
            for columnName, (_, template) in calendarDerived.items()
        )
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {quoteIdentifier(tableName)} AS "
            f"SELECT DISTINCT d AS {DATE_KEY_COLUMN}, year(d) AS annee, quarter(d) AS trimestre, month(d) AS mois, "
            f"day(d) AS jour, isodow(d) AS jour_semaine{derivedSql} "
            f"FROM ({union}) WHERE d IS NOT NULL ORDER BY d"
        )
        for a in dates:
            a.role = EXTRACTED_KEY
            a.dimensionTable = tableName
            a.reason = f"date : jointe au calendrier partage {tableName}" + (
                " par sa partie date" if a.sqlType.upper().startswith("TIMESTAMP") else ""
            )
        for columnName, (dateAssessment, _) in calendarDerived.items():
            derived = self.byName[columnName]
            previousRole = derived.role
            derived.role = MOVED_ATTRIBUTE
            derived.dimensionTable = tableName
            derived.reason = (
                f"fonction deterministe de {dateAssessment.columnName}, deja dans le calendrier partage "
                f"{tableName} : fondue dedans plutot que "
                + ("mesuree separement (hint LLM ignore)" if previousRole == MEASURE else "dupliquee en dimension")
            )
        return ExtractedDimension(
            tableName=tableName, keyColumn=DATE_KEY_COLUMN,
            attributeColumns=list(CALENDAR_ATTRIBUTES) + list(calendarDerived),
            rowCount=self.db.getRowCount(tableName), isDate=True, roleColumns=[a.columnName for a in dates],
        )

    def _materializeDimension(self, key, dependents):
        tableName = dimensionTableName(self.factTable, key.columnName)
        keySql = quoteIdentifier(key.columnName)
        attributesSql = "".join(
            f", MAX({quoteIdentifier(d.columnName)}) AS {quoteIdentifier(d.columnName)}" for d in dependents
        )
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {quoteIdentifier(tableName)} AS "
            f"SELECT {keySql}{attributesSql} FROM {quoteIdentifier(self.factTable)} "
            f"WHERE {keySql} IS NOT NULL GROUP BY {keySql} ORDER BY {keySql}"
        )

        key.role = EXTRACTED_KEY
        key.dimensionTable = tableName
        key.reason = (
            f"aucune table a laquelle se lier : extraite dans {tableName}"
            + (f" avec {', '.join(d.columnName for d in dependents)}" if dependents else "")
        )
        for dependent in dependents:
            dependent.role = MOVED_ATTRIBUTE
            dependent.dimensionTable = tableName
            dependent.reason = f"determinee par {key.columnName} : deplacee dans {tableName}"

        return ExtractedDimension(
            tableName=tableName, keyColumn=key.columnName,
            attributeColumns=[d.columnName for d in dependents],
            rowCount=self.db.getRowCount(tableName), roleColumns=[key.columnName],
        )

    def _materializeFact(self):
        tableName = starFactTableName(self.factTable)
        kept = [a.columnName for a in self.assessments if a.role not in (MOVED_ATTRIBUTE, *DROPPED_ROLES)]
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {quoteIdentifier(tableName)} AS "
            f"SELECT {', '.join(quoteIdentifier(c) for c in kept)} FROM {quoteIdentifier(self.factTable)}"
        )
        return tableName
