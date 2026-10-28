"""HTTP client for `kawakiri-langgraph`'s `POST /propose-fact-tables`: the
LLM hint that names, from the business need and the physical links, which
table(s) to analyse as fact tables, or which facts to assemble from several
tables -- then, for each existing fact table, the column roles the
statistics can't settle (keys, measures, technical columns).

A hint, never a decision: every table name that comes back is checked
against this run's own tables (an unknown one is dropped into
`rejectedTableNames`), every column name against its table (dropped into
`rejectedColumnNames`), every assembled fact against `AssemblySpec`
(dropped into `rejectedAssembledFacts`), and every retained fact still goes
through the deterministic checks of `FactAssembly/` and `FactAnalysis/`.

Also the client of `POST /arbitrate-links` (`arbitrateLinks`): the verdict
on each physical link the deterministic rules left doubtful.

Never raises: a network failure, a non-2xx answer or a malformed body sets
`lastError` (or `lastLinkError`) and returns an empty result.
"""
from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import requests
from pydantic import ValidationError

from kawakiriv3.FactAssembly.assemblySpec import AssemblySpec

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT_SECONDS = 120


def _envValue(key):
    """`key` from the process environment, else from `kawakiriv3/.env`."""
    if os.getenv(key):
        return os.getenv(key)
    envPath = Path(__file__).resolve().parents[2] / ".env"
    if not envPath.exists():
        return None
    for line in envPath.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.startswith(f"{key}=") and "=" in line:
            return line.split("=", 1)[1].strip().strip('"\'')
    return None


@dataclass
class FactTableHint:
    tableName: str
    justification: str
    expectedGrain: Optional[str] = None


