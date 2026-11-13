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
from .analysis_context import branch_scope_filter, requested_metrics, resolve_analysis, wants_branch_code, removes_branch_filter
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
        from .turn_runtime import TurnRuntime
        runtime = TurnRuntime(self, question, context=context, on_progress=on_progress,
            on_clarification=on_clarification, session_store=session_store, session_id=session_id,
            pending_turn_id=pending_turn_id, clarification_response=clarification_response)
        if self.settings.engine == 'legacy':
            return runtime.run()
        from .durable_graph import execute
        run_id = ''
        if pending_turn_id and session_store is not None:
            from .execution_store import ExecutionStore
            with ExecutionStore(session_store.directory, owner=session_store.owner) as runs:
                candidates = [r for r in runs.list() if r['turn_id'] == pending_turn_id]
                if candidates:
                    run_id = candidates[0]['run_id']
        return execute(runtime, run_id=run_id)

    def resume(self, run_id: str, *, session_store: Any, on_progress=None,
               on_clarification=None, clarification_response: str = '') -> AgentAnswer:
        from .turn_runtime import TurnRuntime
        from .durable_graph import execute
        return execute(TurnRuntime(self, '', session_store=session_store, on_progress=on_progress,
            on_clarification=on_clarification, clarification_response=clarification_response), run_id=run_id)

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

    def _collect_query_pages(self, page: Any, jobs: dict[str, dict[str, str]]) -> tuple[Any, list[dict[str, Any]]]:
        """Collect every page and retain its job metadata for the audit trail."""
        rows = list(page.rows)
        self._record_page_job(page, jobs)
        while page.has_next_page:
            page = self.bigquery.read_page(page.query_id)
            rows.extend(page.rows)
            self._record_page_job(page, jobs)
        return page, rows

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
