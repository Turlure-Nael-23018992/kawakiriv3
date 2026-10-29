"""Entry point for kawakiriv3: `python run_v3.py <job.json>`.

See `kawakiriv3/jobPayload.py` for the job file shape. When the job doesn't
impose `factTables`, the `kawakiri-langgraph` service must be running
(`uvicorn langgraph_advisor.service:app --port 8001`; `LANGGRAPH_ADVISOR_URL`
if it lives elsewhere) -- it picks the fact table(s) from `businessNeed`.
"""
import json
import sys
from pathlib import Path

from kawakiriv3.DuckDbManagement.duckDbManager import duckDbManager
from kawakiriv3.DuckDbManagement.duckDbUtils import duckDbUtils
from kawakiriv3.FactAnalysis.columnRoles import ColumnRoles
from kawakiriv3.FactAssembly.linkGraph import parseLinkKey
from kawakiriv3.FactAssembly.linkVerdictCache import LinkVerdictCache
from kawakiriv3.jobPayload import JobPayload
from kawakiriv3.pipeline import KawakiriV3Pipeline
from kawakiriv3.report import exportResults
from kawakiriv3.Sources import buildSource


def main(jobPath):
    if not Path(jobPath).is_file():
        jobs = sorted(str(p) for p in Path("examples").glob("job_*.json"))
        sys.exit(f"{jobPath} n'est pas un fichier de job JSON. Jobs disponibles :\n  " + "\n  ".join(jobs))
    job = JobPayload.model_validate(json.loads(Path(jobPath).read_text(encoding="utf-8")))
    source = buildSource(job.source)
    manager = duckDbManager(job.duckDbPath)
    try:
        db = duckDbUtils(manager.conn)
        db.dropAllTables()
        pipeline = KawakiriV3Pipeline(db, linkVerdictCache=LinkVerdictCache(job.linkCachePath))

        print(f"--- Ingestion ({source.kind}) ---")
        loaded = pipeline.ingest(source, job.tables, job.rowFilters)
        for sourceTable, duckTable in loaded.items():
            print(f"  {sourceTable} -> {duckTable} ({db.getRowCount(duckTable)} lignes)")

        tableDescriptions = {
            loaded.get(name, name): description for name, description in (job.tableDescriptions or {}).items()
        }
        columnRoles = {
            loaded.get(name, name): ColumnRoles.fromLists(spec.keys, spec.measures, spec.excluded)
            for name, spec in (job.columnRoles or {}).items()
        }
        assembledFacts = [spec.renamedTables(loaded) for spec in job.assembledFacts or []]
        unification = job.unification.renamed(loaded) if job.unification else None

        def renamedLink(text):
            sourceTable, sourceColumn, targetTable, targetColumn = parseLinkKey(text)
            return f"{loaded.get(sourceTable, sourceTable)}.{sourceColumn} -> {loaded.get(targetTable, targetTable)}.{targetColumn}"

        results = pipeline.run(
            list(loaded.values()), businessNeed=job.businessNeed, contextType=job.contextType,
            factTables=job.factTables, tableDescriptions=tableDescriptions,
            columnRoles=columnRoles, columnHint=job.columnHint, assembledFacts=assembledFacts,
            declaredLinks=[renamedLink(t) for t in job.declaredLinks],
            forbiddenLinks=[renamedLink(t) for t in job.forbiddenLinks], linkHint=job.linkHint,
            unification=unification,
        )

        if results["factHintError"]:
            print(f"--- Hint LLM en echec : {results['factHintError']} ---")
        for tableName, error in results["columnRolesErrors"].items():
            print(f"--- Roles de colonnes de {tableName} en echec : {error} ---")
        byDecider = {}
        for decision in results["linkDecisions"]:
            key = f"{decision['status']} ({decision['decidedBy']})"
            byDecider[key] = byDecider.get(key, 0) + 1
        print(f"--- {len(results['physicalLinks'])} lien(s) physique(s) retenu(s) : "
              f"{', '.join(f'{n} {k}' for k, n in sorted(byDecider.items()))} ---")
        if results["linkArbitrationError"]:
            print(f"--- Arbitrage des liens en echec : {results['linkArbitrationError']} ---")
        for decision in results["linkDecisions"]:
            if decision["decidedBy"].startswith("LLM") or decision["decidedBy"] == "appelant":
                print(f"  {decision['status']:8} {decision['link']:60} [{decision['decidedBy']}] {decision['reason']}")
        for rejected in results["rejectedAssembledFacts"]:
            print(f"--- Fait assemble ecarte : {rejected['error']} ---")

        unification = results["unification"]
        if unification["source"] or unification["requested"]:
            decidedBy = {"caller": "imposee par le job", "llm": "proposee par le LLM"}.get(unification["source"], "aucune")
            print(f"--- Post-traitement : unification {decidedBy} "
                  f"(demandee par le besoin : {'oui' if unification['requested'] else 'non'}) ---")
            if unification["error"]:
                print(f"  ! en echec : {unification['error']}")
            if unification["reasoning"]:
                print(f"  {unification['reasoning']}")
            for rejected in unification["rejected"]:
                print(f"  ! spec ecartee : {rejected['error']}")
        for dimension in unification["dimensions"]:
            sources = ", ".join(f"{s['dimension']} ({s['label']})" for s in dimension["sources"])
            print(f"--- Dimension unifiee {dimension['name']} : {dimension['status']} -- depuis {sources} ---")
            if dimension["reason"]:
                print(f"  {dimension['reason']}")
            if dimension["status"] == "built":
                print(f"  {dimension['sourceRowCount']} -> {dimension['resultRowCount']} identites, "
                      f"fusions par colonne {dimension['mergedByColumn']}")
                print(f"  attributs : {dimension['survivorship']}")
                print(f"  colonnes par source : {dimension['sourceColumns']}")
            for warning in dimension["typeWarnings"]:
                print(f"  ! {warning}")
            if dimension["aggressiveMergeWarning"]:
                print(f"  ! un cluster absorbe {dimension['largestClusterSize']} lignes : fusion trop agressive ?")
        for key in unification["remappedKeys"]:
            print(f"--- Cle remplacee : {key['fact']}.{key['column']} -> {key['newColumn']} ({key['unified']}), "
                  f"{key['resolvedRows']} rattachee(s), {key['orphanRows']} orpheline(s) ---")
        for fact in unification["facts"]:
            sources = ", ".join(f"{s['fact']} ({s['label']})" for s in fact["sources"])
            print(f"--- Faits reunis {fact['name']} : {fact['status']} -- depuis {sources} ---")
            if fact["reason"]:
                print(f"  {fact['reason']}")

        for model in results["models"]:
            print(f"--- {model.factTable} : {model.verdict} ---")
            if model.gatheredFacts:
                print(f"  reunit       : {', '.join(model.gatheredFacts)} (qu'il remplace)")
            if model.assembly:
                assembly = model.assembly
                print(f"  assemble     : ancre {assembly.anchorTable}, {assembly.rowCount} lignes")
                for plan in assembly.sources:
                    print(f"    {plan.table:<22} {plan.relation}")
                for r in assembly.reconciliations:
                    print(f"    reconciliation {r.column:<16} ecart {r.delta:+.2f} {'ok' if r.ok else 'ECART'}")
                for refused in assembly.refused:
                    print(f"    refuse : {refused.table}.{refused.column}")
            for key in model.remappedKeys:
                print(f"  cle unifiee  : {key.fact}.{key.column} -> {key.newColumn} ({key.unified}), "
                      f"{key.resolvedRows} rattachee(s), {key.orphanRows} orpheline(s)")
            print(f"  grain        : {model.grain.grainColumns} (unique={model.grain.isUnique})")
            print(f"  cles inconnues (nulles) : {model.grain.unknownKeyRows} ligne(s)")
            print(f"  liees        : {[f'{l.sourceColumn}->{l.target}' for l in model.links]}")
            print(f"  extraites    : {[d.tableName for d in model.dimensions]}")
            print(f"  mesures      : {model.measures}")
            if model.nonAdditiveMeasures:
                print(f"  non additives: {model.nonAdditiveMeasures} (a ne jamais sommer)")
            print(f"  techniques   : {model.technicalColumns}")
            for note in model.notes:
                print(f"  note : {note}")
            for warning in model.warnings:
                print(f"  ! {warning}")

        usages = {
            "hint des faits": results["tokenUsage"], "arbitrage des liens": results["linkTokenUsage"],
            "unification": unification["tokenUsage"],
        }
        totalIn = sum((u or {}).get("inputTokens", 0) for u in usages.values())
        totalOut = sum((u or {}).get("outputTokens", 0) for u in usages.values())
        print(f"--- Tokens LLM : {totalIn} en entree, {totalOut} en sortie ---")
        for step, usage in usages.items():
            if usage:
                print(f"  {step:<20} {usage.get('inputTokens', 0)} en entree, {usage.get('outputTokens', 0)} en sortie")
            else:
                print(f"  {step:<20} aucun appel")

        jsonPath, reportPath = exportResults(results, job.outputDir)
        print(f"--- Export : {jsonPath}, {reportPath} ---")
    finally:
        manager.close()
        source.close()


if __name__ == "__main__":
    if len(sys.argv) != 2:
        sys.exit("usage : python run_v3.py <job.json>")
    main(sys.argv[1])
