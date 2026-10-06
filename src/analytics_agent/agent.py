from __future__ import annotations

import hashlib
import json
import re
import time
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping

from sqlglot import exp, parse

from .bigquery_adapter import BigQueryAdapter, BigQueryError
from .guardrails import extract_table_ids, validate_sql
from .llm import LLMError, OpenAICompatibleProvider
from .models import AgentAnswer, AgentSettings, Clarification, SummaryTable, TABLE_IDS
from .semantics import MetricDefinitionError, compile_metric_sql, load_metric_definitions
from .schema import ColumnSchema, SchemaCatalog, TableSchema, merge_bigquery_schema


class AnalyticsAgent:
    """Explores known BigQuery sources with read-only SQL and summarizes results."""

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
        self.metric_definitions = load_metric_definitions()
        known_descriptions = {
            "th_primas_final": "Fuente candidata de primas emitidas; validar columnas y periodo mediante SQL.",
            "mm_final": "Fuente candidata de movimientos mensuales de siniestros; validar definición mediante SQL.",
            "mm_final_netos": "Fuente candidata de valores netos de siniestros; validar definición mediante SQL.",
        }
        for sheet_name, table_id in TABLE_IDS.items():
            existing = self.catalog.tables.get(table_id.lower())
            location = (
                existing.location if existing and existing.location
                else settings.bigquery_location or ("us-east1" if table_id.startswith("centralizacion-datos.") else "")
            )
            self.catalog.add_table(TableSchema(
                sheet_name=existing.sheet_name if existing else sheet_name,
                table_id=table_id,
                columns=existing.columns if existing else {},
                warnings=existing.warnings if existing else (),
                description=(existing.description if existing else "") or known_descriptions.get(sheet_name, ""),
                table_type=existing.table_type if existing else "",
                location=location,
                metadata_loaded=existing.metadata_loaded if existing else False,
            ))

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

    def _ensure_table(
        self,
        table_id: str,
        visiting: set[str] | None = None,
        session_store: Any | None = None,
    ) -> TableSchema:
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
            and time.monotonic() - getattr(existing, "metadata_loaded_at", 0.0) < 3600
            and existing.table_type.upper() not in {"VIEW", "MATERIALIZED_VIEW"}
        ):
            return existing

        metadata = None
        cache_reader = getattr(session_store, "get_schema_metadata", None)
        if callable(cache_reader):
            metadata = cache_reader(table_id, max_age_seconds=3600)
        if metadata is None:
            metadata = self.bigquery.inspect_table_metadata(table_id)
            cache_writer = getattr(session_store, "set_schema_metadata", None)
            if callable(cache_writer):
                cache_writer(table_id, metadata)
        previous = self.catalog.tables.get(normalized_id)
        actual_names = {str(name).lower() for name in metadata.get("actual_schema", {})}
        stale_fields = sorted(set(previous.columns) - actual_names) if previous else []
        if stale_fields:
            warning = (
                f"El diccionario contiene {len(stale_fields)} campo(s) que no aparecen en el esquema actual de "
                f"`{table_id}`; no se usarán para generar SQL."
            )
            self.catalog.warnings = tuple(dict.fromkeys((*self.catalog.warnings, warning)))
        merge_bigquery_schema(self.catalog, {table_id: {"ok": True, **metadata}})
        table = self.catalog.tables.get(normalized_id)
        if table is not None:
            from dataclasses import replace

            table = replace(table, metadata_loaded_at=time.monotonic())
            self.catalog.tables[normalized_id] = table
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
                    self._ensure_table(nested_table, stack, session_store)
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

    @staticmethod
    def _summary_from_evidence(
        raw: Any,
        query_data: Mapping[str, Mapping[str, Any]],
        aggregate_query_ids: set[str],
        query_id: str = "",
    ) -> SummaryTable | None:
        candidates = [query_id] if query_id in aggregate_query_ids else []
        candidates.extend(item for item in reversed(list(query_data))
                          if item in aggregate_query_ids and item not in candidates)
        for candidate in candidates:
            result = query_data[candidate]
            evidence_rows = list(result.get("rows", []))
            evidence_columns = tuple(str(column) for column in result.get("columns", []))
            if not evidence_columns or not evidence_rows:
                continue
            types = result.get("column_types", {})
            numeric_types = {"INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"}
            evidence_numeric_columns = {
                column for column in evidence_columns
                if str(types.get(column, "")).upper() in numeric_types
                or (column not in types and any(
                    isinstance(row.get(column), (int, float, Decimal)) and not isinstance(row.get(column), bool)
                    for row in evidence_rows
                ))
            }
            requested_columns: tuple[str, ...] = ()
            title = "Resultado de BigQuery"
            if isinstance(raw, Mapping):
                raw_columns = raw.get("columns")
                if isinstance(raw_columns, list) and all(isinstance(column, str) for column in raw_columns):
                    if (
                        raw_columns
                        and set(raw_columns).issubset(evidence_columns)
                        and evidence_numeric_columns.issubset(set(raw_columns))
                    ):
                        requested_columns = tuple(raw_columns)
                if isinstance(raw.get("title"), str) and raw["title"].strip():
                    title = raw["title"].strip()
            columns = requested_columns or evidence_columns
            # BigQuery owns every value. The model can suggest a display title
            # or a subset of columns, but cannot invent or alter result rows.
            rows = tuple({column: row.get(column) for column in columns} for row in evidence_rows)
            numeric_columns = tuple(column for column in columns if column in evidence_numeric_columns)
            return SummaryTable(title=title, columns=columns, rows=rows, numeric_columns=numeric_columns)
        return None

    @staticmethod
    def _result_sentence(answer: str, summary: SummaryTable | None, detail: SummaryTable | None = None) -> str:
        # Numeric claims are assembled from the actual rows, never copied from
        # free-form model prose. Dates and amounts remain available in the table
        # and audit panel.
        if summary and summary.rows:
            numeric_columns: list[str] = list(summary.numeric_columns)
            for column in summary.columns:
                if column not in numeric_columns and any(
                    not isinstance(row.get(column), bool)
                    and isinstance(row.get(column), (int, float, Decimal))
                    for row in summary.rows
                ):
                    numeric_columns.append(column)
            if len(numeric_columns) == 1:
                column = numeric_columns[0]
                numeric_rows: list[tuple[Decimal, Mapping[str, Any]]] = []
                for row in summary.rows:
                    try:
                        numeric_rows.append((Decimal(str(row.get(column))), row))
                    except (InvalidOperation, TypeError, ValueError):
                        continue
                if numeric_rows:
                    value, row = max(numeric_rows, key=lambda item: item[0])
                    dimensions = [key for key in summary.columns if key != column]
                    rendered_value = f"{value:,.2f}" if value.as_tuple().exponent < 0 else f"{value:,.0f}"
                    rendered_value = rendered_value.replace(",", "§").replace(".", ",").replace("§", ".")
                    if dimensions and len(summary.rows) > 1:
                        labels = ", ".join(f"{key}: {row.get(key)}" for key in dimensions)
                        return f"El mayor valor de **{column}** corresponde a **{labels}**, con **{rendered_value}**."
                    return f"El resultado de **{column}** es **{rendered_value}**."
            return f"La consulta devolvió {len(summary.rows)} fila(s) agregadas. La tabla contiene los valores calculados."
        if detail and detail.rows:
            return f"La consulta devolvió {len(detail.rows)} fila(s) de detalle; se muestran en la tabla."
        if answer and not any(character.isdigit() for character in answer):
            return answer
        if answer:
            return "La consulta terminó; los valores verificados se muestran en la tabla y en la auditoría."
        return "BigQuery no devolvió filas para el criterio consultado."

    @staticmethod
    def _audit_records(
        query_sql: Mapping[str, str],
        query_tables: Mapping[str, tuple[str, ...]],
        query_jobs: Mapping[str, Mapping[str, str]],
        query_bytes: Mapping[str, int],
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "query_id": query_id,
                "sql": query_sql.get(query_id, ""),
                "tables": list(query_tables.get(query_id, ())),
                "job": dict(query_jobs.get(query_id, {})),
                "bytes_processed": query_bytes.get(query_id, 0),
            }
            for query_id in query_sql
        )

    def answer(
        self,
        question: str,
        *,
        context: str = "",
        on_progress: Callable[[str], None] | None = None,
        on_clarification: Callable[[Clarification], str] | None = None,
        session_store: Any | None = None,
        session_id: str = "",
        pending_turn_id: str = "",
        clarification_response: str = "",
    ) -> AgentAnswer:
        question = question.strip()
        turn_started = time.perf_counter()
        resumed_turn: dict[str, Any] | None = None
        if pending_turn_id:
            if session_store is None or not clarification_response.strip():
                raise ValueError("Reanudar un turno requiere SessionStore y una aclaración no vacía")
            resumed_turn = session_store.resume_pending_turn(pending_turn_id, clarification_response)
            question = str(resumed_turn["question"])
        if not question:
            return AgentAnswer("Escribe una pregunta para iniciar el análisis.")

        def progress(message: str) -> None:
            if on_progress is not None:
                on_progress(message)

        turn_id = ""
        if session_store is not None:
            if resumed_turn is not None:
                session_id = str(resumed_turn["session_id"])
                turn_id = str(resumed_turn["turn_id"])
            else:
                if not session_id:
                    session_id = session_store.create_session(question)
                turn_id = session_store.begin_turn(session_id, question)

        retrieved_memory = ""
        glossary_context = ""
        if session_store is not None:
            recent_context = session_store.recent_context(session_id, limit=2)
            search_context = (
                session_store.search_context(question)
                if re.search(r"\b(consulta|sql|pregunta|resultado)\s+(anterior|pasada|previa)\b", question.lower())
                else ""
            )
            if recent_context:
                retrieved_memory += "Conversación reciente de esta sesión:\n" + recent_context
            if search_context:
                retrieved_memory += "\n\nConversaciones anteriores relevantes:\n" + search_context
            learning_context = session_store.learning_context(question)
            if learning_context:
                retrieved_memory += "\n\nPreferencias y procedimientos aprendidos:\n" + learning_context
            definition_context = session_store.confirmed_business_definitions(question)
            if definition_context:
                retrieved_memory += "\n\nDefiniciones de negocio confirmadas personalmente por el usuario:\n" + definition_context
        try:
            from .memory import load_glossary

            glossary_context, _ = load_glossary(question)
        except Exception:
            glossary_context = ""

        working_notes = ""
        question_for_model = question
        user_clarifications: list[str] = []
        resumed_action_history: list[dict[str, Any]] = []
        if resumed_turn is not None:
            prior_clarification = resumed_turn.get("clarification", {})
            clarification_prompt = str(prior_clarification.get("question", ""))
            proposed_rule = str(prior_clarification.get("proposed_rule", ""))
            user_reply = str(resumed_turn.get("user_response", ""))
            user_clarifications.append(
                f"Pregunta de aclaración: {clarification_prompt}\n"
                + (f"Regla propuesta: {proposed_rule}\n" if proposed_rule else "")
                + f"Respuesta explícita del usuario: {user_reply}"
            )
            question_for_model = (
                f"{question}\n\n{user_clarifications[-1]}\n"
                "Usa esta aclaración para continuar la misma consulta; no repitas la pregunta respondida."
            )
            resumed_action_history.append({
                "action": "clarification_received",
                "question": clarification_prompt,
                "user_answer": user_reply,
            })
        clarification_count = 0
        stage_timings: dict[str, float] = {}
        last_tool_result: dict[str, Any] | None = None
        total_usage: dict[str, int] = {}
        query_count = 0
        bytes_processed = 0
        tables_used: set[str] = set()
        has_aggregate_results = False
        previous_call: tuple[str, str] | None = None
        previous_action_id: str | None = None
        pending_queries: set[str] = set()
        action_history: list[dict[str, Any]] = list(resumed_action_history)
        query_data: dict[str, dict[str, Any]] = {}
        aggregate_query_ids: set[str] = set()
        query_jobs: dict[str, dict[str, str]] = {}
        query_sql: dict[str, str] = {}
        query_tables: dict[str, tuple[str, ...]] = {}
        query_bytes: dict[str, int] = {}
        metric_query_ids: set[str] = set()
        analysis_plan: dict[str, Any] | None = {
            "metric": question,
            "period": "El indicado por el usuario; si hay ambigüedad, aplicar y declarar un supuesto razonable.",
            "grain": "El solicitado; si no se indica, devolver el total del periodo.",
            "population": "Los registros accesibles de la fuente más probable.",
            "filters": [],
            "sources": list(TABLE_IDS.values()),
            "steps": ["Probar una consulta SELECT directa y corregirla con la validación de BigQuery."],
            "assumptions": [],
        }
        review_feedback = ""
        review_attempts = 0
        review_limit = 2
        plan_count = 0
        sequence = session_store.next_action_sequence(turn_id) if session_store is not None and turn_id else 0

        def save_explicit_definition(clarification: Clarification, reply: str) -> None:
            if not (session_store is not None and clarification.metric_key and clarification.proposed_rule):
                return
            normalized_reply = " ".join(reply.lower().split())
            confirmations = (
                "sí", "si", "confirmo", "correcto", "de acuerdo", "cuenta todo",
                "incluye todos", "incluir todos", "usa esa regla", "aplica esa regla",
            )
            if not any(
                normalized_reply == prefix or normalized_reply.startswith(prefix + " ")
                or normalized_reply.startswith(prefix + ",") or normalized_reply.startswith(prefix + ";")
                for prefix in confirmations
            ):
                return
            try:
                session_store.confirm_business_definition(
                    clarification.metric_key,
                    clarification.proposed_rule,
                    clarification.question,
                    reply,
                )
            except ValueError:
                return

        def pending_clarification_answer(clarification: Clarification) -> AgentAnswer:
            if session_store is not None and turn_id:
                session_store.set_pending_clarification(turn_id, {
                    "question": clarification.question,
                    "metric_key": clarification.metric_key,
                    "proposed_rule": clarification.proposed_rule,
                })
            return AgentAnswer(
                answer="La consulta espera la aclaración de negocio para continuar.",
                assumptions=tuple(str(item) for item in (analysis_plan or {}).get("assumptions", []) if item),
                query_count=query_count,
                tables_used=tuple(sorted(tables_used)),
                bytes_processed=bytes_processed,
                usage=total_usage,
                session_id=session_id,
                status="needs_clarification",
                clarification=clarification,
                audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes),
                timings={**stage_timings, "total": time.perf_counter() - turn_started},
                turn_id=turn_id,
            )

        if resumed_turn is not None:
            prior_clarification = resumed_turn.get("clarification", {})
            clarification = Clarification(
                question=str(prior_clarification.get("question", "")),
                metric_key=str(prior_clarification.get("metric_key", "")),
                proposed_rule=str(prior_clarification.get("proposed_rule", "")),
            )
            reply = str(resumed_turn.get("user_response", ""))
            save_explicit_definition(clarification, reply)
            confirmation_event = {
                "action": "clarification_received",
                "question": clarification.question,
                "user_answer": reply,
            }
            if session_store is not None:
                session_store.log_action(turn_id, sequence, "clarification_received", {}, confirmation_event)
                sequence += 1

        def best_effort_answer(reason: str) -> AgentAnswer:
            nonlocal bytes_processed
            for pending_id in tuple(pending_queries):
                try:
                    while pending_id in pending_queries:
                        page = self.bigquery.read_page(pending_id)
                        if not page.has_next_page:
                            pending_queries.discard(pending_id)
                        page_result = self._page_result(page)
                        query_data.setdefault(
                            pending_id, {"columns": list(page.columns), "rows": []}
                        )["rows"].extend(page_result["rows"])
                        bytes_processed += int(page.bytes_processed or 0)
                        self._record_page_job(page, query_jobs)
                except Exception as page_error:
                    action_history.append({
                        "action": "read_page",
                        "request": {"query_id": pending_id},
                        "result": {"error": str(page_error)},
                    })
                    pending_queries.discard(pending_id)
            metric = str((analysis_plan or {}).get("metric") or question).strip()
            period = str((analysis_plan or {}).get("period") or "el periodo pedido").strip()
            verified_summary: SummaryTable | None = None
            for query_id in reversed(list(query_data)):
                data = query_data[query_id]
                rows = data.get("rows", [])
                columns = tuple(str(item) for item in data.get("columns", []))
                if query_id in aggregate_query_ids and query_id not in pending_queries and rows and columns:
                    verified_summary = self._summary_from_evidence(
                        None, query_data, aggregate_query_ids, query_id
                    )
                    break
            latest_error = ""
            attempted_sql = ""
            for entry in reversed(action_history):
                result = entry.get("result", {})
                if not latest_error and result.get("error"):
                    latest_error = str(result["error"])
                if not attempted_sql:
                    attempted_sql = str(entry.get("request", {}).get("sql") or "")
                if latest_error and attempted_sql:
                    break
            if verified_summary is not None:
                answer_text = self._result_sentence(
                    (
                    f"BigQuery ejecutó una consulta agregada para **{metric}** ({period}) y devolvió "
                    f"{len(verified_summary.rows)} filas. La tabla contiene los valores calculados directamente por SQL."
                    ),
                    verified_summary,
                )
                answer = AgentAnswer(
                    answer=answer_text,
                    assumptions=tuple(str(item) for item in (analysis_plan or {}).get("assumptions", []) if item),
                    summary_table=verified_summary,
                    query_count=query_count,
                    tables_used=tuple(sorted(tables_used)),
                    bytes_processed=bytes_processed,
                    usage=total_usage,
                    session_id=session_id,
                    query_jobs=tuple(query_jobs.values()),
                    audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes),
                    timings={**stage_timings, "total": time.perf_counter() - turn_started},
                    turn_id=turn_id,
                )
                if session_store is not None and turn_id:
                    stored_summary = {
                        "title": verified_summary.title,
                        "columns": verified_summary.columns,
                        "rows": verified_summary.rows,
                    }
                    session_store.complete_turn(turn_id, answer.answer, answer.assumptions, stored_summary)
                return answer

            details = f" BigQuery informó: {latest_error}." if latest_error else ""
            sql_note = f"\n\nÚltimo SQL probado:\n```sql\n{attempted_sql}\n```" if attempted_sql else ""
            answer_text = (
                f"**Interpretación:** {metric}; {period}.\n\n"
                f"No pude verificar una cifra en esta ejecución porque {reason}.{details} "
                "La fuente candidata más probable está entre `mm_final_netos`, `mm_final` y `th_primas_final`; "
                "el valor requiere que BigQuery acepte la consulta. No invento un resultado numérico."
                f"{sql_note}"
            )
            answer = AgentAnswer(
                answer=answer_text,
                assumptions=tuple(str(item) for item in (analysis_plan or {}).get("assumptions", []) if item),
                query_count=query_count,
                tables_used=tuple(sorted(tables_used)),
                bytes_processed=bytes_processed,
                usage=total_usage,
                session_id=session_id,
                query_jobs=tuple(query_jobs.values()),
                audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes),
                timings={**stage_timings, "total": time.perf_counter() - turn_started},
                turn_id=turn_id,
            )
            if session_store is not None and turn_id:
                session_store.complete_turn(turn_id, answer.answer, answer.assumptions)
            return answer

        try:
            while True:
                llm_started = time.perf_counter()
                action, usage = self.llm.next_action(
                    question_for_model,
                    context=context,
                    working_notes=working_notes,
                    last_tool_result=last_tool_result,
                    action_history=action_history[-20:],
                    analysis_plan=analysis_plan,
                    retrieved_memory=retrieved_memory,
                    glossary_context=glossary_context,
                    review_feedback=review_feedback,
                    catalog_context=self.catalog.prompt_text(query=question) if not action_history else "",
                    user_clarification="\n".join(user_clarifications),
                )
                stage_timings["model"] = stage_timings.get("model", 0.0) + time.perf_counter() - llm_started
                self._add_usage(total_usage, usage)
                if action["notes"].strip():
                    working_notes = action["notes"].strip()

                if action["action"] == "plan":
                    plan_count += 1
                    analysis_plan = action["plan"]
                    if plan_count == 1:
                        progress(
                            "Interpretación inicial: "
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

                    if plan_count > 3:
                        return best_effort_answer("el agente repitió la planificación y no avanzó a una consulta")

                    # A repeated plan is converted into a useful catalog action instead of
                    # consuming another turn or ending without trying BigQuery.
                    metric_text = f"{question} {analysis_plan.get('metric', '')}".lower()
                    if plan_count == 2:
                        action = {
                            **action,
                            "action": "search_tables",
                            "search_text": metric_text,
                            "offset": 0,
                            "page_size": 100,
                            "table_ids": [],
                        }
                    else:
                        if any(term in metric_text for term in ("prima", "vrprima", "emisi", "ramo")):
                            candidate = TABLE_IDS["th_primas_final"]
                        elif any(term in metric_text for term in ("incurrido", "siniestro", "reserva", "neto", "pago")):
                            candidate = TABLE_IDS["mm_final_netos"]
                        else:
                            candidate = TABLE_IDS["mm_final"]
                        action = {
                            **action,
                            "action": "run_select",
                            "sql": f"SELECT * FROM `{candidate}` LIMIT 0",
                            "table_ids": [],
                        }
                    last_tool_result = {
                        "action": "plan_already_recorded",
                        "instruction": "No vuelvas a planificar. Usa las fuentes candidatas, prueba SQL directamente y acepta supuestos razonables.",
                    }
                    progress(
                        "El plan ya está registrado; "
                        + ("busco la fuente candidata." if plan_count == 2 else "consulto directamente el esquema con SQL para poder continuar.")
                    )

                if action["action"] == "clarify":
                    raw_clarification = action["clarification"]
                    clarification = Clarification(
                        question=raw_clarification["question"].strip(),
                        metric_key=raw_clarification["metric_key"].strip(),
                        proposed_rule=raw_clarification["proposed_rule"].strip(),
                    )
                    sequence += 1
                    clarification_event = {
                        "action": "clarify",
                        "question": clarification.question,
                        "metric_key": clarification.metric_key,
                        "proposed_rule": clarification.proposed_rule,
                    }
                    action_history.append(clarification_event)
                    if session_store is not None:
                        session_store.log_action(turn_id, sequence, "clarify", action, clarification_event)
                    if on_clarification is None:
                        return pending_clarification_answer(clarification)
                    clarification_count += 1
                    if clarification_count > 2:
                        return pending_clarification_answer(clarification)
                    progress(clarification.question)
                    try:
                        reply = on_clarification(clarification).strip()
                    except (EOFError, KeyboardInterrupt):
                        return pending_clarification_answer(clarification)
                    if not reply:
                        return pending_clarification_answer(clarification)
                    save_explicit_definition(clarification, reply)
                    sequence += 1
                    confirmation_event = {
                        "action": "clarification_received",
                        "question": clarification.question,
                        "user_answer": reply,
                    }
                    action_history.append(confirmation_event)
                    if session_store is not None:
                        session_store.log_action(turn_id, sequence, "clarification_received", {}, confirmation_event)
                    user_clarifications.append(
                        f"Pregunta de aclaración: {clarification.question}\nRespuesta explícita del usuario: {reply}"
                    )
                    question_for_model = (
                        f"{question}\n\n{user_clarifications[-1]}\n"
                        "Usa esta aclaración para continuar la misma consulta; no repitas la pregunta respondida."
                    )
                    last_tool_result = {
                        "action": "clarification_received",
                        "question": clarification.question,
                        "user_answer": reply,
                        "instruction": "Continúa con la consulta o solicita únicamente otra decisión material aún pendiente.",
                    }
                    if session_store is not None:
                        recent_definitions = session_store.confirmed_business_definitions(question)
                        if recent_definitions:
                            retrieved_memory += "\n\nDefiniciones de negocio confirmadas personalmente por el usuario:\n" + recent_definitions
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

                    summary = self._summary_from_evidence(
                        action.get("summary_table"),
                        query_data,
                        aggregate_query_ids,
                        str(action.get("summary_query_id", "")),
                    ) if has_aggregate_results else None
                    deterministic_issues = self._verify_summary_table(summary, query_data, aggregate_query_ids)
                    present_rows = bool(action.get("present_rows", False))
                    detail_table = self._detail_table(query_data, aggregate_query_ids) if present_rows else None
                    if present_rows and detail_table is None:
                        deterministic_issues.append("Se solicitaron filas detalladas, pero no hay un resultado tabular disponible para mostrarlas.")

                    reviewer = getattr(self.llm, "review_answer", None)
                    review_result = {"accepted": True, "issues": []}
                    needs_review = any(query_id not in metric_query_ids for query_id in aggregate_query_ids)
                    if callable(reviewer) and review_attempts < review_limit and (needs_review or deterministic_issues):
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
                    learning_eligible = accepted and not any(
                        entry.get("result", {}).get("action") == "tool_error"
                        for entry in action_history
                    )
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

                    has_rows = any(result.get("rows") for result in query_data.values())
                    answer_text = (
                        "BigQuery no devolvió filas para el criterio consultado."
                        if query_count and not has_rows
                        else self._result_sentence(action["answer"].strip(), summary, detail_table)
                    )
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
                        audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes),
                        timings={**stage_timings, "total": time.perf_counter() - turn_started},
                        turn_id=turn_id,
                    )
                    if session_store is not None:
                        stored_summary = None
                        if summary is not None:
                            stored_summary = {"title": summary.title, "columns": summary.columns, "rows": summary.rows}
                        session_store.complete_turn(turn_id, answer.answer, answer.assumptions, stored_summary)
                        learning_updates: list[str] = []
                        queue_learning = getattr(session_store, "queue_learning_review", None)
                        if callable(queue_learning):
                            try:
                                queue_learning(
                                    turn_id,
                                    question=question,
                                    prior_context=retrieved_memory,
                                    plan=analysis_plan or {},
                                    answer=answer.answer,
                                    action_history=action_history,
                                    verified=learning_eligible,
                                    active_learnings=session_store.learning_context(question),
                                )
                                learning_updates.append("Preferencias y procedimientos pendientes de extracción en segundo plano.")
                            except Exception as learning_error:
                                progress(f"No se pudo encolar el aprendizaje ({type(learning_error).__name__}).")
                        answer = AgentAnswer(
                            answer=answer.answer,
                            assumptions=answer.assumptions,
                            summary_table=answer.summary_table,
                            detail_table=answer.detail_table,
                            query_count=answer.query_count,
                            tables_used=answer.tables_used,
                            bytes_processed=answer.bytes_processed,
                            usage=answer.usage,
                            session_id=answer.session_id,
                            query_jobs=answer.query_jobs,
                            learning_updates=tuple(learning_updates),
                            audit=answer.audit,
                            timings={**stage_timings, "total": time.perf_counter() - turn_started},
                            turn_id=turn_id,
                        )
                    return answer

                action_id = self._fingerprint({key: value for key, value in action.items() if key not in {"notes"}})
                if previous_action_id == action_id:
                    return best_effort_answer(
                        "el agente repitió exactamente la misma acción y no aportó evidencia nueva"
                    )
                progress("Buscando en la memoria de conversaciones y procedimientos." if action["action"] == "search_memory" else
                         "Buscando tablas en el catálogo." if action["action"] == "search_tables" else
                         "Consultando el esquema vivo y las descripciones disponibles." if action["action"] == "describe_tables" else
                         "Ejecutando la métrica confirmada." if action["action"] == "run_metric" else
                         "Ejecutando una consulta de lectura." if action["action"] == "run_select" else
                         "Leyendo la siguiente página de resultados.")
                try:
                    if action["action"] == "search_memory":
                        if session_store is None:
                            tool_result = {"action": "search_memory", "matches": [], "note": "No hay memoria persistente en esta ejecución."}
                        else:
                            query = action["search_text"]
                            sessions = session_store.search_context(query)
                            learnings = [
                                item for item in session_store.search_learning(query)
                                if item.get("status") == "active"
                            ]
                            tool_result = {
                                "action": "search_memory",
                                "search_text": query,
                                "conversation_matches": sessions,
                                "learning_matches": learnings,
                            }
                    elif action["action"] == "search_tables":
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
                            normalized_id = str(table_id).replace(":", ".").lower()
                            try:
                                table = self._ensure_table(str(table_id), session_store=session_store)
                            except Exception as schema_error:
                                descriptions.append({
                                    "table_id": str(table_id),
                                    "columns": [],
                                    "note": f"No se pudo leer el esquema vivo ({type(schema_error).__name__}); usa el dry run como alternativa.",
                                })
                                continue
                            if table is None:
                                descriptions.append({
                                    "table_id": str(table_id),
                                    "columns": [],
                                    "note": "No hay descripción local; intenta SQL directamente y usa el dry run para validar columnas.",
                                })
                                continue
                            descriptions.append({
                                "table_id": table.table_id,
                                "description": table.description,
                                "table_type": table.table_type,
                                "location": table.location,
                                "source": "esquema actual de BigQuery; descripciones del diccionario solo en coincidencias exactas",
                                "columns": [
                                    {"name": column.name, "type": column.data_type, "description": column.description}
                                    for column in table.columns.values()
                                ],
                                "note": "Si faltan columnas o definiciones, prueba una consulta SQL candidata; no es necesario tener el esquema completo.",
                            })
                        tool_result = {
                            "action": "describe_tables",
                            "tables": descriptions,
                            "metadata_api_used": True,
                            "instruction": "Los nombres y tipos vienen del esquema actual; una descripción ausente no impide continuar con SQL.",
                        }
                    elif action["action"] in {"run_select", "run_metric"}:
                        metric_definition: Mapping[str, Any] | None = None
                        requested_sql = action["sql"]
                        if action["action"] == "run_metric":
                            metric_definition = self.metric_definitions.get(str(action.get("metric_key", "")))
                            if metric_definition is None:
                                raise MetricDefinitionError(
                                    f"No hay una definición confirmada para `{action.get('metric_key', '')}`."
                                )
                            requested_sql = compile_metric_sql(
                                metric_definition,
                                start_date=action["start_date"],
                                end_date=action["end_date"],
                                group_by=action["group_by"],
                            )
                            action["sql"] = requested_sql
                        validated = validate_sql(requested_sql, self.catalog)
                        locations = {
                            self.catalog.tables[table_id].location.strip().lower()
                            for table_id in validated.tables
                            if table_id in self.catalog.tables and self.catalog.tables[table_id].location.strip()
                        }
                        if len(locations) > 1:
                            raise BigQueryError("La consulta combina tablas de regiones distintas. Separa las consultas por región.")
                        location = next(iter(locations), self.settings.bigquery_location or self.bigquery.location)
                        stage_started = time.perf_counter()
                        dry_run = self.bigquery.dry_run(validated.sql, location=location)
                        stage_timings["dry_run"] = stage_timings.get("dry_run", 0.0) + time.perf_counter() - stage_started
                        if not dry_run.ok:
                            raise BigQueryError(dry_run.error or "El dry run de BigQuery no fue aprobado")
                        stage_started = time.perf_counter()
                        page = self.bigquery.execute(validated.sql, location=location)
                        first_page = page
                        rows = list(page.rows)
                        self._record_page_job(page, query_jobs)
                        while page.has_next_page:
                            page = self.bigquery.read_page(page.query_id)
                            rows.extend(page.rows)
                            self._record_page_job(page, query_jobs)
                        stage_timings["query"] = stage_timings.get("query", 0.0) + time.perf_counter() - stage_started
                        query_count += 1
                        query_bytes[page.query_id] = int(first_page.bytes_processed or dry_run.bytes_processed or 0)
                        bytes_processed = sum(query_bytes.values())
                        tables_used.update(validated.tables)
                        is_aggregate = self._is_aggregate(validated.sql)
                        has_aggregate_results = has_aggregate_results or is_aggregate
                        if is_aggregate:
                            aggregate_query_ids.add(page.query_id)
                        if metric_definition is not None:
                            metric_query_ids.add(page.query_id)
                        expected_rows = getattr(first_page, "total_rows", None)
                        if expected_rows is not None and len(rows) != int(expected_rows):
                            warning = (
                                f"BigQuery reportó {expected_rows} filas y el agente recibió {len(rows)}. "
                                "La tabla puede estar incompleta."
                            )
                            analysis_plan.setdefault("assumptions", []).append(warning)
                        query_data[page.query_id] = {
                            "columns": list(first_page.columns),
                            "column_types": dict(getattr(first_page, "column_types", {}) or {}),
                            "rows": rows,
                        }
                        query_sql[page.query_id] = validated.sql
                        query_tables[page.query_id] = validated.tables
                        tool_result = {
                            "action": "query_page",
                            "query_id": page.query_id,
                            "job_id": getattr(first_page, "job_id", ""),
                            "job_project": getattr(first_page, "job_project", ""),
                            "location": getattr(first_page, "location", ""),
                            "columns": list(first_page.columns),
                            "column_types": dict(getattr(first_page, "column_types", {}) or {}),
                            "rows": rows,
                            "rows_read": len(rows),
                            "total_rows": first_page.total_rows,
                            "all_pages_read": True,
                            "bytes_processed": query_bytes[page.query_id],
                            "estimated_bytes": dry_run.bytes_processed,
                            "sql": validated.sql,
                            "tables": list(validated.tables),
                        }
                    else:  # read_page
                        requested_query_id = action["query_id"]
                        if requested_query_id in query_data and requested_query_id not in pending_queries:
                            existing_rows = query_data[requested_query_id].get("rows", [])
                            tool_result = {
                                "action": "query_already_complete",
                                "query_id": requested_query_id,
                                "rows_read": len(existing_rows),
                                "instruction": "Ya se leyeron todas las páginas por código. Continúa con finish; no pidas otra página.",
                            }
                        else:
                            page = self.bigquery.read_page(requested_query_id)
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
                    return best_effort_answer("BigQuery o el modelo repitieron el mismo paso sin aportar evidencia nueva")
                previous_call = (action_id, result_id)
                previous_action_id = action_id
                action_history.append(self._history_entry(action["action"], action, tool_result))
                last_tool_result = self._model_tool_result(tool_result)
        except Exception as exc:
            if session_store is not None and turn_id:
                with_context = f"La pregunta quedó incompleta por un error: {type(exc).__name__}."
                session_store.complete_turn(turn_id, with_context)
            raise

    @staticmethod
    def _page_result(page: Any) -> dict[str, Any]:
        return {
            "query_id": page.query_id,
            "column_types": dict(getattr(page, "column_types", {}) or {}),
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
    def _model_tool_result(result: Mapping[str, Any]) -> dict[str, Any]:
        compact = {key: value for key, value in result.items() if key != "rows"}
        rows = result.get("rows")
        if isinstance(rows, list):
            compact["rows_read"] = len(rows)
            compact["all_pages_read"] = True
        return compact

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
                numeric_columns=tuple(
                    column for column, column_type in result.get("column_types", {}).items()
                    if str(column_type).upper() in {"INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL"}
                ),
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
                "row_count": len(data.get("rows", [])),
                "sample_rows": data.get("rows", [])[:12] if query_id in aggregate_query_ids else [],
                "remaining_rows_omitted": max(0, len(data.get("rows", [])) - 12) if query_id in aggregate_query_ids else 0,
            }
            for query_id, data in query_data.items()
        ]

    @staticmethod
    def _add_usage(total: dict[str, int], usage: Mapping[str, int]) -> None:
        for name, value in usage.items():
            total[name] = total.get(name, 0) + int(value or 0)
