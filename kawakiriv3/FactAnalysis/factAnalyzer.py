"""Runs the four v3 steps on ONE known fact table:

    classify columns -> link key candidates -> extract the unlinked ones
    into generated dimensions -> verify the grain

and reduces the outcome to a `FactModel` with a verdict.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.FactAnalysis.columnAssessment import DEGENERATE, FOREIGN_KEY, MEASURE, NON_ADDITIVE, TECHNICAL
from kawakiriv3.FactAnalysis.columnClassifier import ColumnClassifier
from kawakiriv3.FactAnalysis.dimensionExtractor import DimensionExtractor
from kawakiriv3.FactAnalysis.factHintCheck import FactHintCheck
from kawakiriv3.FactAnalysis.factLinker import FactLinker
from kawakiriv3.FactAnalysis.grainVerifier import GrainVerifier

OK = "ok"
NEEDS_REVIEW = "needs_review"
REJECTED = "rejected"


@dataclass
class FactModel:
    """The star built around one fact table.

    factTable: the source table the hint designated.
    starFactTable: the rewritten fact in DuckDB (`<fact>__fact`).
    hint: the LLM's (or caller's) justification for choosing this table.
    columnRolesReasoning: the LLM's explanation of the column roles it named, if any.
    verdict: `ok`, `needs_review` (any warning, ambiguous link, or grain not
        unique) or `rejected` (no dimension key at all).
    assembly: the `AssemblyResult` the fact was built from, when no source
        table held it as is (see `FactAssembly/factAssembler.py`).
    remappedKeys: the `RemappedKey`s of its keys remapped to unified
        dimensions by the post-processing (see `StarUnification/keyRemapper.py`);
        `starFactTable` is then the remapped copy `<fact>__fact_unifie`.
    gatheredFacts: the facts this one gathers, when the post-processing
        built it as a union of the facts of several stars (see
        `StarUnification/factGatherer.py`).
    notes: information worth reading that is no reason for a review (a
        grain at the line level, see `GrainVerdict.repeatedWithin`).
    """

    factTable: str
    starFactTable: str
    hint: Optional[str]
    assessments: list
    links: list
    dimensions: list
    grain: object
    warnings: list = field(default_factory=list)
    verdict: str = OK
    columnRolesReasoning: Optional[str] = None
    assembly: Optional[object] = None
    remappedKeys: list = field(default_factory=list)
    gatheredFacts: list = field(default_factory=list)
    notes: list = field(default_factory=list)

    @property
    def measures(self):
        return [a.columnName for a in self.assessments if a.role == MEASURE]

    @property
    def nonAdditiveMeasures(self):
        """Measures never to be summed (a unit price, a rate)."""
        return [a.columnName for a in self.assessments if a.role == MEASURE and a.additivity == NON_ADDITIVE]

    @property
    def degenerateColumns(self):
        return [a.columnName for a in self.assessments if a.role == DEGENERATE]

    @property
    def technicalColumns(self):
        return [a.columnName for a in self.assessments if a.role == TECHNICAL]

    @property
    def foreignKeys(self):
        return [a.columnName for a in self.assessments if a.role == FOREIGN_KEY]


class FactAnalyzer:
    def __init__(
        self, db, schemaEngine, primaryKeyCandidatesByTable, factTable, hint=None, columnRoles=None, assembly=None,
        linkTargets=None, linkVerdicts=None,
    ):
        """`columnRoles`: optional `ColumnRoles` (caller declarations merged
        over the LLM's column hint) -- see `columnClassifier`. `assembly`:
        the `AssemblyResult` of an assembled fact -- its warnings join the
        model's, and the tables folded into it are neither link targets nor
        "finer table" warnings. `linkTargets`: the tables the fact may link
        to (default: every other table). `linkVerdicts`: the physical link
        graph's decisions on this fact's columns, see `FactLinker`."""
        self.linkVerdicts = linkVerdicts
        self.assembly = assembly
        self.linkTargets = linkTargets
        self.db = db
        self.schemaEngine = schemaEngine
        self.primaryKeyCandidatesByTable = primaryKeyCandidatesByTable
        self.factTable = factTable
        self.hint = hint
        self.columnRoles = columnRoles

    def analyze(self):
        profiler = self.schemaEngine.profilers[self.factTable]
        assessments = ColumnClassifier(profiler, self.columnRoles).classify()
        consumed = self.assembly.consumedTables if self.assembly else []
        headers = self.assembly.headerTables if self.assembly else []
        linkTargets = self.linkTargets
        if linkTargets is None and (consumed or headers):
            linkTargets = [t for t in self.schemaEngine.tableNames if t not in consumed and t not in headers]
        links = FactLinker(
            self.schemaEngine, self.primaryKeyCandidatesByTable, self.factTable, assessments, targetTables=linkTargets,
            linkVerdicts=self.linkVerdicts,
        ).link()
        hintWarnings = FactHintCheck(
            self.schemaEngine, self.primaryKeyCandidatesByTable, self.factTable, assessments, links,
            ignoredTables=consumed,
        )
        starFactTable, dimensions = DimensionExtractor(self.db, profiler, self.factTable, assessments).extract()
        grain = GrainVerifier(self.db, starFactTable, assessments).verify()

        # The "no measure" check reads final roles, so it runs after extraction.
        warnings = (list(self.assembly.warnings) if self.assembly else []) + hintWarnings.warnings()
        for link in links:
            if link.ambiguous:
                warnings.append(
                    f"{link.sourceColumn} : lien ambigu, {link.target} retenu parmi "
                    f"{', '.join(link.alternatives)}"
                )
        model = FactModel(
            factTable=self.factTable, starFactTable=starFactTable, hint=self.hint, assessments=assessments,
            links=links, dimensions=dimensions, grain=grain, warnings=warnings,
            columnRolesReasoning=self.columnRoles.reasoning if self.columnRoles else None, assembly=self.assembly,
        )
        return withGrainVerdict(model, grain)


def withGrainVerdict(model, grain):
    """`model` with `grain`, its notes and its verdict: a grain at the line
    level is a note, any other duplicate a warning; no grain at all rejects
    the fact, any warning sends it to review. Shared with the
    post-processing, which rebuilds facts without analysing them again."""
    notes, warnings = _grainMessages(grain, model.factTable)
    model.grain = grain
    model.notes += notes
    model.warnings += warnings
    if not grain.grainColumns:
        model.verdict = REJECTED
    else:
        model.verdict = NEEDS_REVIEW if model.warnings else OK
    return model


def nonGrainWarnings(model):
    """The warnings of `model` that don't come from its grain."""
    grainWarnings = _grainMessages(model.grain, model.factTable)[1]
    return [w for w in model.warnings if w not in grainWarnings]


def _grainMessages(grain, factTable):
    """`(notes, warnings)` the grain verdict gives."""
    if not grain.grainColumns:
        return [], [f"{factTable} : aucune cle de dimension trouvee, pas de grain"]
    if grain.isUnique:
        return [], []
    if grain.repeatedWithin:
        return [
            f"grain a la ligne : {grain.duplicateRows} ligne(s) repetent les memes cles dans un meme "
            f"{grain.repeatedWithin}, distinguees par {grain.completingColumns[0]}"
        ], []
    text = (
        f"grain ({', '.join(grain.grainColumns)}) non unique sur {grain.totalRows} lignes : "
        f"{grain.duplicateRows} ligne(s) en doublon"
    )
    if grain.completingColumns:
        text += f" ; unique en ajoutant la dimension degeneree {grain.completingColumns[0]}"
    return [], [text]
