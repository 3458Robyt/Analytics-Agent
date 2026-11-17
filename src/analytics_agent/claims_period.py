"""Reviewed temporal mapping for the claims source used by the compiler."""

import json
import os
import re
from datetime import date
from pathlib import Path

from .analysis_context import shift_month
from .semantics import MetricDefinitionError

PAYMENT_KEYS = frozenset({"pagos_siniestros", "honorarios", "indemnizaciones"})
BALANCE_KEYS = frozenset({"reservas_avisadas", "reservas_anteriores", "variacion_reservas",
                          "incurrido_siniestros", "avisos_recobro", "avisados_salvamentos"})


def load_claims_period():
    path = os.environ.get("ANALYTICS_AGENT_CLAIMS_PERIOD_FILE")
    if not path:
        return {}
    try:
        contract = json.loads(Path(path).expanduser().read_text(encoding="utf-8"))
        if contract["mode"] not in {"historical_movements", "monthly_closes", "latest_close"}:
            raise ValueError()
        if not all(isinstance(contract.get(k), str) and contract[k].strip()
                   for k in ("reviewed_reference", "grain")):
            raise ValueError()
        start, end = (date.fromisoformat(contract[k]) for k in ("start", "end"))
        if start.day != 1 or end.day != 1 or start >= end:
            raise ValueError()
        if contract["mode"] != "historical_movements":
            if not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", contract["period_field"]):
                raise ValueError()
        if contract["mode"] == "latest_close" and shift_month(start, 1) != end:
            raise ValueError()
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise MetricDefinitionError("Contrato temporal de mm_final_netos inválido") from exc
    return contract


def bind_claims_period(key, rule, spec, table, contract):
    """Bind only definitions covered by reviewed evidence. Never infer a cutoff."""
    if not contract:
        if key in BALANCE_KEYS:
            raise MetricDefinitionError("Falta confirmar el campo de corte mensual")
        return rule
    if not spec.start_date or not (
        contract["start"] <= spec.start_date < spec.end_date <= contract["end"]
    ):
        raise MetricDefinitionError("El periodo está fuera de la cobertura conciliada de mm_final_netos")
    if contract["mode"] == "historical_movements":
        if key in BALANCE_KEYS:
            raise MetricDefinitionError("La fuente conciliada contiene movimientos y aún falta el corte mensual")
        return rule
    field = contract["period_field"]
    if not table or not table.metadata_loaded or field not in table.columns:
        raise MetricDefinitionError("El campo de corte conciliado no aparece en el esquema vivo")
    dtype = table.columns[field].data_type.split()[0].upper()
    if dtype not in {"DATE", "DATETIME", "TIMESTAMP"}:
        raise MetricDefinitionError("El campo de corte conciliado debe ser una fecha")
    start, end = date.fromisoformat(spec.start_date), date.fromisoformat(spec.end_date)
    if key in BALANCE_KEYS and (start.day != 1 or end.day != 1):
        raise MetricDefinitionError("Los saldos y el incurrido de cierre requieren meses completos")
    if key in PAYMENT_KEYS and shift_month(start.replace(day=1), 1) < end:
        raise MetricDefinitionError("Consulta un mes por vez en la fuente de cierres para conciliar las fechas de pago")
    return {**rule, "pending_cutoff": False, "cutoff_field": field, "cutoff_type": dtype,
            "date_field": field if key in BALANCE_KEYS else rule["date_field"],
            "date_field_type": dtype if key in BALANCE_KEYS else rule.get("date_field_type", "TIMESTAMP"),
            **({"time_behavior": "closing_month_sum", "date_bases": {}} if key in BALANCE_KEYS else {})}
