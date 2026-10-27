"""Ask for dates before an expensive unscoped read, independently of model compliance."""
from __future__ import annotations

import re

from sqlglot import exp, parse_one

from .knowledge import normalized


def has_period(text: str) -> bool:
    text = normalized(text)
    return bool(re.search(
        r"\b(?:19\d{2}|20\d{2}|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|setiembre|octubre|noviembre|diciembre)\b"
        r"|\b(?:hoy|ayer|anteayer|semestre|trimestre|bimestre)\b"
        r"|\b\d{1,2}/\d{1,2}/\d{2,4}\b"
        r"|\b(?:mes|ano|semana|dia)\s+(?:actual|pasado|pasada|anterior|en curso)\b"
        r"|\b(?:este|esta|ese|esa|ultimo|ultima|pasado|pasada|proximo|proxima)\s+(?:mes|ano|semana|dia)\b"
        r"|\b(?:ultimos|ultimas)\s+\d+\s+(?:dias|semanas|meses|anos)\b", text))


def all_history(text: str) -> bool:
    return bool(re.search(r"\b(?:todo el historico|todo el historial|historico completo|toda la historia|todos los anos|desde el inicio|sin (?:limite|restriccion) de fechas)\b", normalized(text)))


def needs_period(sql: str, question: str, *, temporal_tables: set[str]) -> bool:
    try:
        tree = parse_one(sql, read="bigquery")
    except Exception:
        return False  # SQL validation owns parsing failures.
    if not list(tree.find_all(exp.Table)):
        return False
    limit = tree.args.get("limit")
    if limit and str(limit.expression) == "0":
        return False
    financial = bool(re.search(r"\b(?:pagos?|primas?|reservas?|incurrido|honorarios?|indemnizaciones?|recobros?|salvamentos?|ventas?|ingresos?|siniestros?)\b", normalized(question)))
    tables = {".".join(part.name for part in table.parts).lower() for table in tree.find_all(exp.Table)}
    if not financial and not tables.intersection(temporal_tables):
        return False
    # Simple catalog/dimension enumeration does not ask for a business period.
    if tree.args.get("distinct") and not any(tree.find_all(exp.AggFunc)) and not financial:
        return False
    return financial or any(tree.find_all(exp.AggFunc)) or bool(tree.find(exp.Star))


class DateScopeRequired(Exception):
    def __init__(self, estimated_bytes: int | None) -> None:
        self.estimated_bytes = estimated_bytes
        volume = f"{estimated_bytes / 1024**3:.2f} GiB" if estimated_bytes is not None else "un volumen no estimable"
        super().__init__(f"Esta consulta sin periodo leería aproximadamente {volume}. ¿Qué fechas quieres consultar? También puedes indicar «todo el histórico».")
