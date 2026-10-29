"""First step on a known fact table: sorts its columns into row identifiers
(out of the grain), measures, technical columns (excluded), and key
candidates (the grain, once each is linked to an existing table or extracted
into a generated dimension).

Statistical first, from the profiling already computed -- no extra SQL
beyond what `aggProfiling` caches. Roles imposed from outside (`ColumnRoles`:
the caller's declarations, then the LLM's column hint) settle what the
statistics can't: an integer identifier no naming convention flags (JD
Edwards' `RPAN8`), an ETL column with an unusual name. Precedence, per
column: caller > LLM > technical name pattern > statistics; uniqueness
stays a statistical fact whoever calls the column a key.
"""
from __future__ import annotations

import re

from kawakiriv3.FactAnalysis import columnRoles as roles
from kawakiriv3.FactAnalysis.columnAssessment import (
    CONSTANT, DATE, DEGENERATE, DESCRIPTIVE, EMPTY, KEY_CANDIDATE, MEASURE, ROW_IDENTIFIER, SOURCE_NAME_PATTERN,
    SOURCE_STATISTICS, TECHNICAL, ColumnAssessment,
)

# Text whose distinct values cover at least this share of its non-null rows
# is a label/comment, not a dimension key: grouping on it would build a
# dimension with about one row per fact row.
DESCRIPTIVE_DISTINCT_RATIO = 0.9

# Key-like names, snake_case or camelCase, English or French conventions:
# `customer_id`, `id_client`, `code_produit`, `productKey`, `ref_commande`...
_SNAKE_KEY_NAME = re.compile(
    r"(^(id|pk|fk|key|code|ref|num|numero|no)$)"
    r"|(^(id|pk|fk|code|ref|num|numero|cle)_)"
    r"|(_(id|pk|fk|key|code|ref|num|numero|no|cle)$)",
    re.IGNORECASE,
)
# Case-sensitive on purpose: `productId` is a key, `valid` is not.
_CAMEL_KEY_NAME = re.compile(r"(?<=[a-z])(Id|ID|Pk|Fk|Key|Code|Ref|Num)$")

# Load/lineage metadata written by the integration tooling, never business
# data: dbt snapshots, Airbyte/Fivetran/Stitch/dlt audit columns, ingestion
# timestamps, source file names.
_TECHNICAL_NAME = re.compile(
    r"^(dbt_|_?etl_|_airbyte_|_fivetran_|_sdc_|_dlt_)"
    r"|^_?(loaded_at|extracted_at|ingested_at|ingestion_(time|date|ts)|load_(date|time|ts)|"
    r"file_name|source_file|source_file_name)$",
    re.IGNORECASE,
)


def looksLikeKeyName(columnName):
    """True when the column is named like an identifier/code."""
    return bool(_SNAKE_KEY_NAME.search(columnName) or _CAMEL_KEY_NAME.search(columnName))


def looksTechnical(columnName):
    """True when the column is named like integration-tool metadata."""
    return bool(_TECHNICAL_NAME.search(columnName))


