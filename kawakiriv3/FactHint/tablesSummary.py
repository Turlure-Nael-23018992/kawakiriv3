"""What the LLM is shown to pick the fact table(s): structure and statistics
only. Built from scratch rather than by stripping a richer summary, so no
real data value (samples, top values, min/max) can ever be in it -- same
rule as kawakiriv2's docs/pipeline.md section 8bis."""
from __future__ import annotations

MAX_DESCRIPTION_LENGTH = 500


def _truncated(text):
    text = text.strip()
    return text if len(text) <= MAX_DESCRIPTION_LENGTH else text[:MAX_DESCRIPTION_LENGTH].rstrip() + "..."


def buildTablesSummary(schemaEngine, tableDescriptions=None):
    """`[{tableName, rowCount, description?, columns: [{columnName,
    inferredType, nullRate, distinctCount, distinctRate, uniqueInTable,
    description?}]}]` -- `tableDescriptions`: optional `{table:
    {"description": str, "columns": {column: str}}}` from the job."""
    tableDescriptions = tableDescriptions or {}
    summary = []
    for tableName, profiler in schemaEngine.profilers.items():
        described = tableDescriptions.get(tableName) or {}
        columnDescriptions = described.get("columns") or {}
        columns = []
        rowCount = 0
        for columnName in profiler.columns:
            profiling = profiler.getAggProfiling(columnName)
            rowCount = profiling.TotalCount
            column = {
                "columnName": columnName,
                "inferredType": profiling.InferredDataType,
                "nullRate": round(profiling.NullRate, 2),
                "distinctCount": profiling.DistinctCount,
                "distinctRate": round(profiling.DistinctRate, 2),
                "uniqueInTable": profiler.isSimpleKeyCandidate(columnName),
            }
            if columnDescriptions.get(columnName):
                column["description"] = _truncated(columnDescriptions[columnName])
            columns.append(column)
        table = {"tableName": tableName, "rowCount": rowCount, "columns": columns}
        if described.get("description"):
            table["description"] = _truncated(described["description"])
        summary.append(table)
    return summary
