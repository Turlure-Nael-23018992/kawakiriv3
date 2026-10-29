from decimal import Decimal
from uuid import UUID

import pandas as pd
import pytest

from kawakiriv3 import envConfig
from kawakiriv3.Sources import ClickHouseSource, CsvSource, PostgresSource, StarRocksSource, _SOURCE_KINDS, buildSource, duckDbTableName


def _writeCsvDir(tmp_path):
    (tmp_path / "Ventes 2024.csv").write_text("id;montant;jour\n1;10.5;2024-01-01\n2;20.0;2024-01-02\n", encoding="utf-8")
    (tmp_path / "clients.csv").write_text("client_id;nom\n1;Dupont\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("pas une table", encoding="utf-8")
    return tmp_path


def testDuckDbTableNameSanitizesSourceNames():
    assert duckDbTableName("Ventes 2024") == "ventes_2024"
    assert duckDbTableName("dbo.Clients-FR") == "dbo_clients_fr"


def testCsvSourceListsOneTablePerCsvFile(tmp_path):
    source = CsvSource(_writeCsvDir(tmp_path))

    assert source.listTables() == ["clients", "Ventes 2024"]
    assert source.listColumns("Ventes 2024") == ["id", "montant", "jour"]


def testCsvSourceLoadsSelectedColumnsNativelyWithInferredTypes(tmp_path, duck_db):
    source = CsvSource(_writeCsvDir(tmp_path), delimiter=";")

    tableName = source.loadInto(duck_db, "Ventes 2024", ["id", "jour"])

    assert tableName == "ventes_2024"
    assert duck_db.getColumns(tableName, dataType=True) == [("id", "BIGINT"), ("jour", "DATE")]
    assert duck_db.getRowCount(tableName) == 2


def testCsvSourceAcceptsASingleFile(tmp_path):
    source = CsvSource(_writeCsvDir(tmp_path) / "clients.csv")

    assert source.listTables() == ["clients"]


def testCsvSourceRejectsAnUnknownColumn(tmp_path, duck_db):
    source = CsvSource(_writeCsvDir(tmp_path))

    with pytest.raises(ValueError, match="colonnes inconnues"):
        source.loadInto(duck_db, "clients", ["client_id", "email"])


class _FakeResult:
    def __init__(self, rows):
        self.result_rows = rows


class _FakeClickHouseClient:
    def __init__(self):
        self.queries = []

    def query(self, sql, parameters=None):
        self.queries.append((sql, parameters))
        if "system.tables" in sql:
            return _FakeResult([("clients",), ("ventes",)])
        return _FakeResult([("client_id",), ("nom",)])

    def query_df(self, sql):
        self.queries.append((sql, None))
        return pd.DataFrame({"client_id": [1, 2], "nom": ["Dupont", "Martin"]})


def testClickHouseSourceListsAndLoadsThroughItsClient(duck_db):
    client = _FakeClickHouseClient()
    source = ClickHouseSource(database="shop", client=client, rowLimit=1000)

    assert source.listTables() == ["clients", "ventes"]
    tableName = source.loadInto(duck_db, "clients")

    assert tableName == "clients"
    assert duck_db.getRowCount("clients") == 2
    assert client.queries[-1][0] == "SELECT `client_id`, `nom` FROM `shop`.`clients` LIMIT 1000"


class _FakeCursor:
    def __init__(self, connection):
        self.connection = connection
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql):
        self.connection.queries.append(sql)
        if sql.startswith("SHOW TABLES"):
            self.pending = [("ventes",), ("clients",)]
        elif sql.startswith("DESC"):
            self.pending = [("client_id", "int", "No", "true", None, ""), ("solde", "decimal(12,2)", "Yes", "false", None, "")]
        else:
            self.pending = [(1, Decimal("10.50")), (2, None), (3, Decimal("7.25"))]

    def fetchall(self):
        rows, self.pending = self.pending, []
        return rows

    def fetchmany(self, size):
        rows, self.pending = self.pending[:size], self.pending[size:]
        return rows


class _FakeMySqlConnection:
    def __init__(self):
        self.queries = []
        self.closed = False

    def cursor(self):
        return _FakeCursor(self)

    def close(self):
        self.closed = True


def testStarRocksSourceListsDescribesAndLoadsAnExternalCatalogTable(duck_db):
    connection = _FakeMySqlConnection()
    source = StarRocksSource(database="crm", catalog="hive_prod", connection=connection, rowLimit=500)

    assert source.listTables() == ["clients", "ventes"]
    assert source.listColumns("clients") == ["client_id", "solde"]
    tableName = source.loadInto(duck_db, "clients")
    source.close()

    assert connection.queries[0] == "SHOW TABLES FROM `hive_prod`.`crm`"
    assert connection.queries[-1] == "SELECT `client_id`, `solde` FROM `hive_prod`.`crm`.`clients` LIMIT 500"
    # DECIMAL values arrive as Python Decimal: loaded as numbers, nulls kept.
    assert duck_db.query('SELECT SUM(solde), COUNT(solde) FROM "clients"') == [(17.75, 2)]
    assert tableName == "clients" and connection.closed


