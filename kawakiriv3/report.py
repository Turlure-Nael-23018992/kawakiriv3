"""Exports a v3 run: a JSON dump of the results and a markdown report with
one Mermaid diagram per star."""
from __future__ import annotations

import dataclasses
import json
from pathlib import Path

from kawakiriv3.FactAnalysis.columnAssessment import (
    CONSTANT, DEGENERATE, EMPTY, EXTRACTED_KEY, FOREIGN_KEY, MEASURE, MOVED_ATTRIBUTE, NON_ADDITIVE, TECHNICAL,
)

_ROLE_LABELS = {
    FOREIGN_KEY: "cle etrangere (liee)",
    EXTRACTED_KEY: "cle extraite (dimension generee)",
    MOVED_ATTRIBUTE: "attribut deplace dans une dimension",
    MEASURE: "mesure",
    DEGENERATE: "degeneree (reste dans le fait, hors grain)",
    TECHNICAL: "technique (exclue)",
    CONSTANT: "constante (exclue)",
    EMPTY: "vide (exclue)",
}


_RELATION_LABELS = {
    "ancre": "ancre (definit le grain)",
    "meme_grain": "meme grain",
    "plus_grossiere": "plus grossiere",
    "plus_fine": "plus fine (agregee)",
    "hors_perimetre": "hors perimetre",
}


def _assemblyToJson(assembly):
    return {
        **dataclasses.asdict(assembly),
        "reconciliations": [
            {**dataclasses.asdict(r), "delta": r.delta, "ok": r.ok} for r in assembly.reconciliations
        ],
    }


def _modelToJson(model):
    return {
        "factTable": model.factTable,
        "starFactTable": model.starFactTable,
        "hint": model.hint,
        "columnRolesReasoning": model.columnRolesReasoning,
        "verdict": model.verdict,
        "warnings": model.warnings,
        "notes": model.notes,
        "grain": {
            **dataclasses.asdict(model.grain),
            "duplicateRows": model.grain.duplicateRows,
            "isUnique": model.grain.isUnique,
        },
        "foreignKeys": [dataclasses.asdict(link) for link in model.links],
        "generatedDimensions": [dataclasses.asdict(d) for d in model.dimensions],
        "measures": model.measures,
        "nonAdditiveMeasures": model.nonAdditiveMeasures,
        "degenerateColumns": model.degenerateColumns,
        "technicalColumns": model.technicalColumns,
        "columns": [dataclasses.asdict(a) for a in model.assessments],
        "assembly": _assemblyToJson(model.assembly) if model.assembly else None,
        "remappedKeys": [dataclasses.asdict(k) for k in model.remappedKeys],
        "gatheredFacts": model.gatheredFacts,
    }


def resultsToJson(results):
    return {
        **{k: v for k, v in results.items() if k not in ("models", "replacedModels", "factTableHints")},
        "factTableHints": [dataclasses.asdict(h) for h in results["factTableHints"]],
        "models": [_modelToJson(m) for m in results["models"]],
        "replacedModels": [_modelToJson(m) for m in results.get("replacedModels") or []],
    }


def _entity(name):
    return '"' + name.replace('"', "") + '"'


MAX_LINKED_TABLE_COLUMNS = 12


def _linkedEntityName(model, targetTable):
    """A linked source table (`clients`, `plans`...) is the one kind of
    entity whose bare name can recur across several diagrams of the same
    report -- every other entity (the fact itself, its generated
    dimensions) is already namespaced by its own fact table. Several
    Mermaid-in-Markdown renderers (VS Code's preview extensions among them)
    share erDiagram state across all diagrams of one page instead of
    resetting it per diagram, so a repeated bare entity name can crash the
    second or third diagram ('Cannot read properties of null (reading
    getAttribute)'). Namespacing by the current fact table keeps every
    entity name unique report-wide."""
    return f"{model.factTable}.{targetTable}"


