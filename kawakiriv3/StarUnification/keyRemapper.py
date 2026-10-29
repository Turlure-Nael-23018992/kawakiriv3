"""Replaces, in each star fact, the keys pointing to a dimension folded into
a unified dimension by the unified id.

A unified dimension keeps ONE row per identity, so a fact's original key
can't be joined to it directly (two customers merged into one identity keep
only one of their keys). The unified id is looked up in
`<unified>__correspondance` on the pair (origin label of the fact's
dimension, key value):

    web_sales__fact.customer_id -> web_customers.id     (the star's link)
    web_customers is the "web" source of clients_unifies
    => web_sales__fact_unifie.client_unifie_id = the correspondence row with
       origine = 'web' AND <key> = web_sales__fact.customer_id

What a fact gets: `<fact>__fact_unifie`, the star fact's rows (same count,
same order), each remapped key replaced in place by its unified id. A key
with no correspondence gets a null id (the unknown member) and a counted
warning. The `FactModel` is copied, never analysed again: only the remapped
key, its link and the dimension it left change; the grain is verified again
on the same roles (two merged customers can make two rows equal).
"""
from __future__ import annotations

import copy
import dataclasses
from dataclasses import dataclass

from kawakiriv3.FactAnalysis.columnAssessment import FOREIGN_KEY, MOVED_ATTRIBUTE, SOURCE_UNIFICATION
from kawakiriv3.FactAnalysis.factAnalyzer import nonGrainWarnings, withGrainVerdict
from kawakiriv3.FactAnalysis.factLinker import ForeignKeyLink
from kawakiriv3.FactAnalysis.grainVerifier import GrainVerifier
from kawakiriv3.Sources.sourceConnector import quoteIdentifier as q

REMAPPED_TABLE_SUFFIX = "_unifie"


@dataclass
class RemappedKey:
    """`column` of `fact`'s star, pointing to `dimension` (the source
    `label` of `unified`), replaced by `newColumn`."""

    fact: str
    column: str
    newColumn: str
    dimension: str
    unified: str
    idColumn: str
    label: str
    resolvedRows: int = 0
    orphanRows: int = 0
    ambiguousKeys: int = 0


def _text(value):
    return "'" + str(value).replace("'", "''") + "'"