def testStarRocksSourceDefaultsToTheInternalCatalog():
    connection = _FakeMySqlConnection()

    StarRocksSource(database="ventes", connection=connection).listColumns("lignes")

    assert connection.queries == ["DESC `ventes`.`lignes`"]


def testBuildSourceDispatchesOnKind(tmp_path):
    assert isinstance(buildSource({"kind": "csv", "path": str(tmp_path)}), CsvSource)
    with pytest.raises(ValueError, match="source inconnue"):
        buildSource({"kind": "oracle"})


def testBuildSourceResolvesPlaceholdersFromTheEnvironmentThenTheEnvFile(tmp_path, monkeypatch):
    envPath = tmp_path / ".env"
    envPath.write_text("# StarRocks\nSR_USER=lecteur\nSR_PASSWORD=\"s3cret\"\n", encoding="utf-8")
    monkeypatch.setattr(envConfig, "ENV_PATH", envPath)
    monkeypatch.setenv("SR_USER", "admin")
    monkeypatch.delenv("SR_PASSWORD", raising=False)
    captured = {}
    monkeypatch.setitem(_SOURCE_KINDS, "fake", lambda **kwargs: captured.update(kwargs))

    buildSource({"kind": "fake", "user": "${SR_USER}", "password": "${SR_PASSWORD}", "url": "mysql://${SR_USER}@h"})

    assert captured == {"user": "admin", "password": "s3cret", "url": "mysql://admin@h"}


def testBuildSourceNamesEveryUndefinedVariable(tmp_path, monkeypatch):
    monkeypatch.setattr(envConfig, "ENV_PATH", tmp_path / ".env")
    monkeypatch.delenv("SR_HOST", raising=False)
    monkeypatch.delenv("SR_PASSWORD", raising=False)

    with pytest.raises(ValueError, match=r"\['SR_HOST', 'SR_PASSWORD'\]"):
        buildSource({"kind": "starrocks", "host": "${SR_HOST}", "password": "${SR_PASSWORD}", "database": "ventes"})


class _FakePgCursor:
    def __init__(self, connection, name=None):
        self.connection = connection
        self.name = name
        self.pending = []

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def execute(self, sql, params=None):
        self.connection.queries.append((sql, params, self.name))
        if "pg_class" in sql and "pg_attribute" not in sql:
            self.pending = [("web_cloud", "web_sales"), ("magasin", "loyalty_cards"), ("web_cloud", "web_customers")]
        elif "pg_attribute" in sql:
            self.pending = [("sale_id",), ("amount",), ("customer_uuid",), ("extra",)]
        else:
            self.pending = [
                (1, Decimal("10.50"), UUID("12345678-1234-5678-1234-567812345678"), {"canal": "web"}),
                (2, None, None, None),
            ]

    def fetchall(self):
        rows, self.pending = self.pending, []
        return rows

    def fetchmany(self, size):
        rows, self.pending = self.pending[:size], self.pending[size:]
        return rows


class _FakePgConnection:
    def __init__(self):
        self.queries = []
        self.rolledBack = 0

    def cursor(self, name=None):
        return _FakePgCursor(self, name)

    def rollback(self):
        self.rolledBack += 1

    def close(self):
        pass


def testPostgresSourceSpansSeveralSchemasAndLoadsUnderTheBareTableName(duck_db):
    connection = _FakePgConnection()
    source = PostgresSource(database="shop", schemas=["magasin", "web_cloud"], connection=connection, rowLimit=10)

    assert source.listTables() == ["magasin.loyalty_cards", "web_cloud.web_customers", "web_cloud.web_sales"]
    tableName = source.loadInto(duck_db, "web_cloud.web_sales")

    assert tableName == "web_sales"
    sql, _, cursorName = connection.queries[-1]
    assert sql == 'SELECT "sale_id", "amount", "customer_uuid", "extra" FROM "web_cloud"."web_sales" LIMIT 10'
    assert cursorName is not None and connection.rolledBack == 1
    # NUMERIC -> number, UUID -> text, JSON -> text; nulls kept.
    assert duck_db.query('SELECT SUM(amount), COUNT(amount), MAX(customer_uuid), MAX(extra) FROM "web_sales"') == [
        (10.5, 1, "12345678-1234-5678-1234-567812345678", '{"canal": "web"}')
    ]


def testPostgresSourceResolvesABareNameAndRejectsAnUnknownOne():
    source = PostgresSource(database="shop", schemas=["magasin", "web_cloud"], connection=_FakePgConnection())

    assert source._resolve("loyalty_cards") == ("magasin", "loyalty_cards")
    with pytest.raises(ValueError, match="table inconnue"):
        source._resolve("produits")
