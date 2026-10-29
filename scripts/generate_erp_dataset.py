"""Generates `examples/erp/`: a complete, deterministic (seeded) multi-company
ERP extract with several real fact tables (ventes, achats, stock, comptabilite,
creances, dettes) around real, attribute-bearing dimensions -- the opposite of
`examples/reel/`, a single flat table with no reference data to link against.
See examples/erp/README.md for what each table and trap tests.

    python scripts/generate_erp_dataset.py [outputDir]
"""
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 11
START = date(2024, 1, 1)
DAYS = 730
LOADED_AT = "2026-10-29 06:00:00"
SOURCE_SYSTEM = "ERP_CORE"

N_CLIENTS = 250
N_FOURNISSEURS = 80
N_PRODUITS = 300
N_ENTREPOTS = 10
N_EMPLOYES = 60
N_COMPTES_GL = 40
N_COMMANDES_VENTE = 1800
N_PO_ACHAT = 700
N_MOUVEMENTS = 8000
N_ECRITURES = 3000
N_FACTURES_CLIENTS = 2000
N_FACTURES_FOURNISSEURS = 900

SOCIETES = [
    (1, "Kawa Industries FR", "France", "EUR"),
    (2, "Kawa Industries DE", "Allemagne", "EUR"),
    (3, "Kawa Industries UK", "Royaume-Uni", "GBP"),
    (4, "Kawa Industries US", "Etats-Unis", "USD"),
    (5, "Kawa Industries CH", "Suisse", "CHF"),
    (6, "Kawa Industries JP", "Japon", "JPY"),
]
DEVISE_PAR_SOCIETE = {societeId: devise for societeId, _, _, devise in SOCIETES}
DEVISES = [
    ("EUR", "Euro", "Europe"), ("USD", "Dollar americain", "Amerique du Nord"),
    ("GBP", "Livre sterling", "Europe"), ("CHF", "Franc suisse", "Europe"),
    ("JPY", "Yen japonais", "Asie"),
]

VILLES = [
    ("Paris", "France"), ("Lyon", "France"), ("Hambourg", "Allemagne"), ("Munich", "Allemagne"),
    ("Londres", "Royaume-Uni"), ("Manchester", "Royaume-Uni"), ("New York", "Etats-Unis"),
    ("Chicago", "Etats-Unis"), ("Geneve", "Suisse"), ("Zurich", "Suisse"), ("Osaka", "Japon"), ("Tokyo", "Japon"),
]
SEGMENTS = ["PME", "grand_compte", "administration", "independant"]
CATEGORIES_ACHAT = ["matieres_premieres", "emballage", "services", "transport", "informatique"]
RACINES = ["Nord", "Delta", "Atlas", "Horizon", "Boreal", "Cypres", "Equinoxe", "Rivage", "Comptoir", "Lumiere"]
FORMES = ["SARL", "SAS", "SA", "GmbH", "Ltd", "Inc", "AG", "KK"]

FAMILLES_PRODUIT = {
    "composants": ("unite", (2.0, 45.0)), "emballages": ("carton", (0.5, 12.0)),
    "consommables": ("kg", (1.0, 30.0)), "equipements": ("unite", (80.0, 900.0)),
    "services": ("heure", (20.0, 150.0)),
}
TYPES_ENTREPOT = ["usine", "plateforme_distribution", "magasin"]
POSTES = {"vendeur": "ventes", "acheteur": "achats", "comptable": "finance", "magasinier": "logistique"}
CANAUX = ["web", "agence", "telephone"]
STATUTS_PO = ["brouillon", "validee", "receptionnee", "cloturee"]
TYPES_MOUVEMENT = ["reception", "sortie", "transfert_entrant", "transfert_sortant", "ajustement"]
JOURNAUX = ["VTE", "ACH", "BQ", "OD", "PAI"]
TYPES_DOC = ["facture", "facture", "facture", "facture", "facture", "facture", "facture", "facture", "facture",
             "avoir"]
STATUTS_PAIEMENT = ["paye", "paye", "partiel", "en_attente", "en_retard"]


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _randomDate(rng, spanDays=DAYS):
    return START + timedelta(days=rng.randrange(spanDays))


