"""StarRocks source, through its MySQL protocol (FE query port, 9030 by
default) with `PyMySQL`.

Tables are listed with `SHOW TABLES FROM` and described with `DESC` rather
than through `information_schema`, so the same code reads the internal
catalog and an external one (Hive, Iceberg, JDBC...: `catalog` set).
"""
from __future__ import annotations

from decimal import Decimal

import pandas as pd

from kawakiriv3.Sources.sourceConnector import SourceConnector

DEFAULT_PORT = 9030
FETCH_BATCH_ROWS = 50_000


def _backquote(identifier):
    return "`" + identifier.replace("`", "``") + "`"


class StarRocksSource(SourceConnector):
    """`database` is the StarRocks database the tables live in, `catalog`
    an optional external catalog (default: the internal one).
    `connection` can be injected (tests); otherwise one is opened with
    `pymysql.connect`.

    `rowLimit`: optional cap on the rows pulled per table, for a first look
    at a very large source -- a limited table is profiled on a partial
    sample, so leave it unset for a real run."""

    kind = "starrocks"

    def __init__(
        self, database, host="localhost", port=DEFAULT_PORT, user="root", password="", catalog=None,
        rowLimit=None, connection=None,
    ):
        self.database = database
        self.catalog = catalog
        self.rowLimit = rowLimit
        if connection is None:
            import pymysql

            try:
                # SSCursor streams the rows instead of buffering a whole table client-side.
                connection = pymysql.connect(
                    host=host, port=int(port), user=user, password=password, charset="utf8mb4",
                    cursorclass=pymysql.cursors.SSCursor,
                )
            except pymysql.err.OperationalError as exc:
                raise ConnectionError(f"StarRocks injoignable a {host}:{port} : {exc}") from exc
        self.connection = connection
        self._columnsCache = {}

    def _qualifiedDatabase(self):
        parts = [self.catalog, self.database] if self.catalog else [self.database]
        return ".".join(_backquote(p) for p in parts)

    def _qualified(self, tableName):
        return f"{self._qualifiedDatabase()}.{_backquote(tableName)}"

    def _rows(self, sql):
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            return list(cursor.fetchall())

    def listTables(self):
        return sorted(row[0] for row in self._rows(f"SHOW TABLES FROM {self._qualifiedDatabase()}"))

    def listColumns(self, tableName):
        if tableName not in self._columnsCache:
            # DESC: one row per column, its name first, in table order.
            self._columnsCache[tableName] = [row[0] for row in self._rows(f"DESC {self._qualified(tableName)}")]
        return self._columnsCache[tableName]

    def fetchDataFrame(self, tableName, columnNames):
        sql = f"SELECT {', '.join(_backquote(c) for c in columnNames)} FROM {self._qualified(tableName)}"
        if self.rowLimit is not None:
            sql += f" LIMIT {int(self.rowLimit)}"
        rows = []
        with self.connection.cursor() as cursor:
            cursor.execute(sql)
            while True:
                batch = cursor.fetchmany(FETCH_BATCH_ROWS)
                if not batch:
                    break
                rows.extend(batch)
        frame = pd.DataFrame(rows, columns=list(columnNames))
        # DECIMAL columns come back as Python Decimal objects: an `object`
        # column DuckDB would not read as a number.
        for column in frame.columns:
            values = frame[column].dropna()
            if not values.empty and all(isinstance(v, Decimal) for v in values.head(100)):
                frame[column] = frame[column].astype(float)
        return frame

    def close(self):
        close = getattr(self.connection, "close", None)
        if close is not None:
            close()
