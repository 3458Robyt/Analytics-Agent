from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, Mapping

from .models import ProposalError


class MetricDefinitionError(ProposalError):
    """A requested metric is not backed by a complete, usable definition."""


@dataclass(frozen=True)
class MetricQuery:
    sql: str
    parameters: tuple[dict[str, Any], ...] = ()


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
    filters: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
) -> str:
    """Compile a metric with identifiers and filter values from its allowlists."""
    return compile_metric_query(
        definition,
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
        filters=filters,
    ).sql


def compile_metric_query(
    definition: Mapping[str, Any],
    *,
    start_date: str,
    end_date: str,
    group_by: list[str] | tuple[str, ...] = (),
    filters: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
) -> MetricQuery:
    """Compile a confirmed metric and typed, allowlisted query parameters."""
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
        configured = allowed_dimensions.get(field)
        alias = configured.get("alias") if isinstance(configured, Mapping) else configured
        if not isinstance(alias, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise MetricDefinitionError(f"La dimensión `{field}` no está confirmada para esta métrica")
        dimensions.append((field, alias))

    metric_alias = str(definition.get("alias", "resultado"))
    if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", metric_alias):
        raise MetricDefinitionError("La definición contiene un alias inválido")
    projection = [f"`{field}` AS `{alias}`" for field, alias in dimensions]
    projection.append(f"{aggregation}(`{value_field}`) AS `{metric_alias}`")
    where = [
        f"`{date_field}` >= DATE '{start.isoformat()}'",
        f"`{date_field}` < DATE '{end.isoformat()}'",
    ]
    parameters: list[dict[str, Any]] = []
    filter_fields = definition.get("filter_fields", {})
    if filters and not isinstance(filter_fields, Mapping):
        raise MetricDefinitionError("La métrica no tiene campos de filtro permitidos")
    for index, item in enumerate(filters):
        if not isinstance(item, Mapping):
            raise MetricDefinitionError("Cada filtro debe incluir campo, operador y valor")
        field = str(item.get("field", ""))
        operator = str(item.get("operator", "eq")).lower()
        value = item.get("value")
        config = filter_fields.get(field) if isinstance(filter_fields, Mapping) else None
        if not isinstance(config, Mapping):
            raise MetricDefinitionError(f"El filtro `{field}` no está permitido para esta métrica")
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
            raise MetricDefinitionError("El filtro contiene un nombre de campo inválido")
        data_type = str(config.get("type", "")).upper()
        allowed_operators = {str(op).lower() for op in config.get("operators", ["eq"])}
        if operator not in allowed_operators or operator not in {"eq", "in", "contains", "gte", "lte", "is_null"}:
            raise MetricDefinitionError(f"El operador `{operator}` no está permitido para `{field}`")
        if operator == "is_null":
            if value not in (None, True):
                raise MetricDefinitionError("El filtro `is_null` no recibe un valor")
            where.append(f"`{field}` IS NULL")
            continue
        if operator == "contains" and data_type != "STRING":
            raise MetricDefinitionError("El operador `contains` solo se permite en campos de texto")
        if operator == "in":
            if not isinstance(value, (list, tuple)) or not value or len(value) > 100:
                raise MetricDefinitionError("El filtro `in` requiere entre 1 y 100 valores")
            converted = [_typed_value(data_type, part) for part in value]
            name = f"metric_filter_{index}"
            parameters.append({"name": name, "type": data_type, "value": converted, "array": True})
            where.append(f"`{field}` IN UNNEST(@{name})")
            continue
        converted = _typed_value(data_type, value)
        if operator == "contains":
            name = f"metric_filter_{index}"
            parameters.append({"name": name, "type": "STRING", "value": converted})
            where.append(f"STRPOS(LOWER(`{field}`), LOWER(@{name})) > 0")
        else:
            name = f"metric_filter_{index}"
            parameters.append({"name": name, "type": data_type, "value": converted})
            sql_operator = {"eq": "=", "gte": ">=", "lte": "<="}[operator]
            where.append(f"`{field}` {sql_operator} @{name}")

    sql = (
        "SELECT " + ", ".join(projection)
        + f" FROM `{table_id}` WHERE " + " AND ".join(where)
    )
    if dimensions:
        sql += " GROUP BY " + ", ".join(f"`{field}`" for field, _ in dimensions)
    return MetricQuery(sql=sql, parameters=tuple(parameters))


def _typed_value(data_type: str, value: Any) -> Any:
    """Validate and convert filter values before they reach BigQuery."""
    if data_type == "STRING":
        if not isinstance(value, str) or len(value) > 1000:
            raise MetricDefinitionError("El filtro de texto debe ser texto de máximo 1000 caracteres")
        return value
    if data_type in {"INT64", "INTEGER"}:
        if isinstance(value, bool) or not re.fullmatch(r"-?\d+", str(value)):
            raise MetricDefinitionError("El filtro debe ser un entero")
        return int(value)
    if data_type in {"NUMERIC", "BIGNUMERIC", "FLOAT64", "FLOAT"}:
        if isinstance(value, bool):
            raise MetricDefinitionError("El filtro debe ser numérico")
        try:
            numeric = Decimal(str(value))
        except (InvalidOperation, ValueError) as exc:
            raise MetricDefinitionError("El filtro debe ser numérico") from exc
        if not numeric.is_finite():
            raise MetricDefinitionError("El filtro numérico debe ser finito")
        return float(numeric) if data_type in {"FLOAT64", "FLOAT"} else numeric
    if data_type in {"BOOL", "BOOLEAN"}:
        if isinstance(value, bool):
            return value
        if isinstance(value, str) and value.lower() in {"true", "false"}:
            return value.lower() == "true"
        raise MetricDefinitionError("El filtro debe ser verdadero o falso")
    if data_type == "DATE":
        try:
            return date.fromisoformat(str(value))
        except (TypeError, ValueError) as exc:
            raise MetricDefinitionError("El filtro de fecha debe usar AAAA-MM-DD") from exc
    raise MetricDefinitionError(f"Tipo de filtro no permitido: `{data_type or 'vacío'}`")
