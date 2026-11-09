"""Business summaries are calculated from evidence, including null/zero semantics."""
from __future__ import annotations

from decimal import Decimal, InvalidOperation
from typing import Any, Mapping

from .analysis_sql import CompiledAnalysis, LABELS
from .models import SummaryTable


def number(value: Any, *, percent: bool = False) -> str:
    if value is None:
        return "No calculable" if percent else "Sin dato"
    try:
        amount = Decimal(str(value))
        if not amount.is_finite():
            return "Sin dato"
    except (InvalidOperation, TypeError, ValueError):
        return str(value)
    rendered = f"{amount:,.2f}".replace(",", "§").replace(".", ",").replace("§", ".")
    return rendered + (" %" if percent else "")


def analysis_presentation(result: Mapping[str, Any], compiled: CompiledAnalysis
                          ) -> tuple[str, SummaryTable, tuple[str, ...]]:
    rows = tuple(dict(row) for row in result.get("rows", ()))
    columns = tuple(column for column in result.get("columns", ()) if not column.startswith("__"))
    if compiled.spec.comparison_periods:
        return comparison_presentation(rows, columns, compiled)
    formats = {column: "percent" if column.startswith("siniestralidad_") else "amount"
               for column in columns if column in LABELS and column not in {"mes", "vicepresidencia", "linea_mayor", "linea_menor", "nombre_lob_mor", "cod_subprofit_loc", "ramo", "sucursal"}}
    table = SummaryTable("Resumen del análisis", columns, tuple({column: row.get(column) for column in columns} for row in rows),
                         tuple(formats), {column: LABELS.get(column, column.replace("_", " ").capitalize()) for column in columns}, formats)
    notes = list(compiled.spec.assumptions)
    if compiled.spec.population:
        notes.append(f"Clasificación utilizada: {compiled.spec.population['label']} ({compiled.spec.population['level'].replace('_', ' ')}).")
    if any(key in compiled.components for key in ("pagos_siniestros", "honorarios", "indemnizaciones")):
        notes.append("Pagos por fecha de pago." if compiled.spec.payment_date_basis == "payment" else "Pagos por fecha de accidente.")
    if not compiled.spec.start_date:
        notes.append("Se consultó todo el histórico disponible.")
    for key in compiled.components:
        if rows and all(row.get(f"__rows_{key}") == 0 for row in rows):
            notes.append(f"{LABELS[key]}: no se encontraron movimientos para el alcance consultado.")
        elif rows and any(row.get(f"__rows_{key}", 0) > 0 and row.get(f"__values_{key}") == 0 for row in rows):
            notes.append(f"{LABELS[key]}: hay movimientos sin importes informados.")
    for ratio in (key for key in compiled.requested if key.startswith("siniestralidad_")):
        denominator = "prima_emitida" if ratio.endswith("_emitida") else "prima_devengada"
        if any(row.get(denominator) is not None and Decimal(str(row[denominator])) == 0 for row in rows):
            notes.append(f"El porcentaje no es calculable cuando {LABELS[denominator].lower()} es cero.")
    if len(rows) == 1 and "mes" not in columns and not compiled.spec.group_by:
        row = rows[0]
        facts = []
        for key in columns:
            if key in formats:
                value = number(row.get(key), percent=formats[key] == "percent")
                facts.append(f"**{LABELS.get(key, key)}:** {value}")
        answer = "; ".join(facts) + "." if facts else "No se encontraron importes para el análisis solicitado."
    elif rows:
        scope = compiled.spec.population.get("label", "el alcance solicitado")
        answer = f"El cuadro muestra {', '.join(LABELS.get(key, key).lower() for key in compiled.requested)} para {scope}."
        # Summarize an actual observed row, without averaging percentages or
        # turning absent components into zero in a new total.
        primary = next((key for key in compiled.requested if key in formats and formats[key] == "amount"), None)
        if primary:
            informed = [row for row in rows if row.get(primary) is not None]
            if informed:
                largest = max(informed, key=lambda row: Decimal(str(row[primary])))
                dimensions = [str(largest.get(key)) for key in columns if key not in formats and largest.get(key) is not None]
                label = " · ".join(dimensions)
                if label:
                    answer += f" El mayor importe de {LABELS[primary].lower()} corresponde a **{label}**, con **{number(largest[primary])}**."
        if compiled.spec.start_date:
            from datetime import date, timedelta
            last = date.fromisoformat(compiled.spec.end_date) - timedelta(days=1)
            answer += f" Periodo: {compiled.spec.start_date} a {last.isoformat()}."
    else:
        answer = "No se encontraron movimientos para el periodo y la clasificación solicitados."
    if compiled.unavailable:
        answer += "\n\n**Resultado parcial.** No pude completar: " + "; ".join(compiled.unavailable) + "."
    return answer, table, tuple(dict.fromkeys(notes))


def comparison_presentation(rows, columns, compiled):
    from datetime import date, timedelta
    periods = [f"{start} a {(date.fromisoformat(end) - timedelta(days=1)).isoformat()}"
               for start, end in compiled.spec.comparison_periods]
    labels, formats = dict(LABELS), {}
    for key in compiled.requested:
        title = LABELS.get(key, key)
        for suffix, label in (("base", "Periodo 1"), ("actual", "Periodo 2"),
                              ("diferencia", "Diferencia"), ("variacion_pct", "Variación %")):
            column = f"{key}_{suffix}"
            labels[column] = f"{title} · {label}" if len(compiled.requested) > 1 else label
            formats[column] = "percent" if suffix == "variacion_pct" or key.startswith("siniestralidad_") and suffix != "diferencia" else "amount"
            if key.startswith("siniestralidad_") and suffix == "diferencia":
                labels[column] += " (puntos porcentuales)"
    title = "Comparación · " + LABELS.get(compiled.requested[0], compiled.requested[0]) if len(compiled.requested) == 1 else "Comparación de periodos"
    table = SummaryTable(title, columns,
                         tuple({key: row.get(key) for key in columns} for row in rows),
                         tuple(formats), {key: labels.get(key, key) for key in columns}, formats)
    answer = f"Comparación de **{periods[0]}** frente a **{periods[1]}**, conservando el mismo alcance."
    if len(rows) == 1:
        for key in compiled.requested:
            row = rows[0]
            percent = key.startswith("siniestralidad_")
            answer += (f" {LABELS.get(key, key)}: **{number(row.get(key + '_base'), percent=percent)}** → "
                       f"**{number(row.get(key + '_actual'), percent=percent)}**; variación: "
                       f"**{number(row.get(key + '_variacion_pct'), percent=True)}**.")
    elif rows:
        answer += " El cuadro compara cada grupo y muestra la diferencia y su variación porcentual."
    else:
        answer += " No se encontraron movimientos en ninguno de los dos periodos."
    notes = [*compiled.spec.assumptions,
             "Variación porcentual = (segundo periodo − primero) / primero × 100. Una base cero o sin dato no permite calcularla; los importes ausentes no se convierten en cero."]
    if compiled.spec.population:
        notes.append(f"Clasificación utilizada: {compiled.spec.population['label']}.")
    if any(key in compiled.components for key in ("pagos_siniestros", "honorarios", "indemnizaciones")):
        notes.append("Pagos por fecha de pago." if compiled.spec.payment_date_basis == "payment" else "Pagos por fecha de accidente.")
    if compiled.unavailable:
        answer += "\n\n**Resultado parcial.** No pude completar: " + "; ".join(compiled.unavailable) + "."
    return answer, table, tuple(notes)
