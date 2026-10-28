import pandas as pd

# Type families DuckDb compares natively within, but not across (e.g. an
# INTEGER against a VARCHAR raises a ConversionException on the first
# non-numeric value). See `duckDbUtils.joinCondition`.
_TEXT_TYPE_PREFIXES = ("VARCHAR", "TEXT", "STRING", "CHAR", "UUID")
_TEMPORAL_TYPE_PREFIXES = ("DATE", "TIMESTAMP", "TIME", "INTERVAL")
_NUMERIC_TYPE_PREFIXES = (
    "TINYINT", "SMALLINT", "INTEGER", "BIGINT", "HUGEINT", "UTINYINT", "USMALLINT", "UINTEGER", "UBIGINT",
    "UHUGEINT", "FLOAT", "DOUBLE", "DECIMAL", "REAL",
)


def _typeFamily(sqlType):
    upper = sqlType.upper()
    for family, prefixes in (
        ("text", _TEXT_TYPE_PREFIXES), ("temporal", _TEMPORAL_TYPE_PREFIXES), ("numeric", _NUMERIC_TYPE_PREFIXES),
    ):
        if upper.startswith(prefixes):
            return family
    return upper


class duckDbUtils:
    """Loads tables (from a live source or a JsonManager payload) into
    DuckDb and exposes them for querying, either through the small helpers
    below or arbitrary SQL via `query`.
    """

    def __init__(self, conn):
        self.conn = conn

    def loadTables(self, tables, replace=True):
        """tables: {tableName: {columnName: [values, ...]}} — the same
        column-oriented shape produced by `dbUtils.exportToJson` /
        `JsonManager.tables`."""
        for tableName, columns in tables.items():
            self.loadTable(tableName, columns, replace=replace)

    def loadTable(self, tableName, columns, replace=True):
        """Build a pandas DataFrame from the column dict and let DuckDb's
        pandas replacement scan create the table in one shot — this is what
        gives DuckDb proper column types (BIGINT, DOUBLE, VARCHAR, ...)
        instead of everything staying an untyped Python object."""
        self._materializeDataFrame(tableName, pd.DataFrame(columns), replace)

    def loadDataFrame(self, tableName, df, replace=True):
        """Materializes a pandas DataFrame as a DuckDb table -- what every
        `Sources` connector without a native DuckDb reader goes through."""
        self._materializeDataFrame(tableName, df, replace)

    def saveRecords(self, tableName, records, replace=True):
        """records: a list of flat dicts (e.g. `dataclasses.asdict(candidate)`
        for every candidate of a pipeline step) -> materializes a DuckDb
        table from them, so results (primary keys, functional groups, joins,
        ...) can be browsed with any DuckDb-compatible tool instead of only
        being printed to the console. A no-op if `records` is empty, since
        DuckDb can't create a table with zero columns."""
        if not records:
            return
        self._materializeDataFrame(tableName, pd.DataFrame(records), replace)

    def _materializeDataFrame(self, tableName, df, replace):
        self.conn.register("_duckdb_import", df)
        verb = "CREATE OR REPLACE TABLE" if replace else "CREATE TABLE"
        self.conn.execute(f'{verb} "{tableName}" AS SELECT * FROM _duckdb_import')
        self.conn.unregister("_duckdb_import")

    def query(self, sql, params=None):
        """Runs arbitrary parameterized SQL and returns all resulting rows."""
        return self.conn.execute(sql, params or []).fetchall()

    def getTableNames(self):
        """Lists the names of every table currently in the database."""
        return [row[0] for row in self.conn.execute("SHOW TABLES").fetchall()]

    def dropAllTables(self):
        """Drop every table AND view currently in the database. Meant to be
        called at the very start of a run: since `output/local.duckdb` is a
        persistent file (not `:memory:`), objects from a previous run —
        source tables, materialized logical tables, exported result tables,
        generated SQL views — would otherwise still be there on the next
        run, alongside whatever the new run creates, and get silently mixed
        into it. DuckDb rejects `DROP TABLE` on a view (and vice versa), so
        each object is dropped with the statement matching its actual type."""
        rows = self.conn.execute("SELECT table_name, table_type FROM information_schema.tables").fetchall()
        for name, tableType in rows:
            verb = "DROP VIEW IF EXISTS" if tableType == "VIEW" else "DROP TABLE IF EXISTS"
            self.conn.execute(f'{verb} "{name}"')

    def getColumns(self, table_name, dataType=False):
        """dataType=True returns [(columnName, duckDbType), ...] instead of
        just the names — used by `aggProfiling` to infer numeric/date/string
        categories straight from the SQL type."""
        rows = self.conn.execute(f'DESCRIBE "{table_name}"').fetchall()
        if dataType:
            return [(row[0], row[1]) for row in rows]
        return [row[0] for row in rows]

    def getColumnType(self, table_name, column_name):
        """The DuckDb type of one column."""
        return dict(self.getColumns(table_name, dataType=True))[column_name]

    def joinCondition(self, leftTable, leftAlias, leftColumn, rightTable, rightAlias, rightColumn):
        """The SQL equality `leftAlias.leftColumn = rightAlias.rightColumn`
        for joining the two tables -- both sides cast to VARCHAR when their
        types belong to different families (text, numeric, temporal, ...),
        which DuckDb can't compare directly (e.g. a foreign key declared by
        the source system between an INTEGER and a VARCHAR column, kept
        whatever its types -- it must never crash a step that joins it).
        Same-family columns (INTEGER/DOUBLE, ...) keep a native comparison."""
        left = f'{leftAlias}."{leftColumn}"'
        right = f'{rightAlias}."{rightColumn}"'
        leftFamily = _typeFamily(self.getColumnType(leftTable, leftColumn))
        rightFamily = _typeFamily(self.getColumnType(rightTable, rightColumn))
        if leftFamily != rightFamily:
            return f"CAST({left} AS VARCHAR) = CAST({right} AS VARCHAR)"
        return f"{left} = {right}"

    def getRowCount(self, table_name):
        """Returns the number of rows in `table_name`."""
        return self.conn.execute(f'SELECT COUNT(*) FROM "{table_name}"').fetchone()[0]

    def selectFromTable(self, table_name, columns="*"):
        """Returns all rows of `table_name`, optionally restricted to a
        comma-separated subset of `columns`."""
        return self.conn.execute(f'SELECT {columns} FROM "{table_name}"').fetchall()
