"""Common interface every input source implements (CSV, ClickHouse,
StarRocks, later Postgres, ...).

The whole v3 pipeline runs on DuckDB: a source's only job is to list what it
has and copy the requested tables/columns into the run's DuckDB database.
Nothing downstream of `loadInto` ever talks to the source again.
"""
from __future__ import annotations

import re
from abc import ABC, abstractmethod

_UNSAFE_TABLE_NAME_CHARS = re.compile(r"[^0-9A-Za-z_]+")


def duckDbTableName(name):
    """The DuckDB table name a source table is loaded under: lowercase,
    every run of non `[0-9a-z_]` characters replaced by `_` (a CSV file
    named `Ventes 2024.csv` becomes `ventes_2024`)."""
    cleaned = _UNSAFE_TABLE_NAME_CHARS.sub("_", name).strip("_").lower()
    return cleaned or "table"


def _quote(identifier):
    return '"' + identifier.replace('"', '""') + '"'


class SourceConnector(ABC):
    """`kind` identifies the connector in a job payload (`"csv"`,
    `"clickhouse"`, ...). Subclasses implement `listTables`/`listColumns`
    and `fetchDataFrame`; `loadInto` can be overridden when the source can
    be read by DuckDB natively (see `CsvSource`)."""

    kind = None

    @abstractmethod
    def listTables(self):
        """Names of every table this source exposes, in the source's own
        spelling (the name `loadInto` and `fetchDataFrame` expect)."""

    @abstractmethod
    def listColumns(self, tableName):
        """Column names of `tableName`, in source order."""

    @abstractmethod
    def fetchDataFrame(self, tableName, columnNames):
        """A pandas DataFrame holding `columnNames` of every row of
        `tableName`."""

    def loadInto(self, db, tableName, columnNames=None, targetName=None):
        """Copies `tableName` (only `columnNames` when given) into DuckDB as
        `targetName` (default `duckDbTableName(tableName)`), replacing any
        existing table of that name. Returns the DuckDB table name."""
        targetName = targetName or duckDbTableName(tableName)
        columnNames = list(columnNames) if columnNames else self.listColumns(tableName)
        unknown = [c for c in columnNames if c not in self.listColumns(tableName)]
        if unknown:
            raise ValueError(f"{self.kind}: colonnes inconnues dans {tableName!r} : {unknown}")
        db.loadDataFrame(targetName, self.fetchDataFrame(tableName, columnNames))
        return targetName

    def close(self):
        """Releases any connection held by the source. No-op by default."""


def selectColumnsSql(columnNames):
    """`"a", "b"` -- quoted column list for a generated SELECT."""
    return ", ".join(_quote(c) for c in columnNames)


def quoteIdentifier(identifier):
    """Double-quoted SQL identifier (DuckDB / ANSI)."""
    return _quote(identifier)
