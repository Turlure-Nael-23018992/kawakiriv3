"""Regression on the full `examples/commerce` dataset (regenerated into a
temporary directory): every case documented in examples/commerce/README.md."""
import importlib.util
from pathlib import Path

import pytest

from kawakiriv3.FactAnalysis.factAnalyzer import NEEDS_REVIEW, OK
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.Sources import CsvSource

_GENERATOR = Path(__file__).resolve().parents[1] / "scripts" / "generate_commerce_dataset.py"


@pytest.fixture
def commerceModels(tmp_path, duck_db):
    spec = importlib.util.spec_from_file_location("generate_commerce_dataset", _GENERATOR)
    generator = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(generator)
    generator.generate(tmp_path)

    pipeline = KawakiriV3Pipeline(duck_db)
    loaded = pipeline.ingest(CsvSource(tmp_path))
    results = pipeline.run(list(loaded.values()), factTables=["lignes_commande", "retours", "commandes"], columnHint=False)
    return {model.factTable: model for model in results["models"]}


def testLignesCommandeBuildsTheExpectedStar(commerceModels, duck_db):
    model = commerceModels["lignes_commande"]

    assert model.verdict == OK, model.warnings
    assert sorted(link.target for link in model.links) == ["commandes.commande_id", "produits.produit_id"]
    attributesByKey = {d.keyColumn: sorted(d.attributeColumns) for d in model.dimensions}
    assert attributesByKey == {
        "date_key": ["annee", "jour", "jour_semaine", "mois", "trimestre"], "code_promo": ["libelle_promo"],
        "entrepot_code": ["entrepot_region", "entrepot_ville"], "mode_livraison": [],
    }
    assert model.measures == ["quantite", "prix_unitaire", "montant_ligne", "remise"]
    assert model.degenerateColumns == ["ligne_id", "ligne_uuid", "commentaire"]
    assert model.grain.isUnique
    calendar = next(d for d in model.dimensions if d.isDate)
    # date_key is the calendar's key: one row per day, even for a single date column.
    assert duck_db.query(f'SELECT COUNT(*) - COUNT(DISTINCT date_key) FROM "{calendar.tableName}"') == [(0,)]


def testRetoursGrainIsNotUniqueAndRetourIdCompletesIt(commerceModels):
    model = commerceModels["retours"]

    assert model.verdict == NEEDS_REVIEW
    assert model.grain.grainColumns == ["ligne_id", "date_retour", "motif_code"]
    assert model.grain.duplicateRows > 0
    assert model.grain.completingColumns == ["retour_id"]


def testCommandesAsFactTriggersEveryHintWarning(commerceModels):
    model = commerceModels["commandes"]

    assert model.verdict == NEEDS_REVIEW
    assert model.measures == []
    assert any("lignes_commande.commande_id" in w for w in model.warnings)
    assert model.grain.unknownKeyRows > 0  # magasin_id/vendeur_id are empty on web orders
    assert "employes.employe_id" in [link.target for link in model.links]
