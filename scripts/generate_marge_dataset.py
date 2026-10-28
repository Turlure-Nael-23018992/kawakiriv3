"""Generates `examples/marge/`: a deterministic (seeded) office-supplies
wholesaler whose measures are spread over several tables at different
grains -- no single table answers a margin analysis, the fact has to be
assembled. See examples/marge/README.md for what each table tests.

    python scripts/generate_marge_dataset.py [outputDir]
"""
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 7
START = date(2025, 1, 1)
DAYS = 180
STOCK_DAYS = 60

N_CLIENTS = 300
N_PRODUITS = 150
N_COMMANDES = 2400
COUVERTURE_COUTS = 0.97      # share of order lines with a purchase cost
PART_NON_EXPEDIEE = 0.08     # share of order lines never shipped
N_COLIS_ORPHELINS = 6        # parcels pointing at no order line (legacy migration)

VILLES = ["Paris", "Lyon", "Marseille", "Lille", "Bordeaux", "Nantes", "Toulouse", "Rennes"]
SEGMENTS = ["PME", "grand_compte", "administration", "independant"]
FORMES = ["SARL", "SAS", "SA", "EURL"]
RACINES = ["Bureau", "Atelier", "Cabinet", "Groupe", "Agence", "Studio", "Comptoir", "Maison"]
SUFFIXES = ["Durand", "Lumiere", "Horizon", "Atlas", "Rivage", "Ardoise", "Boreal", "Cypres", "Delta", "Equinoxe"]
FAMILLES = {  # famille -> (taux de TVA, articles, plage de prix catalogue, poids unitaire kg)
    "papeterie": (0.20, ["Ramette A4", "Bloc notes", "Enveloppes", "Cahier", "Post-it"], (2.0, 25.0), (0.1, 2.5)),
    "ecriture": (0.20, ["Stylo bille", "Feutre", "Surligneur", "Crayon", "Marqueur"], (0.8, 12.0), (0.01, 0.1)),
    "classement": (0.20, ["Classeur", "Chemise", "Boite archive", "Intercalaires", "Trieur"], (1.5, 18.0), (0.1, 1.2)),
    "mobilier": (0.20, ["Chaise", "Bureau", "Caisson", "Etagere", "Lampe"], (45.0, 420.0), (3.0, 35.0)),
    "livres": (0.055, ["Agenda", "Guide fiscal", "Code du travail", "Manuel Excel", "Dictionnaire"], (9.0, 60.0), (0.3, 1.5)),
}
CANAUX = ["web", "commercial", "telephone"]
TRANSPORTEURS = {"Colissimo": (4.2, 0.65), "Chronopost": (7.5, 0.90), "DB Schenker": (12.0, 0.35)}


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def generate(outputDir):
    rng = random.Random(SEED)
    outputDir.mkdir(parents=True, exist_ok=True)

    clients = [
        (i, f"{rng.choice(RACINES)} {rng.choice(SUFFIXES)} {rng.choice(FORMES)}", rng.choice(VILLES),
         rng.choice(SEGMENTS))
        for i in range(1, N_CLIENTS + 1)
    ]
    _write(outputDir / "clients.csv", ["client_id", "raison_sociale", "ville", "segment"], clients)

    produits = []
    for i in range(1, N_PRODUITS + 1):
        famille = rng.choice(list(FAMILLES))
        _, articles, (prixMin, prixMax), (poidsMin, poidsMax) = FAMILLES[famille]
        produits.append({
            "produit_code": f"SKU-{i:04d}",
            "libelle": f"{rng.choice(articles)} {rng.choice(['standard', 'pro', 'eco', 'premium'])} {i}",
            "famille": famille,
            "prix_catalogue": round(rng.uniform(prixMin, prixMax), 2),
            "poids": rng.uniform(poidsMin, poidsMax),
        })
    _write(outputDir / "produits.csv", ["produit_code", "libelle", "famille", "prix_catalogue"],
           [(p["produit_code"], p["libelle"], p["famille"], p["prix_catalogue"]) for p in produits])

    commandes, lignes, tarifs, couts, colis = [], [], [], [], []
    ligneId = 1
    for n in range(N_COMMANDES):
        commandeId = 50001 + n
        dateCommande = START + timedelta(days=rng.randrange(DAYS))
        canal = rng.choice(CANAUX)
        montantCommande = 0.0
        for produit in rng.sample(produits, rng.choice([1, 1, 2, 3, 3, 4, 5, 6])):
            quantite = rng.randint(1, 3) if produit["famille"] == "mobilier" else rng.randint(1, 40)
            tauxRemise = rng.choice([0.0, 0.0, 0.0, 0.05, 0.10, 0.15])
            prixUnitaire = round(produit["prix_catalogue"] * (1 - tauxRemise), 2)
            montantHt = round(quantite * prixUnitaire, 2)
            tauxTva = FAMILLES[produit["famille"]][0]
            montantCommande += montantHt
            lignes.append((ligneId, commandeId, produit["produit_code"], quantite))
            tarifs.append((ligneId, prixUnitaire, tauxRemise, montantHt, round(montantHt * tauxTva, 2)))
            if rng.random() < COUVERTURE_COUTS:
                coutUnitaire = round(produit["prix_catalogue"] * rng.uniform(0.45, 0.75), 2)
                couts.append((ligneId, coutUnitaire, round(quantite * coutUnitaire, 2)))
            if rng.random() >= PART_NON_EXPEDIEE:
                parts = min(quantite, rng.choice([1, 1, 1, 2, 2, 3]))
                transporteur = rng.choice(list(TRANSPORTEURS))
                fixe, parKg = TRANSPORTEURS[transporteur]
                for p in range(parts):
                    poids = round(produit["poids"] * quantite / parts, 3)
                    colis.append([
                        None, ligneId, (dateCommande + timedelta(days=rng.randint(1, 5))).isoformat(),
                        transporteur, poids, round(fixe + parKg * poids, 2),
                    ])
            ligneId += 1
        fraisPort = 0.0 if canal == "commercial" or montantCommande >= 300 else rng.choice([6.9, 9.9, 14.9])
        commandes.append((commandeId, rng.randint(1, N_CLIENTS), dateCommande.isoformat(), canal, fraisPort))

    for _ in range(N_COLIS_ORPHELINS):
        colis.append([None, ligneId + 900 + rng.randrange(100), START.isoformat(), "Colissimo",
                      round(rng.uniform(0.5, 4.0), 3), round(rng.uniform(5.0, 9.0), 2)])
    rng.shuffle(colis)
    for i, row in enumerate(colis, start=1):
        row[0] = f"COL-{i:06d}"

    stocks = []
    for produit in produits:
        stock = rng.randint(50, 900)
        for d in range(STOCK_DAYS):
            stock = max(0, stock + rng.randint(-40, 35))
            stocks.append((produit["produit_code"], (START + timedelta(days=d)).isoformat(), stock,
                           round(stock * produit["prix_catalogue"] * 0.6, 2)))

    rng.shuffle(tarifs)
    rng.shuffle(couts)
    _write(outputDir / "commandes.csv", ["commande_id", "client_id", "date_commande", "canal", "frais_port"],
           commandes)
    _write(outputDir / "lignes_commande.csv", ["ligne_id", "commande_id", "produit_code", "quantite"], lignes)
    _write(outputDir / "tarification_lignes.csv",
           ["ligne_id", "prix_unitaire", "taux_remise", "montant_ht", "montant_tva"], tarifs)
    _write(outputDir / "couts_achat.csv", ["ligne_id", "cout_unitaire", "cout_achat"], couts)
    _write(outputDir / "expeditions.csv",
           ["colis_id", "ligne_id", "date_expedition", "transporteur", "poids_kg", "cout_transport"], colis)
    _write(outputDir / "stocks_journaliers.csv", ["produit_code", "date_stock", "stock_fin_jour", "valeur_stock"],
           stocks)
    return {
        "clients": len(clients), "produits": len(produits), "commandes": len(commandes),
        "lignes_commande": len(lignes), "tarification_lignes": len(tarifs), "couts_achat": len(couts),
        "expeditions": len(colis), "stocks_journaliers": len(stocks),
    }


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "marge"
    for table, count in generate(target).items():
        print(f"{table}: {count} lignes")