class ColumnClassifier:
    """`profiler`: the fact table's `aggProfiling`. `columnRoles`: optional
    `ColumnRoles` imposed from outside the statistics."""

    def __init__(self, profiler, columnRoles=None):
        self.profiler = profiler
        self.columnRoles = columnRoles or roles.ColumnRoles()

    def classify(self):
        """One `ColumnAssessment` per column, in table order."""
        assessments = [self._assess(columnName) for columnName in self.profiler.columns]
        for assessment in assessments:
            assessment.additivity = self.columnRoles.additivity.get(assessment.columnName)
        return assessments

    def _assess(self, columnName):
        profiling = self.profiler.getAggProfiling(columnName)
        sqlType = self.profiler.columnTypes[columnName]
        nonNull = profiling.TotalCount - profiling.NullCount
        imposedRole, imposedSource = self.columnRoles.roleOf(columnName)

        def assessment(role, reason, fallbackRole=None, requiresNameHint=False, source=SOURCE_STATISTICS):
            return ColumnAssessment(
                columnName=columnName, inferredType=profiling.InferredDataType, sqlType=sqlType,
                totalCount=profiling.TotalCount, nullCount=profiling.NullCount,
                distinctCount=profiling.DistinctCount, role=role, fallbackRole=fallbackRole or role,
                requiresNameHint=requiresNameHint, reason=reason, roleSource=source,
            )

        if nonNull == 0:
            return assessment(EMPTY, "aucune valeur non nulle")
        if profiling.DistinctCount <= 1:
            return assessment(CONSTANT, "une seule valeur distincte : n'apporte aucune information")

        if imposedRole == roles.EXCLUDED:
            return assessment(TECHNICAL, f"exclue ({imposedSource}) : colonne technique", source=imposedSource)
        if imposedRole == roles.DEGENERATE:
            return assessment(
                DEGENERATE, f"degeneree ({imposedSource}) : reste dans le fait, hors grain", source=imposedSource,
            )
        if imposedRole is None and looksTechnical(columnName):
            return assessment(
                TECHNICAL, "nommee comme une metadonnee de chargement : exclue", source=SOURCE_NAME_PATTERN,
            )

        isNumeric = profiling.InferredDataType == "numeric"
        if imposedRole == roles.MEASURE and isNumeric:
            return assessment(MEASURE, f"mesure ({imposedSource})", source=imposedSource)
        imposedNote = (
            f" ; designee mesure ({imposedSource}) mais non numerique, ignore"
            if imposedRole == roles.MEASURE else ""
        )

        if profiling.InferredDataType == "date" and self.profiler.isSimpleKeyCandidate(columnName):
            # An event log stamps every row with its own timestamp: still a
            # date, joined to the calendar by its date part.
            return assessment(
                DATE, "date unique sur chaque ligne (horodatage d'evenement) : cle de dimension temporelle"
                + imposedNote,
            )
        if self.profiler.isSimpleKeyCandidate(columnName):
            designatedKey = (
                f" ; designee cle ({imposedSource}) mais unique par ligne" if imposedRole == roles.KEY else ""
            )
            return assessment(
                ROW_IDENTIFIER,
                "unique sur chaque ligne du fait : identifiant de ligne, hors grain" + designatedKey + imposedNote,
                fallbackRole=DEGENERATE, requiresNameHint=True,
            )

        if imposedRole == roles.KEY:
            role = DATE if profiling.InferredDataType == "date" else KEY_CANDIDATE
            return assessment(role, f"cle de dimension ({imposedSource})", source=imposedSource)

        if self.profiler.isContinuousNumeric(columnName):
            return assessment(MEASURE, "numerique continu (decimal/flottant) : mesure")
        if profiling.InferredDataType == "date":
            return assessment(DATE, "date : cle de dimension temporelle" + imposedNote)
        if isNumeric:
            if looksLikeKeyName(columnName):
                return assessment(KEY_CANDIDATE, "entier nomme comme une cle")
            return assessment(
                KEY_CANDIDATE,
                "entier non nomme comme une cle : cle seulement si un lien corrobore par le nom existe, "
                "mesure sinon",
                fallbackRole=MEASURE, requiresNameHint=True,
            )
        if profiling.InferredDataType == "string" and profiling.DistinctCount / nonNull >= DESCRIPTIVE_DISTINCT_RATIO:
            return assessment(
                DESCRIPTIVE,
                f"texte quasi unique (>= {DESCRIPTIVE_DISTINCT_RATIO:.0%} de valeurs distinctes) : libelle, "
                "hors grain sauf s'il reference une autre table" + imposedNote,
                fallbackRole=DEGENERATE,
            )
        return assessment(KEY_CANDIDATE, "valeur discrete qui se repete : cle de dimension" + imposedNote)
