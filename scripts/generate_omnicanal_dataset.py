"""Generates `examples/omnicanal/`: a deterministic (seeded) two-channel retail
dataset in CSV, built to exercise the derived tables -- unions and unified
referentials, for two different entities. See examples/omnicanal/README.md
for the expected outcome.

    python scripts/generate_omnicanal_dataset.py [outputDir]

- customers: `clients_web` (integer ids) and `clients_magasin` (text codes)
  are two id spaces over partly the same people -- 40 shared by email, 20
  more by phone only (no email on the store side);
- products: `produits_web` (integer sku) and `articles_boutique` (text codes)
  share 30 products, by EAN;
- sales: `ventes_web` and `ventes_boutique`, each referencing its own
  channel's customers and products. A few store tickets are anonymous (no
  customer) and 3 reference a customer code that doesn't exist (orphans).
"""
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 7
START = date(2024, 1, 1)
DAYS = 180

N_WEB_ONLY = 140
N_STORE_ONLY = 100
N_SHARED_BY_EMAIL = 40
N_SHARED_BY_PHONE = 20
N_BLANK_WEB_EMAILS = 10

N_WEB_ONLY_PRODUCTS = 30
N_STORE_ONLY_PRODUCTS = 20
N_SHARED_PRODUCTS = 30

N_WEB_SALES = 1500
N_STORE_SALES = 1200
N_ANONYMOUS_TICKETS = 40
N_ORPHAN_TICKETS = 3
ORPHAN_CUSTOMER_CODE = "M9999"

PRENOMS = ["Camille", "Lea", "Hugo", "Louis", "Emma", "Jade", "Lucas", "Nina", "Adam", "Chloe", "Noah", "Manon"]
NOMS = ["Martin", "Bernard", "Dubois", "Thomas", "Robert", "Richard", "Petit", "Durand", "Leroy", "Moreau"]
CATEGORIES = ["Mobilier", "Deco", "Cuisine", "Jardin"]
OBJETS = ["Chaise", "Table", "Lampe", "Tapis", "Bol", "Vase", "Coussin", "Etagere", "Poele", "Jardiniere"]


def _people(rng):
    """`[(nom, email, telephone)]`, one per real person, every email and
    phone distinct."""
    total = N_WEB_ONLY + N_STORE_ONLY + N_SHARED_BY_EMAIL + N_SHARED_BY_PHONE
    people = []
    for i in range(total):
        prenom, nom = rng.choice(PRENOMS), rng.choice(NOMS)
        people.append((f"{prenom} {nom}", f"{prenom.lower()}.{nom.lower()}.{i}@exemple.fr", f"06{i:08d}"))
    return people


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.writer(handle)
        writer.writerow(header)
        writer.writerows(rows)


def generate(outputDir):
    rng = random.Random(SEED)
    outputDir = Path(outputDir)
    outputDir.mkdir(parents=True, exist_ok=True)

    people = _people(rng)
    webOnly = people[:N_WEB_ONLY]
    storeOnly = people[N_WEB_ONLY:N_WEB_ONLY + N_STORE_ONLY]
    sharedEmail = people[N_WEB_ONLY + N_STORE_ONLY:N_WEB_ONLY + N_STORE_ONLY + N_SHARED_BY_EMAIL]
    sharedPhone = people[N_WEB_ONLY + N_STORE_ONLY + N_SHARED_BY_EMAIL:]

    webPeople = webOnly + sharedEmail + sharedPhone
    rng.shuffle(webPeople)
    clientsWeb = []
    for i, (nom, email, telephone) in enumerate(webPeople):
        # A blank email ('') must never merge every blank customer into one.
        if (nom, email, telephone) in webOnly[:N_BLANK_WEB_EMAILS]:
            email = ""
        inscription = START - timedelta(days=rng.randrange(1, 900))
        clientsWeb.append((i + 1, nom, email, telephone, inscription.isoformat()))
    _write(outputDir / "clients_web.csv", ["client_id", "nom", "email", "telephone", "date_inscription"], clientsWeb)

    storePeople = [(p, True) for p in storeOnly + sharedEmail] + [(p, False) for p in sharedPhone]
    rng.shuffle(storePeople)
    clientsMagasin = []
    for i, ((nom, email, telephone), hasEmail) in enumerate(storePeople):
        clientsMagasin.append((f"M{i + 1:04d}", nom.upper(), email if hasEmail else None, telephone, f"F{rng.randrange(10**6):06d}"))
    _write(outputDir / "clients_magasin.csv", ["code_client", "nom_client", "mail", "tel", "carte_fidelite"], clientsMagasin)

    products = [
        (f"{rng.choice(OBJETS)} {i}", rng.choice(CATEGORIES), f"3{i:012d}")
        for i in range(N_WEB_ONLY_PRODUCTS + N_STORE_ONLY_PRODUCTS + N_SHARED_PRODUCTS)
    ]
    webProducts = products[:N_WEB_ONLY_PRODUCTS] + products[-N_SHARED_PRODUCTS:]
    storeProducts = products[N_WEB_ONLY_PRODUCTS:]
    produitsWeb = [(1001 + i, libelle, categorie, ean) for i, (libelle, categorie, ean) in enumerate(webProducts)]
    articlesBoutique = [
        (f"A-{i + 1:03d}", libelle.upper(), categorie, ean) for i, (libelle, categorie, ean) in enumerate(storeProducts)
    ]
    _write(outputDir / "produits_web.csv", ["sku", "libelle", "categorie", "ean"], produitsWeb)
    _write(outputDir / "articles_boutique.csv", ["code_article", "designation", "rayon", "ean13"], articlesBoutique)

    ventesWeb = []
    for i in range(N_WEB_SALES):
        quantite = rng.randint(1, 5)
        prix = round(rng.uniform(5, 300), 2)
        ventesWeb.append((
            100001 + i, (START + timedelta(days=rng.randrange(DAYS))).isoformat(), rng.choice(clientsWeb)[0],
            rng.choice(produitsWeb)[0], quantite, round(quantite * prix, 2),
        ))
    _write(outputDir / "ventes_web.csv", ["vente_id", "date_vente", "client_id", "sku", "quantite", "montant"], ventesWeb)

    ventesBoutique = []
    for i in range(N_STORE_SALES):
        if i < N_ANONYMOUS_TICKETS:
            client = None
        elif i < N_ANONYMOUS_TICKETS + N_ORPHAN_TICKETS:
            client = ORPHAN_CUSTOMER_CODE
        else:
            client = rng.choice(clientsMagasin)[0]
        quantite = rng.randint(1, 5)
        prix = round(rng.uniform(5, 300), 2)
        ventesBoutique.append((
            f"T{i + 1:06d}", (START + timedelta(days=rng.randrange(DAYS))).isoformat(), client,
            rng.choice(articlesBoutique)[0], quantite, round(quantite * prix, 2),
        ))
    rng.shuffle(ventesBoutique)
    _write(
        outputDir / "ventes_boutique.csv",
        ["ticket_id", "date_ticket", "code_client", "code_article", "quantite", "montant_ttc"], ventesBoutique,
    )


if __name__ == "__main__":
    generate(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "omnicanal")
