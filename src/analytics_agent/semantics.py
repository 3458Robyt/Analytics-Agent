from __future__ import annotations

import json
import re
from datetime import date
from importlib.resources import files
from typing import Any, Mapping

from .models import ProposalError


class MetricDefinitionError(ProposalError):
    """A requested metric is not backed by a complete, usable definition."""


def load_metric_definitions() -> dict[str, dict[str, Any]]:
    resource = files("analytics_agent").joinpath("data", "business_glossary.json")
    try:
        payload = json.loads(resource.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    terms = payload.get("terms", []) if isinstance(payload, Mapping) else []
    return {
        str(term["key"]): dict(term)
        for term in terms
        if isinstance(term, Mapping)
        and term.get("status") == "validated"
        and isinstance(term.get("key"), str)
        and all(term.get(field) for field in ("table_id", "value_field", "aggregation", "date_field"))
    }


def compile_metric_sql(
    definition: Mapping[str, Any],
    *,
    start_date: str,
    end_date: str,
    group_by: list[str] | tuple[str, ...] = (),
) -> str:
    """Compile a confirmed period metric with identifiers from its allowlist."""
    try:
        start = date.fromisoformat(start_date)
        end = date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise MetricDefinitionError("Indica inicio y fin del periodo en formato AAAA-MM-DD") from exc
    if start >= end:
        raise MetricDefinitionError("El inicio del periodo debe ser anterior al final exclusivo")

    table_id = str(definition.get("table_id", ""))
    value_field = str(definition.get("value_field", ""))
    date_field = str(definition.get("date_field", ""))
    aggregation = str(definition.get("aggregation", "")).upper()
    allowed_dimensions = definition.get("dimensions", {})
    if not re.fullmatch(r"[\w-]+\.[\w-]+\.[\w-]+", table_id):
        raise MetricDefinitionError("La definición confirmada no contiene una tabla completa")
    for field in (value_field, date_field):
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise MetricDefinitionError("La definición contiene un campo inválido")
    if aggregation not in {"SUM", "COUNT", "AVG", "MIN", "MAX"}:
        raise MetricDefinitionError("La definición no usa una agregación admitida")
    if not isinstance(allowed_dimensions, Mapping):
        raise MetricDefinitionError("La definición no incluye dimensiones permitidas")

    dimensions: list[tuple[str, str]] = []
    for field in dict.fromkeys(group_by):
        alias = allowed_dimensions.get(field)
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise MetricDefinitionError(f"La dimensión `{field}` no está confirmada para esta métrica")
        dimensions.append((field, alias))

    metric_alias = str(definition.get("alias", "resultado"))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", metric_alias):
        raise MetricDefinitionError("La definición contiene un alias inválido")
    projection = [f"`{field}` AS `{alias}`" for field, alias in dimensions]
    projection.append(f"{aggregation}(`{value_field}`) AS `{metric_alias}`")
    sql = (
        "SELECT " + ", ".join(projection)
        + f" FROM `{table_id}` WHERE `{date_field}` >= DATE '{start.isoformat()}'"
        + f" AND `{date_field}` < DATE '{end.isoformat()}'"
    )
    if dimensions:
        sql += " GROUP BY " + ", ".join(f"`{field}`" for field, _ in dimensions)
    return sql
