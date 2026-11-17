#!/usr/bin/env python3
"""Explicit, read-only live acceptance. Includes agent and independent references."""
from __future__ import annotations

import argparse
import copy
import json
import os
import re
import tempfile
import statistics
import subprocess
import sys
from time import perf_counter
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery
from rich.console import Console

from analytics_agent.cli import RuntimeConfig, _build_agent
from analytics_agent.memory import SessionStore
from analytics_agent.guardrails import validate_sql

PREMIUMS = 'centralizacion-datos.analytics_curated.th_primas_final'
MAX_BYTES = 10 * 1024**3
NET = 'centralizacion-datos.analytics_curated.mm_final_netos'


class ValidationBudgetExceeded(RuntimeError):
    pass


class BudgetClient:
    def __init__(self, client, limit):
        self.client, self.limit, self.reserved = client, limit, 0
        self.job_limits = {}

    def __getattr__(self, name):
        return getattr(self.client, name)

    def query(self, sql, *, job_config=None, **kwargs):
        validate_sql(sql)
        cfg = copy.deepcopy(job_config) if job_config else bigquery.QueryJobConfig(use_legacy_sql=False)
        if cfg.destination or cfg.use_legacy_sql:
            raise ValueError('La validación requiere GoogleSQL de solo lectura')
        if not cfg.dry_run:
            identity = kwargs.get('job_id')
            if not identity or identity not in self.job_limits:
                estimate_config = copy.deepcopy(cfg)
                estimate_config.dry_run, estimate_config.use_query_cache = True, False
                estimated = self.client.query(sql, job_config=estimate_config,
                    location=kwargs.get('location')).total_bytes_processed
                if estimated is None or self.reserved + estimated > self.limit:
                    raise ValidationBudgetExceeded('Se alcanzó el presupuesto acumulado de la validación')
                cfg.maximum_bytes_billed = min(cfg.maximum_bytes_billed or self.limit, self.limit - self.reserved)
                self.reserved += estimated
                if identity:
                    self.job_limits[identity] = cfg.maximum_bytes_billed
            else:
                cfg.maximum_bytes_billed = self.job_limits[identity]
        if kwargs.get('job_id'):
            kwargs.setdefault('job_retry', None)
        return self.client.query(sql, job_config=cfg, **kwargs)


def cases():
    october = "fecha_emision >= DATE '2026-10-01' AND fecha_emision < DATE '2026-11-01'"
    comparison = "SUM(IF(EXTRACT(DAY FROM fecha_emision) <= 15, vrprima, NULL)) AS prima_emitida_base, SUM(IF(EXTRACT(DAY FROM fecha_emision) >= 16, vrprima, NULL)) AS prima_emitida_actual"
    return [
        ('septiembre_sucursal', 'Prima emitida por sucursal en septiembre de 2026.',
         f"SELECT nombre_sucursal AS sucursal, SUM(vrprima) AS prima_emitida FROM `{PREMIUMS}` WHERE fecha_emision >= DATE '2026-09-01' AND fecha_emision < DATE '2026-10-01' GROUP BY sucursal", ('sucursal',), False),
        ('ramo_201', 'Total de ventas del ramo comercial código 201 del 1 al 15 de octubre de 2026.',
         f"SELECT SUM(vrprima) AS prima_emitida FROM `{PREMIUMS}` WHERE fecha_emision >= DATE '2026-10-01' AND fecha_emision < DATE '2026-10-16' AND cod_ramo_com=201", (), False),
        ('comparar_quincenas', 'Ahora compara las dos quincenas de octubre de 2026 por sucursal para el ramo 201; incluye diferencia y variación porcentual.',
         f"SELECT nombre_sucursal AS sucursal, {comparison} FROM `{PREMIUMS}` WHERE {october} AND cod_ramo_com=201 GROUP BY sucursal", ('sucursal',), True),
        ('quitar_ramo', 'Quita el filtro de ramo y muestra todos los ramos comparando las dos quincenas de octubre de 2026.',
         f"SELECT nombre_ramo_comercial AS ramo, {comparison} FROM `{PREMIUMS}` WHERE {october} GROUP BY ramo", ('ramo',), True),

    ]


