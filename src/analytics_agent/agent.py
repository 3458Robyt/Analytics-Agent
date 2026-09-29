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
        return any(tree.find_all(*aggregate_nodes)) or bool(tree.find(exp.Group))

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
        session_store: Any | None = None,
        session_id: str = "",
    ) -> AgentAnswer:
        question = question.strip()
        if not question:
            return AgentAnswer("Escribe una pregunta para iniciar el análisis.")

        def progress(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        turn_id = ""
        if session_store is not None:
            if not session_id:
                session_id = session_store.create_session(question)
            turn_id = session_store.begin_turn(session_id, question)

        retrieved_memory = ""
        glossary_context = ""
        if session_store is not None:
            recent_context = session_store.recent_context(session_id)
            search_context = session_store.search_context(question)
            if recent_context:
                retrieved_memory += "Conversación reciente de esta sesión:\n" + recent_context
            if search_context:
                retrieved_memory += "\n\nConversaciones anteriores relevantes:\n" + search_context
        try:
            from .memory import load_glossary

            glossary_context, _ = load_glossary(question)
        except Exception:
            glossary_context = ""

        working_notes = ""
        last_tool_result: dict[str, Any] | None = None
        total_usage: dict[str, int] = {}
        query_count = 0
        bytes_processed = 0
        tables_used: set[str] = set()
        has_aggregate_results = False
        previous_call: tuple[str, str] | None = None
        pending_queries: set[str] = set()
        action_history: list[dict[str, Any]] = []
        query_data: dict[str, dict[str, Any]] = {}
        aggregate_query_ids: set[str] = set()
        query_jobs: dict[str, dict[str, str]] = {}
        analysis_plan: dict[str, Any] | None = None
        review_feedback = ""
        review_attempts = 0
        review_limit = 2
        plan_count = 0
        sequence = 0

        try:
            while True:
                action, usage = self.llm.next_action(
                    question,
                    context=context,
                    working_notes=working_notes,
                    last_tool_result=last_tool_result,
                    action_history=action_history[-20:],
                    analysis_plan=analysis_plan,
                    retrieved_memory=retrieved_memory,
                    glossary_context=glossary_context,
                    review_feedback=review_feedback,
                )
                self._add_usage(total_usage, usage)
                if action["notes"].strip():
                    working_notes = action["notes"].strip()

                if action["action"] == "plan":
                    plan_count += 1
                    if plan_count > 2:
                        answer = AgentAnswer(
                            answer="El análisis no pudo avanzar desde la planificación; no se ejecutó ninguna consulta.",
                            session_id=session_id,
                        )
                        if session_store is not None:
                            session_store.complete_turn(turn_id, answer.answer)
                        return answer
                    analysis_plan = action["plan"]
                    progress(
                        "Plan de análisis: "
                        + "; ".join(
                            value for value in (
                                str(analysis_plan.get("metric", "")).strip(),
                                str(analysis_plan.get("period", "")).strip(),
                                str(analysis_plan.get("grain", "")).strip(),
                            ) if value
                        )
                    )
                    tool_result = {"action": "plan", "plan": analysis_plan}
                    sequence += 1
                    if session_store is not None:
                        session_store.log_action(turn_id, sequence, "plan", action, tool_result)
                    action_history.append({"action": "plan", "plan": analysis_plan})
                    last_tool_result = tool_result
                    continue
                if analysis_plan is None:
                    analysis_plan = {
                        "metric": question,
                        "period": "Según la pregunta; si no se indica, declarar el supuesto usado.",
                        "grain": "Según la pregunta.",
                        "filters": [],
                        "sources": [],
                        "assumptions": [],
                    }
                    progress("Preparando un plan de análisis inicial.")
                    tool_result = {"action": "plan", "plan": analysis_plan, "source": "fallback"}
                    sequence += 1
                    if session_store is not None:
                        session_store.log_action(turn_id, sequence, "plan", {}, tool_result)
                    action_history.append({"action": "plan", "plan": analysis_plan})
                    last_tool_result = tool_result
                    continue

                if action["action"] == "finish":
                    if pending_queries:
                        query_id = sorted(pending_queries)[0]
                        progress("Completando páginas pendientes antes de cerrar el análisis.")
                        page = self.bigquery.read_page(query_id)
                        if not page.has_next_page:
                            pending_queries.discard(query_id)
                        page_result = self._page_result(page)
                        query_data.setdefault(query_id, {"columns": list(page.columns), "rows": []})["rows"].extend(page_result["rows"])
                        self._record_page_job(page, query_jobs)
                        tool_result = {"action": "query_page", **page_result}
                        sequence += 1
                        if session_store is not None:
                            session_store.log_action(turn_id, sequence, "read_page", {"query_id": query_id}, tool_result,
                                                     aggregate=query_id in aggregate_query_ids)
                        action_history.append(self._history_entry("read_page", {"query_id": query_id}, tool_result))
                        last_tool_result = tool_result
                        continue

                    summary = self._summary_table(action.get("summary_table"))
                    if not has_aggregate_results:
                        summary = None
                    deterministic_issues = self._verify_summary_table(summary, query_data, aggregate_query_ids)
                    present_rows = bool(action.get("present_rows", False))
                    detail_table = self._detail_table(query_data, aggregate_query_ids) if present_rows else None
                    if present_rows and detail_table is None:
                        deterministic_issues.append("Se solicitaron filas detalladas, pero no hay un resultado tabular disponible para mostrarlas.")

                    reviewer = getattr(self.llm, "review_answer", None)
                    review_result = {"accepted": True, "issues": []}
                    if callable(reviewer) and review_attempts < review_limit:
                        progress("Revisando que la respuesta coincida con el plan y la evidencia.")
                        try:
                            review_result, review_usage = reviewer(
                                question,
                                plan=analysis_plan,
                                draft=action,
                                action_history=action_history,
                                evidence=self._review_evidence(query_data, aggregate_query_ids, query_jobs),
                                deterministic_issues=deterministic_issues,
                            )
                            self._add_usage(total_usage, review_usage)
                        except Exception as review_error:
                            review_result = {
                                "accepted": False,
                                "issues": [f"No se pudo completar la revisión final ({type(review_error).__name__})."],
                            }
                    issues = [*deterministic_issues, *review_result.get("issues", [])]
                    accepted = bool(review_result.get("accepted", False)) and not deterministic_issues
                    if not accepted and review_attempts < review_limit:
                        review_attempts += 1
                        review_feedback = "\n".join(dict.fromkeys(issues)) or "La respuesta necesita una comprobación adicional."
                        review_event = {"accepted": False, "issues": issues, "attempt": review_attempts}
                        sequence += 1
                        if session_store is not None:
                            session_store.log_action(turn_id, sequence, "review", action, review_event)
                        action_history.append({"action": "review", **review_event})
                        last_tool_result = {"action": "review_feedback", **review_event}
                        continue

                    answer_text = action["answer"].strip()
                    if not accepted:
                        issue_text = "; ".join(dict.fromkeys(issues)) or "la respuesta no quedó verificada"
                        answer_text += f"\n\n**Revisión:** no pude confirmar todos los puntos: {issue_text}"
                    else:
                        sequence += 1
                        review_event = {"accepted": True, "issues": []}
                        if session_store is not None:
                            session_store.log_action(turn_id, sequence, "review", action, review_event)

                    answer = AgentAnswer(
                        answer=answer_text,
                        assumptions=tuple(action["assumptions"]),
                        summary_table=summary,
                        detail_table=detail_table,
                        query_count=query_count,
                        tables_used=tuple(sorted(tables_used)),
                        bytes_processed=bytes_processed,
                        usage=total_usage,
                        session_id=session_id,
                        query_jobs=tuple(query_jobs.values()),
                    )
                    if session_store is not None:
                        stored_summary = None
                        if summary is not None:
                            stored_summary = {"title": summary.title, "columns": summary.columns, "rows": summary.rows}
                        session_store.complete_turn(turn_id, answer.answer, answer.assumptions, stored_summary)
                    return answer

                action_id = self._fingerprint({key: value for key, value in action.items() if key not in {"notes"}})
                progress("Buscando tablas en el catálogo." if action["action"] == "search_tables" else
                         "Consultando metadatos de BigQuery." if action["action"] == "describe_tables" else
                         "Ejecutando una consulta de lectura." if action["action"] == "run_select" else
                         "Leyendo la siguiente página de resultados.")
                try:
                    if action["action"] == "search_tables":
                        matches, total = self.catalog.search_tables(
                            action["search_text"], offset=action["offset"], page_size=action["page_size"]
                        )
                        tool_result = {
                            "action": "search_tables",
                            "search_text": action["search_text"],
                            "offset": action["offset"],
                            "page_size": action["page_size"],
                            "total_matches": total,
                            "next_offset": action["offset"] + len(matches) if action["offset"] + len(matches) < total else None,
                            "tables": [
                                {"table_id": item.table_id, "description": item.description,
                                 "table_type": item.table_type, "location": item.location}
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
                        validated = validate_sql(validated.sql, self.catalog)
                        locations = {
                            self.catalog.tables[table_id].location.strip().lower()
                            for table_id in validated.tables
                            if table_id in self.catalog.tables and self.catalog.tables[table_id].location.strip()
                        }
                        if len(locations) > 1:
                            raise BigQueryError("La consulta combina tablas de regiones distintas. Separa las consultas por región.")
                        location = next(iter(locations), self.settings.bigquery_location or self.bigquery.location)
                        dry_run = self.bigquery.dry_run(validated.sql, location=location)
                        if not dry_run.ok:
                            raise BigQueryError(dry_run.error or "El dry run de BigQuery no fue aprobado")
                        page = self.bigquery.execute(validated.sql, location=location)
                        query_count += 1
                        bytes_processed += int(page.bytes_processed or dry_run.bytes_processed or 0)
                        tables_used.update(validated.tables)
                        is_aggregate = self._is_aggregate(validated.sql)
                        has_aggregate_results = has_aggregate_results or is_aggregate
                        if is_aggregate:
                            aggregate_query_ids.add(page.query_id)
                        query_data[page.query_id] = {"columns": list(page.columns), "rows": list(page.rows)}
                        self._record_page_job(page, query_jobs)
                        tool_result = {
                            "action": "query_page",
                            **self._page_result(page),
                            "estimated_bytes": dry_run.bytes_processed,
                            "sql": validated.sql,
                            "tables": list(validated.tables),
                        }
                        if page.has_next_page:
                            pending_queries.add(page.query_id)
                    else:  # read_page
                        page = self.bigquery.read_page(action["query_id"])
                        if not page.has_next_page:
                            pending_queries.discard(page.query_id)
                        query_data.setdefault(page.query_id, {"columns": list(page.columns), "rows": []})["rows"].extend(page.rows)
                        self._record_page_job(page, query_jobs)
                        tool_result = {"action": "query_page", **self._page_result(page)}
                except Exception as exc:
                    tool_result = {
                        "action": "tool_error",
                        "requested_action": action["action"],
                        "error": str(exc),
                        "instruction": "Corrige el paso o busca otra fuente y continúa; si no hay alternativa, explica la limitación.",
                    }

                sequence += 1
                if session_store is not None:
                    session_store.log_action(
                        turn_id,
                        sequence,
                        action["action"],
                        action,
                        tool_result,
                        aggregate=action["action"] == "run_select" and self._is_aggregate(str(action.get("sql", "")))
                        or action["action"] == "read_page" and action.get("query_id") in aggregate_query_ids,
                    )
                result_id = self._fingerprint({key: value for key, value in tool_result.items() if key != "rows"})
                if previous_call == (action_id, result_id):
                    answer = AgentAnswer(
                        answer="El análisis quedó incompleto porque se repitió una acción sin obtener información nueva.",
                        query_count=query_count,
                        tables_used=tuple(sorted(tables_used)),
                        bytes_processed=bytes_processed,
                        usage=total_usage,
                        session_id=session_id,
                        query_jobs=tuple(query_jobs.values()),
                    )
                    if session_store is not None:
                        session_store.complete_turn(turn_id, answer.answer)
                    return answer
                previous_call = (action_id, result_id)
                action_history.append(self._history_entry(action["action"], action, tool_result))
                last_tool_result = tool_result
        except Exception as exc:
            if session_store is not None and turn_id:
                with_context = f"La pregunta quedó incompleta por un error: {type(exc).__name__}."
                session_store.complete_turn(turn_id, with_context)
            raise

    @staticmethod
    def _page_result(page: Any) -> dict[str, Any]:
        return {
            "query_id": page.query_id,
            "job_id": getattr(page, "job_id", ""),
            "job_project": getattr(page, "job_project", ""),
            "location": getattr(page, "location", ""),
            "columns": list(page.columns),
            "rows": list(page.rows),
            "has_next_page": page.has_next_page,
            "total_rows": page.total_rows,
            "bytes_processed": page.bytes_processed,
        }

    @staticmethod
    def _record_page_job(page: Any, jobs: dict[str, dict[str, str]]) -> None:
        job_id = str(getattr(page, "job_id", "") or "")
        if not job_id:
            return
        jobs[page.query_id] = {
            "query_id": page.query_id,
            "job_id": job_id,
            "project": str(getattr(page, "job_project", "") or ""),
            "location": str(getattr(page, "location", "") or ""),
        }

    @staticmethod
    def _history_entry(action: str, request: Mapping[str, Any], result: Mapping[str, Any]) -> dict[str, Any]:
        compact_result = {key: value for key, value in result.items() if key != "rows"}
        if isinstance(result.get("rows"), list):
            compact_result["rows_in_page"] = len(result["rows"])
        return {"action": action, "request": dict(request), "result": compact_result}

    @staticmethod
    def _same_value(left: Any, right: Any) -> bool:
        from decimal import Decimal, InvalidOperation

        try:
            if isinstance(left, bool) or isinstance(right, bool):
                return left == right
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, ValueError, TypeError):
            return left == right

    def _verify_summary_table(
        self,
        summary: SummaryTable | None,
        query_data: Mapping[str, Mapping[str, Any]],
        aggregate_query_ids: set[str],
    ) -> list[str]:
        if summary is None:
            return []
        if not summary.columns:
            return ["La tabla de resumen no define columnas para validar."]
        evidence_rows = [
            row
            for query_id in aggregate_query_ids
            for row in query_data.get(query_id, {}).get("rows", [])
        ]
        if not evidence_rows:
            return ["La tabla de resumen no tiene filas agregadas de BigQuery que la respalden."]
        for row in summary.rows:
            if set(row) != set(summary.columns):
                return ["Una fila de la tabla de resumen no contiene exactamente todas sus columnas."]
            if not any(
                all(key in evidence and self._same_value(value, evidence[key]) for key, value in row.items())
                for evidence in evidence_rows
            ):
                return ["Al menos una fila de la tabla de resumen no coincide con los resultados agregados leídos."]
        return []

    @staticmethod
    def _detail_table(
        query_data: Mapping[str, Mapping[str, Any]], aggregate_query_ids: set[str]
    ) -> SummaryTable | None:
        for query_id, result in reversed(list(query_data.items())):
            if query_id in aggregate_query_ids or not result.get("columns") or not result.get("rows"):
                continue
            return SummaryTable(
                title="Filas solicitadas",
                columns=tuple(result["columns"]),
                rows=tuple({str(key): value for key, value in row.items()} for row in result["rows"]),
            )
        return None

    @staticmethod
    def _review_evidence(
        query_data: Mapping[str, Mapping[str, Any]],
        aggregate_query_ids: set[str],
        query_jobs: Mapping[str, Mapping[str, str]],
    ) -> list[dict[str, Any]]:
        return [
            {
                "query_id": query_id,
                "job": query_jobs.get(query_id, {}),
                "columns": data.get("columns", []),
                "rows": data.get("rows", []) if query_id in aggregate_query_ids else [],
                "row_count": len(data.get("rows", [])),
            }
            for query_id, data in query_data.items()
        ]

    @staticmethod
    def _add_usage(total: dict[str, int], usage: Mapping[str, int]) -> None:
        for name, value in usage.items():
            total[name] = total.get(name, 0) + int(value or 0)
