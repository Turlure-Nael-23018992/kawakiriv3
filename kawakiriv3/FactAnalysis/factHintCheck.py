"""Deterministic sanity checks on a fact table chosen by the LLM hint (or
imposed by the caller). Never rejects the hint: each check that fails adds a
warning, and any warning sends the model to human review.
"""
from __future__ import annotations

import re

from kawakiriv3.FactAnalysis.columnAssessment import CONSTANT, EMPTY, MEASURE, ROW_IDENTIFIER
from kawakiriv3.SourceJoin.sourceJoinEngine import SourceJoinEngine

# A dbt snapshot's own validity window (or any hand-rolled equivalent):
# present as a FROM/TO pair, it means the table can hold several versions of
# the same business entity side by side -- see `_scdHistoryNotFiltered`.
_VALIDITY_WINDOW = re.compile(r"(^|_)valid_(from|to)$", re.IGNORECASE)
_VALIDITY_TO = re.compile(r"(^|_)valid_to$", re.IGNORECASE)


class FactHintCheck:
    def __init__(self, schemaEngine, primaryKeyCandidatesByTable, factTable, assessments, links, ignoredTables=()):
        """`ignoredTables`: tables never reported as referencing the fact --
        for an assembled fact, the tables already folded into it."""
        self.ignoredTables = set(ignoredTables)
        self.schemaEngine = schemaEngine
        self.primaryKeyCandidatesByTable = primaryKeyCandidatesByTable
        self.factTable = factTable
        self.assessments = assessments
        self.links = links

    def warnings(self):
        """French, human-readable warnings, in check order."""
        return [
            *self._noMeasure(), *self._finerTableReferencesFact(), *self._largerLinkedDimensions(),
            *self._scdHistoryNotFiltered(),
        ]

    def _rowCount(self, tableName):
        profiler = self.schemaEngine.profilers[tableName]
        return profiler.getAggProfiling(profiler.columns[0]).TotalCount if profiler.columns else 0

    def _noMeasure(self):
        if any(a.role == MEASURE for a in self.assessments):
            return []
        return [
            f"{self.factTable} n'a aucune mesure : table de faits sans fait (factless) ou table "
            "de dimension choisie a tort"
        ]

    def _finerTableReferencesFact(self):
        """Header/lines pattern: another table holding more rows that points
        at one of the fact's own row identifiers means the grain asked for
        is probably that table's (e.g. `lignes_commande` -> `commandes`)."""
        rowIdentifiers = [a.columnName for a in self.assessments if a.initialRole == ROW_IDENTIFIER]
        others = [t for t in self.schemaEngine.tableNames if t != self.factTable and t not in self.ignoredTables]
        if not rowIdentifiers or not others:
            return []

        engine = SourceJoinEngine(
            self.schemaEngine, {self.factTable: [
                c for c in self.primaryKeyCandidatesByTable.get(self.factTable) or []
                if not c.isComposite and c.columnNames[0] in rowIdentifiers
            ]},
            sourceColumnsByTable={t: self.schemaEngine.profilers[t].columns for t in others},
            targetTables=[self.factTable],
        )
        factRows = self._rowCount(self.factTable)
        warnings = []
        for candidate in engine.inferSourceJoins():
            if not candidate.nameHint:
                continue
            sourceRows = self._rowCount(candidate.sourceTable)
            if sourceRows > factRows:
                warnings.append(
                    f"{candidate.sourceTable}.{candidate.sourceColumn} ({sourceRows} lignes) reference "
                    f"{self.factTable}.{candidate.targetColumn} ({factRows} lignes) : le grain demande est "
                    f"peut-etre celui de {candidate.sourceTable}"
                )
        return warnings

    def _largerLinkedDimensions(self):
        factRows = self._rowCount(self.factTable)
        return [
            f"la dimension {link.targetTable} ({self._rowCount(link.targetTable)} lignes) a plus de lignes "
            f"que la table de faits ({factRows}) : role fait/dimension a verifier"
            for link in self.links if self._rowCount(link.targetTable) > factRows
        ]

    def _scdHistoryNotFiltered(self):
        """A `*_valid_from`/`*_valid_to` pair (dbt snapshot convention, or a
        hand-rolled equivalent) means the fact may hold several versions of
        the same business entity (a plan change, an address update...) side
        by side. The grain can come out unique anyway -- the versions differ
        on other columns too -- which hides the problem instead of raising
        it: a measure summed without filtering to the current version (e.g.
        `WHERE *_valid_to IS NULL`) is counted once per version. Silenced
        once every `*_valid_to` has collapsed to a single value (typically
        all-NULL): that's what a caller's `rowFilters` on it looks like once
        applied, and with no more than one state left there is nothing left
        to warn about. This check never knows which column names the
        entity nor proves duplication otherwise; it only reports that the
        pattern is present and still varies, same as every other check
        here."""
        validityColumns = sorted(a.columnName for a in self.assessments if _VALIDITY_WINDOW.search(a.columnName))
        if len(validityColumns) < 2:
            return []
        validToColumns = [a for a in self.assessments if _VALIDITY_TO.search(a.columnName)]
        if validToColumns and all(a.role in (EMPTY, CONSTANT) for a in validToColumns):
            return []
        return [
            f"{self.factTable} porte une fenetre de validite technique ({', '.join(validityColumns)}) : "
            "probablement un snapshot historise. Si la version courante n'est pas filtree a la source (ou via "
            "rowFilters), une meme entite peut apparaitre en plusieurs lignes et ses mesures etre comptees "
            "plusieurs fois."
        ]
