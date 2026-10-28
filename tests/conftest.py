import pytest

from kawakiriv3.DuckDbManagement.duckDbManager import duckDbManager
from kawakiriv3.DuckDbManagement.duckDbUtils import duckDbUtils

# A small sales fact with every column role the v3 chain distinguishes:
#   vente_id       unique per row             -> row identifier (degenerate)
#   date_vente     date                       -> extracted date dimension
#   client_id      overlaps clients.client_id -> linked foreign key
#   code_produit   repeats, determines libelle_produit/categorie -> one extracted dimension
#   canal          repeats, determined by nothing -> its own extracted dimension
#   quantite       integer, not key-named     -> measure (its 1..6 values overlap client ids)
#   montant        decimal                    -> measure
#   statut         constant                   -> dropped
CLIENTS = {
    "client_id": [1, 2, 3, 4, 5, 6],
    "nom": ["Dupont", "Martin", "Bernard", "Petit", "Durand", "Leroy"],
}
VENTES = {
    "vente_id": [1001, 1002, 1003, 1004, 1005, 1006, 1007, 1008],
    "date_vente": ["2024-01-05", "2024-01-05", "2024-01-06", "2024-01-07",
                   "2024-01-07", "2024-01-08", "2024-01-08", "2024-01-09"],
    "client_id": [1, 2, 1, 3, 4, 5, 2, 3],
    "code_produit": ["P01", "P02", "P03", "P01", "P04", "P02", "P03", "P04"],
    "libelle_produit": ["Chaise", "Table", "Lampe", "Chaise", "Tapis", "Table", "Lampe", "Tapis"],
    "categorie": ["Mobilier", "Mobilier", "Deco", "Mobilier", "Deco", "Mobilier", "Deco", "Deco"],
    "canal": ["web", "magasin", "web", "magasin", "web", "web", "magasin", "magasin"],
    "quantite": [2, 1, 3, 4, 1, 6, 2, 5],
    "montant": [90.0, 250.0, 45.5, 180.0, 120.0, 250.0, 30.3, 120.0],
    "statut": ["ok"] * 8,
}


@pytest.fixture
def duck_db():
    """A fresh in-memory DuckDb database per test."""
    manager = duckDbManager(":memory:")
    yield duckDbUtils(manager.conn)
    manager.close()


@pytest.fixture
def sales_db(duck_db):
    duck_db.loadTables({"clients": CLIENTS, "ventes": VENTES})
    duck_db.conn.execute("ALTER TABLE ventes ALTER date_vente TYPE DATE")
    return duck_db
