"""Input sources for the v3 pipeline. `buildSource` turns a job payload's
`source` block into a connector; add a new kind by registering it in
`_SOURCE_KINDS`."""
from kawakiriv3.Sources.clickHouseSource import ClickHouseSource
from kawakiriv3.Sources.csvSource import CsvSource
from kawakiriv3.Sources.sourceConnector import SourceConnector, duckDbTableName
from kawakiriv3.Sources.starRocksSource import StarRocksSource

_SOURCE_KINDS = {
    CsvSource.kind: CsvSource,
    ClickHouseSource.kind: ClickHouseSource,
    StarRocksSource.kind: StarRocksSource,
}


def buildSource(config):
    """`config`: `{"kind": "csv", ...}` -- every other key is passed to that
    connector's constructor. Raises ValueError on an unknown kind."""
    config = dict(config)
    kind = config.pop("kind", None)
    if kind not in _SOURCE_KINDS:
        raise ValueError(f"source inconnue {kind!r} (disponibles : {sorted(_SOURCE_KINDS)})")
    return _SOURCE_KINDS[kind](**config)


__all__ = ["SourceConnector", "CsvSource", "ClickHouseSource", "StarRocksSource", "buildSource", "duckDbTableName"]
