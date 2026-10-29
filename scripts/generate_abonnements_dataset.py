"""Generates `examples/abonnements/`: a deliberately adversarial SaaS billing
dataset -- unlike `examples/commerce/` or `examples/marge/`, every table here
is built to hit a specific *known gap* of the v3 chain (see its own README's
"Pas encore fait" section) rather than a case the chain already handles.
Nothing in this dataset is expected to come back clean.

    python scripts/generate_abonnements_dataset.py [outputDir]
"""
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 2026
START = date(2024, 1, 1)
DAYS = 700
NOW = START + timedelta(days=DAYS)
LOADED_AT = "2026-10-29 06:00:00"
SOURCE_SYSTEM = "BILLING_CORE"

N_CLIENTS = 220
PART_CHANGEMENT_PLAN = 0.15   # share of subscriptions with a mid-life plan change (SCD2 trap)
PART_AVOIR = 0.05             # share of invoices credited by a matching avoir (self-FK trap)
N_EVENEMENTS = 7000
PART_FONCTIONNALITE_RETIREE = 0.03  # share of usage events pointing at a retired feature id (orphan FK)

PAYS = ["France", "Allemagne", "Espagne", "Italie", "Belgique", "Pays-Bas", "Pologne", "Suede"]
SEGMENTS = ["startup", "pme", "grand_compte", "agence"]
RACINES = ["Nova", "Pixel", "Cloudly", "Dataform", "Flowbase", "Gridly", "Opsy", "Streamline"]
SUFFIXES = ["SAS", "GmbH", "SL", "Srl", "BV", "AB", "SA"]

PLANS = [  # plan_id, nom_plan (unique -> ex-aequo PK candidate with plan_id), palier, tarif_mensuel
    (1, "Starter", "entree", 19.0), (2, "Starter Plus", "entree", 29.0), (3, "Pro", "milieu", 59.0),
    (4, "Pro Max", "milieu", 89.0), (5, "Business", "haut", 149.0), (6, "Enterprise", "haut", 299.0),
]
PALIER_PAR_PLAN = {p[0]: p[2] for p in PLANS}

FONCTIONNALITES = [
    (1, "export_csv", "donnees"), (2, "export_pdf", "donnees"), (3, "api_publique", "integration"),
    (4, "webhooks", "integration"), (5, "sso", "securite"), (6, "audit_log", "securite"),
    (7, "roles_avances", "securite"), (8, "dashboard_custom", "reporting"), (9, "alertes", "reporting"),
    (10, "export_programme", "reporting"), (11, "support_prioritaire", "support"), (12, "onboarding_dedie", "support"),
    (13, "multi_espace", "organisation"), (14, "sso_scim", "organisation"), (15, "quotas_etendus", "organisation"),
    (16, "ia_assistant", "ia"), (17, "ia_resume", "ia"), (18, "ia_classification", "ia"),
    (19, "sandbox", "integration"), (20, "connecteurs_tiers", "integration"),
]
# Retired feature ids (21-23) exist in no dimension row: evenements_usage references them anyway.
FONCTIONNALITES_RETIREES = [21, 22, 23]
FONCTIONNALITES_PAR_PALIER = {
    "entree": [1, 2, 9], "milieu": [1, 2, 3, 4, 8, 9, 11], "haut": list(range(1, 21)),
}
STATUTS_ABO = ["actif", "annule", "suspendu"]


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def _formatDateMixte(rng, d):
    # ~30% jj/mm/aaaa, ~70% ISO, in the SAME column -- only the dominant format
    # (ISO) is expected to be read; the minority format is the trap.
    return d.strftime("%d/%m/%Y") if rng.random() < 0.3 else d.isoformat()


