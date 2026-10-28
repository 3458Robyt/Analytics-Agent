from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlglot import exp, parse

from .bigquery_adapter import BigQueryAdapter, BigQueryError
from .guardrails import extract_table_ids, validate_sql
from .llm import LLMError, OpenAICompatibleProvider
from .models import AgentAnswer, AgentSettings, Clarification, SummaryTable, TABLE_IDS
from .semantics import MetricDefinitionError, compile_metric_query, load_metric_definitions
from .schema import ColumnSchema, SchemaCatalog, TableSchema, merge_bigquery_schema
from .excel_export import export_results_to_excel
from .claims import (
    enforce_compiled_claims, explicit_class_choices,
    observed_class_filter, requested_payment_class, select_payment_classes,
)
from .field_resolution import UnknownFieldError, resolve_field, resolve_sql_fields
from .query_scope import DateScopeRequired, all_history, has_period, needs_period
from .text_encoding import INPUT_REPAIR_NOTICE, dumps_json, repair_text
from .analysis_context import requested_metrics, resolve_analysis
from .analysis_sql import ROUTINE_ID, compile_analysis, routine_roles
from .organization import OrganizationCatalog
from .presentation import analysis_presentation


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
        self._class_cache: dict[str, tuple[float, tuple[str, ...]]] = {}
        self._routine_cache: dict[str, tuple[float, dict[str, Any]]] = {}
        self.organization = OrganizationCatalog()
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
        payload = dumps_json(value, ensure_ascii=False, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    def _ensure_table(
        self,
        table_id: str,
        visiting: set[str] | None = None,
        session_store: Any | None = None,
        *,
        force_refresh: bool = False,
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
            not force_refresh
            and existing
            and existing.metadata_loaded
            and time.monotonic() - getattr(existing, "metadata_loaded_at", 0.0) < 3600
            and existing.table_type.upper() not in {"VIEW", "MATERIALIZED_VIEW"}
        ):
            return existing

        metadata = None
        cache_reader = getattr(session_store, "get_schema_metadata", None)
        if force_refresh:
            self._class_cache.pop(normalized_id, None)
            invalidator = getattr(session_store, "invalidate_schema_metadata", None)
            if callable(invalidator):
                invalidator(table_id)
        if callable(cache_reader) and not force_refresh:
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
        if tree is None:
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
            if result.get("result_role", "answer") != "answer":
                continue
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
            columns = tuple(column for column in (requested_columns or evidence_columns) if not column.startswith("__"))
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
            if len(summary.rows) == 1:
                from .presentation import number
                row = summary.rows[0]
                facts = [f"**{summary.column_labels.get(column, column.replace('_', ' ').capitalize())}:** "
                         + number(row.get(column), percent=summary.column_formats.get(column) == "percent")
                         for column in numeric_columns]
                if facts:
                    return "; ".join(facts) + "."
                return "No se encontraron importes informados para el alcance consultado."
            return "El cuadro presenta los resultados del análisis para el alcance solicitado."
        if detail and detail.rows:
            return f"La consulta devolvió {len(detail.rows)} fila(s) de detalle; se muestran en la tabla."
        if answer and not any(character.isdigit() for character in answer):
            return answer
        if answer:
            return "No se obtuvo una tabla de resultados que respalde las cifras de la respuesta."
        return "BigQuery no devolvió filas para el criterio consultado."

    @staticmethod
    def _audit_records(
        query_sql: Mapping[str, str],
        query_tables: Mapping[str, tuple[str, ...]],
        query_jobs: Mapping[str, Mapping[str, str]],
        query_bytes: Mapping[str, int],
        query_parameters: Mapping[str, tuple[dict[str, Any], ...]] | None = None,
    ) -> tuple[dict[str, Any], ...]:
        return tuple(
            {
                "query_id": query_id,
                "sql": query_sql.get(query_id, ""),
                "tables": list(query_tables.get(query_id, ())),
                "job": dict(query_jobs.get(query_id, {})),
                "bytes_processed": query_bytes.get(query_id, 0),
                "parameters": list((query_parameters or {}).get(query_id, ())),
            }
            for query_id in query_sql
        )

    def answer(self, question: str, *, context: str = "", on_progress: Callable[[str], None] | None = None,
               on_clarification: Callable[[Clarification], str] | None = None,
               session_store: Any | None = None, session_id: str = "", pending_turn_id: str = "",
               clarification_response: str = "") -> AgentAnswer:
        """Record every attempt, including provider failures and deferred date clarification."""
        clean_question = repair_text(question)
        clean_clarification = repair_text(clarification_response)
        if on_progress is not None and (clean_question != question or clean_clarification != clarification_response):
            on_progress(INPUT_REPAIR_NOTICE)
        question, clarification_response = clean_question, clean_clarification
        context = repair_text(context)
        if on_clarification is not None:
            original_callback = on_clarification

            def readable_clarification(request: Clarification) -> str:
                raw = original_callback(request)
                cleaned = repair_text(raw)
                if cleaned != raw and on_progress is not None:
                    on_progress(INPUT_REPAIR_NOTICE)
                return cleaned

            on_clarification = readable_clarification
        from .llm import AGENT_SYSTEM_PROMPT_VERSION, load_default_agent_system_prompt
        for _attempt in range(3):
            started = time.perf_counter()
            request_id = ""
            if session_store is not None and hasattr(session_store, "begin_request"):
                session_store.prune_history()
                versions = {"model": self.settings.llm_model, "prompt": AGENT_SYSTEM_PROMPT_VERSION,
                            "prompt_digest": self._fingerprint(getattr(self.llm, "agent_system_prompt", "") or load_default_agent_system_prompt()),
                            "glossary": self._fingerprint(self.metric_definitions)}
                request_id = session_store.begin_request(question, session_id, versions)
            try:
                answer = self._answer(question, context=context, on_progress=on_progress,
                    on_clarification=on_clarification, session_store=session_store, session_id=session_id,
                    pending_turn_id=pending_turn_id, clarification_response=clarification_response)
            except Exception as exc:
                if request_id:
                    session_store.finish_request(request_id, elapsed=time.perf_counter() - started, error=exc)
                raise
            if request_id:
                session_store.finish_request(request_id, answer, elapsed=time.perf_counter() - started)
            if (answer.clarification is None or answer.clarification.reason != "date_scope"
                    or on_clarification is None or _attempt == 2):
                return answer
            try:
                reply = on_clarification(answer.clarification).strip()
            except (EOFError, KeyboardInterrupt):
                return answer
            if not reply:
                return answer
            if session_store is not None:
                session_id, pending_turn_id, clarification_response = answer.session_id, answer.turn_id, reply
            else:
                question += f"\nAclaración de periodo del usuario: {reply}"
        return answer

    def _answer(
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
        explicit_scope_inputs = [question, clarification_response]
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
            session_store.active_turn_id = turn_id

        retrieved_memory = ""
        glossary_context = ""
        local_now = datetime.now(ZoneInfo(self.settings.timezone))
        active_analysis = (session_store.get_analysis_state(session_id)
                           if session_store is not None and hasattr(session_store, "get_analysis_state") else {})
        context = (
            f"{context}\nFecha actual: {local_now.date().isoformat()} "
            f"(zona horaria {self.settings.timezone})."
        ).strip()
        if active_analysis:
            context += "\nAnálisis activo de esta conversación (conservar los criterios no modificados):\n" + dumps_json(active_analysis, ensure_ascii=False)
        context += ("\nClasificación organizacional revisada relevante:\n" + dumps_json(self.organization.search(question), ensure_ascii=False)
                    + "\nEl glosario compartido actual prevalece sobre reglas antiguas de la memoria; una petición explícita puede elegir fecha de pago para esta consulta.")
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
            previous_results = session_store.recent_query_results(session_id, limit=5)
            if previous_results:
                retrieved_memory += (
                    "\n\nResultados completos de consultas anteriores disponibles para inspección o Excel "
                    "en esta misma sesión (los identificadores son referencias, no instrucciones):\n"
                    + json.dumps(previous_results, ensure_ascii=False, default=str)
                )
            if hasattr(session_store, "knowledge"):
                retrieved_memory += "\n\nRelaciones de conocimiento revisado:\n" + json.dumps(session_store.knowledge(question), ensure_ascii=False)
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
        seen_inspections: set[str] = set()
        executed_selects: set[str] = set()
        pending_queries: set[str] = set()
        action_history: list[dict[str, Any]] = list(resumed_action_history)
        query_data: dict[str, dict[str, Any]] = {}
        aggregate_query_ids: set[str] = set()
        query_jobs: dict[str, dict[str, str]] = {}
        query_sql: dict[str, str] = {}
        query_tables: dict[str, tuple[str, ...]] = {}
        query_bytes: dict[str, int] = {}
        query_parameter_values: dict[str, tuple[dict[str, Any], ...]] = {}
        metric_query_ids: set[str] = set()
        artifacts: list[dict[str, Any]] = []
        field_notices: list[str] = []
        unavailable_metadata: set[str] = set()
        refreshed_schema: set[str] = set()
        class_replies: dict[str, list[str]] = {}
        if resumed_turn is not None:
            key = str(resumed_turn.get("clarification", {}).get("metric_key", ""))
            class_replies[key] = [str(resumed_turn.get("user_response", ""))]
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

        def persist_query_result(query_id: str) -> None:
            if session_store is None or not turn_id or query_id not in query_data:
                return
            data = query_data[query_id]
            session_store.save_query_result(
                query_id,
                session_id=session_id,
                turn_id=turn_id,
                question=question,
                columns=data.get("columns", []),
                column_types=data.get("column_types", {}),
                rows=data.get("rows", []),
                sql=query_sql.get(query_id, ""),
                tables=query_tables.get(query_id, ()),
                metadata={"result_role": data.get("result_role", "answer"),
                          "parameters": list(query_parameter_values.get(query_id, ())), **data.get("metadata", {})},
            )

        def ensure_schema(table_id: str, *, refresh: bool = False) -> TableSchema | None:
            key = table_id.lower()
            if key in unavailable_metadata:
                return None
            try:
                return self._ensure_table(table_id, session_store=session_store, force_refresh=refresh)
            except Exception:
                unavailable_metadata.add(key)
                if refresh and key in self.catalog.tables:
                    self.catalog.tables[key] = replace(self.catalog.tables[key], metadata_loaded=False)
                return None

        def check_date_scope(sql: str, estimated_bytes: int | None, *, analysis_scope: Mapping[str, Any] | None = None) -> None:
            supplied = "\n".join([question, *explicit_scope_inputs])
            inherited = session_store.temporal_context(session_id) if session_store is not None and hasattr(session_store, "temporal_context") else ""
            if analysis_scope is not None:
                if analysis_scope.get("start_date") or any("todo el histórico" in note for note in analysis_scope.get("assumptions", ())):
                    return
                # A different question's dates must not authorize an unbounded
                # analysis whose resolved SQL contains no temporal restriction.
                supplied = inherited = ""
            elif (all_history(supplied) or has_period(supplied) or has_period(inherited) or all_history(inherited)
                    or active_analysis.get("start_date") or any("todo el histórico" in note for note in active_analysis.get("assumptions", ()))):
                return
            tables = {str(rule["table_id"]).lower() for rule in self.metric_definitions.values()}
            tables.update(key for key, table in self.catalog.tables.items()
                          if any(col.data_type.upper() in {"DATE", "TIMESTAMP", "DATETIME"} for col in table.columns.values()))
            if needs_period(sql, question, temporal_tables=tables) and (
                estimated_bytes is None or estimated_bytes > self.settings.unscoped_query_threshold_bytes
            ):
                record_query(sql, estimated_bytes, "needs_date_scope")
                raise DateScopeRequired(estimated_bytes)

        def record_query(sql: str, estimate: int | None, status: str, error: str = "") -> None:
            if session_store is not None and hasattr(session_store, "record_query"):
                session_store.record_query(sql, estimate, status, error, attempt_key=str(sequence))

        def discover_payment_classes(table_id: str, location: str) -> tuple[str, ...]:
            nonlocal query_count, bytes_processed, sequence
            cached = self._class_cache.get(table_id.lower())
            if cached and time.monotonic() - cached[0] < 3600:
                return cached[1]
            checked = validate_sql(
                f"SELECT DISTINCT clase_pago FROM `{table_id}` "
                "WHERE LOWER(TRIM(tipo)) = 'pagos' AND clase_pago IS NOT NULL ORDER BY clase_pago",
                self.catalog,
            )
            started = time.perf_counter()
            dry = self.bigquery.dry_run(checked.sql, location=location)
            stage_timings["dry_run"] = stage_timings.get("dry_run", 0.0) + time.perf_counter() - started
            if not dry.ok:
                record_query(checked.sql, dry.bytes_processed, "error", dry.error or "dry run fallido")
                raise BigQueryError(dry.error or "No se pudo descubrir clase_pago")
            check_date_scope(checked.sql, dry.bytes_processed)
            started = time.perf_counter()
            page = first = self.bigquery.execute(checked.sql, location=location)
            record_query(checked.sql, dry.bytes_processed, "executed")
            query_count += 1
            query_bytes[first.query_id] = int(first.bytes_processed or dry.bytes_processed or 0)
            bytes_processed = sum(query_bytes.values())
            rows = list(page.rows)
            self._record_page_job(page, query_jobs)
            while page.has_next_page:
                page = self.bigquery.read_page(page.query_id)
                rows.extend(page.rows)
                self._record_page_job(page, query_jobs)
            stage_timings["query"] = stage_timings.get("query", 0.0) + time.perf_counter() - started
            query_sql[first.query_id] = checked.sql
            query_tables[first.query_id] = checked.tables
            tables_used.update(checked.tables)
            values = tuple(dict.fromkeys(str(row["clase_pago"]) for row in rows if row.get("clase_pago") is not None))
            self._class_cache[table_id.lower()] = (time.monotonic(), values)
            sequence += 1
            result = {"action": "payment_class_values", "table_id": table_id, "values": values,
                      "sql": checked.sql, "query_id": first.query_id, "all_pages_read": True}
            if session_store is not None:
                session_store.log_action(turn_id, sequence, "discover_payment_classes", {"sql": checked.sql}, result)
            action_history.append(result)
            return values

        def verified_routine() -> dict[str, Any]:
            nonlocal query_count, bytes_processed, sequence
            cached = self._routine_cache.get(ROUTINE_ID)
            if cached and time.monotonic() - cached[0] < 3600:
                return cached[1]
            cache_key = "routine:" + ROUTINE_ID
            metadata = session_store.get_schema_metadata(cache_key) if session_store is not None else None
            if metadata is not None:
                routine_roles(metadata)
            else:
                try:
                    metadata = self.bigquery.inspect_routine_metadata(ROUTINE_ID)
                except Exception:
                    checked = validate_sql("SELECT p.parameter_name, p.data_type, p.ordinal_position, "
                        "r.routine_type, r.routine_body, r.external_language "
                        "FROM `analytics-487218.functions.INFORMATION_SCHEMA.PARAMETERS` AS p "
                        "JOIN `analytics-487218.functions.INFORMATION_SCHEMA.ROUTINES` AS r "
                        "ON p.specific_name = r.specific_name "
                        "WHERE p.specific_name = 'prima_devengada' AND p.ordinal_position > 0 ORDER BY p.ordinal_position")
                    started = time.perf_counter()
                    dry = self.bigquery.dry_run(checked.sql, location=self.settings.bigquery_location or "us-east1")
                    stage_timings["dry_run"] = stage_timings.get("dry_run", 0.0) + time.perf_counter() - started
                    if not dry.ok:
                        raise MetricDefinitionError("No se pudo verificar la firma de la función de devengada: " + str(dry.error))
                    started = time.perf_counter()
                    first = page = self.bigquery.execute(checked.sql, location=self.settings.bigquery_location or "us-east1")
                    record_query(checked.sql, dry.bytes_processed, "executed")
                    query_count += 1
                    rows = list(page.rows)
                    self._record_page_job(page, query_jobs)
                    while page.has_next_page:
                        page = self.bigquery.read_page(page.query_id)
                        rows.extend(page.rows)
                        self._record_page_job(page, query_jobs)
                    stage_timings["query"] = stage_timings.get("query", 0.0) + time.perf_counter() - started
                    query_bytes[first.query_id] = int(first.bytes_processed or dry.bytes_processed or 0)
                    bytes_processed = sum(query_bytes.values())
                    query_sql[first.query_id], query_tables[first.query_id] = checked.sql, checked.tables
                    tables_used.update(checked.tables)
                    if (not rows or any(row.get("routine_type") != "FUNCTION" or row.get("routine_body") != "SQL"
                                        or row.get("external_language") for row in rows)):
                        raise MetricDefinitionError("No se pudo verificar que prima_devengada sea una función SQL interna")
                    metadata = {"routine_id": ROUTINE_ID, "routine_type": "SCALAR_FUNCTION", "language": "SQL",
                                "arguments": [{"name": row["parameter_name"], "data_type": row["data_type"]}
                                              for row in sorted(rows, key=lambda row: int(row["ordinal_position"]))]}
                    routine_roles(metadata)
                    sequence += 1
                    event = {"action": "routine_signature", "result_role": "metadata", "query_id": first.query_id,
                             "signature": metadata}
                    if session_store is not None:
                        session_store.log_action(turn_id, sequence, "routine_signature", {"sql": checked.sql}, event)
                    action_history.append(event)
                routine_roles(metadata)
                if session_store is not None:
                    session_store.set_schema_metadata(cache_key, metadata)
            self._routine_cache[ROUTINE_ID] = (time.monotonic(), metadata)
            return metadata

        def prepare_analysis(action: Mapping[str, Any], failed_components: Mapping[str, str] | None = None):
            nonlocal active_analysis
            employee_input = "\n".join(dict.fromkeys([question, *explicit_scope_inputs]))
            spec = resolve_analysis(action, employee_input,
                                    today=local_now.date(), active=active_analysis, organization=self.organization)
            if has_period(employee_input) and not spec.start_date and not all_history(employee_input):
                raise MetricDefinitionError("Convierte el periodo pedido a start_date y end_date exclusivo; no ejecutes todo el histórico")
            if any(key not in self.metric_definitions for key in spec.metric_keys):
                raise MetricDefinitionError("Una de las métricas no tiene una definición revisada")
            active_analysis = spec.to_dict()
            if session_store is not None:
                session_store.set_analysis_state(session_id, active_analysis)
            components = []
            for key in spec.metric_keys:
                rule = self.metric_definitions[key]
                components.extend((rule["numerator"], rule["denominator"]) if rule.get("kind") == "ratio" else (key,))
            definitions = {key: dict(value) for key, value in self.metric_definitions.items()}
            unavailable = dict(failed_components or {})
            signature = None
            for key in dict.fromkeys(components):
                rule = definitions[key]
                table = ensure_schema(rule["table_id"])
                if rule.get("kind") == "routine":
                    try:
                        signature = verified_routine()
                    except Exception as error:
                        unavailable[key] = str(error)
                if rule.get("payment_class") or (key in {"pagos_siniestros", "honorarios", "indemnizaciones"}
                                                and any(item.get("field") == "clase_pago" for item in spec.filters)):
                    try:
                        values = discover_payment_classes(rule["table_id"], (table.location if table else "") or self.settings.bigquery_location)
                        goal = rule.get("payment_class", "")
                        explicit = []
                        for reply in class_replies.get(goal, []):
                            explicit = explicit_class_choices(reply, values) or explicit
                        reader = getattr(session_store, "confirmed_payment_classes", None)
                        if not explicit and callable(reader):
                            explicit = reader(rule["table_id"], goal)
                        if goal:
                            labels = select_payment_classes(values, goal, explicit=explicit)
                            rule["required_filters"] = [*rule.get("required_filters", []),
                                {"field": "clase_pago", "operator": "in", "value": labels}]
                        if any(item.get("field") == "clase_pago" for item in spec.filters):
                            spec = replace(spec, filters=tuple(observed_class_filter(values, spec.filters)))
                    except (MetricDefinitionError, BigQueryError) as error:
                        unavailable[key] = str(error)
            active_analysis = spec.to_dict()
            if session_store is not None:
                session_store.set_analysis_state(session_id, active_analysis)
            return compile_analysis(spec, definitions, tables=self.catalog.tables, timezone=self.settings.timezone,
                                    routine_metadata=signature, unavailable=unavailable)

        def save_explicit_definition(clarification: Clarification, reply: str) -> None:
            if clarification.reason == "date_scope":
                return
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
                    "reason": clarification.reason,
                    "estimated_bytes": clarification.estimated_bytes,
                })
            return AgentAnswer(
                answer="La consulta espera la aclaración de negocio para continuar.",
                assumptions=tuple(dict.fromkeys([*(str(item) for item in (analysis_plan or {}).get("assumptions", []) if item), *field_notices])),
                query_count=query_count,
                tables_used=tuple(sorted(tables_used)),
                bytes_processed=bytes_processed,
                usage=total_usage,
                session_id=session_id,
                status="needs_clarification",
                clarification=clarification,
                audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes, query_parameter_values),
                timings={**stage_timings, "total": time.perf_counter() - turn_started},
                turn_id=turn_id,
            )

        if resumed_turn is not None:
            prior_clarification = resumed_turn.get("clarification", {})
            clarification = Clarification(
                question=str(prior_clarification.get("question", "")),
                metric_key=str(prior_clarification.get("metric_key", "")),
                proposed_rule=str(prior_clarification.get("proposed_rule", "")),
                reason=str(prior_clarification.get("reason", "business_definition")),
                estimated_bytes=prior_clarification.get("estimated_bytes"),
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
                if (query_id in aggregate_query_ids and query_id not in pending_queries and rows and columns
                        and data.get("result_role", "answer") == "answer"):
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
                    assumptions=tuple(dict.fromkeys([*(str(item) for item in (analysis_plan or {}).get("assumptions", []) if item), *field_notices])),
                    summary_table=verified_summary,
                    query_count=query_count,
                    tables_used=tuple(sorted(tables_used)),
                    bytes_processed=bytes_processed,
                    usage=total_usage,
                    session_id=session_id,
                    query_jobs=tuple(query_jobs.values()),
                    audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes, query_parameter_values),
                    timings={**stage_timings, "total": time.perf_counter() - turn_started},
                    turn_id=turn_id,
                    artifacts=tuple(artifacts),
                )
                if session_store is not None and turn_id:
                    stored_summary = asdict(verified_summary)
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
                status="failed",
                query_count=query_count,
                tables_used=tuple(sorted(tables_used)),
                bytes_processed=bytes_processed,
                usage=total_usage,
                session_id=session_id,
                query_jobs=tuple(query_jobs.values()),
                audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes, query_parameter_values),
                timings={**stage_timings, "total": time.perf_counter() - turn_started},
                turn_id=turn_id,
                artifacts=tuple(artifacts),
            )
            if session_store is not None and turn_id:
                session_store.complete_turn(turn_id, answer.answer, answer.assumptions)
            return answer

        try:
            while True:
                llm_started = time.perf_counter()
                try:
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
                finally:
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
                        elif re.search(r"\bnet[oa]s?\b", metric_text):
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
                    explicit_scope_inputs.append(reply)
                    class_replies.setdefault(clarification.metric_key, []).append(reply)
                    previous_action_id = None
                    previous_call = None
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
                        if query_data and not has_rows
                        else self._result_sentence(action["answer"].strip(), summary, detail_table)
                    )
                    if (query_data and summary is None and detail_table is None and requested_metrics(question)
                            and all(data.get("result_role", "answer") != "answer" for data in query_data.values())):
                        answer_text = "No se obtuvo el cálculo solicitado; solo se recopilaron datos de diagnóstico o metadatos."
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
                        assumptions=tuple(dict.fromkeys([*action["assumptions"], *field_notices])),
                        summary_table=summary,
                        detail_table=detail_table,
                        query_count=query_count,
                        tables_used=tuple(sorted(tables_used)),
                        bytes_processed=bytes_processed,
                        usage=total_usage,
                        session_id=session_id,
                        query_jobs=tuple(query_jobs.values()),
                        audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes, query_parameter_values),
                        timings={**stage_timings, "total": time.perf_counter() - turn_started},
                        turn_id=turn_id,
                        artifacts=tuple(artifacts),
                    )
                    if session_store is not None:
                        stored_summary = None
                        if summary is not None:
                            stored_summary = asdict(summary)
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
                            artifacts=answer.artifacts,
                        )
                    return answer

                action_id = self._fingerprint({key: value for key, value in action.items() if key not in {"notes"}})
                if action["action"] == "inspect_result":
                    inspection_key = self._fingerprint({key: action.get(key) for key in ("query_id", "operation", "column", "offset", "limit")})
                    if inspection_key in seen_inspections:
                        return best_effort_answer("la inspección ya se realizó y no aportaría evidencia nueva")
                    seen_inspections.add(inspection_key)
                if previous_action_id == action_id:
                    return best_effort_answer(
                        "el agente repitió exactamente la misma acción y no aportó evidencia nueva"
                    )
                progress("Buscando en la memoria de conversaciones y procedimientos." if action["action"] == "search_memory" else
                         "Buscando tablas en el catálogo." if action["action"] == "search_tables" else
                         "Consultando el esquema vivo y las descripciones disponibles." if action["action"] == "describe_tables" else
                         "Ejecutando el análisis confirmado." if action["action"] == "run_analysis" else
                         "Ejecutando la métrica confirmada." if action["action"] == "run_metric" else
                         "Ejecutando una consulta de lectura." if action["action"] == "run_select" else
                         "Analizando el resultado completo." if action["action"] == "inspect_result" else
                         "Creando el archivo Excel local." if action["action"] == "create_excel" else
                         "Leyendo la siguiente página de resultados.")
                try:
                    if action["action"] == "search_knowledge":
                        tool_result = (session_store.knowledge(action["search_text"]) if session_store is not None
                                       else {"matches": [], "note": "El glosario validado sigue disponible en el contexto."})
                    elif action["action"] == "search_memory":
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
                    elif action["action"] == "inspect_result":
                        requested_query_id = action["query_id"]
                        result_data = query_data.get(requested_query_id)
                        if result_data is None and session_store is not None:
                            result_data = session_store.get_query_result(requested_query_id, session_id=session_id)
                        if result_data is None:
                            raise BigQueryError("Ese resultado no está disponible en esta sesión local")
                        tool_result = self._inspect_result(
                            requested_query_id,
                            result_data,
                            operation=action["operation"],
                            column=action.get("column", ""),
                            offset=action.get("offset", 0),
                            limit=action.get("limit", 20),
                        )
                    elif action["action"] == "create_excel":
                        explicit_excel_request = re.search(
                            r"\b(excel|xlsx|hoja de c[aá]lculo|archivo descargable|exporta|exportar|exportaci[oó]n)\b",
                            question,
                            flags=re.IGNORECASE,
                        )
                        if not explicit_excel_request:
                            raise BigQueryError("Solo se crea el archivo cuando el usuario lo solicita explícitamente")
                        requested_ids = list(dict.fromkeys(action.get("query_ids", [])))
                        if not requested_ids:
                            requested_ids = list(query_data)[-1:]
                            if not requested_ids and session_store is not None:
                                requested_ids = [
                                    item["query_id"]
                                    for item in session_store.recent_query_results(session_id, limit=1)
                                ]
                        result_items = []
                        for requested_query_id in requested_ids:
                            result_data = query_data.get(requested_query_id)
                            if result_data is None and session_store is not None:
                                result_data = session_store.get_query_result(
                                    requested_query_id, session_id=session_id
                                )
                            if result_data is None:
                                raise BigQueryError("No se encontró uno de los resultados seleccionados en esta sesión")
                            result_items.append({
                                **result_data,
                                "query_id": requested_query_id,
                                "question": result_data.get("question", question),
                                "sql": result_data.get("sql") or query_sql.get(requested_query_id, ""),
                                "tables": result_data.get("tables") or query_tables.get(requested_query_id, ()),
                                "title": str(result_data.get("question") or f"Resultado {requested_query_id}"),
                            })
                        if not result_items:
                            raise BigQueryError("Todavía no hay resultados que se puedan exportar")
                        export_title = action.get("title", "").strip() or question[:120]
                        artifact_path = export_results_to_excel(
                            result_items,
                            title=export_title,
                            export_dir=self.settings.export_dir,
                            timezone_name=self.settings.timezone,
                        )
                        artifact = {
                            "type": "excel",
                            "path": str(artifact_path),
                            "name": artifact_path.name,
                            "query_ids": requested_ids,
                            "row_count": sum(len(item.get("rows", [])) for item in result_items),
                        }
                        artifacts.append(artifact)
                        tool_result = {"action": "create_excel", "artifact": artifact}
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
                    elif action["action"] in {"run_select", "run_metric", "run_analysis"}:
                        metric_definition: Mapping[str, Any] | None = None
                        requested_sql = action.get("sql", "")
                        query_parameters: tuple[dict[str, Any], ...] = ()
                        compiled_analysis = None
                        if action["action"] == "run_metric" and (
                            self.metric_definitions.get(action.get("metric_key", ""), {}).get("kind", "base") != "base"
                            or len(requested_metrics(question)) > 1
                            or self.organization.from_question(question)
                        ):
                            action = {**action, "action": "run_analysis"}
                        if action["action"] == "run_analysis":
                            compiled_analysis = prepare_analysis(action)
                            requested_sql = compiled_analysis.query.sql
                            query_parameters = compiled_analysis.query.parameters
                            action["sql"] = requested_sql
                        elif action["action"] == "run_metric":
                            scope_text = "\n".join([question, *explicit_scope_inputs])
                            inherited_scope = session_store.temporal_context(session_id) if session_store is not None and hasattr(session_store, "temporal_context") else ""
                            current_period = has_period(scope_text)
                            if (all_history(scope_text) and not current_period) or (not current_period and not has_period(inherited_scope)):
                                # A model-suggested date is not an employee-specified period.
                                action["start_date"] = action["end_date"] = ""
                            elif not action.get("start_date") or not action.get("end_date"):
                                raise MetricDefinitionError("El usuario indicó un periodo: conviértelo a start_date y end_date, sin omitir el filtro temporal")
                            if action.get("metric_key") == "pagos_siniestros":
                                # A generic payments action must not erase an explicit business subset.
                                requested_class = requested_payment_class(question)
                                if requested_class:
                                    action["metric_key"] = requested_class
                            metric_definition = self.metric_definitions.get(str(action.get("metric_key", "")))
                            if metric_definition is None:
                                raise MetricDefinitionError(
                                    f"No hay una definición confirmada para `{action.get('metric_key', '')}`."
                                )
                            metric_definition = dict(metric_definition)
                            legacy_spec = resolve_analysis(action, question, today=local_now.date(), active=active_analysis,
                                                           organization=self.organization)
                            if str(action.get("metric_key")) in {"pagos_siniestros", "honorarios", "indemnizaciones"}:
                                metric_definition["date_field"] = "fecha_pago" if legacy_spec.payment_date_basis == "payment" else "fecha_accidente"
                            table_id = str(metric_definition["table_id"])
                            table = ensure_schema(table_id)
                            required_fields = {metric_definition["value_field"], metric_definition["date_field"]}
                            required_fields.update(item["field"] for item in metric_definition.get("required_filters", []))
                            if metric_definition.get("payment_class"):
                                required_fields.add("clase_pago")
                            if table and table.metadata_loaded:
                                aliases = {cfg["alias"]: field for field, cfg in metric_definition.get("dimensions", {}).items()
                                           if isinstance(cfg, Mapping)}
                                requested_fields = [*action.get("group_by", []),
                                                    *(item.get("field", "") for item in action.get("filters", []))]
                                missing = required_fields - set(table.columns)
                                unknown = [field for field in requested_fields
                                           if field.lower() not in table.columns and aliases.get(field) not in table.columns]
                                if (missing or unknown) and table_id not in refreshed_schema:
                                    refreshed_schema.add(table_id)
                                    table = ensure_schema(table_id, refresh=True)
                            if table and table.metadata_loaded:
                                missing = required_fields - set(table.columns)
                                if missing:
                                    raise MetricDefinitionError(
                                        "Faltan campos necesarios para la regla confirmada: " + ", ".join(sorted(missing))
                                        + ". No sustituyas incurrido_total por otro importe."
                                    )
                                dimensions = dict(metric_definition.get("dimensions", {}))
                                filter_fields = dict(metric_definition.get("filter_fields", {}))
                                for field, column in table.columns.items():
                                    dtype = column.data_type.split()[0]
                                    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field) and dtype in {
                                        "STRING", "INT64", "INTEGER", "NUMERIC", "BIGNUMERIC", "FLOAT", "FLOAT64",
                                        "DATE", "BOOL", "BOOLEAN",
                                    }:
                                        dimensions.setdefault(field, {"alias": field})
                                        filter_fields[field] = {**filter_fields.get(field, {}), "type": dtype,
                                                                "operators": filter_fields.get(field, {}).get("operators", ["eq", "in"])}
                                metric_definition.update(dimensions=dimensions, filter_fields=filter_fields)
                                dtype = table.columns[metric_definition["date_field"]].data_type.split()[0]
                                if dtype not in {"DATE", "TIMESTAMP", "DATETIME"}:
                                    raise MetricDefinitionError("El campo temporal de la métrica no tiene un tipo compatible")
                                metric_definition["date_field_type"] = dtype
                                aliases = {cfg["alias"]: field for field, cfg in dimensions.items() if isinstance(cfg, Mapping)}
                                def resolve_metric_field(field):
                                    corrected = resolve_field(field, table, aliases)
                                    if corrected != field:
                                        field_notices.append(f"Interpreté `{field}` como el campo comprobado `{corrected}`.")
                                    return corrected
                                action["group_by"] = [resolve_metric_field(field) for field in action.get("group_by", [])]
                                action["filters"] = [{**item, "field": resolve_metric_field(item.get("field", ""))}
                                                     for item in action.get("filters", [])]
                            filters = action.get("filters", [])
                            class_goal = metric_definition.get("payment_class", "")
                            if class_goal or any(item.get("field") == "clase_pago" for item in filters):
                                values = discover_payment_classes(table_id, (table.location if table else "") or self.settings.bigquery_location)
                                filters = observed_class_filter(values, filters)
                                if class_goal:
                                    try:
                                        selected = select_payment_classes(values, class_goal)
                                    except MetricDefinitionError:
                                        # Only an explicit user's reply can resolve an ambiguous category.
                                        explicit = []
                                        for reply in class_replies.get(str(class_goal), []):
                                            explicit = explicit_class_choices(reply, values) or explicit
                                        reader = getattr(session_store, "confirmed_payment_classes", None)
                                        if not explicit and callable(reader):
                                            explicit = reader(table_id, str(class_goal))
                                        if not explicit:
                                            raise
                                        selected = select_payment_classes(values, class_goal, explicit=explicit)
                                        if session_store is not None and class_replies.get(str(class_goal)):
                                            session_store.confirm_business_definition(
                                                str(class_goal) + "_clase_pago",
                                                json.dumps({"kind": "payment_class_mapping", "table_id": table_id,
                                                            "goal": class_goal, "labels": selected}, ensure_ascii=False),
                                                "Etiquetas exactas de clase_pago comprobadas en BigQuery",
                                                class_replies[str(class_goal)][-1],
                                            )
                                    metric_definition["required_filters"] = [
                                        *metric_definition.get("required_filters", []),
                                        {"field": "clase_pago", "operator": "in", "value": selected},
                                    ]
                                action["filters"] = filters
                            metric_query = compile_metric_query(
                                metric_definition,
                                start_date=action["start_date"],
                                end_date=action["end_date"],
                                group_by=action["group_by"],
                                filters=filters,
                                timezone=self.settings.timezone,
                                allow_unbounded=True,
                                payment_date_basis=legacy_spec.payment_date_basis,
                            )
                            requested_sql = metric_query.sql
                            query_parameters = metric_query.parameters
                            action["sql"] = requested_sql
                            active_analysis = replace(legacy_spec, start_date=action["start_date"], end_date=action["end_date"]).to_dict()
                            if session_store is not None:
                                session_store.set_analysis_state(session_id, active_analysis)
                        else:
                            # Even technically valid SQL can violate a confirmed financial definition.
                            enforce_compiled_claims(requested_sql, question_for_model, str(action.get("metric_key", "")))
                            for table_id in extract_table_ids(requested_sql):
                                ensure_schema(table_id)
                            try:
                                requested_sql, notices = resolve_sql_fields(requested_sql, self.catalog, allow_typo=False)
                            except UnknownFieldError as field_error:
                                if field_error.table_id in refreshed_schema:
                                    raise
                                refreshed_schema.add(field_error.table_id)
                                ensure_schema(field_error.table_id, refresh=True)
                                requested_sql, notices = resolve_sql_fields(requested_sql, self.catalog)
                            field_notices.extend(notices)
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
                        execution_key = self._fingerprint({"sql": validated.sql, "parameters": query_parameters, "location": location})
                        if execution_key in executed_selects:
                            return best_effort_answer("la consulta y sus parámetros ya se ejecutaron en este turno")
                        stage_started = time.perf_counter()
                        if query_parameters:
                            dry_run = self.bigquery.dry_run(
                                validated.sql, location=location, parameters=query_parameters
                            )
                        else:
                            dry_run = self.bigquery.dry_run(validated.sql, location=location)
                        stage_timings["dry_run"] = stage_timings.get("dry_run", 0.0) + time.perf_counter() - stage_started
                        if (not dry_run.ok and compiled_analysis is not None
                                and "prima_devengada" in compiled_analysis.components
                                and "prima_devengada" in str(dry_run.error).lower()):
                            record_query(validated.sql, dry_run.bytes_processed, "error", dry_run.error or "")
                            compiled_analysis = prepare_analysis(action, {"prima_devengada": str(dry_run.error)})
                            query_parameters = compiled_analysis.query.parameters
                            validated = validate_sql(compiled_analysis.query.sql, self.catalog)
                            action["sql"] = validated.sql
                            execution_key = self._fingerprint({"sql": validated.sql, "parameters": query_parameters, "location": location})
                            stage_started = time.perf_counter()
                            dry_run = self.bigquery.dry_run(validated.sql, location=location, parameters=query_parameters)
                            stage_timings["dry_run"] += time.perf_counter() - stage_started
                        if not dry_run.ok:
                            record_query(validated.sql, dry_run.bytes_processed, "error", dry_run.error or "dry run fallido")
                            if any(token in (dry_run.error or "").lower() for token in ("unrecognized name", "not found inside", "no such field")):
                                for table_id in validated.tables:
                                    if table_id not in refreshed_schema:
                                        refreshed_schema.add(table_id)
                                        ensure_schema(table_id, refresh=True)
                            raise BigQueryError(dry_run.error or "El dry run de BigQuery no fue aprobado")
                        check_date_scope(validated.sql, dry_run.bytes_processed,
                                         analysis_scope=compiled_analysis.spec.to_dict() if compiled_analysis else None)
                        stage_started = time.perf_counter()
                        if query_parameters:
                            page = self.bigquery.execute(
                                validated.sql, location=location, parameters=query_parameters
                            )
                        else:
                            page = self.bigquery.execute(validated.sql, location=location)
                        record_query(validated.sql, dry_run.bytes_processed, "executed")
                        executed_selects.add(execution_key)
                        query_count += 1
                        query_bytes[page.query_id] = int(page.bytes_processed or dry_run.bytes_processed or 0)
                        bytes_processed = sum(query_bytes.values())
                        if metric_definition is not None and not action.get("start_date"):
                            field_notices.append("Se consultó todo el histórico disponible; no se aplicó un filtro de fechas.")
                        first_page = page
                        rows = list(page.rows)
                        self._record_page_job(page, query_jobs)
                        while page.has_next_page:
                            page = self.bigquery.read_page(page.query_id)
                            rows.extend(page.rows)
                            self._record_page_job(page, query_jobs)
                        stage_timings["query"] = stage_timings.get("query", 0.0) + time.perf_counter() - stage_started
                        tables_used.update(validated.tables)
                        is_aggregate = self._is_aggregate(validated.sql)
                        has_aggregate_results = has_aggregate_results or is_aggregate
                        if is_aggregate:
                            aggregate_query_ids.add(page.query_id)
                        if metric_definition is not None or compiled_analysis is not None:
                            metric_query_ids.add(page.query_id)
                        expected_rows = getattr(first_page, "total_rows", None)
                        completeness_warning = ""
                        if expected_rows is not None and len(rows) != int(expected_rows):
                            warning = (
                                f"BigQuery reportó {expected_rows} filas y el agente recibió {len(rows)}. "
                                "La tabla puede estar incompleta."
                            )
                            analysis_plan.setdefault("assumptions", []).append(warning)
                            completeness_warning = warning
                        role = "answer"
                        if any("information_schema." in table.lower() for table in validated.tables) or re.search(r"\bLIMIT\s+0\b", validated.sql, re.I):
                            role = "metadata"
                        elif (compiled_analysis is None and requested_metrics(question)
                              and not re.search(r"\b(?:cu[aá]nt[oa]s?|cantidad de|n[uú]mero de|conteo)\b", question, re.I)):
                            tree = parse(validated.sql, read="bigquery")[0]
                            if tree.find(exp.Count) and not tree.find(exp.Sum):
                                role = "diagnostic"
                        query_data[page.query_id] = {
                            "columns": list(first_page.columns),
                            "column_types": dict(getattr(first_page, "column_types", {}) or {}),
                            "rows": rows,
                            "result_role": role,
                            "metadata": {"analysis": compiled_analysis.spec.to_dict()} if compiled_analysis else {},
                        }
                        query_sql[page.query_id] = validated.sql
                        query_tables[page.query_id] = validated.tables
                        query_parameter_values[page.query_id] = query_parameters
                        try:
                            persist_query_result(page.query_id)
                        except Exception as storage_error:
                            analysis_plan.setdefault("assumptions", []).append(
                                "No se pudo guardar una copia local del resultado para exportarlo en otra pregunta: "
                                + type(storage_error).__name__
                            )
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
                            "field_corrections": list(dict.fromkeys(field_notices)),
                            "query_parameters": list(query_parameters),
                            "metric_key": action.get("metric_key", ""),
                            "result_role": role,
                        }
                        if compiled_analysis is not None:
                            answer_text, summary, notes = analysis_presentation(query_data[page.query_id], compiled_analysis)
                            status = "partial" if compiled_analysis.unavailable else "complete"
                            if completeness_warning:
                                status = "partial"
                                notes = (*notes, completeness_warning)
                                answer_text += "\n\n**Resultado parcial.** No se recibieron todas las filas informadas por BigQuery."
                            if re.search(r"\b(?:excel|xlsx)\b", question, re.I):
                                try:
                                    export_rows = [{summary.column_labels.get(key, key): value for key, value in row.items()} for row in summary.rows]
                                    path = export_results_to_excel([{"query_id": page.query_id,
                                        "columns": [summary.column_labels.get(key, key) for key in summary.columns],
                                        "rows": export_rows, "sql": validated.sql, "tables": validated.tables,
                                        "title": summary.title}], title=question[:120],
                                        export_dir=self.settings.export_dir, timezone_name=self.settings.timezone)
                                    artifacts.append({"type": "excel", "path": str(path), "name": path.name,
                                                      "query_ids": [page.query_id], "row_count": len(summary.rows)})
                                except Exception as export_error:
                                    status = "partial"
                                    notes = (*notes, f"Los datos se calcularon, pero no se pudo crear el Excel ({type(export_error).__name__}).")
                            sequence += 1
                            if session_store is not None:
                                session_store.log_action(turn_id, sequence, "run_analysis", action, tool_result, aggregate=True)
                                session_store.complete_turn(turn_id, answer_text, notes, asdict(summary), status=status)
                                try:
                                    session_store.queue_learning_review(turn_id, question=question,
                                        prior_context=retrieved_memory, plan={**analysis_plan, "analysis": compiled_analysis.spec.to_dict()},
                                        answer=answer_text, action_history=[*action_history, tool_result],
                                        verified=status == "complete", active_learnings=session_store.learning_context(question))
                                except Exception as learning_error:
                                    progress(f"No se pudo encolar el aprendizaje ({type(learning_error).__name__}).")
                            return AgentAnswer(answer_text, assumptions=tuple(dict.fromkeys([*notes, *field_notices])),
                                summary_table=summary, query_count=query_count, tables_used=tuple(sorted(tables_used)),
                                bytes_processed=bytes_processed, usage=total_usage, session_id=session_id,
                                query_jobs=tuple(query_jobs.values()), status=status,
                                audit=self._audit_records(query_sql, query_tables, query_jobs, query_bytes, query_parameter_values),
                                timings={**stage_timings, "total": time.perf_counter() - turn_started}, turn_id=turn_id,
                                artifacts=tuple(artifacts))
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
                except DateScopeRequired:
                    raise
                except Exception as exc:
                    if action["action"] in {"run_select", "run_metric", "run_analysis"} and action.get("sql"):
                        record_query(action["sql"], None, "error", str(exc))
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
                        aggregate=action["action"] in {"run_select", "run_metric", "run_analysis"} and self._is_aggregate(str(action.get("sql", "")))
                        or action["action"] == "read_page" and action.get("query_id") in aggregate_query_ids,
                    )
                result_id = self._fingerprint({key: value for key, value in tool_result.items() if key != "rows"})
                if previous_call == (action_id, result_id):
                    return best_effort_answer("BigQuery o el modelo repitieron el mismo paso sin aportar evidencia nueva")
                previous_call = (action_id, result_id)
                previous_action_id = action_id
                action_history.append(self._history_entry(action["action"], action, tool_result))
                last_tool_result = self._model_tool_result(tool_result)
        except DateScopeRequired as exc:
            return pending_clarification_answer(Clarification(str(exc), reason="date_scope", estimated_bytes=exc.estimated_bytes))
        except Exception as exc:
            if session_store is not None and turn_id:
                if hasattr(session_store, "snapshot_request"):
                    session_store.snapshot_request(session_id, turn_id, stage_timings, total_usage, query_count, bytes_processed)
                with_context = f"La pregunta quedó incompleta por un error: {type(exc).__name__}."
                session_store.complete_turn(turn_id, with_context)
            raise

    @staticmethod
    def _column_profile(rows: list[Mapping[str, Any]], column: str, data_type: str = "") -> dict[str, Any]:
        profile: dict[str, Any] = {
            "column": column,
            "type": data_type,
            "row_count": len(rows),
            "null_count": 0,
            "non_null_count": 0,
        }
        unique: set[str] = set()
        frequencies: Counter[str] = Counter()
        originals: dict[str, Any] = {}
        distinct_complete = True
        normalized_type = data_type.upper().split()[0] if data_type else ""
        numeric = normalized_type in {
            "INT64", "INTEGER", "FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC", "DECIMAL", "BIGDECIMAL",
        }
        numeric_count = 0
        numeric_sum = Decimal(0)
        numeric_min: Decimal | None = None
        numeric_max: Decimal | None = None
        for row in rows:
            value = row.get(column)
            if value is None:
                profile["null_count"] += 1
                continue
            profile["non_null_count"] += 1
            if distinct_complete:
                key = (
                    str(value)
                    if isinstance(value, (str, int, float, bool))
                    else json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
                )
                if key in unique:
                    frequencies[key] += 1
                elif len(unique) < 20_000:
                    unique.add(key)
                    frequencies[key] = 1
                    originals[key] = value
                else:
                    distinct_complete = False
                    unique.clear()
                    frequencies.clear()
                    originals.clear()
            if numeric:
                try:
                    number = Decimal(str(value))
                    if number.is_finite():
                        numeric_count += 1
                        numeric_sum += number
                        numeric_min = number if numeric_min is None else min(numeric_min, number)
                        numeric_max = number if numeric_max is None else max(numeric_max, number)
                except (InvalidOperation, TypeError, ValueError):
                    pass
        if distinct_complete:
            profile["distinct_count"] = len(unique)
        else:
            profile["distinct_count_lower_bound"] = 20_001
            profile["frequency_summary"] = "omitido por cardinalidad alta; el conteo no es exacto"
        if numeric and numeric_count:
            profile["numeric"] = {
                "count": numeric_count,
                "min": str(numeric_min),
                "max": str(numeric_max),
                "sum": str(numeric_sum),
                "average": str(numeric_sum / numeric_count),
            }
        if distinct_complete:
            top_values = []
            for key, count in sorted(frequencies.items(), key=lambda item: (-item[1], item[0]))[:10]:
                value = originals[key]
                if isinstance(value, str) and len(value) > 200:
                    value = value[:200] + "…"
                top_values.append({"value": value, "count": count})
            profile["top_values"] = top_values
        return profile

    @classmethod
    def _inspect_result(
        cls,
        query_id: str,
        result: Mapping[str, Any],
        *,
        operation: str,
        column: str = "",
        offset: int = 0,
        limit: int = 20,
    ) -> dict[str, Any]:
        rows = result.get("rows", [])
        columns = [str(item) for item in result.get("columns", [])]
        types = result.get("column_types", {})
        if not isinstance(rows, list) or not isinstance(types, Mapping):
            raise BigQueryError("El resultado guardado no tiene un formato válido")
        base = {
            "action": "inspect_result",
            "query_id": query_id,
            "operation": operation,
            "row_count": len(rows),
            "columns": columns,
            "column_types": dict(types),
        }
        if operation == "sample":
            base.update({"offset": offset, "rows": rows[offset:offset + limit]})
            return base
        if operation == "profile":
            base["column_profiles"] = [
                cls._column_profile(rows, name, str(types.get(name, ""))) for name in columns
            ]
            return base
        if column not in columns:
            raise BigQueryError(f"La columna `{column}` no está en este resultado")
        profile = cls._column_profile(rows, column, str(types.get(column, "")))
        if operation == "column_stats":
            base["column_profile"] = profile
            return base
        if operation == "top_values":
            top_values = profile.get("top_values")
            if top_values is None:
                base["note"] = "El campo tiene cardinalidad alta; use una agregación SQL GROUP BY para un conteo exacto."
                base["distinct_count_lower_bound"] = profile.get("distinct_count_lower_bound")
            else:
                base["top_values"] = top_values[:limit]
                base["distinct_count"] = profile.get("distinct_count", 0)
            return base
        raise BigQueryError("La operación de inspección no está permitida")

    def _page_result(self, page: Any) -> dict[str, Any]:
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

    def _model_tool_result(self, result: Mapping[str, Any]) -> dict[str, Any]:
        compact = {key: value for key, value in result.items() if key != "rows"}
        rows = result.get("rows")
        if isinstance(rows, list):
            compact["rows_read"] = len(rows)
            compact["all_pages_read"] = True
            if len(str(compact.get("sql", ""))) > 12_000:
                compact["sql"] = str(compact["sql"])[:12_000] + " … [SQL recortado en contexto]"
            budget = self.settings.evidence_max_chars
            complete = {**compact, "rows": [], "evidence_mode": "all_rows"}
            serialized_size = len(json.dumps(complete, ensure_ascii=False, default=str))
            fully_in_context = True
            separator = 0
            for row in rows:
                serialized_size += separator + len(json.dumps(row, ensure_ascii=False, default=str))
                if serialized_size > budget:
                    fully_in_context = False
                    break
                separator = 1
            if fully_in_context:
                complete["rows"] = rows
                return complete

            columns = [str(item) for item in result.get("columns", [])]
            column_types = result.get("column_types", {})
            profiles = [
                self._column_profile(rows, column, str(column_types.get(column, "")))
                for column in columns[:200]
            ]
            reduced = {
                **compact,
                "evidence_mode": "complete_profile_and_samples",
                "result_profile": {"row_count": len(rows), "column_profiles": profiles},
                "sample_rows": {"first": rows[:5], "last": rows[-5:] if len(rows) > 5 else []},
                "instruction": (
                    "El perfil se calculó sobre todas las filas leídas; las muestras son ejemplos. "
                    "Usa inspect_result para obtener estadísticas exactas o una muestra por desplazamiento."
                ),
            }
            if len(columns) > 200:
                reduced["profile_columns_omitted"] = len(columns) - 200
            while len(json.dumps(reduced, ensure_ascii=False, default=str)) > budget:
                first = reduced["sample_rows"]["first"]
                last = reduced["sample_rows"]["last"]
                if last:
                    reduced["sample_rows"]["last"] = last[:-1]
                elif first:
                    reduced["sample_rows"]["first"] = first[:-1]
                elif reduced["result_profile"]["column_profiles"]:
                    reduced["result_profile"]["column_profiles"].pop()
                    reduced["profile_columns_omitted"] = reduced.get("profile_columns_omitted", 0) + 1
                else:
                    break
            if len(json.dumps(reduced, ensure_ascii=False, default=str)) > budget:
                reduced = {
                    "action": compact.get("action"),
                    "query_id": compact.get("query_id"),
                    "rows_read": len(rows),
                    "columns": columns[:100],
                    "profile_columns_omitted": max(0, len(columns) - 100),
                    "evidence_mode": "result_available_for_inspection",
                    "instruction": "El resultado completo está disponible; usa inspect_result para analizarlo.",
                }
            return reduced
        budget = self.settings.evidence_max_chars
        if len(json.dumps(compact, ensure_ascii=False, default=str)) > budget:
            profiles = compact.get("column_profiles")
            if isinstance(profiles, list):
                original_profile_count = len(profiles)
                for item in profiles:
                    if isinstance(item, dict):
                        item.pop("top_values", None)
                while len(profiles) > 40 and len(json.dumps(compact, ensure_ascii=False, default=str)) > budget:
                    profiles.pop()
                if len(json.dumps(compact, ensure_ascii=False, default=str)) > budget:
                    del profiles[20:]
                compact["column_profiles_omitted"] = original_profile_count - len(profiles)
            single_profile = compact.get("column_profile")
            if isinstance(single_profile, dict):
                single_profile.pop("top_values", None)
            if len(json.dumps(compact, ensure_ascii=False, default=str)) > budget:
                compact = {
                    key: compact.get(key)
                    for key in ("action", "query_id", "operation", "row_count", "column", "note")
                    if key in compact
                }
                compact["instruction"] = "Usa inspect_result con una columna específica para obtener más detalle."
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
            if (query_id in aggregate_query_ids or not result.get("columns") or not result.get("rows")
                    or result.get("result_role", "answer") != "answer"):
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
