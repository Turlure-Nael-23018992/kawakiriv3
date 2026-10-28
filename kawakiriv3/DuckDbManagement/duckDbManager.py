import duckdb
from pathlib import Path


class duckDbManager:
    """Opens (or creates) a local DuckDb file and exposes the connection.

    Locally, the file lives under output/. In Docker, `path` will point to a
    file generated on the fly on the SSD volume allocated to the user.
    """

    def __init__(self, path=None):
        self.path = Path(path) if path else Path("output/local.duckdb")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = duckdb.connect(str(self.path))

    def close(self):
        """Closes the DuckDb connection."""
        self.conn.close()

    def __enter__(self):
        """Enables `with duckDbManager(...) as db:` usage."""
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Ensures the connection is closed on exit, even if the `with`
        block raised."""
        self.close()