def generate(outputDir):
    rng = random.Random(SEED)
    outputDir.mkdir(parents=True, exist_ok=True)

    clients = []
    for i in range(N_CLIENTS):
        clientId = 10001 + i
        inscription = START - timedelta(days=rng.randint(0, 900))
        clients.append((
            clientId, f"{rng.choice(RACINES)} {rng.choice(SUFFIXES)} {i}", rng.choice(PAYS), rng.choice(SEGMENTS),
            rng.randint(1, 8), _formatDateMixte(rng, inscription),
        ))
    _write(outputDir / "clients.csv", [
        "client_id", "raison_sociale", "pays", "segment", "code_region", "date_inscription",
    ], clients)
    clientIds = [c[0] for c in clients]

    _write(outputDir / "plans.csv", ["plan_id", "nom_plan", "palier", "tarif_mensuel"], PLANS)

    _write(outputDir / "fonctionnalites.csv", ["fonctionnalite_id", "nom_fonctionnalite", "categorie"],
           FONCTIONNALITES)

    planFonctionnalites = [
        (planId, fid) for planId, _, palier, _ in PLANS for fid in FONCTIONNALITES_PAR_PALIER[palier]
    ]
    _write(outputDir / "plans_fonctionnalites.csv", ["plan_id", "fonctionnalite_id"], planFonctionnalites)

    abonnements = []
    abonnementId = 1
    abonnementVersionsParClient = {}
    for clientId in clientIds:
        logiqueId = clientId  # abonnement_logique_id: looks like an ordinary column, not flagged specially
        plan = rng.choice(PLANS)
        debut = START + timedelta(days=rng.randint(0, DAYS - 180))
        if rng.random() < PART_CHANGEMENT_PLAN:
            changement = debut + timedelta(days=rng.randint(30, 300))
            finAbonnement = None if rng.random() < 0.7 else changement + timedelta(days=rng.randint(30, 200))
            nouveauPlan = rng.choice([p for p in PLANS if p[0] != plan[0]])
            versions = [
                (debut, changement - timedelta(days=1), plan, changement),
                (changement, finAbonnement, nouveauPlan, finAbonnement),
            ]
        else:
            fin = None if rng.random() < 0.65 else debut + timedelta(days=rng.randint(60, 500))
            versions = [(debut, fin, plan, fin)]

        versionIds = []
        for dateDebut, dateFin, planVersion, validTo in versions:
            statut = "actif" if dateFin is None or dateFin > NOW else "annule"
            if statut == "actif" and rng.random() < 0.04:
                statut = "suspendu"
            abonnements.append((
                abonnementId, logiqueId, clientId, planVersion[0], dateDebut.isoformat(),
                dateFin.isoformat() if dateFin else None, planVersion[3], statut,
                LOADED_AT[:10], dateDebut.isoformat(), validTo.isoformat() if validTo else None, LOADED_AT,
                LOADED_AT,
            ))
            versionIds.append((abonnementId, dateDebut, dateFin or NOW, planVersion))
            abonnementId += 1
        abonnementVersionsParClient[clientId] = versionIds

    _write(outputDir / "abonnements.csv", [
        "abonnement_id", "abonnement_logique_id", "client_id", "plan_id", "date_debut", "date_fin",
        "tarif_mensuel_effectif", "statut", "SNAPSHOT_DATE", "DBT_VALID_FROM", "DBT_VALID_TO", "EXTRACTED_AT",
        "LOADED_AT",
    ], abonnements)

    factures = []
    factureId = 1
    compteurPieceParClientAnnee = {}
    facturesParClient = {}
    for clientId, versions in abonnementVersionsParClient.items():
        for aboId, dateDebut, dateFinEffective, planVersion in versions:
            mois = dateDebut.replace(day=1)
            while mois <= dateFinEffective and mois <= NOW:
                montantHt = planVersion[3]
                cle = (clientId, mois.year)
                compteurPieceParClientAnnee[cle] = compteurPieceParClientAnnee.get(cle, 0) + 1
                factures.append([
                    factureId, clientId, aboId, mois.isoformat(), montantHt, round(montantHt * 0.20, 2),
                    "facture", None, compteurPieceParClientAnnee[cle], SOURCE_SYSTEM, LOADED_AT,
                ])
                facturesParClient.setdefault(clientId, []).append(factureId)
                factureId += 1
                mois = date(mois.year + (mois.month == 12), mois.month % 12 + 1, 1)

    for clientId, factureIds in facturesParClient.items():
        for origineId in factureIds:
            if rng.random() < PART_AVOIR:
                origine = next(f for f in factures if f[0] == origineId)
                cle = (clientId, date.fromisoformat(origine[3]).year)
                compteurPieceParClientAnnee[cle] = compteurPieceParClientAnnee.get(cle, 0) + 1
                factures.append([
                    factureId, clientId, origine[2], origine[3], -origine[4], -origine[5], "avoir", origineId,
                    compteurPieceParClientAnnee[cle], SOURCE_SYSTEM, LOADED_AT,
                ])
                factureId += 1

    _write(outputDir / "factures.csv", [
        "facture_id", "client_id", "abonnement_id", "date_facture", "montant_ht", "montant_tva", "type_doc",
        "facture_origine_id", "numero_piece", "source_system", "loaded_at",
    ], factures)

    evenements = []
    for e in range(1, N_EVENEMENTS + 1):
        clientId = rng.choice(clientIds)
        if rng.random() < PART_FONCTIONNALITE_RETIREE:
            fonctionnaliteId = rng.choice(FONCTIONNALITES_RETIREES)
        else:
            fonctionnaliteId = rng.randint(1, 20)
        dateEvenement = _randomDate(rng)
        evenements.append((
            e, clientId, fonctionnaliteId, dateEvenement.isoformat(), rng.randint(1, 20), rng.randint(5, 4000),
            dateEvenement.isocalendar()[1], SOURCE_SYSTEM, LOADED_AT,
        ))
    _write(outputDir / "evenements_usage.csv", [
        "evenement_id", "client_id", "fonctionnalite_id", "date_evenement", "nb_appels", "duree_ms",
        "semaine_iso", "source_system", "loaded_at",
    ], evenements)

    return {name: len(rows) for name, rows in (
        ("clients", clients), ("plans", PLANS), ("fonctionnalites", FONCTIONNALITES),
        ("plans_fonctionnalites", planFonctionnalites), ("abonnements", abonnements), ("factures", factures),
        ("evenements_usage", evenements),
    )}


def _randomDate(rng, spanDays=DAYS):
    return START + timedelta(days=rng.randrange(spanDays))


if __name__ == "__main__":
    target = (
        Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "abonnements"
    )
    for tableName, rowCount in generate(target).items():
        print(f"{tableName}: {rowCount} lignes")