def mermaidDiagram(model, tableColumns=None, fullTables=None):
    """`tableColumns`: `{table: [(column, inferredType), ...]}` of the
    source tables -- a linked table is drawn with its key and columns
    instead of an empty box. `fullTables`: `{table: shared}`, the unified
    dimensions, drawn with every column, under their own name unless
    `shared` by several diagrams of the report (see `_linkedEntityName`)."""
    tableColumns = tableColumns or {}
    fullTables = fullTables or {}
    entityName = lambda table: (
        table if table in fullTables and not fullTables[table] else _linkedEntityName(model, table)
    )
    lines = ["erDiagram"]
    factColumns = [a for a in model.assessments if a.role in (FOREIGN_KEY, EXTRACTED_KEY, MEASURE, DEGENERATE)]
    lines.append(f"    {_entity(model.starFactTable)} {{")
    for a in factColumns:
        marker = " FK" if a.role in (FOREIGN_KEY, EXTRACTED_KEY) else ""
        if a.role == MEASURE and a.additivity == NON_ADDITIVE:
            marker = ' "non additive"'
        lines.append(f"        {a.inferredType} {a.columnName}{marker}")
    lines.append("    }")
    for dimension in model.dimensions:
        lines.append(f"    {_entity(dimension.tableName)} {{")
        lines.append(f"        key {dimension.keyColumn} PK")
        for column in dimension.attributeColumns:
            lines.append(f"        attr {column}")
        lines.append("    }")
    for targetTable in sorted({link.targetTable for link in model.links}):
        if targetTable not in tableColumns:
            continue
        keys = {link.targetColumn for link in model.links if link.targetTable == targetTable}
        columns = tableColumns[targetTable]
        shown = columns if targetTable in fullTables else columns[:MAX_LINKED_TABLE_COLUMNS]
        lines.append(f"    {_entity(entityName(targetTable))} {{")
        for column, inferredType in shown:
            lines.append(f"        {inferredType} {column}{' PK' if column in keys else ''}")
        if len(columns) > len(shown):
            lines.append(f"        more autres_colonnes_{len(columns) - len(shown)}")
        lines.append("    }")
    for link in model.links:
        lines.append(
            f'    {_entity(entityName(link.targetTable))} ||--o{{ {_entity(model.starFactTable)} '
            f': "{link.sourceColumn}"'
        )
    for dimension in model.dimensions:
        for column in dimension.roleColumns:
            lines.append(f'    {_entity(dimension.tableName)} ||--o{{ {_entity(model.starFactTable)} : "{column}"')
    return "\n".join(lines)


def _assemblySection(assembly):
    lines = [
        f"### Assemblage de {assembly.tableName}", "",
        f"Ancre : `{assembly.anchorTable}` ({assembly.anchorRowCount} lignes), fait assemble : "
        f"{assembly.rowCount} lignes" + (f" -- grain attendu : {assembly.expectedGrain}" if assembly.expectedGrain else ""),
        "", "| table | relation au grain | chemin |", "|---|---|---|",
    ]
    for plan in assembly.sources:
        lines.append(f"| {plan.table} | {_RELATION_LABELS.get(plan.relation, plan.relation)} | {' / '.join(plan.path) or '-'} |")
    lines += ["", "| colonne | source | type | operation | lignes nulles |", "|---|---|---|---|---|"]
    for column in assembly.columns:
        lines.append(
            f"| {column.name} | {column.sourceTable}.{column.sourceColumn} | {column.kind} | {column.operation} "
            f"| {column.nullRows} |"
        )
    if assembly.reconciliations:
        lines += ["", "Reconciliation (total de la source contre total du fait assemble) :", "",
                  "| colonne | source | total source | total assemble | ecart | |", "|---|---|---|---|---|---|"]
        for r in assembly.reconciliations:
            lines.append(
                f"| {r.column} | {r.source} | {r.sourceTotal:.2f} | {r.assembledTotal:.2f} | {r.delta:+.2f} "
                f"| {'ok' if r.ok else 'ECART'} |"
            )
    if assembly.refused:
        lines += ["", "Refuse :"] + [f"- `{r.table}.{r.column}` ({r.kind}) : {r.reason}" for r in assembly.refused]
    return lines + [""]