def diagnose(client, location, candidates):
    table = client.get_table(NET)
    fields = {field.name: field.field_type for field in table.schema}
    date_fields = [name for name, kind in fields.items() if kind in {"DATE", "DATETIME", "TIMESTAMP"}]
    selected = list(dict.fromkeys(["fecha_pago", *candidates]))
    if any(field not in date_fields or not re.fullmatch(r"[A-Za-z_][A-Za-z_0-9]*", field) for field in selected):
        raise ValueError("Uno de los campos de diagnóstico no es una fecha del esquema vivo")
    profiles, jobs = {}, {}
    for field in selected:
        zone = "America/Bogota" if field == "fecha_pago" else "UTC"
        expression = f"DATE(`{field}`, '{zone}')" if fields[field] == "TIMESTAMP" else f"CAST(`{field}` AS DATE)"
        distinct_key = "COUNT(DISTINCT llave) AS llaves, COUNTIF(llave IS NULL) AS llave_nula," if "llave" in fields else ""
        sql = f"""
        SELECT DATE_TRUNC({expression}, MONTH) AS month, cod_estimacion,
          COUNT(*) AS row_count, {distinct_key}
          COUNTIF(pago_gross IS NULL) AS null_amounts,
          COUNTIF(pago_gross = 0) AS zero_amounts, COUNTIF(pago_gross > 0) AS positive_amounts,
          COUNTIF(pago_gross < 0) AS negative_amounts,
          SUM(pago_gross) AS gross, SUM(pago_neto) AS net
        FROM `{NET}`
        WHERE {expression} >= DATE '2026-09-01' AND {expression} < DATE '2026-11-01'
        GROUP BY month, cod_estimacion ORDER BY month, cod_estimacion
        """
        job = client.query(sql, location=location)
        profiles[field] = [dict(row) for row in job.result()]
        jobs[field] = {"job_id": job.job_id, "sql": sql}
    legacy = "centralizacion-datos.analytics_curated.mm_final"
    old_schema = {field.name: field.field_type for field in client.get_table(legacy).schema}
    if {"fecha_pago", "cod_tipo", "pago_moneda_local"} <= old_schema.keys():
        date = "DATE(fecha_pago, 'America/Bogota')" if old_schema["fecha_pago"] == "TIMESTAMP" else "CAST(fecha_pago AS DATE)"
        sql = f"""
        SELECT DATE_TRUNC({date}, MONTH) AS month, COUNT(*) AS row_count,
               COUNTIF(pago_moneda_local != 0) AS nonzero_count, SUM(pago_moneda_local) AS gross
        FROM `{legacy}`
        WHERE CAST(cod_tipo AS STRING) = '4'
          AND {date} >= DATE '2026-09-01' AND {date} < DATE '2026-11-01'
        GROUP BY month ORDER BY month
        """
        job = client.query(sql, location=location)
        profiles["mm_final_legacy"] = [dict(row) for row in job.result()]
        jobs["mm_final_legacy"] = {"job_id": job.job_id, "sql": sql}
    return {"schema": fields, "date_candidates": date_fields, "profiles": profiles, "jobs": jobs,
            "financial_status": "pending_reconciliation"}


def claims_cases(contract, table, timezone):
    if not contract:
        return
    schema = {field.name: field.field_type for field in table.schema}
    for start, end, month in (("2026-09-01", "2026-10-01", "septiembre"),
                              ("2026-10-01", "2026-11-01", "octubre")):
        if not contract["start"] <= start < end <= contract["end"]:
            continue
        time_type = schema["fecha_pago"]
        paid = f"DATE(fecha_pago, '{timezone}')" if time_type == "TIMESTAMP" else "CAST(fecha_pago AS DATE)"
        predicate = f"{paid} >= DATE '{start}' AND {paid} < DATE '{end}'"
        balance = ""
        if contract["mode"] != "historical_movements":
            field = contract["period_field"]
            expression = f"CAST(`{field}` AS DATE)"
            balance = f"{expression} >= DATE '{start}' AND {expression} < DATE '{end}'"
            predicate += " AND " + balance
        specs = [
            ("pagos_siniestros", "Pagos brutos", "pago_gross", predicate),
            ("pagos_siniestros", "Pagos netos", "pago_neto", predicate),
            ("indemnizaciones", "Indemnizaciones brutas", "pago_gross",
             predicate + " AND UPPER(TRIM(cod_estimacion)) IN ('INDEMNIZACIONES', 'INDEMNIZACIÓN', 'INDEMNIZACION')"),
            ("honorarios", "Honorarios brutos", "pago_gross",
             predicate + " AND UPPER(TRIM(cod_estimacion)) IN ('HONORARIOS', 'HONORARIO', 'HONORARIOS Y GASTOS')"),
        ]
        if balance:
            specs.extend([
                ("reservas_avisadas", "Reservas actuales brutas", "reserva_gross", balance),
                ("reservas_avisadas", "Reservas actuales netas", "reserva_actual_neto", balance),
                ("variacion_reservas", "Variación neta de reservas", "variacion_neto", balance),
                ("incurrido_siniestros", "Incurrido neto", "incurrido_neto", balance),
                ("incurrido_siniestros", "Incurrido bruto", "pago_gross + reserva_gross - reserva_gross_anterior", balance),
            ])
        for key, title, amount, where in specs:
            yield (f"{month} · {title}", f"{title} en {month} de 2026.",
                   f"SELECT SUM({amount}) AS {key} FROM `{NET}` WHERE {where}", (), False)


