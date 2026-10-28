"""Generates `examples/jde_liens/`: a small, synthetic, JD Edwards-shaped
receivables ledger whose links all follow JDE's "table prefix + field"
naming -- `RPAN8` (F03B11) designates `ABAN8` (F0101), so no name ever
corroborates a link. Built to exercise the LLM link arbitration; see
examples/jde_liens/README.md.

    python scripts/generate_jde_liens_dataset.py [outputDir]
"""
import csv
import random
import sys
from datetime import date, timedelta
from pathlib import Path

SEED = 11
N_FACTURES = 3000
N_CLIENTS = 250
SOCIETES = [(1, "Kawa Distribution France"), (2, "Kawa Benelux"), (3, "Kawa Iberica"),
            (4, "Kawa Services"), (5, "Kawa Holding")]
CENTRES = ["M10", "M20", "M30", "M40", "S10", "S20", "D10"]


def _write(path, header, rows):
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(header)
        writer.writerows(rows)


def generate(outputDir):
    rng = random.Random(SEED)
    outputDir.mkdir(parents=True, exist_ok=True)

    # Address numbers are spread out, as in a real address book (not 1..N).
    adresses = sorted(rng.sample(range(100000, 999999), N_CLIENTS))
    _write(outputDir / "f0101.csv", ["ABAN8", "ABALPH", "ABAT1"],
           [(an8, f"Client {i:04d}", rng.choice(["C", "C", "C", "V"])) for i, an8 in enumerate(adresses)])
    _write(outputDir / "f0010.csv", ["CCCO", "CCNAME"], SOCIETES)
    # Business units are right-aligned, space-padded codes in JDE.
    _write(outputDir / "f0006.csv", ["MCMCU", "MCDL01", "MCCO"],
           [(f"{code:>12}", f"Centre {code}", rng.choice(SOCIETES)[0]) for code in CENTRES])

    factures = []
    debut = date(2025, 1, 1)
    for doc in range(1, N_FACTURES // 2 + 1):
        societe = rng.choice(SOCIETES)[0]
        client = rng.choice(adresses)
        centre = rng.choice(CENTRES)
        dateFacture = debut + timedelta(days=rng.randrange(270))
        echeances = rng.choice([1, 1, 2, 3])
        montant = round(rng.uniform(80, 25000), 2)
        for sfx in range(1, echeances + 1):
            part = round(montant / echeances, 2)
            ouvert = 0.0 if rng.random() < 0.6 else part
            factures.append((
                200000 + doc, "RI", societe, sfx, client, societe, f"{centre:>12}",
                dateFacture.isoformat(), (dateFacture + timedelta(days=30 * sfx)).isoformat(), part, ouvert,
            ))
    _write(outputDir / "f03b11.csv",
           ["RPDOC", "RPDCT", "RPKCO", "RPSFX", "RPAN8", "RPCO", "RPMCU", "RPDIVJ", "RPDDJ", "RPAG", "RPAAP"],
           factures)
    return {"f03b11": len(factures), "f0101": len(adresses), "f0010": len(SOCIETES), "f0006": len(CENTRES)}


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parents[1] / "examples" / "jde_liens"
    for table, count in generate(target).items():
        print(f"{table}: {count} lignes")