class FactHintAdvisor:
    def __init__(self, baseUrl=None, timeoutSeconds=DEFAULT_TIMEOUT_SECONDS, session=None):
        self.baseUrl = (baseUrl or _envValue("LANGGRAPH_ADVISOR_URL") or DEFAULT_BASE_URL).rstrip("/")
        self.timeoutSeconds = timeoutSeconds
        self.session = session or requests.Session()
        self.lastError = None
        self.lastReasoning = None
        self.lastTokenUsage = None
        self.rejectedTableNames = []
        self.lastColumnRolesByTable = {}
        self.lastColumnRolesErrors = {}
        self.rejectedColumnNames = {}
        self.lastAssembledFacts = []
        self.rejectedAssembledFacts = []
        self.lastLinkError = None
        self.lastLinkReasoning = None
        self.lastLinkTokenUsage = None

    def arbitrateLinks(self, tablesSummary, candidateLinks, businessNeed=None, contextType=None):
        """`{linkId: (accepted, justification)}` for the doubtful links
        (`LinkGraph.pendingSummary()`); `{}` on failure (see
        `lastLinkError`). A verdict on an id that wasn't asked is dropped."""
        self.lastLinkError = None
        self.lastLinkReasoning = None
        self.lastLinkTokenUsage = None
        asked = {link["linkId"] for link in candidateLinks}
        try:
            response = self.session.post(
                f"{self.baseUrl}/arbitrate-links",
                json={
                    "tablesSummary": tablesSummary, "candidateLinks": candidateLinks,
                    "businessNeed": businessNeed, "contextType": contextType,
                },
                timeout=self.timeoutSeconds,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            self.lastLinkError = f"(langgraph_advisor injoignable a {self.baseUrl}) {type(exc).__name__}: {exc}"
            return {}

        self.lastLinkReasoning = body.get("reasoning")
        self.lastLinkTokenUsage = body.get("tokenUsage")
        self.lastLinkError = body.get("lastError")
        verdicts = {}
        for verdict in body.get("verdicts") or []:
            if isinstance(verdict, dict) and verdict.get("linkId") in asked and isinstance(verdict.get("accepted"), bool):
                verdicts[verdict["linkId"]] = (verdict["accepted"], verdict.get("justification") or "")
        return verdicts

    def proposeFactTables(
        self, tablesSummary, businessNeed=None, contextType=None, factTables=None, classifyColumns=True,
        physicalLinks=None,
    ):
        """`[FactTableHint, ...]`, most relevant first; `[]` on failure (see
        `lastError`). `factTables`: tables imposed by the caller -- the
        service then only classifies their columns, and the imposed tables
        are returned as is. The column roles land in
        `lastColumnRolesByTable` (`{table: {reasoning, keyColumns,
        measureColumns, technicalColumns}}`), a per-table failure in
        `lastColumnRolesErrors`. `physicalLinks`: `LinkGraph.toSummary()`,
        shown to the LLM; the facts it proposes to assemble land in
        `lastAssembledFacts` (`[AssemblySpec, ...]`)."""
        self.lastError = None
        self.lastReasoning = None
        self.lastTokenUsage = None
        self.rejectedTableNames = []
        self.lastColumnRolesByTable = {}
        self.lastColumnRolesErrors = {}
        self.rejectedColumnNames = {}
        self.lastAssembledFacts = []
        self.rejectedAssembledFacts = []
        knownTables = {table["tableName"] for table in tablesSummary}

        try:
            response = self.session.post(
                f"{self.baseUrl}/propose-fact-tables",
                json={
                    "tablesSummary": tablesSummary, "businessNeed": businessNeed, "contextType": contextType,
                    "factTables": list(factTables) if factTables else None, "classifyColumns": classifyColumns,
                    "physicalLinks": list(physicalLinks or []),
                },
                timeout=self.timeoutSeconds,
            )
            response.raise_for_status()
            body = response.json()
        except Exception as exc:
            self.lastError = f"(langgraph_advisor injoignable a {self.baseUrl}) {type(exc).__name__}: {exc}"
            return []

        self.lastReasoning = body.get("reasoning")
        self.lastTokenUsage = body.get("tokenUsage")
        self.lastColumnRolesErrors = dict(body.get("columnRolesErrors") or {})
        self._keepKnownColumnRoles(tablesSummary, body.get("columnRolesByTable") or {})
        if body.get("lastError"):
            self.lastError = body["lastError"]
            return []

        hints = []
        for entry in body.get("factTables") or []:
            tableName = entry.get("tableName") if isinstance(entry, dict) else None
            if tableName not in knownTables:
                self.rejectedTableNames.append(tableName)
                continue
            if any(h.tableName == tableName for h in hints):
                continue
            hints.append(FactTableHint(
                tableName=tableName, justification=entry.get("justification") or "",
                expectedGrain=entry.get("expectedGrain"),
            ))
        self._keepValidAssembledFacts(knownTables, body.get("assembledFacts") or [])
        if not hints and not self.lastAssembledFacts:
            self.lastError = "le hint LLM ne designe aucune table de faits connue"
        return hints

    def _keepValidAssembledFacts(self, knownTables, assembledFacts):
        for entry in assembledFacts:
            try:
                spec = AssemblySpec.model_validate(entry)
            except ValidationError as exc:
                self.rejectedAssembledFacts.append({"fact": entry, "error": str(exc)})
                continue
            unknown = sorted({spec.anchorTable, *(i.table for i in [*spec.measures, *spec.keys])} - knownTables)
            if unknown:
                self.rejectedAssembledFacts.append({"fact": entry, "error": f"tables inconnues : {unknown}"})
            elif all(existing.name != spec.name for existing in self.lastAssembledFacts):
                self.lastAssembledFacts.append(spec)

    def _keepKnownColumnRoles(self, tablesSummary, columnRolesByTable):
        columnsByTable = {
            table["tableName"]: {column["columnName"] for column in table.get("columns") or []}
            for table in tablesSummary
        }
        for tableName, roles in columnRolesByTable.items():
            if tableName not in columnsByTable or not isinstance(roles, dict):
                continue
            known = columnsByTable[tableName]
            kept = {"reasoning": roles.get("reasoning")}
            for listName in ("keyColumns", "measureColumns", "technicalColumns"):
                columns = roles.get(listName) or []
                kept[listName] = [c for c in columns if c in known]
                unknown = [c for c in columns if c not in known]
                if unknown:
                    self.rejectedColumnNames.setdefault(tableName, []).extend(unknown)
            self.lastColumnRolesByTable[tableName] = kept
