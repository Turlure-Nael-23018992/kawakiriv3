"""A grain repeated inside one document (the same item scanned twice on one
ticket) is a line grain, completed by the line number: a note, not a reason
for review. Repeated across documents, it stays a warning."""
from kawakiriv3.FactAnalysis.columnRoles import ColumnRoles
from kawakiriv3.FactAnalysis.factAnalyzer import NEEDS_REVIEW, OK
from kawakiriv3.pipeline import KawakiriV3Pipeline

ROLES = {"lignes": ColumnRoles.fromLists(keys=["produit", "jour"], measures=["qte"], degenerate=["ticket"])}


def _model(db, tickets):
    db.loadTables({"lignes": {
        "ligne_id": [1, 2, 3, 4, 5],
        "ticket": tickets,
        "produit": ["P1", "P1", "P1", "P1", "P2"],
        "jour": ["j1", "j1", "j1", "j2", "j2"],
        "qte": [1.5, 2.5, 1.0, 1.0, 3.0],
    }})
    results = KawakiriV3Pipeline(db).run(["lignes"], factTables=["lignes"], columnHint=False, columnRoles=ROLES)
    return results["models"][0]


def testTheSameItemTwiceOnOneTicketIsALineGrain(duck_db):
    # P1 on j1 three times, all on ticket T1.
    model = _model(duck_db, ["T1", "T1", "T1", "T2", "T2"])

    assert model.verdict == OK
    assert model.grain.repeatedWithin == "ticket" and model.grain.completingColumns == ["ligne_id"]
    assert model.notes == ["grain a la ligne : 2 ligne(s) repetent les memes cles dans un meme ticket, "
                           "distinguees par ligne_id"]


def testTheSameKeysOnTwoTicketsStayAWarning(duck_db):
    # P1 on j1 on T1 twice and on T2 once: not only a repetition inside a ticket.
    model = _model(duck_db, ["T1", "T1", "T2", "T3", "T3"])

    assert model.verdict == NEEDS_REVIEW
    assert model.grain.repeatedWithin is None
    assert any("non unique" in w for w in model.warnings)
