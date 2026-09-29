from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from pathlib import Path
from typing import Any, Mapping, Sequence

from .agent import AnalyticsAgent
from .bigquery_adapter import DryRunResult, QueryPage
from .llm import OpenAICompatibleProvider
from .models import AgentSettings
from .schema import ColumnSchema, SchemaCatalog, TableSchema


@dataclass(frozen=True)
class CaseResult:
    case_id: str
    passed: bool
    failures: tuple[str, ...]
    sql: tuple[str, ...]
    answer: str
    total_tokens: int


class FixtureBigQuery:
    """A deterministic BigQuery stand-in that only accepts the SQL required by a case."""

    location = "us-east1"

    def __init__(self, case: Mapping[str, Any]) -> None:
        self.case = case
        self.queries: list[str] = []
        self.executed_sql: list[str] = []
        self._remaining_rows: list[dict[str, Any]] = []
        self._query_id = "evaluation-query"

    def dry_run(self, sql: str, *, location: str = "") -> DryRunResult:
        self.queries.append(sql)
        normalized = sql.lower()
        missing = [fragment for fragment in self.case.get("required_sql_fragments", [])
                   if str(fragment).lower() not in normalized]
        forbidden = [fragment for fragment in self.case.get("forbidden_sql_fragments", [])
                     if str(fragment).lower() in normalized]
        if missing:
            return DryRunResult(False, 0, "Faltan componentes esperados en el SQL: " + ", ".join(missing))
        if forbidden:
            return DryRunResult(False, 0, "El SQL contiene componentes excluidos en la evaluación: " + ", ".join(forbidden))
        return DryRunResult(True, 4096)

    def execute(self, sql: str, *, location: str = "") -> QueryPage:
        self.executed_sql.append(sql)
        rows = [dict(row) for row in self.case.get("result_rows", [])]
        self._remaining_rows = []
        if self.case.get("expect_detail_rows") and len(rows) > 1:
            first_page = rows[:2]
            self._remaining_rows = rows[2:]
        else:
            first_page = rows
        return QueryPage(
            query_id=self._query_id,
            columns=tuple(self.case.get("result_columns", [])),
            rows=tuple(first_page),
            has_next_page=bool(self._remaining_rows),
            total_rows=len(rows),
            bytes_processed=4096,
            job_id="evaluation-job",
            job_project="evaluation-project",
            location=location or self.location,
        )

    def read_page(self, query_id: str) -> QueryPage:
        rows, self._remaining_rows = self._remaining_rows, []
        return QueryPage(
            query_id=query_id,
            columns=tuple(self.case.get("result_columns", [])),
            rows=tuple(rows),
            has_next_page=False,
            total_rows=len(self.case.get("result_rows", [])),
            bytes_processed=4096,
            job_id="evaluation-job",
            job_project="evaluation-project",
            location=self.location,
        )


def load_evaluation_cases(path: str = "") -> list[dict[str, Any]]:
    if path:
        payload = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
    else:
        resource = files("analytics_agent").joinpath("data", "evaluation_cases.json")
        payload = json.loads(resource.read_text(encoding="utf-8"))
    cases = payload.get("cases") if isinstance(payload, dict) else None
    if not isinstance(cases, list) or not all(isinstance(case, dict) for case in cases):
        raise ValueError("El archivo de evaluación debe contener una lista `cases` de objetos")
    return cases


def _catalog_for_case(case: Mapping[str, Any]) -> SchemaCatalog:
    table_id = str(case["table_id"])
    columns = {
        "fecha_emision": ColumnSchema("fecha_emision", "DATE", "Fecha de emisión"),
        "vrprima": ColumnSchema("vrprima", "FLOAT64", "Prima emitida"),
        "nombre_ramo_comercial": ColumnSchema("nombre_ramo_comercial", "STRING", "Ramo comercial"),
    }
    table = TableSchema(
        sheet_name="evaluation_fixture",
        table_id=table_id,
        columns=columns,
        description="Esquema sintético para evaluar prompts sin BigQuery.",
        table_type="TABLE",
        location="us-east1",
        metadata_loaded=True,
    )
    return SchemaCatalog(tables={table_id.lower(): table})


