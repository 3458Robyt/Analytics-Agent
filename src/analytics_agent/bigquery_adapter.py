from __future__ import annotations

import contextvars
import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator, Mapping, Sequence
from uuid import uuid4


_request_id_context: contextvars.ContextVar[str] = contextvars.ContextVar(
    "analytics_agent_bigquery_request_id", default=""
)


@contextmanager
def bigquery_request_scope(request_id: str):
    """Give each queued Teams request stable BigQuery job IDs for safe redelivery."""
    token = _request_id_context.set(request_id.strip())
    try:
        yield
    finally:
        _request_id_context.reset(token)


class BigQueryError(RuntimeError):
    """A BigQuery metadata, dry-run, or query operation failed."""


@dataclass(frozen=True)
class DryRunResult:
    ok: bool
    bytes_processed: int | None
    error: str = ""
    referenced_routines: tuple[str, ...] = ()


@dataclass(frozen=True)
class DiscoveryReport:
    projects: tuple[str, ...]
    datasets: int
    tables: tuple[dict[str, str], ...]
    warnings: tuple[str, ...] = ()


@dataclass(frozen=True)
class QueryPage:
    query_id: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    has_next_page: bool
    total_rows: int | None
    bytes_processed: int | None
    job_id: str = ""
    job_project: str = ""
    location: str = ""
    column_types: Mapping[str, str] = field(default_factory=dict)


def _to_json_value(value: Any) -> Any:
    if isinstance(value, (date, datetime)):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Mapping):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_to_json_value(item) for item in value]
    if hasattr(value, "items") and callable(value.items):
        return {str(key): _to_json_value(item) for key, item in value.items()}
    return value


def _safe_error(exc: Exception) -> str:
    message = " ".join(str(exc).split())
    upper_message = message.upper()
    status = getattr(exc, "code", None)
    if callable(status):
        try:
            status = status()
        except Exception:
            status = None
    if any(token in upper_message for token in (
        "VPC SERVICE CONTROLS", "VPC_SERVICE_CONTROLS", "PERIMETER_VIOLATION",
    )):
        return "VPC Service Controls bloqueó la solicitud a BigQuery"
    if status == 403 or "403" in message or "FORBIDDEN" in upper_message or "ACCESS DENIED" in upper_message:
        return "BigQuery denegó el acceso; verifica IAM y el perímetro de VPC Service Controls"
    if not message:
        return f"BigQuery devolvió {type(exc).__name__}"
    # Keep column, type, and location diagnostics so the agent can revise a
    # candidate query after a dry-run error. Permission details stay sanitized.
    return f"BigQuery devolvió {type(exc).__name__}: {message[:1200]}"


