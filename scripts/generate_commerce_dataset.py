"""Generates `examples/commerce/`: a complete, deterministic (seeded) retail
dataset in CSV, built to exercise every case the v3 chain distinguishes --
see examples/commerce/README.md for the expected outcome of each table and
column.

    python scripts/generate_commerce_dataset.py [outputDir]
"""
import csv
import random
import sys
import uuid
from datetime import date, timedelta
from pathlib import Path

SEED = 42
START = date(2024, 1, 1)
DAYS = 365

N_CLIENTS = 400
N_PRODUITS = 120
N_EMPLOYES = 40
N_COMMANDES = 3000

VILLES = [
    ("Paris", "Ile-de-France"), ("Lyon", "Auvergne-Rhone-Alpes"), ("Marseille", "Provence-Alpes-Cote d'Azur"),
    ("Lille", "Hauts-de-France"), ("Bordeaux", "Nouvelle-Aquitaine"), ("Nantes", "Pays de la Loire"),
    ("Toulouse", "Occitanie"), ("Strasbourg", "Grand Est"),
]
PRENOMS = ["Camille", "Lea", "Hugo", "Louis", "Emma", "Jade", "Lucas", "Nina", "Adam", "Chloe", "Noah", "Manon"]
NOMS = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau",
        "Simon", "Laurent", "Lefebvre", "Michel", "Garcia", "David"]
SEGMENTS = ["particulier", "professionnel", "premium"]
CATALOGUE = {
    "Mobilier": {"Assise": ["Chaise", "Tabouret", "Fauteuil"], "Rangement": ["Etagere", "Commode", "Armoire"]},
    "Deco": {"Luminaire": ["Lampe", "Suspension", "Applique"], "Textile": ["Tapis", "Coussin", "Rideau"]},
    "Cuisine": {"Ustensile": ["Poele", "Casserole", "Couteau"], "Art de la table": ["Assiette", "Verre", "Bol"]},
}
MARQUES = ["Nordika", "Atelier B", "Maison Lune", "Kobo", "Verdi"]
MAGASINS = [  # magasin_id 1..12: small integers on purpose (see `quantite` in the README)
    (i + 1, f"Kawa {ville} {suffixe}", ville, region)
    for i, ((ville, region), suffixe) in enumerate(
        [(v, s) for v in VILLES[:6] for s in ("Centre", "Sud")]
    )
]
ENTREPOTS = [  # entrepot_code -> ville, region: a dimension with no table of its own
    ("ENT-NORD", "Lille", "Hauts-de-France"), ("ENT-EST", "Strasbourg", "Grand Est"),
    ("ENT-IDF", "Gonesse", "Ile-de-France"), ("ENT-OUEST", "Nantes", "Pays de la Loire"),
    ("ENT-SUD", "Marseille", "Provence-Alpes-Cote d'Azur"), ("ENT-SO", "Toulouse", "Occitanie"),
]
PROMOS = [  # code_promo -> libelle_promo (taux_remise is a decimal: it stays a measure)
    ("AUCUNE", "Sans promotion", 0.0), ("BIENVENUE10", "Offre de bienvenue", 0.10),
    ("SOLDES20", "Soldes d'hiver", 0.20), ("ETE15", "Operation ete", 0.15),
    ("FIDELITE5", "Programme fidelite", 0.05), ("BLACKFRI30", "Black Friday", 0.30),
    ("NOEL25", "Fetes de fin d'annee", 0.25), ("FLASH40", "Vente flash", 0.40),
]
MODES_LIVRAISON = ["domicile", "point_relais", "retrait_magasin"]
CANAUX = ["web", "magasin", "telephone"]
STATUTS = ["livree", "livree", "livree", "expediee", "annulee"]
MOTIFS = [("DEF", "Produit defectueux"), ("TAI", "Taille ou dimension"), ("ERR", "Erreur de commande"),
          ("RET", "Livraison en retard"), ("AVI", "Ne correspond pas a la description")]