def _unificationSection(results):
    """Step 2, the unification on the stars: who decided it, the unified
    dimensions, the keys remapped to them and the gathered facts -- nothing
    when no unification was asked."""
    unification = results.get("unification") or {}
    if not unification.get("source") and not unification.get("requested"):
        return []
    decidedBy = {"caller": "imposee par le job", "llm": "proposee par le LLM"}.get(unification.get("source"))
    lines = ["## Etape 2 : dedoublonnage sur l'etoile", "",
             "Les etoiles de l'etape 1 sont reprises telles quelles : rien n'est reclasse ni relie a nouveau.", "",
             f"Demandee par le besoin (hint) : {'oui' if unification.get('requested') else 'non'}"
             + (f" -- {decidedBy}" if decidedBy else "")]
    if unification.get("error"):
        lines.append(f"\n> Unification en echec : {unification['error']}")
    if unification.get("reasoning"):
        lines += ["", f"> {unification['reasoning']}"]
    for rejected in unification.get("rejected") or []:
        spec = rejected.get("spec")
        name = spec.get("name") if isinstance(spec, dict) else None
        lines.append(f"\n> Spec {name or '?'} ecartee : {rejected.get('error')}")
    if unification.get("tokenUsage"):
        usage = unification["tokenUsage"]
        lines.append(f"\nTokens : {usage.get('inputTokens', 0)} en entree, {usage.get('outputTokens', 0)} en sortie")
    dimensions = unification.get("dimensions") or []
    if dimensions:
        lines += ["", "### Dimensions unifiees", "",
                  "| dimension unifiee | sources | statut | fusions | fiche prioritaire | colonnes par source |",
                  "|---|---|---|---|---|---|"]
        for d in dimensions:
            sources = ", ".join(f"{s['dimension']} ({s['label']}, cle {s['keyColumn']})" for s in d["sources"])
            if d["status"] != "built":
                lines.append(f"| {d['name']} | {sources} | {d['status']} | {d.get('reason') or ''} | | |")
                continue
            merges = (f"{d['sourceRowCount']} -> {d['resultRowCount']} identites, sur "
                      f"{', '.join(d['matchColumns'])} : {d['mergedByColumn']}"
                      + (" -- fusion peut-etre trop agressive" if d["aggressiveMergeWarning"] else ""))
            perSource = "; ".join(", ".join(columns) for columns in d["sourceColumns"].values())
            lines.append(
                f"| {d['name']} (`{d['idColumn']}`) | {sources} | {d['status']} | {merges} | {d.get('survivorship')} "
                f"| {perSource} |"
            )
        for d in dimensions:
            lines += [f"\n> {d['name']} : {warning}" for warning in d.get("typeWarnings") or []]
    keys = unification.get("remappedKeys") or []
    if keys:
        lines += ["", "### Cles remplacees", "",
                  "| fait | cle de l'etoile | id unifie | dimension unifiee (source) | rattachees | orphelines |",
                  "|---|---|---|---|---|---|"]
        for k in keys:
            lines.append(
                f"| {k['fact']} | {k['column']} -> {k['dimension']} | {k['newColumn']} | {k['unified']} ({k['label']}) "
                f"| {k['resolvedRows']} | {k['orphanRows']} |"
            )
    facts = unification.get("facts") or []
    if facts:
        lines += ["", "### Faits reunis", "", "| fait reuni | sources | statut | detail |", "|---|---|---|---|"]
        for f in facts:
            sources = ", ".join(f"{s['fact']} ({s['label']})" for s in f["sources"])
            detail = (f"{f['rowCount']} lignes, colonne d'origine {f['labelColumn']}" if f["status"] == "built"
                      else f.get("reason") or "")
            if f.get("typeWarnings"):
                detail += f" -- {'; '.join(f['typeWarnings'])}"
            lines.append(f"| {f['name']} | {sources} | {f['status']} | {detail} |")
    return lines + [""]