def compare(expected, actual, keys):
    detail = [row for row in actual if not any(row.get(k) in ("Total general", "Total del mes") for k in keys)]
    totals = [row for row in actual if any(row.get(k) == "Total general" for k in keys)]
    if keys and expected:
        if len(totals) != 1:
            raise AssertionError("Falta un único total general en el desglose")
        total = totals[0]
        for field in set(expected[0]) - set(keys):
            if field.endswith(("_variacion_pct", "_diferencia")):
                continue
            values = [Decimal(str(row[field])) for row in expected if row[field] is not None]
            actual_value = total.get(field)
            if (not values and actual_value is not None) or (
                values and (actual_value is None or abs(Decimal(str(actual_value)) - sum(values)) > Decimal("0.01"))
            ):
                raise AssertionError(f"El total general no coincide para {field}")
    indexed = {tuple(row.get(k) for k in keys): row for row in detail}
    if len(indexed) != len(detail):
        raise AssertionError('El agente devolvió claves duplicadas')
    if set(indexed) != {tuple(row.get(k) for k in keys) for row in expected}:
        raise AssertionError('Las dimensiones del resultado no coinciden')
    for row in expected:
        received = indexed[tuple(row.get(k) for k in keys)]
        for field, value in row.items():
            if field in keys:
                continue
            if field not in received:
                raise AssertionError(f'Falta la columna {field}')
            if value is None:
                if received[field] is not None:
                    raise AssertionError(f'{field}: se esperaba un valor nulo')
            else:
                tolerance = Decimal('0.000001' if field.endswith('variacion_pct') else '0.01')
                if received[field] is None or abs(Decimal(str(value))-Decimal(str(received[field]))) > tolerance:
                    raise AssertionError(f'Diferencia en {field}')


def provisional_claims_cases(zone):
    def paid(month, end):
        return f"DATE(fecha_pago, '{zone}') >= DATE '{month}' AND DATE(fecha_pago, '{zone}') < DATE '{end}'"

    def payment(where, field='pago_gross', key='pagos_siniestros', prefix='', suffix=''):
        return f'SELECT {prefix}SUM({field}) AS {key} FROM `{NET}` WHERE {where}{suffix}'

    sep = paid('2026-09-01', '2026-10-01')
    octo = paid('2026-10-01', '2026-11-01')
    return [
        ('pagos_septiembre', 'Cuánto fueron los pagos brutos de siniestros en septiembre de 2026', payment(sep), (), False),
        ('pagos_octubre_por_ramo', 'Cuánto pagamos en siniestros durante octubre de 2026, por ramo?', payment(octo, prefix='ramo_comercial AS ramo, ', suffix=' GROUP BY ramo'), ('ramo',), False),
        ('total_sin_desglose', 'Y en total cuánto fue?', payment(octo), (), True),
        ('explicar_total', 'Explícame de dónde sale ese número', None, (), True),
        ('pagos_netos', 'Ahora netos', payment(octo, field='pago_neto'), (), True),
        ('indemnizaciones', 'Cuánto pagamos en indemnizaciones durante septiembre de 2026?',
         payment(sep + " AND UPPER(TRIM(cod_estimacion)) IN ('INDEMNIZACIONES','INDEMNIZACION','INDEMNIZACIÓN')", key='indemnizaciones'), (), False),
        ('honorarios', 'Cuánto pagamos en honorarios durante octubre de 2026?',
         payment(octo + " AND UPPER(TRIM(cod_estimacion)) IN ('HONORARIOS','HONORARIO','HONORARIOS Y GASTOS')", key='honorarios'), (), False),
        ('pagos_y_reservas', 'Muéstrame pagos y reservas de septiembre de 2026', payment(sep), (), False),
        ('incurrido_neto', 'Cuál es el incurrido neto en octubre neto de 2026', None, (), True),
        ('aclaracion_fecha_pago', 'fecha_pago', None, (), True),
    ]


