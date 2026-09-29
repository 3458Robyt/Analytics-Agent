from __future__ import annotations

import hashlib
import json
from typing import Any, Callable, Mapping

from sqlglot import exp, parse

from .bigquery_adapter import BigQueryAdapter, BigQueryError
from .guardrails import extract_table_ids, validate_sql
from .llm import LLMError, OpenAICompatibleProvider
from .models import AgentAnswer, AgentSettings, SummaryTable, TABLE_IDS
from .schema import ColumnSchema, SchemaCatalog, TableSchema, merge_bigquery_schema


class AnalyticsAgent:
    """Discovers BigQuery metadata, runs read-only queries and summarizes results."""

    def __init__(
        self,
        *,
        catalog: SchemaCatalog,
        llm: OpenAICompatibleProvider,
        bigquery: BigQueryAdapter,
        settings: AgentSettings,
    ) -> None:
        self.catalog = catalog
        self.llm = llm
        self.bigquery = bigquery
        self.settings = settings

    def discover(self, seed_projects: tuple[str, ...] | list[str] = ()) -> dict[str, Any]:
        report = self.bigquery.discover_tables(seed_projects)
        for item in report.tables:
            existing = self.catalog.tables.get(str(item["table_id"]).lower())
            self.catalog.add_table(TableSchema(
                sheet_name=existing.sheet_name if existing else "",
                table_id=item["table_id"],
                columns=existing.columns if existing else {},
                warnings=existing.warnings if existing else (),
                description=item.get("description", "") or (existing.description if existing else ""),
                table_type=item.get("table_type", "") or (existing.table_type if existing else ""),
                location=item.get("location", "") or (existing.location if existing else ""),
            ))
        self.catalog.warnings = tuple(dict.fromkeys((*self.catalog.warnings, *report.warnings)))
        for sheet_name, table_id in TABLE_IDS.items():
            if table_id.lower() not in self.catalog.tables:
                self.catalog.add_table(TableSchema(sheet_name, table_id, {}))
        return {
            "projects": list(report.projects),
            "datasets": report.datasets,
            "tables": len(report.tables),
            "warnings": list(report.warnings),
        }

    @staticmethod
    def _fingerprint(value: Any) -> str:
        payload = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _ensure_table(self, table_id: str, visiting: set[str] | None = None) -> TableSchema:
        normalized_id = table_id.replace(":", ".").lower()
        if "*" in normalized_id:
            candidates = [
                table for key, table in self.catalog.tables.items()
                if key.rsplit(".", 1)[0] == normalized_id.rsplit(".", 1)[0]
            ]
            if not candidates:
                raise BigQueryError(f"No se pudo confirmar el dataset del comodín `{table_id}`")
            return candidates[0]

        existing = self.catalog.tables.get(normalized_id)
        if (
            existing
            and existing.metadata_loaded
            and existing.table_type.upper() not in {"VIEW", "MATERIALIZED_VIEW"}
        ):
            return existing

        metadata = self.bigquery.inspect_table_metadata(table_id)
        table = self.catalog.tables.get(normalized_id)
        merge_bigquery_schema(self.catalog, {table_id: {"ok": True, **metadata}})
        table = self.catalog.tables.get(normalized_id)
        if table is None:
            table = TableSchema(
                sheet_name="",
                table_id=table_id,
                columns={
                    name: ColumnSchema(item["name"], item["data_type"], item.get("description", ""))
                    for name, item in metadata["actual_schema"].items()
                },
                description=metadata.get("description", ""),
                table_type=metadata.get("table_type", ""),
                location=metadata.get("location", ""),
                metadata_loaded=True,
            )
            self.catalog.add_table(table)
        if table.table_type.upper() in {"VIEW", "MATERIALIZED_VIEW"}:
            view_query = metadata.get("view_query", "").strip()
            if not view_query:
                raise BigQueryError(f"No se pudo inspeccionar la definición de la vista `{table_id}`")
            checked_view = validate_sql(view_query, self.catalog)
            stack = visiting if visiting is not None else set()
            if normalized_id in stack:
                raise BigQueryError(f"Se detectó una referencia circular en la vista `{table_id}`")
            stack.add(normalized_id)
            try:
                for nested_table in checked_view.tables:
                    self._ensure_table(nested_table, stack)
            finally:
                stack.remove(normalized_id)
        return table

    @staticmethod
    def _is_aggregate(sql: str) -> bool:
        try:
            tree = parse(sql, read="bigquery")[0]
        except Exception:
            return False
        aggregate_nodes = (exp.AggFunc,)
        return bool(tree.find_all(*aggregate_nodes)) or bool(tree.find(exp.Group))

    @staticmethod
    def _summary_table(raw: Any) -> SummaryTable | None:
        if raw is None:
            return None
        if not isinstance(raw, Mapping):
            raise LLMError("La tabla resumen no tiene formato de objeto")
        title = str(raw.get("title") or "Resumen")
        columns = raw.get("columns")
        rows = raw.get("rows")
        if not isinstance(columns, list) or not all(isinstance(column, str) for column in columns):
            raise LLMError("Las columnas de la tabla resumen deben ser una lista de textos")
        if not isinstance(rows, list):
            raise LLMError("Las filas de la tabla resumen deben ser una lista")
        normalized_rows: list[dict[str, Any]] = []
        for row in rows:
            if isinstance(row, Mapping):
                normalized_rows.append({str(key): value for key, value in row.items()})
            elif isinstance(row, list) and len(row) == len(columns):
                normalized_rows.append(dict(zip(columns, row)))
            else:
                raise LLMError("Una fila de la tabla resumen no coincide con sus columnas")
        return SummaryTable(title=title, columns=tuple(columns), rows=tuple(normalized_rows))

    def answer(
        self,
        question: str,
        *,
        context: str = "",
        on_progress: Callable[[str], None] | None = None,
    ) -> AgentAnswer:
        question = question.strip()
        if not question:
            return AgentAnswer("Escribe una pregunta para iniciar el análisis.")

        def progress(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        working_notes = ""
        last_tool_result: dict[str, Any] | None = None
        total_usage: dict[str, int] = {}
        query_count = 0
        bytes_processed = 0
        tables_used: set[str] = set()
        has_aggregate_results = False
        previous_call: tuple[str, str] | None = None
        pending_queries: set[str] = set()

        while True:
            action, usage = self.llm.next_action(
                question,
                context=context,
                working_notes=working_notes,
                last_tool_result=last_tool_result,
            )
            self._add_usage(total_usage, usage)
            if action["notes"].strip():
                working_notes = action["notes"].strip()

            if action["action"] == "finish":
                if pending_queries:
                    query_id = sorted(pending_queries)[0]
                    progress("Completando páginas pendientes antes de cerrar el análisis.")
                    page = self.bigquery.read_page(query_id)
                    if not page.has_next_page:
                        pending_queries.discard(query_id)
                    last_tool_result = {
                        "action": "query_page",
                        "query_id": page.query_id,
                        "columns": list(page.columns),
                        "rows": list(page.rows),
                        "has_next_page": page.has_next_page,
                        "total_rows": page.total_rows,
                        "bytes_processed": page.bytes_processed,
                        "instruction": "Integra esta página. Todavía no cierres hasta recibir todas las páginas pendientes.",
                    }
                    continue
                summary = self._summary_table(action.get("summary_table"))
                if not has_aggregate_results:
                    summary = None
                return AgentAnswer(
                    answer=action["answer"].strip(),
                    assumptions=tuple(action["assumptions"]),
                    summary_table=summary,
                    query_count=query_count,
                    tables_used=tuple(sorted(tables_used)),
                    bytes_processed=bytes_processed,
                    usage=total_usage,
                )

            action_id = self._fingerprint({key: value for key, value in action.items() if key not in {"notes"}})
            progress("Buscando tablas en el catálogo." if action["action"] == "search_tables" else
                     "Consultando metadatos de BigQuery." if action["action"] == "describe_tables" else
                     "Ejecutando una consulta de lectura." if action["action"] == "run_select" else
                     "Leyendo la siguiente página de resultados.")
            try:
                if action["action"] == "search_tables":
                    matches, total = self.catalog.search_tables(
                        action["search_text"],
                        offset=action["offset"],
                        page_size=action["page_size"],
                    )
                    tool_result = {
                        "action": "search_tables",
                        "search_text": action["search_text"],
                        "offset": action["offset"],
                        "page_size": action["page_size"],
                        "total_matches": total,
                        "next_offset": action["offset"] + len(matches) if action["offset"] + len(matches) < total else None,
                        "tables": [
                            {
                                "table_id": item.table_id,
                                "description": item.description,
                                "table_type": item.table_type,
                                "location": item.location,
                            }
                            for item in matches
                        ],
                    }
                elif action["action"] == "describe_tables":
                    if not action["table_ids"]:
                        raise BigQueryError("Indica al menos una tabla completa para describir")
                    descriptions = []
                    for table_id in action["table_ids"]:
                        table = self._ensure_table(str(table_id))
                        descriptions.append({
                            "table_id": table.table_id,
                            "description": table.description,
                            "table_type": table.table_type,
                            "location": table.location,
                            "columns": [
                                {"name": column.name, "type": column.data_type, "description": column.description}
                                for column in table.columns.values()
                            ],
                        })
                    tool_result = {"action": "describe_tables", "tables": descriptions}
                elif action["action"] == "run_select":
                    validated = validate_sql(action["sql"], self.catalog)
                    for table_id in validated.tables:
                        self._ensure_table(table_id)
                    # Re-validate after metadata load; this also catches changes in view definitions.
                    validated = validate_sql(validated.sql, self.catalog)
                    locations = {
                        self.catalog.tables[table_id].location.strip().lower()
                        for table_id in validated.tables
                        if table_id in self.catalog.tables and self.catalog.tables[table_id].location.strip()
                    }
                    if len(locations) > 1:
                        raise BigQueryError(
                            "La consulta combina tablas de regiones distintas. Ejecuta consultas separadas por región y luego combina los resúmenes."
                        )
                    location = next(iter(locations), self.settings.bigquery_location or self.bigquery.location)
                    dry_run = self.bigquery.dry_run(validated.sql, location=location)
                    if not dry_run.ok:
                        raise BigQueryError(dry_run.error or "El dry run de BigQuery no fue aprobado")
                    page = self.bigquery.execute(validated.sql, location=location)
                    query_count += 1
                    bytes_processed += int(page.bytes_processed or dry_run.bytes_processed or 0)
                    tables_used.update(validated.tables)
                    has_aggregate_results = has_aggregate_results or self._is_aggregate(validated.sql)
                    tool_result = {
                        "action": "query_page",
                        "query_id": page.query_id,
                        "columns": list(page.columns),
                        "rows": list(page.rows),
                        "has_next_page": page.has_next_page,
                        "total_rows": page.total_rows,
                        "bytes_processed": page.bytes_processed,
                        "estimated_bytes": dry_run.bytes_processed,
                    }
                    if page.has_next_page:
                        pending_queries.add(page.query_id)
                else:  # read_page
                    page = self.bigquery.read_page(action["query_id"])
                    if not page.has_next_page:
                        pending_queries.discard(page.query_id)
                    tool_result = {
                        "action": "query_page",
                        "query_id": page.query_id,
                        "columns": list(page.columns),
                        "rows": list(page.rows),
                        "has_next_page": page.has_next_page,
                        "total_rows": page.total_rows,
                        "bytes_processed": page.bytes_processed,
                    }
            except Exception as exc:
                tool_result = {
                    "action": "tool_error",
                    "requested_action": action["action"],
                    "error": str(exc),
                    "instruction": "Corrige el paso o busca otra fuente y continúa; si no hay alternativa, explica la limitación en la respuesta final.",
                }

            result_id = self._fingerprint(tool_result)
            if previous_call == (action_id, result_id):
                return AgentAnswer(
                    answer=("El análisis quedó incompleto porque se repitió una acción sin obtener información nueva. "
                            "No puedo confirmar un resultado final con la evidencia disponible."),
                    assumptions=(),
                    query_count=query_count,
                    tables_used=tuple(sorted(tables_used)),
                    bytes_processed=bytes_processed,
                    usage=total_usage,
                )
            previous_call = (action_id, result_id)
            last_tool_result = tool_result

    @staticmethod
    def _add_usage(total: dict[str, int], usage: Mapping[str, int]) -> None:
        for name, value in usage.items():
            total[name] = total.get(name, 0) + int(value or 0)