def _row_matches(actual: Mapping[str, Any], expected: Mapping[str, Any]) -> bool:
    if set(actual) != set(expected):
        return False
    for key, expected_value in expected.items():
        actual_value = actual.get(key)
        try:
            if isinstance(actual_value, bool) or isinstance(expected_value, bool):
                if actual_value != expected_value:
                    return False
            elif Decimal(str(actual_value)) != Decimal(str(expected_value)):
                return False
        except (InvalidOperation, ValueError, TypeError):
            if actual_value != expected_value:
                return False
    return True


def _rows_match(actual: Sequence[Mapping[str, Any]], expected: Sequence[Mapping[str, Any]]) -> bool:
    remaining = list(actual)
    for expected_row in expected:
        match_index = next((index for index, row in enumerate(remaining)
                            if _row_matches(row, expected_row)), None)
        if match_index is None:
            return False
        remaining.pop(match_index)
    return not remaining


def evaluate_case(
    case: Mapping[str, Any],
    llm: OpenAICompatibleProvider,
    settings: AgentSettings,
) -> CaseResult:
    bigquery = FixtureBigQuery(case)
    agent = AnalyticsAgent(
        catalog=_catalog_for_case(case),
        llm=llm,
        bigquery=bigquery,  # type: ignore[arg-type]
        settings=settings,
    )
    try:
        answer = agent.answer(str(case["question"]))
    except Exception as exc:
        return CaseResult(str(case.get("id", "case")), False,
                          (f"La ejecución falló ({type(exc).__name__}).",),
                          tuple(bigquery.executed_sql or bigquery.queries), "", 0)

    failures: list[str] = []
    sql_text = "\n".join(bigquery.executed_sql).lower()
    if not bigquery.executed_sql:
        failures.append("No se ejecutó ninguna consulta.")
    for fragment in case.get("required_sql_fragments", []):
        if str(fragment).lower() not in sql_text:
            failures.append(f"Falta SQL requerido: {fragment}")
    for fragment in case.get("forbidden_sql_fragments", []):
        if str(fragment).lower() in sql_text:
            failures.append(f"Apareció SQL excluido: {fragment}")
    table_id = str(case.get("table_id", "")).lower()
    if table_id not in {table.lower() for table in answer.tables_used}:
        failures.append("No se consultó la tabla esperada.")
    if "**revisión:**" in answer.answer.lower():
        failures.append("La respuesta no pasó la revisión final del agente.")

    expected_rows = case.get("result_rows", [])
    if case.get("expect_detail_rows"):
        if answer.detail_table is None:
            failures.append("No se presentó la tabla de detalle solicitada.")
        elif not _rows_match(answer.detail_table.rows, expected_rows):
            failures.append("La tabla de detalle no contiene todas las filas simuladas.")
    else:
        if answer.summary_table is None:
            failures.append("No se presentó la tabla resumen esperada.")
        elif not _rows_match(answer.summary_table.rows, expected_rows):
            failures.append("La tabla resumen no coincide con los resultados simulados.")
    return CaseResult(
        case_id=str(case.get("id", "case")),
        passed=not failures,
        failures=tuple(failures),
        sql=tuple(bigquery.executed_sql),
        answer=answer.answer,
        total_tokens=int(answer.usage.get("total_tokens", 0)),
    )


def evaluate_prompt(
    prompt_name: str,
    prompt: str,
    cases: Sequence[Mapping[str, Any]],
    settings: AgentSettings,
    *,
    wire_api: str = "responses",
    store_responses: bool = False,
) -> tuple[str, list[CaseResult]]:
    results: list[CaseResult] = []
    for case in cases:
        llm = OpenAICompatibleProvider(
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            api_key=settings.llm_api_key,
            wire_api=wire_api,
            store_responses=store_responses,
            agent_system_prompt=prompt,
        )
        results.append(evaluate_case(case, llm, settings))
    return prompt_name, results
