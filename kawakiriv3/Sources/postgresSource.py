"""PostgreSQL source, through `psycopg` (v3).

One source can span several schemas of the same database (`schemas`): with a
single schema its tables are named as is, with several they are named
`schema.table` -- either way each one is loaded into DuckDB under its bare
table name, so two schemas holding the same table name can't be read in the
same run (`ingest` refuses the collision).

The session is read-only and rows are streamed through a server-side cursor.
"""
from __future__ import annotations

import pandas as pd

from kawakiriv3.Sources.sourceConnector import SourceConnector, duckDbTableName, normalizeObjectColumns

DEFAULT_PORT = 5432
FETCH_BATCH_ROWS = 50_000
CONNECT_TIMEOUT_SECONDS = 15
# Ordinary and partitioned tables, views, materialized views, foreign tables.
_TABLE_KINDS = ["r", "p", "v", "m", "f"]


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


class PostgresSource(SourceConnector):
    """`database` is the PostgreSQL database, `schemas` the schema(s) read
    from it (default `["public"]`). `connection` can be injected (tests);
    otherwise one is opened with `psycopg.connect`.

    `rowLimit`: optional cap on the rows pulled per table, for a first look
    at a very large source -- a limited table is profiled on a partial
    sample, so leave it unset for a real run."""

    kind = "postgres"

    def __init__(
        self, database, host="localhost", port=DEFAULT_PORT, user="postgres", password="", schemas=None,
        sslmode="prefer", rowLimit=None, connection=None,
    ):
        self.database = database
        self.schemas = list(schemas or ["public"])
        self.rowLimit = rowLimit
        if connection is None:
            import psycopg

            try:
                connection = psycopg.connect(
                    host=host, port=int(port), user=user, password=password, dbname=database, sslmode=sslmode,
                    connect_timeout=CONNECT_TIMEOUT_SECONDS,
                )
            except psycopg.OperationalError as exc:
                raise ConnectionError(f"PostgreSQL injoignable a {host}:{port}/{database} : {exc}") from exc
            connection.read_only = True
        self.connection = connection
        self._tablesCache = None
        self._columnsCache = {}

    def _rows(self, sql, params=None):
        with self.connection.cursor() as cursor:
            cursor.execute(sql, params)
            return list(cursor.fetchall())

    def _schemaTables(self):
        """`[(schema, table), ...]` for every table of `schemas`, schema order first."""
        if self._tablesCache is None:
            rows = self._rows(
                "SELECT n.nspname, c.relname FROM pg_catalog.pg_class c "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = ANY(%s) AND c.relkind = ANY(%s)",
                (self.schemas, _TABLE_KINDS),
            )
            self._tablesCache = sorted(rows, key=lambda row: (self.schemas.index(row[0]), row[1]))
        return self._tablesCache

    def listTables(self):
        if len(self.schemas) == 1:
            return [table for _, table in self._schemaTables()]
        return [f"{schema}.{table}" for schema, table in self._schemaTables()]

    def _resolve(self, tableName):
        """`(schema, table)` for `tableName`, qualified (`schema.table`) or
        bare when exactly one of `schemas` holds it."""
        known = self._schemaTables()
        if "." in tableName and tuple(tableName.split(".", 1)) in known:
            return tuple(tableName.split(".", 1))
        matches = [(schema, table) for schema, table in known if table == tableName]
        if len(matches) == 1:
            return matches[0]
        if not matches:
            raise ValueError(f"postgres: table inconnue {tableName!r} dans les schemas {self.schemas}")
        raise ValueError(
            f"postgres: table {tableName!r} presente dans plusieurs schemas "
            f"{[schema for schema, _ in matches]} : la qualifier en schema.table"
        )

    def _qualified(self, tableName):
        schema, table = self._resolve(tableName)
        return f"{_quote(schema)}.{_quote(table)}"

    def listColumns(self, tableName):
        if tableName not in self._columnsCache:
            schema, table = self._resolve(tableName)
            rows = self._rows(
                "SELECT a.attname FROM pg_catalog.pg_attribute a "
                "JOIN pg_catalog.pg_class c ON c.oid = a.attrelid "
                "JOIN pg_catalog.pg_namespace n ON n.oid = c.relnamespace "
                "WHERE n.nspname = %s AND c.relname = %s AND a.attnum > 0 AND NOT a.attisdropped "
                "ORDER BY a.attnum",
                (schema, table),
            )
            self._columnsCache[tableName] = [row[0] for row in rows]
        return self._columnsCache[tableName]

    def fetchDataFrame(self, tableName, columnNames, rowFilter=None):
        sql = f"SELECT {', '.join(_quote(c) for c in columnNames)} FROM {self._qualified(tableName)}"
        if rowFilter:
            sql += f" WHERE {rowFilter}"
        if self.rowLimit is not None:
            sql += f" LIMIT {int(self.rowLimit)}"
        rows = []
        # A named cursor is a server-side one: rows come in batches instead of all at once.
        with self.connection.cursor(name="kawakiri_fetch") as cursor:
            cursor.execute(sql)
            while True:
                batch = cursor.fetchmany(FETCH_BATCH_ROWS)
                if not batch:
                    break
                rows.extend(batch)
        self.connection.rollback()
        return normalizeObjectColumns(pd.DataFrame(rows, columns=list(columnNames)))

    def loadInto(self, db, tableName, columnNames=None, targetName=None, rowFilter=None):
        """Loaded under the bare table name: `web_cloud.web_sales` -> `web_sales`."""
        targetName = targetName or duckDbTableName(self._resolve(tableName)[1])
        return super().loadInto(db, tableName, columnNames, targetName=targetName, rowFilter=rowFilter)

    def close(self):
        close = getattr(self.connection, "close", None)
        if close is not None:
            close()