def extended_cases(zone):
    def premium(start, end, filter_sql='', group=''):
        dimension = {'nombre_sucursal': 'sucursal', 'nombre_ramo_comercial': 'ramo'}.get(group)
        column = f'{group} AS {dimension}, ' if dimension else ''
        by = f' GROUP BY {dimension}' if dimension else ''
        return (
            f"SELECT {column}SUM(vrprima) AS prima_emitida FROM `{PREMIUMS}` "
            f"WHERE fecha_emision >= DATE '{start}' AND fecha_emision < DATE '{end}' {filter_sql}{by}"
        )

    def claims(start, end, field='pago_gross', date='fecha_pago'):
        return (
            f"SELECT SUM({field}) AS pagos_siniestros FROM `{NET}` "
            f"WHERE DATE({date}, '{zone}') >= DATE '{start}' AND DATE({date}, '{zone}') < DATE '{end}'"
        )

    return [
        ('ventas_201','¿Cuántas ventas en prima emitida tuvo el ramo 201 del 3 al 12 de octubre de 2026?',
         premium('2026-10-03','2026-10-13','AND cod_ramo_com=201'), (), False),
        ('reemplazar_ramo','Cambia el ramo por el código 202 y conserva las mismas fechas.',
         premium('2026-10-03','2026-10-13','AND cod_ramo_com=202'), (), True),
        ('ramo_dimension','Quita el filtro de ramo y desglosa por ramo comercial.',
         premium('2026-10-03','2026-10-13',group='nombre_ramo_comercial'),('ramo',), True),
        ('cambiar_sucursal','Mejor por sucursal en vez de por ramo, mantén las fechas.',
         premium('2026-10-03','2026-10-13',group='nombre_sucursal'),('sucursal',), True),
        ('volver_total','Dame solo el total, sin desglosar por sucursal.',
         premium('2026-10-03','2026-10-13'), (), True),
        ('pagos_ocurrencia','¿Cuánto suman los pagos brutos de siniestros ocurridos en septiembre de 2026, por fecha de ocurrencia?',
         claims('2026-09-01','2026-10-01',date='fecha_accidente'), (), False),
        ('ocurrencia_neto','Dame ahora el valor neto, manteniendo la fecha de ocurrencia.',
         claims('2026-09-01','2026-10-01',field='pago_neto',date='fecha_accidente'), (), True),
        ('cambiar_base_pago','Usa fecha de pago en lugar de fecha de ocurrencia, conservando mes y que sea neto.',
         claims('2026-09-01','2026-10-01',field='pago_neto'), (), True),
        ('pagos_cambiar_mes','¿Y en octubre de 2026 con el mismo criterio?',
         claims('2026-10-01','2026-11-01',field='pago_neto'), (), True),
        ('fecha_sola','Dame la prima emitida por ramo el 15 de octubre de 2026.',
         premium('2026-10-15','2026-10-16',group='nombre_ramo_comercial'),('ramo',),False),
    ]

def check_answer(name, answer, memory, session):
    pending = {'pagos_y_reservas', 'incurrido_neto', 'aclaracion_fecha_pago'}
    status = 'partial' if name in pending else 'complete'
    if answer.status != status:
        raise AssertionError(f'Se esperaba {status}, se obtuvo {answer.status}')
    if name in {'explicar_total', 'incurrido_neto', 'aclaracion_fecha_pago'} and answer.query_count:
        raise AssertionError('El seguimiento debía resolverse sin BigQuery')
    if name == 'explicar_total' and 'SELECT' not in answer.answer.upper():
        raise AssertionError('El seguimiento debe recuperar el SQL auditado')
    if name in pending and 'corte' not in answer.answer.casefold():
        raise AssertionError('Falta indicar el corte pendiente')
    if name in {'incurrido_neto', 'aclaracion_fecha_pago'}:
        get_intent = getattr(memory, 'get_analysis_intent', None)
        intent = (get_intent(session) or {}) if get_intent else {}
        if intent.get('metric_bases', {}).get('incurrido_siniestros') != 'net':
            raise AssertionError('El seguimiento debe conservar la base neta')
    if name in {'pagos_septiembre', 'pagos_octubre_por_ramo', 'total_sin_desglose',
                'pagos_netos', 'indemnizaciones', 'honorarios', 'pagos_y_reservas'}:
        if 'pendiente de conciliación' not in answer.answer.casefold():
            raise AssertionError('Falta informar que la cobertura de pagos es provisional')


