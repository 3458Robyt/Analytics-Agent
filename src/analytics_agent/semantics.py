from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import date
from decimal import Decimal, InvalidOperation
from importlib.resources import files
from typing import Any, Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

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
    entries = {
        str(term["key"]): dict(term)
        for term in terms
        if isinstance(term, Mapping)
        and term.get("status") == "validated"
        and isinstance(term.get("key"), str)
    }
    resolved: dict[str, dict[str, Any]] = {}

    def resolve(key: str, stack: tuple[str, ...] = ()) -> dict[str, Any]:
        if key in stack or key not in entries:
            raise MetricDefinitionError("Herencia inválida en el glosario de métricas")
        entry = entries[key]
        parent = resolve(str(entry["extends"]), (*stack, key)) if entry.get("extends") else {}
        result = {**parent, **entry}
        result.pop("extends", None)
        if not all(result.get(field) for field in ("table_id", "value_field", "aggregation", "date_field")):
            raise MetricDefinitionError(f"La definición de `{key}` está incompleta")
        resolved[key] = result
        return result

    for key in entries:
        resolve(key)
    return resolved


def compile_metric_sql(
    definition: Mapping[str, Any],
    *,
    start_date: str,
    end_date: str,
    group_by: list[str] | tuple[str, ...] = (),
    filters: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    timezone: str = "America/Bogota",
) -> str:
    """Compile a metric with identifiers and filter values from its allowlists."""
    return compile_metric_query(
        definition,
        start_date=start_date,
        end_date=end_date,
        group_by=group_by,
        filters=filters,
        timezone=timezone,
    ).sql


def compile_metric_query(
    definition: Mapping[str, Any],
    *,
    start_date: str,
    end_date: str,
    group_by: list[str] | tuple[str, ...] = (),
    filters: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...] = (),
    timezone: str = "America/Bogota",
    allow_unbounded: bool = False,
) -> MetricQuery:
    """Compile a confirmed metric and typed, allowlisted query parameters."""
    unbounded = allow_unbounded and not start_date and not end_date
    try:
        start = None if unbounded else date.fromisoformat(start_date)
        end = None if unbounded else date.fromisoformat(end_date)
    except (TypeError, ValueError) as exc:
        raise MetricDefinitionError("Indica inicio y fin del periodo en formato AAAA-MM-DD") from exc
    if not unbounded and start >= end:
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
    bounds = [f"DATE '{day.isoformat()}'" for day in (start, end) if day is not None]
    if definition.get("date_field_type", "DATE").upper() == "TIMESTAMP":
        # A closing month is a calendar label, not an instant to move into the previous month.
        calendar_zone = "UTC" if definition.get("time_behavior") == "closing_month_sum" else timezone
        try:
            ZoneInfo(calendar_zone)
        except (ValueError, ZoneInfoNotFoundError) as exc:
            raise MetricDefinitionError("La zona horaria de la métrica no es válida") from exc
        if not re.fullmatch(r"[A-Za-z0-9_+./-]+", calendar_zone):
            raise MetricDefinitionError("La zona horaria de la métrica no es válida")
        bounds = [f"TIMESTAMP({bound}, '{calendar_zone}')" for bound in bounds]
    elif definition.get("date_field_type", "DATE").upper() == "DATETIME":
        bounds = [f"DATETIME({bound})" for bound in bounds]
    where = [f"`{date_field}` >= {bounds[0]}", f"`{date_field}` < {bounds[1]}"] if bounds else []
    parameters: list[dict[str, Any]] = []
    filter_fields = definition.get("filter_fields", {})
    if filters and not isinstance(filter_fields, Mapping):
        raise MetricDefinitionError("La métrica no tiene campos de filtro permitidos")
    required = definition.get("required_filters", [])
    if not isinstance(required, (list, tuple)) or not all(isinstance(item, Mapping) for item in required):
        raise MetricDefinitionError("Los filtros obligatorios de la métrica no son válidos")
    for item in filters:
        if not isinstance(item, Mapping):
            raise MetricDefinitionError("Cada filtro debe incluir campo, operador y valor")
        for fixed in required:
            if item.get("field") != fixed.get("field"):
                continue
            config = filter_fields.get(str(item.get("field")), {})
            def values(part):
                raw = part.get("value")
                raw = raw if part.get("operator", "eq") == "in" and isinstance(raw, (list, tuple)) else [raw]
                if config.get("normalize") == "lower_trim":
                    return {str(value).strip().lower() for value in raw}
                return {str(value) for value in raw}
            if item.get("operator", "eq") not in {"eq", "in"} or not values(item).issubset(values(fixed)):
                raise MetricDefinitionError(f"El filtro `{item.get('field')}` contradice la definición confirmada")
    for index, item in enumerate([*required, *filters]):
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
        field_sql = f"`{field}`"
        if config.get("normalize") == "lower_trim":
            if data_type != "STRING":
                raise MetricDefinitionError("Solo se normalizan filtros de texto")
            field_sql = f"LOWER(TRIM(`{field}`))"
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
            if config.get("normalize") == "lower_trim":
                converted = [part.strip().lower() for part in converted]
            name = f"metric_filter_{index}"
            parameters.append({"name": name, "type": data_type, "value": converted, "array": True})
            where.append(f"{field_sql} IN UNNEST(@{name})")
            continue
        converted = _typed_value(data_type, value)
        if config.get("normalize") == "lower_trim":
            converted = converted.strip().lower()
        if operator == "contains":
            name = f"metric_filter_{index}"
            parameters.append({"name": name, "type": "STRING", "value": converted})
            where.append(f"STRPOS(LOWER(`{field}`), LOWER(@{name})) > 0")
        else:
            name = f"metric_filter_{index}"
            parameters.append({"name": name, "type": data_type, "value": converted})
            sql_operator = {"eq": "=", "gte": ">=", "lte": "<="}[operator]
            where.append(f"{field_sql} {sql_operator} @{name}")

    sql = (
        "SELECT " + ", ".join(projection)
        + f" FROM `{table_id}`" + (" WHERE " + " AND ".join(where) if where else "")
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
