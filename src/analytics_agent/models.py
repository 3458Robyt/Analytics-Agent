from __future__ import annotations

import os
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
        return cls(
            llm_base_url=base_url,
            llm_model=model,
            llm_api_key=api_key,
            bigquery_project=os.environ.get("BQ_JOB_PROJECT", "sbscol-dbreplication-prd").strip(),
            bigquery_location=os.environ.get("BQ_LOCATION", "").strip(),
        )


@dataclass(frozen=True)
class SummaryTable:
    title: str
    columns: tuple[str, ...]
    rows: tuple[dict[str, Any], ...]


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