class KeyRemapper:
    """`unifiedDimensions`: the built `UnifiedDimension`s."""

    def __init__(self, db, catalog, unifiedDimensions):
        self.db = db
        self.catalog = catalog
        self.byDimension = {
            source["dimension"]: unified for unified in unifiedDimensions for source in unified.sources
        }

    def remap(self, star):
        """The star's `FactModel` with its keys remapped, or None when no
        key of the fact points to a unified dimension."""
        keys = [column for column in star.columns.values() if column.dimension in self.byDimension]
        if not keys:
            return None
        model = star.model
        perUnified = {}
        for column in keys:
            perUnified.setdefault(self.byDimension[column.dimension].name, []).append(column)
        factColumns = self.db.getColumns(model.starFactTable)

        remapped, lookups, joins, replaced = [], [], {}, {}
        for index, column in enumerate(keys):
            unified = self.byDimension[column.dimension]
            newColumn = unified.idColumn
            if len(perUnified[unified.name]) > 1 or newColumn in factColumns:
                newColumn = f"{column.name}_{unified.idColumn}"
            label = unified.labelOf(column.dimension)
            lookups.append(
                f"lk{index}_all AS (SELECT CAST(m.{q(unified.keyTarget)} AS VARCHAR) AS cle, "
                f"MIN(m.{q(unified.idColumn)}) AS uid, COUNT(DISTINCT m.{q(unified.idColumn)}) AS n "
                f"FROM {q(unified.mappingTable)} m WHERE CAST(m.{q(unified.labelColumn)} AS VARCHAR) = {_text(label)} "
                f"AND m.{q(unified.keyTarget)} IS NOT NULL GROUP BY 1), "
                f"lk{index} AS (SELECT cle, uid FROM lk{index}_all WHERE n = 1)"
            )
            joins[column.name] = f"LEFT JOIN lk{index} ON lk{index}.cle = CAST(f.{q(column.name)} AS VARCHAR)"
            replaced[column.name] = (index, newColumn)
            remapped.append(RemappedKey(
                fact=star.name, column=column.name, newColumn=newColumn, dimension=column.dimension,
                unified=unified.name, idColumn=unified.idColumn, label=label,
            ))

        withSql = f"WITH {', '.join(lookups)}"
        fromSql = f"FROM {q(model.starFactTable)} f {' '.join(joins.values())}"
        for key, column in zip(remapped, keys):
            index = replaced[column.name][0]
            key.resolvedRows, key.orphanRows, key.ambiguousKeys = self.db.query(
                f"{withSql} SELECT COUNT(lk{index}.uid), "
                f"COUNT(*) FILTER (WHERE f.{q(column.name)} IS NOT NULL AND lk{index}.uid IS NULL), "
                f"(SELECT COUNT(*) FROM lk{index}_all WHERE n > 1) {fromSql}"
            )[0]

        selects = [
            f"lk{replaced[c][0]}.uid AS {q(replaced[c][1])}" if c in replaced else f"f.{q(c)}" for c in factColumns
        ]
        tableName = f"{model.starFactTable}{REMAPPED_TABLE_SUFFIX}"
        self.db.conn.execute(
            f"CREATE OR REPLACE TABLE {q(tableName)} AS {withSql} SELECT {', '.join(selects)} {fromSql} ORDER BY f.rowid"
        )
        return self._copyModel(model, tableName, remapped)

    def _copyModel(self, model, tableName, remapped):
        byColumn = {key.column: key for key in remapped}
        totalRows = self.db.getRowCount(tableName)
        folded = {key.dimension: key for key in remapped}
        assessments, links = [], []
        for a in model.assessments:
            a = copy.copy(a)
            key = byColumn.get(a.columnName)
            if key is not None:
                unified = self.byDimension[key.dimension]
                a.columnName, a.role, a.roleSource = key.newColumn, FOREIGN_KEY, SOURCE_UNIFICATION
                a.linkedTo, a.dimensionTable = f"{unified.name}.{unified.idColumn}", None
                a.nullCount = totalRows - key.resolvedRows
                a.distinctCount = self.db.query(f"SELECT COUNT(DISTINCT {q(key.newColumn)}) FROM {q(tableName)}")[0][0]
                a.reason = (
                    f"{key.column} remplacee par l'id unifie de {unified.name} (source {key.label} : "
                    f"{key.dimension}) : {key.resolvedRows} ligne(s) rattachee(s), {key.orphanRows} orpheline(s)"
                )
            elif a.role == MOVED_ATTRIBUTE and a.dimensionTable in folded:
                unifiedName = folded[a.dimensionTable].unified
                a.reason = f"attribut de {a.dimensionTable}, porte par la dimension unifiee {unifiedName}"
                a.dimensionTable = unifiedName
            assessments.append(a)
        for link in model.links:
            key = byColumn.get(link.sourceColumn)
            links.append(link if key is None else ForeignKeyLink(
                sourceColumn=key.newColumn, targetTable=key.unified, targetColumn=key.idColumn,
                joinSuccessRatio=key.resolvedRows / totalRows if totalRows else 0.0, nameHint=False, ambiguous=False,
            ))
        for key in remapped:
            if key.column not in {link.sourceColumn for link in model.links}:
                # An extracted key: it had no link, its dimension is folded now.
                links.append(ForeignKeyLink(
                    sourceColumn=key.newColumn, targetTable=key.unified, targetColumn=key.idColumn,
                    joinSuccessRatio=key.resolvedRows / totalRows if totalRows else 0.0, nameHint=False,
                    ambiguous=False,
                ))

        warnings = [
            w for w in nonGrainWarnings(model) if not any(w.startswith(f"{key.column} : lien ambigu") for key in remapped)
        ]
        for key in remapped:
            if key.orphanRows:
                warnings.append(
                    f"{key.column} : {key.orphanRows} ligne(s) sans correspondance dans {key.unified} (membre inconnu)"
                )
            if key.ambiguousKeys:
                warnings.append(
                    f"{key.column} : {key.ambiguousKeys} valeur(s) rattachee(s) a plusieurs identites de "
                    f"{key.unified}, non remplacee(s)"
                )
        copied = dataclasses.replace(
            model, starFactTable=tableName, assessments=assessments, links=links,
            dimensions=[d for d in model.dimensions if d.tableName not in folded],
            warnings=warnings, notes=[], remappedKeys=remapped,
        )
        return withGrainVerdict(copied, GrainVerifier(self.db, tableName, assessments).verify())