def write_report(path, report):
    path = Path(path)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=path.parent, delete=False) as stream:
        json.dump(report, stream, ensure_ascii=False, default=str, indent=2)
        temporary = Path(stream.name)
    temporary.replace(path)


def failure_status(error):
    if isinstance(error, AssertionError):
        return 'regression'
    if isinstance(error, ValidationBudgetExceeded):
        return 'budget_exhausted'
    current = error
    while current is not None:
        if isinstance(current, (TimeoutError, ConnectionError)) or getattr(current, 'code', None) in {
            401, 403, 408, 429, 500, 502, 503, 504,
        } or re.search(r'HTTP (401|403|408|429|5\d\d)\b', str(current)):
            return 'infrastructure'
        current = current.__cause__
    return 'error'


def select_cases(suite, selected):
    if not selected:
        return suite
    unknown = set(selected) - {case[0] for case in suite}
    if unknown:
        raise ValueError(f'Casos desconocidos: {", ".join(sorted(unknown))}')
    keep = set()
    for index, case in enumerate(suite):
        if case[0] in selected:
            keep.add(index)
            while suite[index][4] and index:
                index -= 1
                keep.add(index)
    return [case for index, case in enumerate(suite) if index in keep]


def run_cases(agent, budget, location, suite, report, path):
    with tempfile.TemporaryDirectory(prefix='agent-live-acceptance-') as folder, SessionStore(folder) as memory:
        session = ''
        for name, question, sql, keys, followup in suite:
            if not followup:
                session = memory.create_session(question)
            result = {'name': name, 'status': 'running'}
            report['cases'].append(result)
            try:
                expected = None
                if sql:
                    job = budget.query(sql, location=location)
                    expected = [dict(row) for row in job.result()]
                    result['reference_job_id'] = job.job_id
                    if name in {'comparar_quincenas', 'quitar_ramo'}:
                        for row in expected:
                            before, after = row['prima_emitida_base'], row['prima_emitida_actual']
                            delta = None if before is None or after is None else after - before
                            row['prima_emitida_diferencia'] = delta
                            row['prima_emitida_variacion_pct'] = delta / before * 100 if before and delta is not None else None
                start = perf_counter()
                answer = agent.answer(question, session_store=memory, session_id=session)
                result.update(seconds=round(perf_counter() - start, 3), result_status=answer.status,
                              timings=answer.timings, usage=answer.usage, query_count=answer.query_count,
                              bytes_processed=answer.bytes_processed, jobs=answer.query_jobs, audit=answer.audit)
                check_answer(name, answer, memory, session)
                if expected is not None:
                    if answer.summary_table is None:
                        raise AssertionError('Falta la tabla de resultados')
                    compare(expected, answer.summary_table.rows, keys)
                result['status'] = 'passed'
            except Exception as exc:
                result.update(status=failure_status(exc),
                              error_type=type(exc).__name__, error=str(exc))
            report['estimated_bytes_reserved'] = budget.reserved
            write_report(path, report)


