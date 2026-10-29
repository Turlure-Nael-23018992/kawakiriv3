"""Input sources for the v3 pipeline. `buildSource` turns a job payload's
`source` block into a connector; add a new kind by registering it in
`_SOURCE_KINDS`."""
from kawakiriv3.envConfig import resolvePlaceholders
from kawakiriv3.Sources.clickHouseSource import ClickHouseSource
from kawakiriv3.Sources.csvSource import CsvSource
from kawakiriv3.Sources.postgresSource import PostgresSource
from kawakiriv3.Sources.sourceConnector import SourceConnector, duckDbTableName
from kawakiriv3.Sources.starRocksSource import StarRocksSource

_SOURCE_KINDS = {
    CsvSource.kind: CsvSource,
    ClickHouseSource.kind: ClickHouseSource,
    StarRocksSource.kind: StarRocksSource,
    PostgresSource.kind: PostgresSource,
}


def buildSource(config):
    """`config`: `{"kind": "csv", ...}` -- every other key is passed to that
    connector's constructor, once its `${VAR}` placeholders are resolved from
    the environment or `kawakiriv3/.env` (credentials stay out of the job
    file). Raises ValueError on an unknown kind or an undefined variable."""
    config = resolvePlaceholders(dict(config))
    kind = config.pop("kind", None)
    if kind not in _SOURCE_KINDS:
        raise ValueError(f"source inconnue {kind!r} (disponibles : {sorted(_SOURCE_KINDS)})")
    return _SOURCE_KINDS[kind](**config)


__all__ = ["SourceConnector", "CsvSource", "ClickHouseSource", "StarRocksSource", "PostgresSource", "buildSource", "duckDbTableName"]