def generate(outputDir):
    rng = random.Random(SEED)
    outputDir.mkdir(parents=True, exist_ok=True)

    _write(outputDir / "societes.csv", ["societe_id", "raison_sociale", "pays", "devise_base"], SOCIETES)
    _write(outputDir / "devises.csv", ["code", "libelle", "zone"], DEVISES)

    clients = []
    for i in range(1, N_CLIENTS + 1):
        ville, pays = rng.choice(VILLES)
        societeId = rng.choice(SOCIETES)[0]
        creation = START - timedelta(days=rng.randint(0, 2000))
        clients.append((
            i, f"{rng.choice(RACINES)} {rng.choice(FORMES)} {i}", pays, ville, rng.choice(SEGMENTS), societeId,
            creation.isoformat(), 1 if rng.random() > 0.04 else 0,
        ))
    _write(outputDir / "clients.csv", [
        "client_id", "raison_sociale", "pays", "ville", "segment", "societe_id", "date_creation", "actif",
    ], clients)

    fournisseurs = []
    for i in range(N_FOURNISSEURS):
        fournisseurId = 5001 + i
        ville, pays = rng.choice(VILLES)
        creation = START - timedelta(days=rng.randint(0, 3000))
        fournisseurs.append((
            fournisseurId, f"{rng.choice(RACINES)} {rng.choice(FORMES)} Supply {i}", pays,
            rng.choice(CATEGORIES_ACHAT), creation.isoformat(), 1 if rng.random() > 0.03 else 0,
        ))
    _write(outputDir / "fournisseurs.csv", [
        "fournisseur_id", "raison_sociale", "pays", "categorie_achat", "date_creation", "actif",
    ], fournisseurs)

    produits = []
    for i in range(1, N_PRODUITS + 1):
        famille = rng.choice(list(FAMILLES_PRODUIT))
        uom, (coutMin, coutMax) = FAMILLES_PRODUIT[famille]
        coutStandard = round(rng.uniform(coutMin, coutMax), 2)
        produits.append((
            i, f"REF-{i:05d}", f"{famille[:-1].capitalize()} {i}", famille, rng.choice(["standard", "premium"]),
            uom, round(coutStandard * rng.uniform(1.3, 1.8), 2), coutStandard,
        ))
    _write(outputDir / "produits.csv", [
        "produit_id", "reference", "libelle", "famille", "categorie", "uom", "prix_catalogue", "cout_standard",
    ], produits)
    produitById = {p[0]: p for p in produits}

    entrepots = [
        (i, f"Site {rng.choice(RACINES)} {i}", *rng.choice(VILLES), rng.choice(TYPES_ENTREPOT))
        for i in range(1, N_ENTREPOTS + 1)
    ]
    entrepots = [(eid, nom, ville, pays, typeEntrepot) for eid, nom, ville, pays, typeEntrepot in entrepots]
    _write(outputDir / "entrepots.csv", ["entrepot_id", "nom", "ville", "pays", "type"], entrepots)

    employes = []
    for i in range(1, N_EMPLOYES + 1):
        poste = rng.choice(list(POSTES))
        employes.append((
            i, f"Employe{i}", rng.choice(["Camille", "Hugo", "Lea", "Noah", "Mia", "Louis"]), poste,
            POSTES[poste], rng.randint(1, N_ENTREPOTS),
        ))
    _write(outputDir / "employes.csv", [
        "employe_id", "nom", "prenom", "poste", "departement", "entrepot_id",
    ], employes)
    vendeurs = [e[0] for e in employes if e[3] == "vendeur"]
    acheteurs = [e[0] for e in employes if e[3] == "acheteur"]

    comptes = []
    for i in range(N_COMPTES_GL):
        typeCompte = ["actif", "passif", "produit", "charge"][i % 4]
        comptes.append((f"{(4 + i % 4)}{i:04d}", f"Compte {typeCompte} {i}", typeCompte, 1))
    _write(outputDir / "comptes_gl.csv", ["compte_gl", "libelle", "type_compte", "actif"], comptes)

    ventes = []
    ligneId = 1
    for n in range(N_COMMANDES_VENTE):
        commandeId = 900001 + n
        client = rng.choice(clients)
        societeId = client[5]
        dateCommande = _randomDate(rng)
        canal = rng.choice(CANAUX)
        vendeurId = rng.choice(vendeurs) if canal != "web" else None
        for produitId in rng.sample(list(produitById), rng.choice([1, 2, 2, 3, 4, 5])):
            produit = produitById[produitId]
            entrepotId = rng.randint(1, N_ENTREPOTS)
            quantite = rng.randint(1, 10)
            prixUnitaire = round(produit[6] * rng.uniform(0.9, 1.1), 2)
            montantHt = round(quantite * prixUnitaire, 2)
            ventes.append((
                ligneId, commandeId, client[0], produitId, societeId, entrepotId, vendeurId, canal,
                dateCommande.isoformat(), quantite, prixUnitaire, montantHt, round(montantHt * 0.20, 2),
                DEVISE_PAR_SOCIETE[societeId], SOURCE_SYSTEM, LOADED_AT,
            ))
            ligneId += 1
    _write(outputDir / "ventes_lignes.csv", [
        "ligne_id", "commande_id", "client_id", "produit_id", "societe_id", "entrepot_id", "vendeur_id", "canal",
        "date_commande", "quantite", "prix_unitaire", "montant_ht", "montant_tva", "devise", "source_system",
        "loaded_at",
    ], ventes)

    achats = []
    ligneId = 1
    for n in range(N_PO_ACHAT):
        poId = 700001 + n
        fournisseur = rng.choice(fournisseurs)
        societeId = rng.choice(SOCIETES)[0]
        dateCommande = _randomDate(rng)
        statut = rng.choice(STATUTS_PO)
        dateReception = (
            None if statut in ("brouillon", "validee")
            else (dateCommande + timedelta(days=rng.randint(3, 30))).isoformat()
        )
        for produitId in rng.sample(list(produitById), rng.choice([1, 2, 2, 3, 4])):
            produit = produitById[produitId]
            entrepotId = rng.randint(1, N_ENTREPOTS)
            quantite = rng.randint(1, 10)
            prixUnitaire = round(produit[7] * rng.uniform(0.85, 1.05), 2)
            achats.append((
                ligneId, poId, fournisseur[0], produitId, societeId, entrepotId, rng.choice(acheteurs), statut,
                dateCommande.isoformat(), dateReception, quantite, prixUnitaire, round(quantite * prixUnitaire, 2),
                DEVISE_PAR_SOCIETE[societeId], SOURCE_SYSTEM, LOADED_AT,
            ))
            ligneId += 1
    _write(outputDir / "achats_lignes.csv", [
        "ligne_id", "po_id", "fournisseur_id", "produit_id", "societe_id", "entrepot_id", "acheteur_id", "statut",
        "date_commande", "date_reception", "quantite", "prix_unitaire", "montant_ht", "devise", "source_system",
        "loaded_at",
    ], achats)

    mouvements = []
    for m in range(1, N_MOUVEMENTS + 1):
        produitId = rng.randint(1, N_PRODUITS)
        produit = produitById[produitId]
        typeMouvement = rng.choice(TYPES_MOUVEMENT)
        signe = 1 if typeMouvement in ("reception", "transfert_entrant") else -1
        quantite = signe * rng.randint(1, 500)
        mouvements.append((
            m, produitId, rng.randint(1, N_ENTREPOTS), typeMouvement, _randomDate(rng).isoformat(), quantite,
            round(quantite * produit[7], 2), f"MVT-{m:07d}", SOURCE_SYSTEM, LOADED_AT,
        ))
    _write(outputDir / "mouvements_stock.csv", [
        "mouvement_id", "produit_id", "entrepot_id", "type_mouvement", "date_mouvement", "quantite",
        "valeur_mouvement", "piece_origine", "source_system", "loaded_at",
    ], mouvements)

    ecritures = []
    for e in range(1, N_ECRITURES + 1):
        societeId = rng.choice(SOCIETES)[0]
        journalCode = rng.choice(JOURNAUX)
        dateEcriture = _randomDate(rng)
        devise = DEVISE_PAR_SOCIETE[societeId]
        nbLignes = rng.choice([2, 2, 3, 4])
        total = 0.0
        for numeroLigne in range(1, nbLignes):
            montant = round(rng.uniform(20, 5000), 2)
            total += montant
            debit = montant if rng.random() < 0.5 else None
            credit = None if debit is not None else montant
            ecritures.append((
                e, numeroLigne, rng.choice(comptes)[0], societeId, journalCode, dateEcriture.isoformat(), devise,
                debit, credit, f"Piece {e}-{numeroLigne}", SOURCE_SYSTEM, LOADED_AT,
            ))
        # Balancing line: whichever side is short gets the running difference, so debit == credit for the entry.
        debitSum = sum(row[7] for row in ecritures[-(nbLignes - 1):] if row[7] is not None)
        creditSum = sum(row[8] for row in ecritures[-(nbLignes - 1):] if row[8] is not None)
        ecart = round(debitSum - creditSum, 2)
        ecritures.append((
            e, nbLignes, rng.choice(comptes)[0], societeId, journalCode, dateEcriture.isoformat(), devise,
            None if ecart >= 0 else -ecart, ecart if ecart >= 0 else None, f"Piece {e}-{nbLignes} (contrepartie)",
            SOURCE_SYSTEM, LOADED_AT,
        ))
    _write(outputDir / "ecritures_gl.csv", [
        "ecriture_id", "numero_ligne", "compte_gl", "societe_id", "journal_code", "date_ecriture", "devise",
        "montant_debit", "montant_credit", "libelle_piece", "source_system", "loaded_at",
    ], ecritures)

    facturesClients = []
    for d in range(1, N_FACTURES_CLIENTS + 1):
        client = rng.choice(clients)
        societeId = client[5]
        dateFacture = _randomDate(rng)
        delaiPaiement = rng.choice([15, 30, 45, 60])
        montantBrut = round(rng.uniform(80, 25000), 2)
        statut = rng.choice(STATUTS_PAIEMENT)
        montantOuvert = 0.0 if statut == "paye" else round(montantBrut * rng.uniform(0.1, 1.0), 2)
        facturesClients.append((
            900000 + d, rng.choice(TYPES_DOC), societeId, client[0],
            dateFacture.isoformat(), (dateFacture + timedelta(days=delaiPaiement)).isoformat(),
            DEVISE_PAR_SOCIETE[societeId], montantBrut, montantOuvert, statut, SOURCE_SYSTEM, LOADED_AT,
        ))
    _write(outputDir / "factures_clients.csv", [
        "doc_id", "type_doc", "societe_id", "ref_tiers", "date_facture", "date_echeance", "devise", "montant_brut",
        "montant_ouvert", "statut_paiement", "source_system", "loaded_at",
    ], facturesClients)

    facturesFournisseurs = []
    for d in range(1, N_FACTURES_FOURNISSEURS + 1):
        fournisseur = rng.choice(fournisseurs)
        societeId = rng.choice(SOCIETES)[0]
        dateFacture = _randomDate(rng)
        delaiPaiement = rng.choice([30, 45, 60, 90])
        montantBrut = round(rng.uniform(150, 60000), 2)
        statut = rng.choice(STATUTS_PAIEMENT)
        montantOuvert = 0.0 if statut == "paye" else round(montantBrut * rng.uniform(0.1, 1.0), 2)
        facturesFournisseurs.append((
            800000 + d, rng.choice(TYPES_DOC), societeId, fournisseur[0],
            dateFacture.isoformat(), (dateFacture + timedelta(days=delaiPaiement)).isoformat(),
            DEVISE_PAR_SOCIETE[societeId], montantBrut, montantOuvert, statut, SOURCE_SYSTEM, LOADED_AT,
        ))
    _write(outputDir / "factures_fournisseurs.csv", [
        "doc_id", "type_doc", "societe_id", "ref_tiers", "date_facture", "date_echeance", "devise", "montant_brut",
        "montant_ouvert", "statut_paiement", "source_system", "loaded_at",
    ], facturesFournisseurs)

    return {name: len(rows) for name, rows in (
        ("societes", SOCIETES), ("devises", DEVISES), ("clients", clients), ("fournisseurs", fournisseurs),
        ("produits", produits), ("entrepots", entrepots), ("employes", employes), ("comptes_gl", comptes),
        ("ventes_lignes", ventes), ("achats_lignes", achats), ("mouvements_stock", mouvements),
        ("ecritures_gl", ecritures), ("factures_clients", facturesClients),
        ("factures_fournisseurs", facturesFournisseurs),
    )}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "erp"
    for tableName, rowCount in generate(target).items():
        print(f"{tableName}: {rowCount} lignes")