def markdownReport(results):
    lines = ["# Kawakiri v3 -- rapport", ""]
    if results.get("businessNeed"):
        lines += [f"**Besoin metier** : {results['businessNeed']}", ""]
    source = "imposee par l'appelant" if results["factTableSource"] == "caller" else "hint LLM"
    lines.append(f"**Choix des tables de faits** : {source}")
    if results.get("factHintError"):
        lines.append(f"\n> Echec du hint : {results['factHintError']}")
    if results.get("factHintReasoning"):
        lines += ["", f"> {results['factHintReasoning']}"]
    if results.get("rejectedHintTables"):
        lines.append(f"\nTables proposees par le LLM mais inconnues (ignorees) : {results['rejectedHintTables']}")
    for tableName, error in (results.get("columnRolesErrors") or {}).items():
        lines.append(f"\n> Roles de colonnes de {tableName} en echec : {error}")
    for tableName, columns in (results.get("rejectedColumnNames") or {}).items():
        lines.append(f"\nColonnes proposees par le LLM mais inconnues dans {tableName} (ignorees) : {columns}")
    for rejected in results.get("rejectedAssembledFacts") or []:
        fact = rejected.get("fact")
        name = fact.get("name") if isinstance(fact, dict) else None
        lines.append(f"\n> Fait assemble {name or '?'} ecarte : {rejected.get('error')}")
    decisions = results.get("linkDecisions") or []
    if decisions:
        accepted = sum(d["status"] == "accepte" for d in decisions)
        if results.get("linkArbitrationError"):
            lines.append(f"\n> Arbitrage des liens en echec : {results['linkArbitrationError']}")
        lines += [
            "", f"<details><summary>Liens physiques : {accepted} retenus sur {len(decisions)} candidats</summary>",
            "", "| lien | couverture | inverse | statut | decide par | raison |", "|---|---|---|---|---|---|",
        ]
        for d in decisions:
            reverse = "" if d["reverseCoverage"] is None else f"{d['reverseCoverage']:.0%}"
            lines.append(
                f"| {d['link']} | {d['coverage']:.0%} | {reverse} | {d['status']} | {d['decidedBy']} | {d['reason']} |"
            )
        lines += ["", "</details>"]
    if results.get("tokenUsage"):
        usage = results["tokenUsage"]
        lines.append(f"\nTokens : {usage.get('inputTokens', 0)} en entree, {usage.get('outputTokens', 0)} en sortie")
    if results.get("linkTokenUsage"):
        usage = results["linkTokenUsage"]
        lines.append(
            f"\nTokens de l'arbitrage des liens : {usage.get('inputTokens', 0)} en entree, "
            f"{usage.get('outputTokens', 0)} en sortie"
        )
    lines.append("")
    lines += _unificationSection(results)

    unified = [d["name"] for d in (results.get("unification") or {}).get("dimensions") or [] if d["status"] == "built"]
    fullTables = {
        table: sum(any(link.targetTable == table for link in m.links) for m in results["models"]) > 1
        for table in unified
    }
    for model in results["models"]:
        grain = model.grain
        lines += [f"## {model.factTable} -- **{model.verdict}**", ""]
        if model.hint:
            lines += [f"Pourquoi cette table : {model.hint}", ""]
        if model.gatheredFacts:
            lines += [f"Reunit les faits : {', '.join(model.gatheredFacts)}", ""]
        if model.assembly:
            lines += _assemblySection(model.assembly)
        if model.remappedKeys:
            lines += ["Cles remplacees par l'id d'une dimension unifiee :", ""]
            for key in model.remappedKeys:
                lines.append(
                    f"- `{key.fact}.{key.column}` -> `{key.newColumn}` ({key.unified}) : {key.resolvedRows} ligne(s) "
                    f"rattachee(s), {key.orphanRows} orpheline(s)"
                )
            lines.append("")
        lines += ["```mermaid", mermaidDiagram(model, results.get("tableColumns"), fullTables), "```", ""]
        lines.append(
            f"Grain : ({', '.join(grain.grainColumns) or 'aucun'}) -- "
            + ("unique" if grain.isUnique else
               f"unique a la ligne ({grain.completingColumns[0]})" if grain.repeatedWithin else
               f"non unique ({grain.duplicateRows} doublons)")
            + f" sur {grain.totalRows} lignes"
            + (f" ; {grain.unknownKeyRows} ligne(s) avec au moins une cle inconnue (nulle)" if grain.unknownKeyRows else "")
        )
        if model.columnRolesReasoning:
            lines += ["", f"Roles de colonnes (LLM) : {model.columnRolesReasoning}"]
        if model.notes:
            lines += ["", "Notes :"] + [f"- {n}" for n in model.notes]
        if model.warnings:
            lines += ["", "Avertissements :"] + [f"- {w}" for w in model.warnings]
        lines += ["", "| colonne | role | decide par | raison |", "|---|---|---|---|"]
        for a in model.assessments:
            role = _ROLE_LABELS.get(a.role, a.role)
            if a.role == MEASURE and a.additivity == NON_ADDITIVE:
                role = "mesure non additive (ne jamais sommer)"
            lines.append(f"| {a.columnName} | {role} | {a.roleSource} | {a.reason} |")
        lines.append("")
    return "\n".join(lines) + "\n"


def exportResults(results, outputDir="output"):
    outputDir = Path(outputDir)
    outputDir.mkdir(parents=True, exist_ok=True)
    jsonPath = outputDir / "v3_results.json"
    reportPath = outputDir / "v3_report.md"
    jsonPath.write_text(
        json.dumps(resultsToJson(results), indent=2, ensure_ascii=False, default=str), encoding="utf-8",
    )
    reportPath.write_text(markdownReport(results), encoding="utf-8")
    return jsonPath, reportPath
