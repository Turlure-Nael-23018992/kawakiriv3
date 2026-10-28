"""Defines `SchemaEngine`, which wires up per-table profiling/identifiability/
primary-key/functional-group engines across a whole set of tables at once.

Adapted from kawakiriv2: the logical-table split and the LLM veto on
functional groups are gone -- v3 extracts dimensions out of the fact table
itself (see `FactAnalysis/dimensionExtractor.py`).
"""
from kawakiriv3.FunctionalGroup.functionalGroupEngine import FunctionalGroupEngine
from kawakiriv3.Identifiability.Identifiability import Identifiability
from kawakiriv3.PrimaryKey.primaryKeyEngine import PrimaryKeyEngine
from kawakiriv3.Profiling.aggProfiling import aggProfiling


class SchemaEngine:
    """Builds and holds one `aggProfiling` + `Identifiability` +
    `PrimaryKeyEngine` + `FunctionalGroupEngine` per table of a DuckDb
    database, so that multi-table operations (like `SourceJoinEngine`) can
    look across all tables at once."""

    def __init__(self, db, tableNames, previous=None, declaredKeyColumnsByTable=None):
        """db: a duckDbUtils instance backing every table's `aggProfiling`.

        `previous`: optional, an earlier `SchemaEngine` built against the
        same `db`. When set, `build()` seeds each fresh `aggProfiling` from
        `previous.profilers[tableName]`'s own cache (see
        `aggProfiling.reuseCacheFrom`), so rebuilding after new tables were
        materialized never reprofiles the tables that didn't change.

        `declaredKeyColumnsByTable`: optional `{tableName: columnNames}`,
        the keys the source system declared -- forwarded to each table's
        `PrimaryKeyEngine` as `declaredKeyColumns` (imposed, see its
        docstring). A declaration naming a column the table doesn't have is
        ignored."""
        self.db = db
        self.tableNames = list(tableNames)
        self.declaredKeyColumnsByTable = declaredKeyColumnsByTable or {}
        self.previous = previous
        self.profilers = {}
        self.identifiabilities = {}
        self.primaryKeyEngines = {}
        self.functionalGroupEngines = {}

    def build(self):
        """Instantiates and wires up the four per-table engines for every
        table in `self.tableNames`. Returns `self` to allow chaining, e.g.
        `SchemaEngine(db, tables).build()`."""
        for tableName in self.tableNames:
            profiler = aggProfiling(self.db, tableName)
            if self.previous is not None:
                profiler.reuseCacheFrom(self.previous.profilers.get(tableName))
            identifiability = Identifiability(profiler)
            declaredKeyColumns = self.declaredKeyColumnsByTable.get(tableName)
            if declaredKeyColumns and not set(declaredKeyColumns) <= set(profiler.columns):
                declaredKeyColumns = None
            primaryKeyEngine = PrimaryKeyEngine(profiler, identifiability, declaredKeyColumns=declaredKeyColumns)
            functionalGroupEngine = FunctionalGroupEngine(profiler, identifiability)

            self.profilers[tableName] = profiler
            self.identifiabilities[tableName] = identifiability
            self.primaryKeyEngines[tableName] = primaryKeyEngine
            self.functionalGroupEngines[tableName] = functionalGroupEngine

        return self

    def inferAllPrimaryKeyCandidates(self):
        """{ tableName: [primaryKeyCandidate, ...] } for every table at once."""
        return {
            tableName: engine.inferSourceKeyCandidates()
            for tableName, engine in self.primaryKeyEngines.items()
        }

    def inferAllFunctionalGroups(self):
        """{ tableName: [FunctionalGroupCandidate, ...] } for every table at once."""
        return {
            tableName: engine.inferFunctionalGroups()
            for tableName, engine in self.functionalGroupEngines.items()
        }
