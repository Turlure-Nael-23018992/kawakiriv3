"""The physical links between every table of the run: which column's values
are found in which other table's key (`SourceJoinEngine`, repris de la v2),
with the link's direction and cardinality.

Unlike `FactLinker`, which only links OUT of a known fact table, this tests
every column of every table: the LLM is shown the whole graph to tell a
fact (many outgoing N:1 links, measures) from a dimension, and
`FactAssembler` follows it to bring measures onto a grain.

Each candidate link gets a decision, in three stages:

1. **Deterministic** -- most links, no LLM call:
   - accepted when the names corroborate it (`commande_id ->
     commandes.commande_id`), or, for a 1:1 link, when the key is covered
     in BOTH directions and isn't a dense integer range (a shared key:
     `tarifs.ligne_id <-> lignes.ligne_id`);
   - rejected when another target of the same column has corroborating
     names, or when a 1:1 link is covered in one direction only -- two
     sequential primary keys overlapping by chance (`clients.client_id`
     1..300 inside `lignes.ligne_id` 1..7403: 4% the other way);
   - names only corroborate through a meaningful concept: a generic name
     (`id`, `ref`, `code`...) shared by two tables proves nothing, unless
     the source names the target table (`client_id -> clients.id`);
2. **Doubtful** (`pending`): a strong overlap with unrelated names
   (JD Edwards' `RPAN8 -> F0101.ABAN8`), corroborating names with a weaker
   overlap (80 to 95%), or a 1:1 link between two dense integer ranges
   (1..N on both sides: shared key or two sequences of the same length).
   The cap is 60 links per run. Resolved by `resolve(...)`: a cached verdict,
   the LLM's (one batched call, see `FactHint/factHintAdvisor.py`), or
   rejected when nobody arbitrates;
3. **The caller** has the last word: a declared link is always accepted, a
   forbidden one always rejected.

Decimal columns are never sources (measures), and a table never links to
itself (hierarchies are out of scope).
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from typing import Optional

from kawakiriv3.SourceJoin.sourceJoinEngine import SourceJoinEngine, _concept, _tableConcept

ONE_TO_ONE = "1:1"
MANY_TO_ONE = "N:1"
MAX_PATHS = 10

STRONG_COVERAGE = 0.95
WEAK_COVERAGE = 0.80
# A 1:1 link covered less than this the other way is a coincidence, not a doubt.
COINCIDENTAL_REVERSE_COVERAGE = 0.50
MAX_PENDING_LINKS = 60

ACCEPTED = "accepte"
REJECTED = "refuse"
PENDING = "douteux"

DECIDED_BY_NAMES = "noms"
DECIDED_BY_STATISTICS = "statistiques"
DECIDED_BY_LLM = "LLM"
DECIDED_BY_CACHE = "LLM (cache)"
DECIDED_BY_CALLER = "appelant"

# Names any table may give its key: sharing one proves nothing.
GENERIC_CONCEPTS = {"id", "ref", "code", "key", "pk", "fk", "num", "no", "nr", "uuid", "guid", "cle", "numero"}


def meaningfulNameHint(sourceColumn, targetTable, targetColumn):
    """The source column names the target table, or shares a non-generic
    concept with the target column."""
    concept = _concept(sourceColumn)
    if concept and concept == _tableConcept(targetTable):
        return True
    return concept == _concept(targetColumn) and concept not in GENERIC_CONCEPTS


@dataclass(frozen=True)
class PhysicalLink:
    """`sourceTable.sourceColumn` values found in `targetTable.targetColumn`,
    a key candidate of the target. `oneToOne`: the source column is unique
    in its own table too -- each target row is then matched by one source
    row at most. `coverage`: share of non-null source values found.
    `reverseCoverage`: share of the target's values found in the source
    (measured for 1:1 links only)."""

    sourceTable: str
    sourceColumn: str
    targetTable: str
    targetColumn: str
    coverage: float
    nameHint: bool
    oneToOne: bool
    reverseCoverage: Optional[float] = None

    @property
    def cardinality(self):
        return ONE_TO_ONE if self.oneToOne else MANY_TO_ONE

    @property
    def key(self):
        return f"{self.sourceTable}.{self.sourceColumn}->{self.targetTable}.{self.targetColumn}"

    def describe(self):
        return f"{self.sourceTable}.{self.sourceColumn} -> {self.targetTable}.{self.targetColumn} ({self.cardinality})"


@dataclass
class LinkDecision:
    """`linkId`: set on a doubtful link, the id the LLM answers with."""

    link: PhysicalLink
    status: str
    decidedBy: str
    reason: str
    linkId: Optional[str] = None


def parseLinkKey(text):
    """`"table.column -> table.column"` (spaces optional) ->
    `(sourceTable, sourceColumn, targetTable, targetColumn)`."""
    source, target = (part.strip() for part in text.split("->"))
    sourceTable, sourceColumn = source.rsplit(".", 1)
    targetTable, targetColumn = target.rsplit(".", 1)
    return sourceTable, sourceColumn, targetTable, targetColumn


class LinkGraph:
    def __init__(self, decisions):
        self.decisions = list(decisions)

    @property
    def links(self):
        return [d.link for d in self.decisions if d.status == ACCEPTED]

    @property
    def pending(self):
        return [d for d in self.decisions if d.status == PENDING]

    @classmethod
    def build(cls, schemaEngine, primaryKeyCandidatesByTable, declaredLinks=(), forbiddenLinks=()):
        """`declaredLinks`/`forbiddenLinks`: `"table.column -> table.column"`
        strings from the job. A declared link is tested even below the
        overlap threshold, and accepted whatever its statistics."""
        declared = {parseLinkKey(text) for text in declaredLinks or ()}
        forbidden = {parseLinkKey(text) for text in forbiddenLinks or ()}
        sourceColumnsByTable = {
            tableName: [c for c in profiler.columns if not profiler.isContinuousNumeric(c)]
            for tableName, profiler in schemaEngine.profilers.items()
        }
        engine = SourceJoinEngine(
            schemaEngine, primaryKeyCandidatesByTable, declaredForeignKeys=sorted(declared),
            sourceColumnsByTable=sourceColumnsByTable,
        )

        candidatesByColumn = {}
        for candidate in engine.inferSourceJoins(threshold=WEAK_COVERAGE):
            if candidate.sourceTable == candidate.targetTable:
                continue
            candidatesByColumn.setdefault((candidate.sourceTable, candidate.sourceColumn), []).append(candidate)

        builder = cls([])
        builder.schemaEngine = schemaEngine
        for (sourceTable, sourceColumn), candidates in sorted(candidatesByColumn.items()):
            oneToOne = schemaEngine.profilers[sourceTable].isSimpleKeyCandidate(sourceColumn)
            named = {
                (c.targetTable, c.targetColumn): meaningfulNameHint(sourceColumn, c.targetTable, c.targetColumn)
                for c in candidates
            }
            namedTargetExists = any(
                named[(c.targetTable, c.targetColumn)] and c.joinSuccessRatio >= STRONG_COVERAGE for c in candidates
            )
            for c in sorted(candidates, key=lambda c: (c.targetTable, c.targetColumn)):
                identity = (c.sourceTable, c.sourceColumn, c.targetTable, c.targetColumn)
                link = PhysicalLink(
                    sourceTable=sourceTable, sourceColumn=sourceColumn, targetTable=c.targetTable,
                    targetColumn=c.targetColumn, coverage=c.joinSuccessRatio,
                    nameHint=named[(c.targetTable, c.targetColumn)], oneToOne=oneToOne,
                )
                if identity in forbidden:
                    decision = LinkDecision(link, REJECTED, DECIDED_BY_CALLER, "interdit par le job")
                elif identity in declared:
                    decision = LinkDecision(link, ACCEPTED, DECIDED_BY_CALLER, "declare par le job")
                else:
                    decision = builder._decide(link, namedTargetExists)
                if decision is not None:
                    builder.decisions.append(decision)

        pending = builder.pending
        for index, decision in enumerate(sorted(pending, key=lambda d: -d.link.coverage)):
            if index < MAX_PENDING_LINKS:
                decision.linkId = f"L{index + 1}"
            else:
                decision.status, decision.decidedBy = REJECTED, DECIDED_BY_STATISTICS
                decision.reason += f" ; au-dela des {MAX_PENDING_LINKS} liens douteux soumis a l'arbitrage"
        return builder

    def _decide(self, link, namedTargetExists):
        """The deterministic stage. None: not worth reporting (a weak
        overlap without names -- noise)."""
        if link.coverage < STRONG_COVERAGE:
            if not link.nameHint:
                return None
            return LinkDecision(link, PENDING, DECIDED_BY_STATISTICS, "noms concordants mais couverture faible")
        if link.nameHint:
            return LinkDecision(link, ACCEPTED, DECIDED_BY_NAMES, "noms concordants")
        if namedTargetExists:
            return LinkDecision(
                link, REJECTED, DECIDED_BY_NAMES, "une autre cible de la meme colonne a un nom concordant",
            )
        if link.oneToOne:
            link = self._withReverseCoverage(link)
            if link.reverseCoverage >= STRONG_COVERAGE:
                if self._isDenseIntegerRange(link.sourceTable, link.sourceColumn):
                    return LinkDecision(
                        link, PENDING, DECIDED_BY_STATISTICS,
                        "couverte dans les deux sens, mais plage d'entiers dense : cle partagee ou deux "
                        "sequences de meme longueur",
                    )
                return LinkDecision(link, ACCEPTED, DECIDED_BY_STATISTICS, "cle partagee, couverte dans les deux sens")
            if link.reverseCoverage < COINCIDENTAL_REVERSE_COVERAGE:
                return LinkDecision(
                    link, REJECTED, DECIDED_BY_STATISTICS,
                    f"cle unique couverte dans un seul sens ({link.reverseCoverage:.0%} dans l'autre) : "
                    "coincidence entre deux cles",
                )
            return LinkDecision(link, PENDING, DECIDED_BY_STATISTICS, "noms discordants, cle partiellement partagee")
        return LinkDecision(link, PENDING, DECIDED_BY_STATISTICS, "noms discordants")

    def _isDenseIntegerRange(self, tableName, columnName):
        """Every integer between the min and the max is present: what any
        sequence (1..N) looks like."""
        try:
            low, high, distinct = self.schemaEngine.db.conn.execute(
                f'SELECT MIN(TRY_CAST("{columnName}" AS BIGINT)), MAX(TRY_CAST("{columnName}" AS BIGINT)), '
                f'COUNT(DISTINCT TRY_CAST("{columnName}" AS BIGINT)) FROM "{tableName}"'
            ).fetchone()
        except Exception:
            return False
        return low is not None and distinct > 1 and high - low + 1 == distinct

    def _withReverseCoverage(self, link):
        """Share of the target's distinct values found in the source,
        compared as text (the two columns may have different physical
        types)."""
        source = f'CAST("{link.sourceColumn}" AS VARCHAR)'
        target = f'CAST("{link.targetColumn}" AS VARCHAR)'
        matched, total = self.schemaEngine.db.conn.execute(
            f'SELECT COUNT(DISTINCT {target}) FILTER (WHERE {target} IN '
            f'(SELECT {source} FROM "{link.sourceTable}" WHERE "{link.sourceColumn}" IS NOT NULL)), '
            f'COUNT(DISTINCT {target}) FROM "{link.targetTable}"'
        ).fetchone()
        reverse = matched / total if total else 0.0
        return PhysicalLink(**{**link.__dict__, "reverseCoverage": reverse})

    def resolve(self, verdicts, decidedBy=DECIDED_BY_LLM):
        """Applies `{linkId: (accepted, justification)}` to the doubtful
        links. Returns how many were resolved."""
        resolved = 0
        for decision in self.pending:
            if decision.linkId in verdicts:
                accepted, justification = verdicts[decision.linkId]
                decision.status = ACCEPTED if accepted else REJECTED
                decision.decidedBy = decidedBy
                decision.reason = justification
                resolved += 1
        return resolved

    def rejectPending(self, reason):
        for decision in self.pending:
            decision.status, decision.decidedBy = REJECTED, DECIDED_BY_STATISTICS
            decision.reason = f"{decision.reason} ; {reason}"

    def pendingSummary(self):
        """What the LLM is shown to arbitrate: statistics only."""
        return [
            {
                "linkId": d.linkId, "sourceTable": d.link.sourceTable, "sourceColumn": d.link.sourceColumn,
                "targetTable": d.link.targetTable, "targetColumn": d.link.targetColumn,
                "cardinality": d.link.cardinality, "coverage": round(d.link.coverage, 4),
                "reverseCoverage": None if d.link.reverseCoverage is None else round(d.link.reverseCoverage, 4),
                "nameHint": d.link.nameHint, "reason": d.reason,
            }
            for d in self.pending
        ]

    def verdictsFor(self, columnOrigins):
        """`{column: {"accepted": {(table, column): decidedBy}, "vetoed":
        {(table, column)}}}` for a fact table, `columnOrigins` mapping each
        of its columns to the source `(table, column)` it comes from. Vetoed:
        rejected by the LLM or the caller -- a statistical rejection is left
        to the linker's own rules."""
        verdicts = {}
        for column, (table, sourceColumn) in columnOrigins.items():
            for decision in self.decisions:
                link = decision.link
                if (link.sourceTable, link.sourceColumn) != (table, sourceColumn):
                    continue
                entry = verdicts.setdefault(column, {"accepted": {}, "vetoed": set()})
                if decision.status == ACCEPTED:
                    entry["accepted"][(link.targetTable, link.targetColumn)] = decision.decidedBy
                elif decision.decidedBy in (DECIDED_BY_LLM, DECIDED_BY_CACHE, DECIDED_BY_CALLER):
                    entry["vetoed"].add((link.targetTable, link.targetColumn))
        return verdicts

    def linksFrom(self, tableName):
        return [link for link in self.links if link.sourceTable == tableName]

    def shortestPaths(self, startTable):
        """`{table: [path, ...]}`: every shortest path (a tuple of accepted
        links, each one followed from its source to its target) from
        `startTable` to each table it reaches. Following links only ever
        maps a row to ONE row of the next table, so a path is a function
        from `startTable`'s rows -- never a fan-out."""
        bySource = {}
        for link in self.links:
            bySource.setdefault(link.sourceTable, []).append(link)
        paths = {startTable: [()]}
        depth = {startTable: 0}
        queue = deque([startTable])
        while queue:
            table = queue.popleft()
            for link in bySource.get(table, []):
                target = link.targetTable
                if target in depth and depth[target] < depth[table] + 1:
                    continue
                newPaths = [path + (link,) for path in paths[table]]
                if target not in depth:
                    depth[target] = depth[table] + 1
                    paths[target] = []
                    queue.append(target)
                paths[target] = (paths[target] + newPaths)[:MAX_PATHS]
        del paths[startTable]
        return paths

    def toSummary(self):
        """The accepted links, as the fact-table hint is shown them."""
        return [
            {
                "sourceTable": l.sourceTable, "sourceColumn": l.sourceColumn,
                "targetTable": l.targetTable, "targetColumn": l.targetColumn,
                "cardinality": l.cardinality, "coverage": round(l.coverage, 4), "nameHint": l.nameHint,
            }
            for l in self.links
        ]

    def decisionsSummary(self):
        return [
            {
                "link": d.link.describe(), "status": d.status, "decidedBy": d.decidedBy, "reason": d.reason,
                "coverage": round(d.link.coverage, 4),
                "reverseCoverage": None if d.link.reverseCoverage is None else round(d.link.reverseCoverage, 4),
                "linkId": d.linkId,
            }
            for d in self.decisions
        ]