class BigQueryAdapter:
    """BigQuery metadata access and paged SELECT execution, with no row/byte ceiling."""

    def __init__(self, client: Any, *, location: str = "", page_size: int = 500) -> None:
        if page_size < 1:
            raise ValueError("page_size debe ser mayor que cero")
        self.client = client
        self.location = location.strip()
        self.page_size = page_size
        self._pages: dict[str, Iterator[Any]] = {}
        self._jobs: dict[str, Any] = {}

    @classmethod
    def from_default_credentials(
        cls,
        *,
        project: str,
        location: str = "",
        page_size: int = 500,
    ) -> "BigQueryAdapter":
        try:
            from google.cloud import bigquery
        except ImportError as exc:  # pragma: no cover - Workbench dependency
            raise BigQueryError("Instala google-cloud-bigquery") from exc
        client = bigquery.Client(project=project, location=location or None)
        return cls(client, location=location, page_size=page_size)

    @staticmethod
    def _short_table_name(value: str) -> str:
        return value.rsplit(".", 1)[-1].rsplit(":", 1)[-1]

    def discover_tables(self, seed_projects: tuple[str, ...] | list[str] = ()) -> DiscoveryReport:
        """List projects, datasets and tables visible to the runtime identity."""
        warnings: list[str] = []
        project_ids: set[str] = set(seed_projects)
        try:
            for project in self.client.list_projects():
                project_id = str(getattr(project, "project_id", "") or getattr(project, "projectId", "")).strip()
                if project_id:
                    project_ids.add(project_id)
        except Exception as exc:
            warnings.append("No se pudo enumerar todos los proyectos visibles: " + _safe_error(exc))

        tables_by_id: dict[str, dict[str, str]] = {}
        dataset_count = 0
        for project_id in sorted(project_ids):
            try:
                datasets = self.client.list_datasets(project=project_id, include_all=True)
                for dataset in datasets:
                    dataset_count += 1
                    actual_project = str(getattr(dataset, "project", "") or project_id)
                    dataset_id = str(getattr(dataset, "dataset_id", "") or "")
                    if not dataset_id:
                        reference = getattr(dataset, "reference", None)
                        dataset_id = str(getattr(reference, "dataset_id", "") or "")
                        actual_project = str(getattr(reference, "project", "") or actual_project)
                    if not dataset_id:
                        warnings.append(f"Se omitió un dataset sin identificador en `{project_id}`.")
                        continue
                    dataset_ref = f"{actual_project}.{dataset_id}"
                    dataset_location = str(getattr(dataset, "location", "") or "").strip()
                    if not dataset_location:
                        try:
                            dataset_location = str(
                                getattr(self.client.get_dataset(dataset_ref), "location", "") or ""
                            ).strip()
                        except Exception as exc:
                            warnings.append(f"No se pudo leer la región de `{dataset_ref}`: {_safe_error(exc)}")
                    try:
                        table_items = self.client.list_tables(dataset_ref)
                        for item in table_items:
                            short_name = self._short_table_name(str(getattr(item, "table_id", "") or ""))
                            if not short_name:
                                continue
                            table_id = f"{actual_project}.{dataset_id}.{short_name}"
                            tables_by_id[table_id.lower()] = {
                                "table_id": table_id,
                                "description": str(getattr(item, "description", "") or "").strip(),
                                "table_type": str(getattr(item, "table_type", "") or "").strip(),
                                "location": dataset_location,
                            }
                    except Exception as exc:
                        warnings.append(f"No se pudieron enumerar tablas de `{dataset_ref}`: {_safe_error(exc)}")
            except Exception as exc:
                warnings.append(f"No se pudo enumerar datasets de `{project_id}`: {_safe_error(exc)}")

        return DiscoveryReport(
            projects=tuple(sorted(project_ids)),
            datasets=dataset_count,
            tables=tuple(tables_by_id[key] for key in sorted(tables_by_id)),
            warnings=tuple(warnings),
        )

    def inspect_table_metadata(self, table_id: str) -> dict[str, Any]:
        try:
            table = self.client.get_table(table_id)
        except Exception as exc:
            raise BigQueryError(_safe_error(exc)) from exc
        schema: dict[str, dict[str, str]] = {}

        def add_fields(fields: Any, parent: str = "") -> None:
            for field in fields or ():
                name = f"{parent}.{field.name}" if parent else field.name
                field_type = str(field.field_type or "").upper()
                mode = str(getattr(field, "mode", "") or "").upper()
                data_type = "STRUCT" if field_type == "RECORD" else field_type
                if mode == "REPEATED":
                    data_type = f"ARRAY<{data_type}>"
                elif mode == "REQUIRED":
                    data_type += " REQUIRED"
                schema[name.lower()] = {
                    "name": name,
                    "data_type": data_type,
                    "description": str(field.description or "").strip(),
                }
                add_fields(getattr(field, "fields", ()), name)

        add_fields(table.schema)
        materialized_view = getattr(table, "materialized_view", None)
        view_query = str(getattr(table, "view_query", "") or "").strip()
        if not view_query and materialized_view is not None:
            view_query = str(getattr(materialized_view, "query", "") or "").strip()
        return {
            "table_id": str(getattr(table, "full_table_id", "") or table_id).replace(":", "."),
            "actual_schema": schema,
            "location": str(getattr(table, "location", "") or "").strip(),
            "description": str(getattr(table, "description", "") or "").strip(),
            "table_type": str(getattr(table, "table_type", "") or "").strip(),
            "view_query": view_query,
        }

    def inspect_table(self, table_id: str) -> tuple[dict[str, str], str]:
        info = self.inspect_table_metadata(table_id)
        return ({name: column["data_type"] for name, column in info["actual_schema"].items()}, info["location"])

    def inspect_table_schema(self, table_id: str) -> tuple[dict[str, dict[str, str]], str]:
        info = self.inspect_table_metadata(table_id)
        return info["actual_schema"], info["location"]

    def dry_run(
        self,
        sql: str,
        *,
        location: str = "",
        parameters: Sequence[Mapping[str, Any]] = (),
    ) -> DryRunResult:
        try:
            from google.cloud import bigquery
        except ImportError as exc:  # pragma: no cover - Workbench dependency
            raise BigQueryError("Instala google-cloud-bigquery") from exc
        try:
            config = bigquery.QueryJobConfig(
                dry_run=True,
                use_query_cache=False,
                use_legacy_sql=False,
            )
            if parameters:
                config.query_parameters = self._query_parameters(bigquery, parameters)
            job = self.client.query(sql, job_config=config, location=location or self.location or None)
            processed = int(job.total_bytes_processed or 0)
            routines = tuple(
                sorted({
                    ".".join(str(part) for part in (
                        getattr(routine, "project_id", ""),
                        getattr(routine, "dataset_id", ""),
                        getattr(routine, "routine_id", ""),
                    ) if part)
                    for routine in (getattr(job, "referenced_routines", None) or ())
                })
            )
            if routines:
                return DryRunResult(
                    False,
                    processed,
                    "La consulta usa rutinas definidas por el usuario; no se ejecutará porque su comportamiento no se puede verificar como solo lectura",
                    routines,
                )
            return DryRunResult(True, processed)
        except Exception as exc:
            return DryRunResult(False, None, _safe_error(exc))

    def execute(
        self,
        sql: str,
        *,
        location: str = "",
        parameters: Sequence[Mapping[str, Any]] = (),
    ) -> QueryPage:
        try:
            from google.cloud import bigquery
        except ImportError as exc:  # pragma: no cover - Workbench dependency
            raise BigQueryError("Instala google-cloud-bigquery") from exc
        try:
            config = bigquery.QueryJobConfig(use_legacy_sql=False)
            if parameters:
                config.query_parameters = self._query_parameters(bigquery, parameters)
            request_id = _request_id_context.get()
            if request_id:
                canonical_parameters = json.dumps(list(parameters), sort_keys=True, default=str, ensure_ascii=False)
                digest = hashlib.sha256(
                    (request_id + "\0" + sql + "\0" + canonical_parameters).encode("utf-8")
                ).hexdigest()[:48]
                job_id = "analytics_agent_" + digest
                try:
                    job = self.client.query(
                        sql,
                        job_config=config,
                        location=location or self.location or None,
                        job_id=job_id,
                    )
                except Exception as exc:
                    # Pub/Sub can redeliver after a worker restart. Reuse the job
                    # created for this request instead of charging for it twice.
                    is_conflict = getattr(exc, "code", None) == 409 or type(exc).__name__ == "Conflict"
                    if not is_conflict:
                        raise
                    job = self.client.get_job(job_id, location=location or self.location or None)
                    if str(getattr(job, "query", "")) != sql:
                        raise BigQueryError("El identificador idempotente de BigQuery corresponde a otra consulta")
            else:
                job = self.client.query(sql, job_config=config, location=location or self.location or None)
            iterator = job.result(page_size=self.page_size)
            pages = iter(iterator.pages)
            query_id = str(uuid4())
            self._pages[query_id] = pages
            self._jobs[query_id] = job
            first_page = self._next_page(query_id, iterator=iterator)
            return first_page
        except BigQueryError:
            raise
        except Exception as exc:
            raise BigQueryError(_safe_error(exc)) from exc

    def read_page(self, query_id: str) -> QueryPage:
        if query_id not in self._pages:
            raise BigQueryError("El identificador de consulta no existe o ya terminó")
        return self._next_page(query_id)

    @staticmethod
    def _query_parameters(bigquery: Any, parameters: Sequence[Mapping[str, Any]]) -> list[Any]:
        converted = []
        for item in parameters:
            name = str(item["name"])
            data_type = str(item["type"]).upper()
            value = item.get("value")
            if item.get("array"):
                converted.append(bigquery.ArrayQueryParameter(name, data_type, value))
            else:
                converted.append(bigquery.ScalarQueryParameter(name, data_type, value))
        return converted

    def _next_page(self, query_id: str, *, iterator: Any = None) -> QueryPage:
        pages = self._pages[query_id]
        job = self._jobs[query_id]
        try:
            page = next(pages)
        except StopIteration:
            self._pages.pop(query_id, None)
            self._jobs.pop(query_id, None)
            return QueryPage(
                query_id,
                (),
                (),
                False,
                int(getattr(iterator, "total_rows", 0) or 0) if iterator else None,
                int(getattr(job, "total_bytes_processed", 0) or 0),
                str(getattr(job, "job_id", "") or ""),
                str(getattr(job, "project", "") or ""),
                str(getattr(job, "location", "") or ""),
            )
        schema = getattr(iterator, "schema", None) if iterator is not None else None
        schema = schema or getattr(job, "schema", None) or ()
        columns = tuple(str(getattr(field, "name", "")) for field in schema if getattr(field, "name", ""))
        column_types = {
            str(getattr(field, "name", "")): str(getattr(field, "field_type", "") or "").upper()
            for field in schema if getattr(field, "name", "")
        }
        rows = tuple(
            {str(key): _to_json_value(value) for key, value in row.items()}
            for row in page
        )
        has_next = bool(getattr(page, "next_page_token", None))
        total_rows = getattr(iterator, "total_rows", None) if iterator is not None else None
        bytes_processed = int(getattr(job, "total_bytes_processed", 0) or 0)
        if not has_next:
            self._pages.pop(query_id, None)
            self._jobs.pop(query_id, None)
        return QueryPage(
            query_id=query_id,
            columns=columns,
            rows=rows,
            has_next_page=has_next,
            total_rows=int(total_rows) if total_rows is not None else None,
            bytes_processed=bytes_processed,
            job_id=str(getattr(job, "job_id", "") or ""),
            job_project=str(getattr(job, "project", "") or ""),
            location=str(getattr(job, "location", "") or ""),
            column_types=column_types,
        )


def read_bigquery_metadata(catalog: Any, adapter: BigQueryAdapter) -> dict[str, dict[str, Any]]:
    """Read available live schema metadata for workbook tables, skipping failures."""
    report: dict[str, dict[str, Any]] = {}
    for table_id, table in catalog.tables.items():
        try:
            report[table.table_id] = {"ok": True, **adapter.inspect_table_metadata(table.table_id)}
        except BigQueryError as exc:
            report[table.table_id] = {"ok": False, "error": str(exc)}
    return report