def compare_reports(baseline, candidate):
    base = {case['name']: case for case in baseline['cases'] if case['status'] == 'passed'}
    common = [(base[c['name']], c) for c in candidate['cases']
              if c['status'] == 'passed' and c['name'] in base]
    return {
        'common_passed': len(common),
        'slower_cases': [after['name'] for before, after in common
                         if after['seconds'] - before['seconds'] > max(2, .2 * before['seconds'])],
        'new_failures': [c['name'] for c in candidate['cases'] if c['status'] != 'passed' and c['name'] in base],
        'median_seconds': {
            label: statistics.median(pair[index]['seconds'] for pair in common) if common else None
            for index, label in enumerate(('baseline', 'candidate'))
        },
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='Confirma las consultas de lectura')
    parser.add_argument('--diagnose', action='store_true', help='Perfila siniestros sin llamar al modelo')
    parser.add_argument('--extended', action='store_true', help='Añade los seguimientos de la batería ampliada')
    parser.add_argument('--case', action='append', default=[], help='Selecciona casos y sus turnos previos')
    parser.add_argument('--period-candidate', action='append', default=[])
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--report', default='workbench-validation.json')
    parser.add_argument('--baseline', type=Path, help='Checkout aislado del commit de main')
    parser.add_argument('--budget-bytes', type=int, default=MAX_BYTES)
    parser.add_argument('--reviewed-claims', action='store_true', help='Valida adicionalmente el contrato temporal revisado')
    args = parser.parse_args()
    if not args.execute or not 0 < args.budget_bytes <= MAX_BYTES:
        parser.error('Añade --execute y usa un presupuesto entre 1 byte y 10 GiB')
    if args.baseline:
        baseline = args.baseline.resolve()
        if args.diagnose or args.reviewed_claims or args.budget_bytes < 2 or not (baseline / 'src/analytics_agent').is_dir():
            parser.error('--baseline necesita un checkout válido y la batería técnica')
        return run_comparison(args, baseline)
    load_dotenv(args.env_file)
    budget = None
    report = {'status': 'running', 'cases': [], 'maximum_estimated_bytes': args.budget_bytes}
    try:
        if args.diagnose:
            location = os.environ.get('BQ_LOCATION') or 'us-east1'
            client = bigquery.Client(project=os.environ.get('BQ_JOB_PROJECT') or 'sbscol-dbreplication-prd')
            budget = BudgetClient(client, args.budget_bytes)
            report.update(diagnose(budget, location, args.period_candidate))
            report['status'] = 'diagnostic_only'
        else:
            if args.reviewed_claims:
                from analytics_agent.claims_period import load_claims_period
                contract = load_claims_period()
                if not contract:
                    raise ValueError('Configura un contrato temporal revisado')
            else:
                os.environ.pop('ANALYTICS_AGENT_CLAIMS_PERIOD_FILE', None)
                contract = None
            config = RuntimeConfig.from_env()
            config = replace(config, settings=replace(config.settings, engine='langgraph'))
            agent, adapter = _build_agent(config, Console(stderr=True))
            budget = BudgetClient(adapter.client, args.budget_bytes)
            adapter.client = budget
            location = config.settings.bigquery_location or 'us-east1'
            zone = config.settings.timezone
            suite = list(claims_cases(contract, budget.get_table(NET), zone)) if contract else [*cases(), *provisional_claims_cases(zone)]
            if args.extended and not contract:
                suite.extend(extended_cases(zone))
            suite = select_cases(suite, args.case)
            run_cases(agent, budget, location, suite, report, args.report)
            report['status'] = 'passed' if suite and all(c['status'] == 'passed' for c in report['cases']) else 'failed'
            report['financial_status'] = 'pending_reconciliation'
    except Exception as exc:
        report.update(status=failure_status(exc), error_type=type(exc).__name__, error=str(exc))
    report['estimated_bytes_reserved'] = budget.reserved if budget else 0
    write_report(args.report, report)
    print(json.dumps({key: report[key] for key in ('status', 'estimated_bytes_reserved')}))
    return 0 if report['status'] in {'passed', 'diagnostic_only'} else 1


def run_comparison(args, baseline):
    report_path = Path(args.report)
    candidate = Path(__file__).resolve().parents[1]
    reports = {}
    with tempfile.TemporaryDirectory(prefix='agent-comparison-') as folder:
        remaining = args.budget_bytes
        for label, checkout in (('baseline', baseline), ('candidate', candidate)):
            target = Path(folder) / f'{label}.json'
            # Preserve the candidate's share even if the baseline stops unexpectedly.
            limit = remaining // 2 if label == 'baseline' else remaining
            env = dict(os.environ, PYTHONPATH=os.pathsep.join(
                filter(None, (str(checkout / 'src'), os.environ.get('PYTHONPATH')))))
            selected = [item for name in args.case for item in ('--case', name)]
            if args.extended:
                selected.append('--extended')
            child = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--execute',
                                    '--env-file', args.env_file, '--budget-bytes', str(limit),
                                    '--report', str(target), *selected], env=env, check=False)
            data = json.loads(target.read_text()) if target.exists() else {'status': 'error', 'cases': []}
            used = data.get('estimated_bytes_reserved', limit)
            if child.returncode and data.get('status') in {'passed', 'running'}:
                if data['status'] == 'running':
                    used = limit
                data['status'] = 'error'
            remaining -= used
            reports[label] = data
    report = {**reports, 'comparison': compare_reports(reports['baseline'], reports['candidate']),
              'maximum_estimated_bytes': args.budget_bytes,
              'estimated_bytes_reserved': args.budget_bytes - remaining}
    write_report(report_path, report)
    baseline_complete = reports['baseline']['status'] in {'passed', 'failed'} and all(
        case['status'] in {'passed', 'regression'} for case in reports['baseline']['cases'])
    return 0 if reports['candidate']['status'] == 'passed' and baseline_complete else 1


if __name__ == '__main__':
    raise SystemExit(main())
