from __future__ import annotations

import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError
from dataclasses import dataclass, field
from typing import Any


TABLE_IDS = {
    "th_primas_final": "centralizacion-datos.analytics_curated.th_primas_final",
    "mm_final": "centralizacion-datos.analytics_curated.mm_final",
    "mm_final_netos": "centralizacion-datos.analytics_curated.mm_final_netos",
}


class ProposalError(ValueError):
    """A generated query is not one read-only BigQuery SELECT."""


@dataclass(frozen=True)
class AgentSettings:
    llm_base_url: str
    llm_model: str
    llm_api_key: str
    bigquery_project: str = "sbscol-dbreplication-prd"
    bigquery_location: str = ""
    timezone: str = "America/Bogota"
    export_dir: str = "./exports"
    evidence_max_chars: int = 40_000

    @classmethod
    def from_env(cls) -> "AgentSettings":
        base_url = os.environ.get("LLM_BASE_URL", "").strip()
        model = os.environ.get("LLM_MODEL", "").strip()
        api_key = os.environ.get("LLM_API_KEY", "").strip()
        missing = [name for name, value in (
            ("LLM_BASE_URL", base_url),
            ("LLM_MODEL", model),
            ("LLM_API_KEY", api_key),
        ) if not value]
        if missing:
            raise ValueError("Faltan variables de entorno: " + ", ".join(missing))
        timezone = os.environ.get("ANALYTICS_AGENT_TIMEZONE", "America/Bogota").strip()
        try:
            ZoneInfo(timezone)
        except ZoneInfoNotFoundError as exc:
            raise ValueError(f"Zona horaria no válida: {timezone}") from exc
        export_dir = os.environ.get("ANALYTICS_AGENT_EXPORT_DIR", "./exports").strip() or "./exports"
        try:
            evidence_max_chars = int(os.environ.get("ANALYTICS_AGENT_EVIDENCE_MAX_CHARS", "40000"))
        except ValueError as exc:
            raise ValueError("ANALYTICS_AGENT_EVIDENCE_MAX_CHARS debe ser un entero") from exc
        if evidence_max_chars < 1000:
            raise ValueError("ANALYTICS_AGENT_EVIDENCE_MAX_CHARS debe ser al menos 1000")
        return cls(
            llm_base_url=base_url,
            llm_model=model,
            llm_api_key=api_key,
            bigquery_project=os.environ.get("BQ_JOB_PROJECT", "sbscol-dbreplication-prd").strip(),
            bigquery_location=os.environ.get("BQ_LOCATION", "").strip(),
            timezone=timezone,
            export_dir=export_dir,
            evidence_max_chars=evidence_max_chars,
        )


@dataclass(frozen=True)
class SummaryTable:
    title: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]
    numeric_columns: tuple[str, ...] = ()


@dataclass(frozen=True)
class Clarification:
    question: str
    metric_key: str = ""
    proposed_rule: str = ""


@dataclass(frozen=True)
class AgentAnswer:
    answer: str
    assumptions: tuple[str, ...] = ()
    summary_table: SummaryTable | None = None
    detail_table: SummaryTable | None = None
    query_count: int = 0
    tables_used: tuple[str, ...] = ()
    bytes_processed: int = 0
    usage: dict[str, int] = field(default_factory=dict)
    session_id: str = ""
    query_jobs: tuple[dict[str, str], ...] = ()
    learning_updates: tuple[str, ...] = ()
    status: str = "complete"
    clarification: Clarification | None = None
    audit: tuple[dict[str, Any], ...] = ()
    timings: dict[str, float] = field(default_factory=dict)
    turn_id: str = ""
    artifacts: tuple[dict[str, Any], ...] = ()
