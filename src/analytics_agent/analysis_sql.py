"""Compile reviewed multi-metric analyses; never join individual claim/premium facts."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Mapping

from sqlglot import parse_one

from .analysis_context import AnalysisSpec, shift_month
from .organization import dimension_name
from .schema import TableSchema
from .semantics import MetricDefinitionError, MetricQuery, compile_metric_query

ROUTINE_ID = "analytics-487218.functions.prima_devengada"
PAYMENTS = {"pagos_siniestros", "honorarios", "indemnizaciones"}
LABELS = {"prima_emitida": "Prima emitida", "prima_devengada": "Prima devengada",
          "pagos_siniestros": "Pagos de siniestros", "reservas_avisadas": "Reservas avisadas",
          "avisos_recobro": "Avisos de recobro", "avisados_salvamentos": "Avisados de salvamentos",
          "honorarios": "Honorarios", "indemnizaciones": "Indemnizaciones",
          "siniestralidad_pagos": "Siniestralidad sobre devengada",
          "siniestralidad_pagos_emitida": "Pagos sobre prima emitida",
          "linea_mayor": "Línea mayor", "linea_menor": "Línea menor", "vicepresidencia": "Vicepresidencia",
          "nombre_lob_mor": "Nombre MOR", "cod_subprofit_loc": "Código local",
          "ramo": "Ramo", "sucursal": "Sucursal", "mes": "Mes"}


@dataclass(frozen=True)
class CompiledAnalysis:
    query: MetricQuery
    spec: AnalysisSpec
    components: tuple[str, ...]
    requested: tuple[str, ...]
    unavailable: tuple[str, ...] = ()


def routine_roles(metadata: Mapping[str, Any]) -> tuple[str, ...]:
    """Bind business arguments by verified names, preserving the actual ordinal order."""
    if metadata.get("routine_id") != ROUTINE_ID or metadata.get("routine_type") != "SCALAR_FUNCTION":
        raise MetricDefinitionError("La rutina de devengada no es la función escalar esperada")
    if metadata.get("language", "SQL") != "SQL" or metadata.get("remote"):
        raise MetricDefinitionError("La devengada debe usar la función SQL interna")
    mapping = {"iniciovigencia": "start_coverage", "finvigencia": "end_coverage",
               "inicioanalisis": "start_analysis", "finanalisis": "end_analysis", "primaemitida": "premium"}
    roles = []
    for argument in metadata.get("arguments", []):
        name = re.sub(r"_", "", str(argument.get("name", "")).lower())
        role = mapping.get(name)
        kind = str(argument.get("data_type", "")).upper()
        if not role or kind not in ({"FLOAT64", "FLOAT", "NUMERIC", "BIGNUMERIC"} if role == "premium" else {"DATE"}):
            raise MetricDefinitionError("La firma de prima_devengada no coincide con los argumentos revisados")
        roles.append(role)
    if len(roles) != 5 or set(roles) != set(mapping.values()):
        raise MetricDefinitionError("No se pudo comprobar la firma completa de prima_devengada")
    return tuple(roles)


def _dimension(field: str) -> str:
    name = dimension_name(field)
    return {"nombre_sucursal": "sucursal", "nombre_ramo_comercial": "ramo", "ramo_comercial": "ramo",
            "ramo comercial": "ramo", "lob_l1": "vicepresidencia", "lob_l2": "linea_mayor",
            "lob_l3": "linea_menor"}.get(name, name)


def _field(field: str, definition: Mapping[str, Any], table: TableSchema | None) -> str:
    semantic = _dimension(field)
    claims = definition["table_id"].endswith(".mm_final")
    physical = ({"vicepresidencia": "lob_l1", "linea_mayor": "lob_l2", "linea_menor": "lob_l3",
                 "ramo": "ramo_comercial", "sucursal": "nombre_sucursal"} if claims else
                {"ramo": "nombre_ramo_comercial", "sucursal": "nombre_sucursal"}).get(semantic, semantic)
    allowed = set(table.columns) if table and table.metadata_loaded else set(definition.get("dimensions", {})) | set(definition.get("filter_fields", {}))
    if physical not in allowed:
        candidates = [name for name, config in definition.get("dimensions", {}).items()
                      if config.get("alias") in {field, semantic} and name in allowed]
        if len(candidates) == 1:
            physical = candidates[0]
    if physical not in allowed:
        raise MetricDefinitionError(f"El campo {physical} no está comprobado en {definition['table_id']}")
    return physical


def _prepare_definition(key: str, original: Mapping[str, Any], table: TableSchema | None,
                        spec: AnalysisSpec) -> dict[str, Any]:
    result = dict(original)
    if key in PAYMENTS:
        result["date_field"] = "fecha_pago" if spec.payment_date_basis == "payment" else "fecha_accidente"
    dimensions = dict(result.get("dimensions", {}))
    filters = dict(result.get("filter_fields", {}))
    if table and table.metadata_loaded:
        for field, column in table.columns.items():
            if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", field):
                dimensions.setdefault(field, {"alias": field})
                filters[field] = {**filters.get(field, {}), "type": column.data_type.split()[0],
                                  "operators": filters.get(field, {}).get("operators", ["eq", "in"])}
        needed = set(result.get("required_fields", [])) if result.get("kind") == "routine" else {
            result["value_field"], result["date_field"], *(f["field"] for f in result.get("required_filters", []))}
        if needed - set(table.columns):
            raise MetricDefinitionError("Faltan campos comprobados: " + ", ".join(sorted(needed - set(table.columns))))
        if result["date_field"] in table.columns:
            result["date_field_type"] = table.columns[result["date_field"]].data_type.split()[0]
    for field in ("vicepresidencia", "linea_mayor", "linea_menor", "nombre_lob_mor", "lob_l1", "lob_l2", "lob_l3"):
        if field in filters and filters[field].get("type") == "STRING":
            filters[field] = {**filters[field], "normalize": "lower_trim"}
    result.update(dimensions=dimensions, filter_fields=filters)
    return result


def _filters(spec: AnalysisSpec, definition: Mapping[str, Any], table: TableSchema | None) -> list[dict[str, Any]]:
    result = [{**item, "field": _field(str(item["field"]), definition, table)} for item in spec.filters]
    if not spec.population:
        return result
    if any(_dimension(item["field"]) in {"ramo", "vicepresidencia", "linea_mayor", "linea_menor", "cod_subprofit_loc", "nombre_lob_mor"}
           for item in spec.filters):
        raise MetricDefinitionError("El filtro de población contradice o duplica la clasificación resuelta; conserva una sola población")
    columns = set(table.columns) if table and table.metadata_loaded else set(definition.get("filter_fields", {}))
    codes = spec.population["codes"]
    if "cod_subprofit_loc" in columns:
        kind = table.columns["cod_subprofit_loc"].data_type.upper() if table and table.metadata_loaded else "INT64"
        result.append({"field": "cod_subprofit_loc", "operator": "in", "value": [str(code) for code in codes] if kind == "STRING" else codes})
    else:
        level = spec.population["level"]
        if level not in {"vicepresidencia", "linea_mayor", "linea_menor"}:
            paths = spec.population.get("paths", [])
            # A local code may share its leaf with other codes; using that leaf
            # would broaden the population and silently change its amount.
            if len(paths) == 1 and len(codes) == 1:
                raise MetricDefinitionError("Esta fuente no tiene código local para aplicar ese alcance exacto")
            raise MetricDefinitionError("La población no tiene una correspondencia comprobada en esta fuente")
        result.append({"field": _field(level, definition, table), "operator": "eq", "value": spec.population["label"]})
    return result


def _date_sql(field: str, definition: Mapping[str, Any], timezone: str) -> str:
    dtype = definition.get("date_field_type", "DATE").upper()
    if dtype == "TIMESTAMP":
        zone = "UTC" if definition.get("time_behavior") == "closing_month_sum" else timezone
        return f"DATE(`{field}`, '{zone}')"
    return f"DATE(`{field}`)" if dtype == "DATETIME" else f"`{field}`"


def compile_analysis(spec: AnalysisSpec, definitions: Mapping[str, Mapping[str, Any]], *,
                     tables: Mapping[str, TableSchema], timezone: str,
                     routine_metadata: Mapping[str, Any] | None = None,
                     unavailable: Mapping[str, str] | None = None) -> CompiledAnalysis:
    """Every component is aggregated independently, then joined on common dimensions."""
    if not re.fullmatch(r"[A-Za-z0-9_+./-]+", timezone):
        raise MetricDefinitionError("Zona horaria inválida")
    for key in spec.metric_keys:
        if key not in definitions:
            raise MetricDefinitionError(f"La métrica {key} no tiene una definición revisada")
    requested = spec.metric_keys
    components: list[str] = []
    for key in requested:
        rule = definitions[key]
        components.extend((rule["numerator"], rule["denominator"]) if rule.get("kind") == "ratio" else (key,))
    components = list(dict.fromkeys(components))
    periods: list[tuple[date, date]] = []
    if spec.start_date:
        first, end = date.fromisoformat(spec.start_date), date.fromisoformat(spec.end_date)
        if first >= end:
            raise MetricDefinitionError("Periodo invertido")
        while first < end:
            next_day = min(shift_month(first, 1), end)
            periods.append((first, next_day))
            first = next_day
    if spec.time_grain == "month" and not periods:
        raise MetricDefinitionError("El cuadro mensual necesita un periodo de análisis")
    monthly = spec.time_grain == "month"
    group = tuple(dict.fromkeys(_dimension(item) for item in spec.group_by))
    if any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", item) for item in group):
        raise MetricDefinitionError("Dimensión inválida")
    key_columns = (*(("mes",) if monthly else ()), *group)
    ctes: list[str] = []
    if periods:
        ctes.append("calendar AS (" + " UNION ALL ".join(
            f"SELECT DATE '{start}' AS period_start, DATE '{end}' AS period_end, '{start:%Y-%m}' AS mes"
            for start, end in periods) + ")")
    parameters: list[dict[str, Any]] = []
    available: list[str] = []
    issues: list[str] = []
    for index, key in enumerate(components):
        try:
            if key in (unavailable or {}):
                raise MetricDefinitionError(unavailable[key])
            original = definitions[key]
            table = tables.get(original["table_id"].lower())
            definition = _prepare_definition(key, original, table, spec)
            filters = _filters(spec, definition, table)
            dims = [(_field(item, definition, table), item) for item in group]
            prefix = f"m{index}_"
            is_routine = definition.get("kind") == "routine"
            base_definition = {**definition, "kind": "base"}
            base = compile_metric_query(base_definition, start_date="" if is_routine else spec.start_date,
                                        end_date="" if is_routine else spec.end_date, filters=filters,
                                        timezone=timezone, allow_unbounded=True,
                                        payment_date_basis=spec.payment_date_basis)
            expression = parse_one(base.sql, read="bigquery")
            where_node = expression.args.get("where")
            where = where_node.this.sql(dialect="bigquery") if where_node is not None else "TRUE"
            where = re.sub(r"@metric_filter_(\d+)", lambda m: "@" + prefix + "metric_filter_" + m[1], where)
            local_parameters = [{**param, "name": prefix + param["name"]} for param in base.parameters]
            projection = [f"`{physical}` AS `{alias}`" for physical, alias in dims]
            from_sql = f"`{definition['table_id']}`"
            if is_routine:
                if not periods:
                    raise MetricDefinitionError("La devengada necesita un periodo para evaluar la función")
                roles = routine_roles(routine_metadata or {})
                coverage_start = "`fecha_iniciovig`"
                coverage_end = "`fecha_finvig`"
                if table and table.metadata_loaded:
                    for field in ("fecha_iniciovig", "fecha_finvig"):
                        dtype = table.columns[field].data_type.upper()
                        if dtype not in {"DATE", "DATETIME", "TIMESTAMP"}:
                            raise MetricDefinitionError("Las vigencias deben ser fechas comprobadas")
                        rendered = f"DATE(`{field}`, '{timezone}')" if dtype == "TIMESTAMP" else f"DATE(`{field}`)" if dtype == "DATETIME" else f"`{field}`"
                        if field == "fecha_iniciovig": coverage_start = rendered
                        else: coverage_end = rendered
                values = {"start_coverage": coverage_start, "end_coverage": coverage_end,
                          "start_analysis": "c.period_start", "end_analysis": "DATE_SUB(c.period_end, INTERVAL 1 DAY)",
                          "premium": "`vrprima`"}
                amount = f"`{ROUTINE_ID}`(" + ", ".join(values[role] for role in roles) + ")"
                from_sql += " CROSS JOIN calendar AS c"
                where += (f" AND {coverage_start} < c.period_end AND {coverage_end} >= c.period_start"
                          f" AND {coverage_start} < DATE '{spec.end_date}' AND {coverage_end} >= DATE '{spec.start_date}'")
                if monthly:
                    projection.insert(0, "c.mes AS mes")
            else:
                amount = f"`{definition['value_field']}`"
                if monthly:
                    projection.insert(0, f"FORMAT_DATE('%Y-%m', {_date_sql(definition['date_field'], definition, timezone)}) AS mes")
            projection.extend((f"SUM({amount}) AS `{key}`", f"COUNT(*) AS `__rows_{key}`",
                               f"COUNT({amount}) AS `__values_{key}`"))
            body = "SELECT " + ", ".join(projection) + " FROM " + from_sql + " WHERE " + where
            if key_columns:
                body += " GROUP BY " + ", ".join(f"`{column}`" for column in key_columns)
            ctes.append(f"a_{key} AS ({body})")
            parameters.extend(local_parameters)
            available.append(key)
        except MetricDefinitionError as error:
            issues.append(f"{LABELS.get(key, key)}: {error}")
    if not available:
        raise MetricDefinitionError("; ".join(issues) or "No hay componentes calculables")
    if key_columns:
        key_select = ", ".join(f"`{column}`" for column in key_columns)
        unions = " UNION DISTINCT ".join(f"SELECT {key_select} FROM a_{key}" for key in available)
        if monthly and not group:
            unions = "SELECT mes FROM calendar"
        elif monthly:
            dim_select = ", ".join(f"`{column}`" for column in group)
            ctes.append("dimension_keys AS (" + " UNION DISTINCT ".join(
                f"SELECT {dim_select} FROM a_{key}" for key in available) + ")")
            unions = "SELECT c.mes, d.* FROM calendar c CROSS JOIN dimension_keys d"
        ctes.append(f"analysis_keys AS ({unions})")
    else:
        ctes.append("analysis_keys AS (SELECT 1 AS __one)")
    projection = [f"k.`{column}`" for column in key_columns]
    output_components = list(dict.fromkeys([*components, *requested]))
    for key in output_components:
        definition = definitions[key]
        if definition.get("kind") == "ratio":
            numerator, denominator = definition["numerator"], definition["denominator"]
            value = (f"SAFE_DIVIDE(a_{numerator}.`{numerator}`, a_{denominator}.`{denominator}`) * 100"
                     if numerator in available and denominator in available else "CAST(NULL AS FLOAT64)")
        else:
            value = f"a_{key}.`{key}`" if key in available else "CAST(NULL AS FLOAT64)"
        projection.append(f"{value} AS `{key}`")
    for key in available:
        projection.extend(f"COALESCE(a_{key}.`__{kind}_{key}`, 0) AS `__{kind}_{key}`" for kind in ("rows", "values"))
    joins = []
    for key in available:
        condition = " AND ".join(f"k.`{column}` IS NOT DISTINCT FROM a_{key}.`{column}`" for column in key_columns) or "TRUE"
        joins.append(f"LEFT JOIN a_{key} ON {condition}")
    sql = "WITH " + ",\n".join(ctes) + "\nSELECT " + ", ".join(projection) + " FROM analysis_keys k " + " ".join(joins)
    if key_columns:
        sql += " ORDER BY " + ", ".join(f"k.`{column}`" for column in key_columns)
    return CompiledAnalysis(MetricQuery(sql, tuple(parameters)), spec, tuple(available), requested, tuple(issues))
