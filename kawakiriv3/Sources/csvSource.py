"""CSV source: one table per `.csv` file, read by DuckDB itself
(`read_csv`) -- no row ever goes through pandas or Python."""
from __future__ import annotations

from pathlib import Path

from kawakiriv3.Sources.sourceConnector import SourceConnector, duckDbTableName, quoteIdentifier, selectColumnsSql


class CsvSource(SourceConnector):
    """`path`: a directory (every `*.csv` directly inside it is a table,
    named after the file stem) or a single `.csv` file.

    `delimiter`/`header`/`encoding`: forwarded to DuckDB's `read_csv`;
    `delimiter=None` lets DuckDB sniff it. Types are inferred by DuckDB's
    own sniffer (dates included), which is what the profiling relies on."""

    kind = "csv"

    def __init__(self, path, delimiter=None, header=True, encoding="utf-8"):
        self.path = Path(path)
        if not self.path.exists():
            raise FileNotFoundError(f"csv: chemin introuvable : {self.path}")
        self.delimiter = delimiter
        self.header = header
        self.encoding = encoding
        self._columnsCache = {}

    def _files(self):
        if self.path.is_file():
            return {self.path.stem: self.path}
        # Explicit, OS-independent order (Windows paths compare case-insensitively).
        return {file.stem: file for file in sorted(self.path.glob("*.csv"), key=lambda f: f.name.lower())}

    def listTables(self):
        return list(self._files())

    def _fileFor(self, tableName):
        files = self._files()
        if tableName not in files:
            raise ValueError(f"csv: table inconnue {tableName!r} (fichiers : {list(files)})")
        return files[tableName]

    def _readCsvExpression(self, tableName):
        options = [f"header = {'true' if self.header else 'false'}", f"encoding = '{self.encoding}'"]
        if self.delimiter is not None:
            options.append("delim = '" + self.delimiter.replace("'", "''") + "'")
        filePath = self._fileFor(tableName).as_posix().replace("'", "''")
        return f"read_csv('{filePath}', {', '.join(options)})"

    def listColumns(self, tableName):
        if tableName not in self._columnsCache:
            import duckdb

            rows = duckdb.sql(f"DESCRIBE SELECT * FROM {self._readCsvExpression(tableName)}").fetchall()
            self._columnsCache[tableName] = [row[0] for row in rows]
        return self._columnsCache[tableName]

    def fetchDataFrame(self, tableName, columnNames):
        import duckdb

        return duckdb.sql(
            f"SELECT {selectColumnsSql(columnNames)} FROM {self._readCsvExpression(tableName)}"
        ).df()

    def loadInto(self, db, tableName, columnNames=None, targetName=None):
        """Native path: `CREATE TABLE ... AS SELECT ... FROM read_csv(...)`
        straight into the run's DuckDB connection."""
        targetName = targetName or duckDbTableName(tableName)
        available = self.listColumns(tableName)
        columnNames = list(columnNames) if columnNames else available
        unknown = [c for c in columnNames if c not in available]
        if unknown:
            raise ValueError(f"csv: colonnes inconnues dans {tableName!r} : {unknown}")
        db.conn.execute(
            f"CREATE OR REPLACE TABLE {quoteIdentifier(targetName)} AS "
            f"SELECT {selectColumnsSql(columnNames)} FROM {self._readCsvExpression(tableName)}"
        )
        return targetName
