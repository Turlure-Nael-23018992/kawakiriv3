"""Shape of a v3 job file.

    {
      "source": {"kind": "csv", "path": "data/ventes"},
      "tables": {"ventes": null, "clients": ["client_id", "nom"]},
      "rowFilters": {"abonnements": "DBT_VALID_TO IS NULL"},
      "businessNeed": "Analyser le chiffre d'affaires par client et par produit",
      "contextType": "e-commerce",
      "factTables": ["ventes"],
      "tableDescriptions": {"ventes": {"description": "...", "columns": {"montant": "..."}}},
      "columnRoles": {"ventes": {"keys": ["RPAN8"], "measures": ["quantite"], "excluded": ["LOADED_AT"]}},
      "columnHint": true,
      "assembledFacts": [{"name": "marge_lignes", "anchorTable": "lignes_commande",
                          "measures": [{"table": "tarifs", "column": "montant_ht"}]}],
      "unification": {"dimensions": [{"name": "clients_unifies", "idColumn": "client_unifie_id",
                                      "sources": [{"dimension": "clients_web", "label": "web",
                                                   "columnMapping": {"email": "mail"}}, ...],
                                      "matchColumns": ["email"], "sourcePriority": ["web", "magasin"]}],
                      "facts": [{"name": "ventes", "labelColumn": "canal",
                                 "sources": [{"fact": "ventes_web", "label": "web"}, ...]}]},
      "declaredLinks": ["f03b11.RPAN8 -> f0101.ABAN8"],
      "forbiddenLinks": ["lignes.quantite -> clients.client_id"],
      "linkHint": true,
      "linkCachePath": "output/link_verdicts.json",
      "duckDbPath": "output/v3.duckdb",
      "outputDir": "output"
    }

Only `source` is required. `tables` omitted = every table, every column;
a table mapped to null = every column. `rowFilters`: an optional SQL boolean
expression per table, applied as a `WHERE` clause while loading it -- the
caller's way to pick a current slice out of a historised source (a dbt
snapshot's `*_valid_to IS NULL`, a specific `SNAPSHOT_DATE`...) before the
rest of the chain ever sees the other versions. `factTables` omitted = the LLM hint
chooses from `businessNeed`. `columnRoles`: roles the caller imposes on
columns of a fact table, over the LLM's column hint and the statistics (see
`FactAnalysis/columnRoles.py`). `columnHint` (default true): ask the LLM
for the column roles of each fact table -- with `factTables` imposed and
`columnHint: false`, the run makes no LLM call at all. `assembledFacts`:
facts to build from measures spread over several tables (see
`FactAssembly/assemblySpec.py`); imposed, the LLM doesn't choose the facts
either. `unification`: what the post-processing unifies once the stars are
built, in the stars' words -- unified dimensions (the stars' dimensions of
one entity, deduplicated), every fact's keys to them replaced by the unified
id, then facts of several stars gathered into one fact, which replaces them
(see `StarUnification/unificationSpec.py`); a dimension is named by its
table (a linked source table, or a generated `<fact>__dim_<column>`), a
fact as `factTables` or `assembledFacts` names it. Without it, the LLM
describes the unification itself when the business need asks for one.
`declaredLinks`/`forbiddenLinks`: physical links the caller imposes
or forbids, `"table.column -> table.column"`, over every other decision.
`linkHint` (default true): let the LLM arbitrate the doubtful links (never
in a run that makes no other LLM call). `linkCachePath`: where the LLM's
link verdicts are kept from one run to the next. `source` keys other than
`kind` are the connector's constructor arguments (see `Sources/`).
"""
from __future__ import annotations

from typing import Dict, List, Optional

from pydantic import BaseModel, ConfigDict, field_validator

from kawakiriv3.FactAssembly.assemblySpec import AssemblySpec
from kawakiriv3.FactAssembly.linkGraph import parseLinkKey
from kawakiriv3.Sources import _SOURCE_KINDS
from kawakiriv3.StarUnification.unificationSpec import UnificationSpec


class ColumnRolesSpec(BaseModel):
    model_config = ConfigDict(extra="forbid")

    keys: List[str] = []
    measures: List[str] = []
    excluded: List[str] = []


class JobPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    source: dict
    tables: Optional[Dict[str, Optional[List[str]]]] = None
    rowFilters: Dict[str, str] = {}
    businessNeed: Optional[str] = None
    contextType: Optional[str] = None
    factTables: Optional[List[str]] = None
    tableDescriptions: Optional[dict] = None
    columnRoles: Optional[Dict[str, ColumnRolesSpec]] = None
    columnHint: bool = True
    assembledFacts: Optional[List[AssemblySpec]] = None
    unification: Optional[UnificationSpec] = None
    declaredLinks: List[str] = []
    forbiddenLinks: List[str] = []
    linkHint: bool = True
    linkCachePath: str = "output/link_verdicts.json"
    duckDbPath: str = "output/v3.duckdb"
    outputDir: str = "output"

    @field_validator("declaredLinks", "forbiddenLinks")
    @classmethod
    def _wellFormedLinks(cls, links):
        for text in links:
            try:
                parseLinkKey(text)
            except ValueError:
                raise ValueError(f"lien mal forme {text!r} : attendu \"table.colonne -> table.colonne\"") from None
        return links

    @field_validator("source")
    @classmethod
    def _knownSourceKind(cls, source):
        if source.get("kind") not in _SOURCE_KINDS:
            raise ValueError(f"source.kind doit valoir l'un de {sorted(_SOURCE_KINDS)}, recu {source.get('kind')!r}")
        return source
