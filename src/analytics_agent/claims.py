"""Company claim rules; schema and observed values outrank guesses in text."""
from __future__ import annotations

import re
import unicodedata
from typing import Any, Mapping, Sequence

from sqlglot import exp, parse_one

from .models import TABLE_IDS
from .semantics import MetricDefinitionError


CLAIM_METRICS = {
    "pagos_siniestros", "reservas_avisadas", "avisos_recobro",
    "avisados_salvamentos", "honorarios", "indemnizaciones",
}


def normalize_label(value: Any) -> str:
    text = unicodedata.normalize("NFKD", str(value or ""))
    return " ".join("".join(c for c in text if not unicodedata.combining(c)).lower().split())


def explicit_class_choices(reply: str, values: Sequence[str]) -> list[str]:
    """An affirmative exact label/list, not a substring in a negated sentence."""
    text = normalize_label(reply).strip("`\"'")
    text = re.sub(r"^(?:usa|incluye|incluir|selecciona|solo|solamente)\s+", "", text)
    labels = {normalize_label(value): value for value in values}
    if text in labels:
        return [labels[text]]
    parts = [part.strip().strip("`\"'") for part in re.split(r"[,;\n]+|\s+y\s+", text)]
    if parts and all(part in labels for part in parts):
        return list(dict.fromkeys(labels[part] for part in parts))
    return []


def requested_claim_metrics(question: str) -> set[str]:
    text = normalize_label(question)
    patterns = {
        "honorarios": r"\bhonorarios?\b",
        "indemnizaciones": r"\bindemnizaci(?:on|ones)\b|\bpag(?:o|ado|ado[s]) al asegurado\b",
        "avisos_recobro": r"\brecobros?\b",
        "avisados_salvamentos": r"\bsalvamentos?\b",
        "reservas_avisadas": r"\breservas?\b|\bavisado\b(?! de (?:salvamento|recobro))",
        "pagos_siniestros": r"\bpag(?:o|os|ado|ados|ada|adas)\b",
    }
    found = {key for key, pattern in patterns.items() if re.search(pattern, text)}
    if found & {"honorarios", "indemnizaciones"}:
        found.discard("pagos_siniestros")
    return found


def requested_payment_class(question: str) -> str:
    """Resolve a clearly requested subset; inclusive/exclusive comparisons stay explicit."""
    text = normalize_label(question)
    if re.search(r"\b(?:sin|excepto|exclu\w*|inclu\w*|primas?)\b|\bno solo\b", text):
        return ""
    if re.search(r"\bpagos\s+y\s+(?:honorarios|indemnizaciones)|\b(?:honorarios|indemnizaciones)\s+y\s+pagos", text):
        return ""
    goals = requested_claim_metrics(question) & {"honorarios", "indemnizaciones"}
    return next(iter(goals)) if len(goals) == 1 else ""


def enforce_compiled_claims(sql: str, question: str, metric_key: str = "") -> None:
    """Amount aggregations for confirmed claims must use the compiler, not arbitrary SQL.

    DISTINCT/COUNT, metadata probes and detail queries remain available. Explicit
    net analyses have no confirmed mapping here and must not inherit gross rules.
    """
    keys = requested_claim_metrics(question)
    if metric_key in CLAIM_METRICS:
        keys.add(metric_key)
    if not keys or (re.search(r"\bnet[oa]s?\b", normalize_label(question)) and not metric_key):
        return
    expression = parse_one(sql, read="bigquery")
    claims_sources = {TABLE_IDS["mm_final"], TABLE_IDS["mm_final_netos"]}
    if not any(".".join((t.catalog, t.db, t.name)).lower() in claims_sources for t in expression.find_all(exp.Table)):
        return
    amounts = {"incurrido_total", "incurrido", "incurrido_neto", "pago", "pago_neto",
               "pago_moneda_local", "pago_moneda_estim", "reserva_actual", "reserva_anterior",
               "reserva_gross", "reserva_actual_neto", "variacion", "variacion_neto"}
    amount_aggregation = any(isinstance(node, (exp.Sum, exp.Avg)) for node in expression.walk())
    amount_aggregation = amount_aggregation or any(
        isinstance(node, (exp.Min, exp.Max)) and any(c.name.lower() in amounts for c in node.find_all(exp.Column))
        for node in expression.walk()
    )
    if amount_aggregation:
        raise MetricDefinitionError(
            "Este cálculo tiene una regla confirmada. Usa run_metric con metric_key "
            + ", ".join(sorted(keys))
            + ", start_date, end_date, group_by y filters. El compilador aplica incurrido_total, tipo y fecha; "
            "no intentes otra fórmula en run_select. Usa SELECT DISTINCT para explorar clases."
        )


def select_payment_classes(values: Sequence[str], goal: str, *, explicit: Sequence[str] = ()) -> list[str]:
    """Select only unambiguous class labels actually returned by BigQuery."""
    labels = list(dict.fromkeys(value for value in values if isinstance(value, str) and value.strip()))
    aliases = {
        "honorarios": {"honorario", "honorarios"},
        "indemnizaciones": {"indemnizacion", "indemnizaciones", "pago de indemnizacion", "pago al asegurado"},
    }
    if explicit:
        wanted = {normalize_label(value) for value in explicit}
        selected = [value for value in labels if normalize_label(value) in wanted]
        if wanted == {normalize_label(value) for value in selected}:
            return selected
        raise MetricDefinitionError("La clase solicitada no existe en clase_pago. Valores observados: " + repr(labels))
    selected = [value for value in labels if normalize_label(value) in aliases.get(goal, set())]
    # A compound label might be another subtype (or an exclusion). Never silently
    # drop it from a claimed total or infer its financial meaning from a substring.
    stems = "honorari" if goal == "honorarios" else "indemniza"
    ambiguous = [value for value in labels if stems in normalize_label(value) and value not in selected]
    if not selected or ambiguous:
        raise MetricDefinitionError(
            f"Hay que identificar qué clases representan {goal}. Valores observados en clase_pago: {labels!r}. "
            "Formula una aclaración concreta con estas opciones; después usa solo etiquetas observadas."
        )
    return selected


def observed_class_filter(values: Sequence[str], filters: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    result = []
    for item in filters:
        item = dict(item)
        if item.get("field") == "clase_pago":
            if item.get("operator", "eq") not in {"eq", "in"}:
                raise MetricDefinitionError("Para clase_pago selecciona etiquetas observadas mediante eq o in")
            raw = item.get("value")
            requested = raw if isinstance(raw, (list, tuple)) else [raw]
            if not all(isinstance(value, str) for value in requested):
                raise MetricDefinitionError("Las clases de pago deben ser etiquetas de texto")
            selected = select_payment_classes(values, "", explicit=requested)
            item.update(operator="in", value=selected)
        result.append(item)
    return result