COMMENTAIRES = ["livrer apres 18h", "fragile", "cadeau, ne pas mettre la facture", "sonner deux fois",
                "laisser au gardien", "appeler avant", "emballage soigne", "etage 3 sans ascenseur"]


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def generate(outputDir):
    rng = random.Random(SEED)
    outputDir.mkdir(parents=True, exist_ok=True)

    clients = []
    for i in range(N_CLIENTS):
        ville, _ = rng.choice(VILLES)
        prenom, nom = rng.choice(PRENOMS), rng.choice(NOMS)
        inscription = START - timedelta(days=rng.randint(0, 1500))
        clients.append((
            10001 + i, nom, prenom, f"{prenom.lower()}.{nom.lower()}{i}@exemple.fr", ville,
            rng.choice(SEGMENTS), inscription.strftime("%d/%m/%Y"),
        ))
    _write(outputDir / "clients.csv",
           ["client_id", "nom", "prenom", "email", "ville", "segment", "date_inscription"], clients)

    produits = []
    for i in range(N_PRODUITS):
        categorie = rng.choice(list(CATALOGUE))
        sousCategorie = rng.choice(list(CATALOGUE[categorie]))
        article = rng.choice(CATALOGUE[categorie][sousCategorie])
        marque = rng.choice(MARQUES)
        produits.append((
            201 + i, f"PRD-{201 + i:04d}", f"{article} {marque} {i % 7 + 1}", categorie, sousCategorie, marque,
            round(rng.uniform(4, 400), 2),
        ))
    _write(outputDir / "produits.csv",
           ["produit_id", "reference", "nom_produit", "categorie", "sous_categorie", "marque", "prix_catalogue"],
           produits)
    prixById = {p[0]: p[6] for p in produits}

    _write(outputDir / "magasins.csv", ["magasin_id", "nom_magasin", "ville", "region"], MAGASINS)

    employes = [
        (101 + i, rng.choice(NOMS), rng.choice(PRENOMS), rng.choice(["vendeur", "vendeur", "responsable"]),
         rng.choice(MAGASINS)[0])
        for i in range(N_EMPLOYES)
    ]
    _write(outputDir / "employes.csv", ["employe_id", "nom", "prenom", "poste", "magasin_id"], employes)

    commandes, lignes = [], []
    ligneId = 9000001
    for i in range(N_COMMANDES):
        commandeId = 500001 + i
        dateCommande = START + timedelta(days=rng.randrange(DAYS))
        canal = rng.choice(CANAUX)
        magasinId = rng.choice(MAGASINS)[0] if canal == "magasin" else ""
        vendeurId = rng.choice(employes)[0] if canal != "web" else ""
        commandes.append((
            commandeId, rng.choice(clients)[0], dateCommande.isoformat(), canal, rng.choice(STATUTS),
            magasinId, vendeurId,
        ))

        modeLivraison = "retrait_magasin" if canal == "magasin" else rng.choice(MODES_LIVRAISON[:2])
        for produitId in rng.sample([p[0] for p in produits], rng.randint(1, 5)):
            code, libelle, taux = rng.choice(PROMOS) if rng.random() < 0.4 else PROMOS[0]
            entrepot = rng.choice(ENTREPOTS)
            quantite = rng.randint(1, 10)
            prix = round(prixById[produitId] * rng.uniform(0.9, 1.1), 2)
            remise = round(quantite * prix * taux, 2)
            lignes.append((
                ligneId, str(uuid.UUID(int=rng.getrandbits(128))), commandeId, produitId,
                (dateCommande + timedelta(days=rng.randint(1, 10))).isoformat(),
                code, libelle, entrepot[0], entrepot[1], entrepot[2], modeLivraison,
                quantite, prix, round(quantite * prix - remise, 2), remise,
                rng.choice(COMMENTAIRES) + f" #{ligneId}" if rng.random() < 0.05 else "",
                "EUR", "",
            ))
            ligneId += 1

    _write(outputDir / "commandes.csv",
           ["commande_id", "client_id", "date_commande", "canal", "statut", "magasin_id", "vendeur_id"], commandes)
    _write(outputDir / "lignes_commande.csv", [
        "ligne_id", "ligne_uuid", "commande_id", "produit_id", "date_livraison", "code_promo", "libelle_promo",
        "entrepot_code", "entrepot_ville", "entrepot_region", "mode_livraison", "quantite", "prix_unitaire",
        "montant_ligne", "remise", "commentaire", "devise", "code_tva_legacy",
    ], lignes)

    retours = []
    retourId = 70001
    for ligne in rng.sample(lignes, 400):
        motifCode, motifLibelle = rng.choice(MOTIFS)
        dateRetour = date.fromisoformat(ligne[4]) + timedelta(days=rng.randint(1, 30))
        # Partial returns: some lines are returned twice, same day, same reason
        # -- the grain of `retours` (ligne_id, date_retour, motif_code) is NOT unique.
        for _ in range(2 if rng.random() < 0.03 else 1):
            quantiteRetournee = rng.randint(1, ligne[11])
            retours.append((
                retourId, ligne[0], dateRetour.isoformat(), motifCode, motifLibelle, quantiteRetournee,
                round(quantiteRetournee * ligne[12], 2),
            ))
            retourId += 1
    _write(outputDir / "retours.csv", [
        "retour_id", "ligne_id", "date_retour", "motif_code", "motif_libelle", "quantite_retournee",
        "montant_rembourse",
    ], retours)

    return {name: len(rows) for name, rows in (
        ("clients", clients), ("produits", produits), ("magasins", MAGASINS), ("employes", employes),
        ("commandes", commandes), ("lignes_commande", lignes), ("retours", retours),
    )}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "commerce"
    for tableName, rowCount in generate(target).items():
        print(f"{tableName}: {rowCount} lignes")
