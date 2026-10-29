"""ClickHouse source, through `clickhouse-connect` (HTTP interface)."""
from __future__ import annotations

from kawakiriv3.Sources.sourceConnector import SourceConnector

DEFAULT_PORT = 8123


def _backquote(identifier):
    return "`" + identifier.replace("`", "``") + "`"


class ClickHouseSource(SourceConnector):
    """`database` is the ClickHouse database the tables live in. `client`
    can be injected (tests); otherwise one is opened with
    `clickhouse_connect.get_client`.

    `rowLimit`: optional cap on the rows pulled per table, for a first look
    at a very large source -- a limited table is profiled on a partial
    sample, so leave it unset for a real run."""

    kind = "clickhouse"

    def __init__(
        self, database, host="localhost", port=DEFAULT_PORT, user="default", password="",
        secure=False, rowLimit=None, client=None,
    ):
        self.database = database
        self.rowLimit = rowLimit
        if client is None:
            import clickhouse_connect

            client = clickhouse_connect.get_client(
                host=host, port=port, username=user, password=password, database=database, secure=secure,
            )
        self.client = client
        self._columnsCache = {}

    def _qualified(self, tableName):
        return f"{_backquote(self.database)}.{_backquote(tableName)}"

    def listTables(self):
        result = self.client.query(
            "SELECT name FROM system.tables WHERE database = {database:String} ORDER BY name",
            parameters={"database": self.database},
        )
        return [row[0] for row in result.result_rows]

    def listColumns(self, tableName):
        if tableName not in self._columnsCache:
            result = self.client.query(
                "SELECT name FROM system.columns WHERE database = {database:String} AND table = {table:String} "
                "ORDER BY position",
                parameters={"database": self.database, "table": tableName},
            )
            self._columnsCache[tableName] = [row[0] for row in result.result_rows]
        return self._columnsCache[tableName]

    def fetchDataFrame(self, tableName, columnNames, rowFilter=None):
        sql = f"SELECT {', '.join(_backquote(c) for c in columnNames)} FROM {self._qualified(tableName)}"
        if rowFilter:
            sql += f" WHERE {rowFilter}"
        if self.rowLimit is not None:
            sql += f" LIMIT {int(self.rowLimit)}"
        return self.client.query_df(sql)

    def close(self):
        close = getattr(self.client, "close", None)
        if close is not None:
            close()
