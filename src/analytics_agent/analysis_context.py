"""Explicit, session-scoped analysis intent and deterministic calendar defaults."""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from datetime import date, timedelta
from typing import Any, Mapping

from .organization import OrganizationCatalog, dimension_name, normalized
from .query_scope import all_history
from .semantics import MetricDefinitionError

MONTHS = {name: i for i, name in enumerate(("enero", "febrero", "marzo", "abril", "mayo", "junio",
                                          "julio", "agosto", "septiembre", "octubre", "noviembre", "diciembre"), 1)}


def shift_month(day: date, offset: int) -> date:
    number = day.year * 12 + day.month - 1 + offset
    return date(number // 12, number % 12 + 1, 1)


@dataclass(frozen=True)
class AnalysisSpec:
    metric_keys: tuple[str, ...]
    start_date: str = ""
    end_date: str = ""
    time_grain: str = "total"
    group_by: tuple[str, ...] = ()
    filters: tuple[dict[str, Any], ...] = ()
    population: dict[str, Any] = field(default_factory=dict)
    payment_date_basis: str = "accident"
    assumptions: tuple[str, ...] = ()
    as_of: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def period_from_text(text: str, *, today: date, inherited: Mapping[str, Any] | None = None
                     ) -> tuple[str, str, tuple[str, ...]] | None:
    text = normalized(text)
    inherited = inherited or {}
    years = re.findall(r"\b(20\d{2})\b", text)
    old_year = str(inherited.get("start_date", ""))[:4]
    year = int(years[-1]) if years else int(old_year) if old_year.isdigit() else today.year
    if all_history(text):
        return "", "", ("Se consultó todo el histórico disponible.",)
    if re.search(r"\b(?:anteayer|ayer|hoy)\b", text):
        offset = 2 if "anteayer" in text else 1 if re.search(r"\bayer\b", text) else 0
        start = today - timedelta(days=offset)
        return start.isoformat(), (start + timedelta(days=1)).isoformat(), ()
    relative = re.search(r"\bultimos?\s+(\d+|seis|tres|doce|dos)\s+mes(?:es)?\b", text)
    if relative:
        counts = {"seis": 6, "tres": 3, "doce": 12, "dos": 2}
        count = counts.get(relative[1], int(relative[1]) if relative[1].isdigit() else 0)
        if not 1 <= count <= 120:
            raise MetricDefinitionError("Indica entre 1 y 120 meses para el cuadro mensual")
        closed = bool(re.search(r"\b(?:cerrados|completos)\b", text))
        first = shift_month(today, -count if closed else 1 - count)
        last = today.replace(day=1) if closed else today + timedelta(days=1)
        note = () if closed else (f"El mes de {today:%Y-%m} es parcial, hasta el {today.isoformat()}.",)
        return first.isoformat(), last.isoformat(), note
    if re.search(r"\bmes\s+(?:actual|en curso)\b", text):
        return today.replace(day=1).isoformat(), (today + timedelta(days=1)).isoformat(), (
            f"El mes actual es parcial, hasta el {today.isoformat()}.",)
    if re.search(r"\bmes\s+(?:pasado|anterior)\b", text):
        return shift_month(today, -1).isoformat(), today.replace(day=1).isoformat(), ()
    if re.search(r"\bano\s+(?:pasado|anterior|actual|en curso)\b", text):
        target = today.year - int(bool(re.search(r"\b(?:pasado|anterior)\b", text)))
        end = date(target + 1, 1, 1) if target < today.year else today + timedelta(days=1)
        return date(target, 1, 1).isoformat(), end.isoformat(), ()
    iso = re.findall(r"\b\d{4}-\d{2}-\d{2}\b", text)
    if iso:
        start = date.fromisoformat(iso[0])
        end = date.fromisoformat(iso[1]) + timedelta(days=1) if len(iso) > 1 else start + timedelta(days=1)
        if start >= end:
            raise MetricDefinitionError("El periodo indicado está invertido")
        return start.isoformat(), end.isoformat(), ()
    semester = re.search(r"\b(primer|primero|segundo|1er|2do)\s+semestre\b", text)
    if semester:
        first = date(year, 7 if semester[1] in {"segundo", "2do"} else 1, 1)
        return first.isoformat(), shift_month(first, 6).isoformat(), ()
    months = [(m.start(), value) for name, value in MONTHS.items()
              for m in re.finditer(r"\b" + name + r"\b", text)]
    if months:
        months.sort()
        def adjacent_year(position: int) -> int | None:
            found = re.match(r"[a-z]+\s+(?:de\s+)?(20\d{2})\b", text[position:])
            return int(found[1]) if found else None
        first_explicit = adjacent_year(months[0][0])
        last_explicit = adjacent_year(months[-1][0])
        crosses_year = int(months[-1][1] < months[0][1])
        if first_explicit is not None:
            year = first_explicit
        elif last_explicit is not None:
            year = last_explicit - crosses_year
        first = date(year, months[0][1], 1)
        last_year = last_explicit if last_explicit is not None else year + crosses_year
        last = date(last_year, months[-1][1], 1)
        note = () if years else (f"Se interpretó el periodo en {year}.",)
        # Explicit days in natural Spanish are inclusive.
        days = re.search(r"\b(?:del?\s+)?(\d{1,2})\s+(?:al|a)\s+(\d{1,2})\s+de\s+(?:" + "|".join(MONTHS) + r")\b", text)
        first_day = re.search(r"\b(\d{1,2})(?:\s+de)?\s+$", text[:months[0][0]])
        last_day = re.search(r"\b(\d{1,2})(?:\s+de)?\s+$", text[:months[-1][0]])
        if len(months) > 1 and first_day and last_day:
            first = date(year, first.month, int(first_day[1]))
            end = date(last_year, last.month, int(last_day[1])) + timedelta(days=1)
        elif days:
            first = date(year, first.month, int(days[1]))
            end = date(last_year, last.month, int(days[2])) + timedelta(days=1)
        else:
            single_day = re.search(r"\b(\d{1,2})\s+de\s+(?:" + "|".join(MONTHS) + r")\b", text)
            if single_day:
                first = date(year, first.month, int(single_day[1]))
                end = first + timedelta(days=1)
            else:
                end = shift_month(last, 1)
        if first >= end:
            raise MetricDefinitionError("El periodo indicado está invertido")
        return first.isoformat(), end.isoformat(), note
    if years and re.search(r"\b(?:ano|anual|durante|en)\s+(?:el\s+)?20\d{2}\b", text):
        return date(year, 1, 1).isoformat(), date(year + 1, 1, 1).isoformat(), ()
    return None


def requested_metrics(question: str) -> tuple[str, ...]:
    text = normalized(question)
    keys: list[str] = []
    earned = bool(re.search(r"\b(?:devengad[ao]s?|ganad[ao]s?)\b|prima_devengada", text))
    issued = bool(re.search(r"\b(?:emitid[ao]s?|emision|produccion)\b|prima_emitida", text))
    other_premium = bool(re.search(r"\b(?:retenid[ao]s?|net[ao]s?|cedid[ao]s?)\b", text))
    if issued or re.search(r"\bprimas?\b", text) and not earned and not other_premium:
        keys.append("prima_emitida")
    if earned:
        keys.append("prima_devengada")
    all_payments = bool(re.search(r"\b(?:todos? los pagos|pagos totales|incluy(?:endo|e|an|a) honorarios)\b", text))
    for pattern, key in ((r"\bhonorarios?\b", "honorarios"),
                         (r"\bindemnizaci(?:on|ones)\b", "indemnizaciones"),
                         (r"\breservas?\b|\breservas_avisadas\b", "reservas_avisadas"),
                         (r"\brecobros?\b", "avisos_recobro"),
                         (r"\bsalvamentos?\b", "avisados_salvamentos")):
        if re.search(pattern, text):
            if all_payments and key in {"honorarios", "indemnizaciones"}:
                continue
            keys.append(key)
    if re.search(r"\b(?:pagos?|pagad[oa]s?|siniestros?)\b|pagos_siniestros", text) and not (
            set(keys) & {"honorarios", "indemnizaciones"}):
        keys.append("pagos_siniestros")
    if "siniestralidad" in text:
        use_issued = bool(re.search(r"\b(?:con|sobre|usando)\s+(?:la\s+)?prima\s+emitida\b", text))
        keys.append("siniestralidad_pagos_emitida" if use_issued else "siniestralidad_pagos")
    return tuple(dict.fromkeys(keys))


BRANCH_NAME_FIELDS = {"ramo", "nombre_ramo_comercial", "ramo_comercial", "descripcion_ramo_com",
                      "nombre_ramo_tecnico", "desc_ramo_contable"}
BRANCH_CODE_FIELDS = {"codigo_ramo", "cod_ramo", "cod_ramo_com", "cod_ramo_tec", "cod_ramo_cont",
                      "codigo_ramo_contable", "ramo_contable"}


def _branch_code_field(target: str) -> str:
    if "tecnico" in target or "cod_ramo_tec" in target:
        return "cod_ramo_tec"
    if "contable" in target or "cod_ramo_cont" in target:
        return "codigo_ramo_contable"
    return "codigo_ramo"


def wants_branch_code(question: str) -> bool:
    text = normalized(question)
    target = r"(?:cod_ramo(?:_(?:com|tec|cont))?|codigo_ramo|(?:codigo|numero)\s+(?:del?\s+)?ramo)\b"
    if re.search(r"\b(?:no(?: (?:uses?|usar|debes? usar))?|en vez (?:del?|de)|en lugar (?:del?|de))\s+(?:el\s+)?" + target, text):
        return False
    return bool(re.search(r"\b" + target, text))


def branch_scope_filter(question: str) -> dict[str, Any] | None:
    """Parse an explicit branch identifier, never arbitrary prose after 'ramo'."""
    text = normalized(question)
    target = (r"(?:cod_ramo(?:_(?:com|tec|cont))?|codigo_ramo|(?:codigo|numero)\s+(?:del?\s+)?"
              r"ramo(?:\s+(?:comercial|tecnico|contable))?|ramos?(?:\s+(?:comercial|tecnico|contable))?"
              r"(?:\s+(?:codigo|numero|nro\.?|no\.?))?)")
    match = re.search(r"\b(?P<target>" + target + r")\s*(?:(?:es|=|:|#)\s*)?[`'\"]?(?P<code>\d+)(?![\w.])", text)
    if match:
        values = [match["code"]]
        tail = text[match.end():].lstrip("`'\"")
        while following := re.match(r"\s*(?:,|y|o)\s*[`'\"]?(\d+)(?![\w.])[`'\"]?", tail):
            values.append(following[1])
            tail = tail[following.end():]
        values = list(dict.fromkeys(values))
        return {"field": _branch_code_field(match["target"]), "operator": "in" if len(values) > 1 else "eq",
                "value": values if len(values) > 1 else values[0]}
    head = re.search(r"\bramo(?:\s+(?:comercial|t[eé]cnico|contable))?\s+(?:de\s+)?[=:\s]*", question, re.I)
    if not head:
        return None
    tail = question[head.end():].strip()
    quoted = re.match(r"([`'\"])(.+?)\1", tail)
    if quoted:
        label = quoted[2].strip()
    else:
        label = re.split(r"[,;\n?'\"`]|\s+(?:en|para|durante|por|desde|hasta|del|de (?:20\d{2}|enero|febrero|marzo|abril|mayo|junio|julio|agosto|septiembre|octubre|noviembre|diciembre))\b",
                         tail, maxsplit=1, flags=re.I)[0].strip()
        words = normalized(label).split()
        invalid = {"en", "para", "durante", "por", "del", "de", "y", "comercial", "tecnico", "contable",
                   "debes", "debe", "deberias", "usa", "usar", "uses", "utiliza", "utilizar", "no", "es", "son",
                   "se", "que", "tiene", "nombre", "numero", "codigo", "campo", "el", "lo"}
        if (not words or words[0] in invalid or not label[0].isalpha()
                or any(word in invalid - {"de", "del", "y", "el", "comercial", "tecnico", "contable"} for word in words)
                or re.search(r"\bcod_\w+", normalized(label))):
            return None
    if not label or len(label) > 120:
        return None
    head_text = normalized(head[0])
    field = ("nombre_ramo_tecnico" if "tecnic" in head_text else
             "desc_ramo_contable" if "contable" in head_text else "ramo")
    return {"field": field, "operator": "eq", "value": label}


def explicit_branch(question: str) -> str:
    scope = branch_scope_filter(question)
    return str(scope["value"]) if scope and scope["field"] in BRANCH_NAME_FIELDS else ""


def resolve_analysis(action: Mapping[str, Any], question: str, *, today: date,
                     active: Mapping[str, Any] | None = None,
                     organization: OrganizationCatalog | None = None) -> AnalysisSpec:
    """Only explicit employee changes may replace a resolved population or period."""
    text = normalized(question)
    followup = bool(re.search(r"^(?:[¿? ]*)(?:y\b|ahora\b|usa\b|haz el calculo\b|vuelve\b|esos\b|confirma\b|cambia\b|igual\b|por\b|con\b)", text))
    active = dict(active or {})
    previous = active if followup or not requested_metrics(question) else {}
    requested = requested_metrics(question)
    supplied = action.get("metric_keys") or ([action["metric_key"]] if action.get("metric_key") else [])
    keys = requested or tuple(supplied) or tuple(previous.get("metric_keys", ()))
    if (re.search(r"\b(?:net[ao]s?|retenid[ao]s?|cedid[ao]s?)\b", text)
            and (re.search(r"\bprimas?\b", text) and "prima_emitida" in keys
                 or re.search(r"\b(?:pagos?|siniestros?)\b", text) and "pagos_siniestros" in keys)):
        raise MetricDefinitionError("El importe neto, retenido o cedido necesita su propio mapeo; explora la fuente con SQL sin sustituirlo por la métrica bruta")
    if not keys:
        raise MetricDefinitionError("Indica qué métricas debe contener el análisis")
    # A follow-up asking only for a denominator changes the ratio already requested.
    if previous and "siniestralidad" in " ".join(previous.get("metric_keys", ())) and not re.search(
            r"\b(?:listado|list[ae]|tabla|cuadro)\b", text):
        if "siniestralidad" in text or re.search(r"^(?:usa|con|ahora con)\b", text):
            if "prima_devengada" in requested:
                keys = tuple("siniestralidad_pagos" if key.startswith("siniestralidad_") else key for key in previous["metric_keys"])
            elif "prima_emitida" in requested:
                keys = tuple("siniestralidad_pagos_emitida" if key.startswith("siniestralidad_") else key for key in previous["metric_keys"])
    if re.search(r"^(?:vuelve a intentarlo|confirma|esos valores)", text) or (
            previous and not requested and re.search(r"fecha_(?:accidente|pago)|fecha de (?:accidente|pago)", text)):
        keys = tuple(previous.get("metric_keys", keys))
    keys = tuple(dict.fromkeys(keys))
    period = period_from_text(question, today=today, inherited=active)
    if period is None:
        start, end = previous.get("start_date", ""), previous.get("end_date", "")
        notes = tuple(previous.get("assumptions", ())) if previous else ()
        if not start and not end and action.get("start_date") and action.get("end_date"):
            # For less common explicit date phrases the model may normalize them,
            # but may never invent a range for a question without any date.
            from .query_scope import has_period
            if has_period(question):
                start, end = action["start_date"], action["end_date"]
    else:
        start, end, notes = period
    if bool(start) != bool(end) or start and date.fromisoformat(start) >= date.fromisoformat(end):
        raise MetricDefinitionError("Indica un inicio y final exclusivo válidos")
    organization = organization or OrganizationCatalog()
    population = organization.from_question(question)
    branch = branch_scope_filter(question)
    previous_branch = any(item.get("field") in BRANCH_CODE_FIELDS | BRANCH_NAME_FIELDS
                          for item in previous.get("filters", ()))
    correction_wording = bool(re.search(r"\b(?:te dije|te di|debes?|usa|usar|utiliza|no (?:el )?nombre)\b", text))
    code_correction = wants_branch_code(question) and branch is None and (previous_branch or correction_wording)
    if code_correction and previous and not requested:
        keys = tuple(previous.get("metric_keys", keys))
    if population is None and (branch or code_correction) and previous:
        population = {}  # explicitly switching dimension must not keep the old LOB scope
    elif population is None:
        population = dict(previous.get("population", {}))
    basis = str(previous.get("payment_date_basis", "accident"))
    if re.search(r"fecha_pago|fecha de pago|fecha del pago|pagos efectuados|pagos realizados", text):
        basis = "payment"
    elif re.search(r"fecha_accidente|fecha de accidente|fecha del accidente", text):
        basis = "accident"
    if basis not in {"accident", "payment"}:
        raise MetricDefinitionError("La fecha de pagos debe ser accidente o pago")
    group = list(action.get("group_by") or previous.get("group_by", ()))
    group_month = any(value in {"mes", "month"} for value in group)
    if re.search(r"\b(?:sin desglose|sin agrupar|total general)\b", text):
        group = []
    match = re.search(r"\bpor\s+(area|areas|vp|vicepresidencia|linea mayor|linea menor|lob[ _]?l?[123]|sucursal|ramo(?: comercial)?)\b", text)
    if match:
        group = [dimension_name(match[1])]
    group = [dimension_name(str(value)) for value in group if value not in {"mes", "month"}]
    monthly = bool(re.search(r"\b(?:mes a mes|mensual(?:es)?|por mes|cada mes|ultimos?\s+(?:\d+|seis|tres|doce|dos)\s+meses)\b", text))
    grain = "month" if monthly or group_month else str(action.get("time_grain") or previous.get("time_grain", "total"))
    if previous and not monthly and action.get("time_grain") == "total" and not re.search(r"\b(?:sin desglose|sin agrupar|total general|sin meses)\b", text):
        grain = str(previous.get("time_grain", "total"))
    if re.search(r"\b(?:sin desglose|sin agrupar|total general|sin meses)\b", text):
        grain = "total"
    if grain not in {"total", "month"}:
        raise MetricDefinitionError("La granularidad debe ser total o month")
    if all_history(text) and not monthly:
        grain = "total"
    filters = tuple(dict(item) for item in (action.get("filters") or previous.get("filters", ())))
    if previous.get("filters") and action.get("filters"):
        by_field = {item["field"]: dict(item) for item in previous["filters"]}
        by_field.update({item["field"]: dict(item) for item in action["filters"]})
        filters = tuple(by_field.values())
    if code_correction:
        # Recover the previous identifier before merging model filters. A
        # correction is not a new value, and must remove the old name predicate.
        stored = list(previous.get("filters", ()))
        codes = [item for item in stored if item.get("field") in BRANCH_CODE_FIELDS]
        if not codes:
            codes = [item for item in filters if item.get("field") in BRANCH_CODE_FIELDS]
        if codes:
            branch = dict(codes[0])
            branch["field"] = _branch_code_field(str(branch["field"]))
            explicit_kind = _branch_code_field(text)
            if explicit_kind != "codigo_ramo" and explicit_kind != branch["field"]:
                raise MetricDefinitionError("El código técnico o contable no equivale al comercial anterior; indica el código de esa clasificación")
        else:
            numeric_names = [item for item in stored if item.get("field") in BRANCH_NAME_FIELDS
                             and re.fullmatch(r"\d+", str(item.get("value", "")))]
            if numeric_names:
                branch = {"field": "codigo_ramo", "operator": "eq", "value": str(numeric_names[0]["value"])}
            else:
                raise MetricDefinitionError("No se pudo recuperar el número del ramo anterior; indica el código que quieres consultar")
    if branch:
        filters = tuple(item for item in filters if item.get("field") not in BRANCH_NAME_FIELDS | BRANCH_CODE_FIELDS
                        | {"cod_subprofit_loc", "linea_mayor", "lob_l2"})
        filters = (*filters, branch)
    elif population:
        # A model's inferred branch must not override an employee's named LOB.
        filters = tuple(item for item in filters if not (
            item.get("field") in {"ramo", "nombre_ramo_comercial", "ramo_comercial"}
            and normalized(item.get("value")) == normalized(population["label"])))
    if re.search(r"\b(?:sin filtros|quita (?:los )?filtros)\b", text):
        filters = ()
        population = {}
    return AnalysisSpec(keys, start, end, grain, tuple(dict.fromkeys(group)), filters,
                        population or {}, basis, notes, today.isoformat())
