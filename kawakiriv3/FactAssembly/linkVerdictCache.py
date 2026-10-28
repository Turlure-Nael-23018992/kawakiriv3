"""Remembers the LLM's verdict on each doubtful link, keyed by the link's
signature (tables, columns, cardinality, rounded coverages): the same
dataset gives the same link graph from one run to the next, and an
already-judged link never costs a second call.

A change in the data that moves a coverage (rounded to the percent) changes
the signature: the link is judged again. A JSON file, readable and editable
by hand -- delete an entry to have it re-judged.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path


def linkSignature(link):
    reverse = "-" if link.reverseCoverage is None else f"{link.reverseCoverage:.2f}"
    return f"{link.key}|{link.cardinality}|{link.coverage:.2f}|{reverse}"


class LinkVerdictCache:
    """`path`: the JSON file, None for an in-memory cache (tests)."""

    def __init__(self, path=None):
        self.path = Path(path) if path else None
        self.entries = {}
        if self.path and self.path.exists():
            try:
                self.entries = json.loads(self.path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                self.entries = {}

    def lookup(self, decisions):
        """`{linkId: (accepted, justification)}` for the doubtful links
        already judged."""
        verdicts = {}
        for decision in decisions:
            entry = self.entries.get(linkSignature(decision.link))
            if entry is not None:
                verdicts[decision.linkId] = (entry["accepted"], entry["justification"])
        return verdicts

    def store(self, decisions, verdicts):
        """Records `{linkId: (accepted, justification)}` and saves."""
        byId = {d.linkId: d for d in decisions}
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        for linkId, (accepted, justification) in verdicts.items():
            if linkId in byId:
                self.entries[linkSignature(byId[linkId].link)] = {
                    "accepted": accepted, "justification": justification, "judgedAt": now,
                }
        if self.path:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(self.entries, indent=2, ensure_ascii=False, sort_keys=True), encoding="utf-8")
