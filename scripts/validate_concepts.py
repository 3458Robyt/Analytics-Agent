#!/usr/bin/env python3
"""Run the versioned business and conversation corpus in an isolated Workbench session."""
from __future__ import annotations

import argparse
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime
from hashlib import sha256
from unittest.mock import patch
from zoneinfo import ZoneInfo
from pathlib import Path
from tempfile import TemporaryDirectory
from time import perf_counter

from dotenv import load_dotenv
from rich.console import Console

from analytics_agent.llm import load_default_agent_system_prompt
from analytics_agent.analysis_sql import ROUTINE_ID, routine_roles
from analytics_agent.bigquery_adapter import DryRunResult, QueryPage
from analytics_agent.cli import RuntimeConfig, _build_agent
from analytics_agent.memory import SessionStore
from analytics_agent.organization import normalized
from scripts.validate_workbench import (
    BudgetClient, MAX_BYTES, NET, PREMIUMS, compare, failure_status, write_report,
)

LIVE = {1, 3, 7, 11, 16, *range(21, 33), 35, 36, 41, 45, 49, 50, 51, 54, 81, *range(91, 101)}
PENDING = {41, 42, 45, 47, 48, 50}
FIXTURE_DIMENSIONS = {
    2: ('nombre_sucursal', 'sucursal'),
    5: ('nombre_ramo_comercial', 'ramo'),
    55: ('vicepresidencia', 'vicepresidencia'),
    61: ('nombre_intermediario', 'intermediario'),
    63: ('canal', 'canal'),
    64: ('nueva_renovacion', 'tipo_negocio'),
}


@dataclass(frozen=True)
class Case:
    number: int
    question: str
    criterion: str

    @property
    def lane(self):
        return 'live' if self.number in LIVE else 'fixture'


@dataclass(frozen=True)
class Reference:
    sql: str
    keys: tuple[str, ...] = ()
    table: str = ''


def corpus():
    path = Path(__file__).with_name('validation') / 'corpus.tsv'
    return [Case(i, *line.split('\t')) for i, line in enumerate(path.read_text().splitlines(), 1)]


def references(zone, earned=None):
    def premium(start='2026-10-01', end='2026-11-01', extra='', group=None):
        field, alias = group or ('', '')
        select = f'{field} AS {alias}, ' if group else ''
        suffix = f' GROUP BY {alias}' if group else ''
        return Reference(f"SELECT {select}SUM(vrprima) AS prima_emitida FROM `{PREMIUMS}` "
                         f"WHERE fecha_emision >= DATE '{start}' AND fecha_emision < DATE '{end}' {extra}{suffix}",
                         (alias,) if group else (), PREMIUMS)

    def payment(month='10', *, field='pago_gross', date='fecha_pago', group=None, label='', interval=None, select=None):
        start, end = interval or (('2026-09-01', '2026-10-01') if month == '09' else ('2026-10-01', '2026-11-01'))
        source, alias = group or ('', '')
        dimensions = f'{source} AS {alias}, ' if group else ''
        suffix = f' GROUP BY {alias}' if group else ''
        types = {
            'honorarios': "('HONORARIOS', 'HONORARIO', 'HONORARIOS Y GASTOS')",
            'indemnizaciones': "('INDEMNIZACIONES', 'INDEMNIZACION', 'INDEMNIZACIÓN')",
        }
        category = f' AND UPPER(TRIM(cod_estimacion)) IN {types[label]}' if label else ''
        select = select or f'SUM({field}) AS {label or "pagos_siniestros"}'
        return Reference(f"SELECT {dimensions}{select} FROM `{NET}` "
                         f"WHERE DATE({date}, '{zone}') >= DATE '{start}' AND DATE({date}, '{zone}') < DATE '{end}'"
                         f"{category}{suffix}", (alias,) if group else (), NET)

    start, end = '2026-10-03', '2026-10-13'
    ramo = ('nombre_ramo_comercial', 'ramo')
    suc = ('nombre_sucursal', 'sucursal')
    quincena = Reference(
        f"SELECT nombre_sucursal AS sucursal, "
        f"SUM(IF(EXTRACT(DAY FROM fecha_emision) <= 15, vrprima, NULL)) AS prima_emitida_base, "
        f"SUM(IF(EXTRACT(DAY FROM fecha_emision) > 15, vrprima, NULL)) AS prima_emitida_actual "
        f"FROM `{PREMIUMS}` WHERE fecha_emision >= DATE '2026-10-01' AND fecha_emision < DATE '2026-11-01' "
        f"GROUP BY sucursal", ('sucursal',), PREMIUMS)
    refs = {
        'issued': premium(), 'issued_branch': premium('2026-09-01', '2026-10-01', group=suc),
        'issued201': premium('2026-10-01', '2026-10-16', 'AND cod_ramo_com = 201'),
        'issued_ramo': premium('2026-10-15', '2026-10-16', group=ramo),
        'issued_cross': premium('2026-09-28', '2026-10-04'),
        'seq201': premium(start, end, 'AND cod_ramo_com = 201'),
        'seq202': premium(start, end, 'AND cod_ramo_com = 202'),
        'seq_ramo': premium(start, end, group=ramo), 'seq_suc': premium(start, end, group=suc),
        'seq_total': premium(start, end), 'seq_quincena': quincena,
        'copro': premium(extra='AND cod_subprofit_loc = 34'),
        'pay_oct': payment(), 'pay_sep': payment('09'), 'pay_zero': payment('09'),
        'pay_empty': payment(interval=('1900-01-01', '1900-01-02')),
        'net_oct': payment(field='pago_neto'), 'net_sep': payment('09', field='pago_neto'),
        'acc_sep': payment('09', date='fecha_accidente'),
        'acc_net': payment('09', date='fecha_accidente', field='pago_neto'),
        'pay_ramo': payment(group=('ramo_comercial', 'ramo')),
        'area': payment(group=('lob_l2', 'linea_mayor')),
        'indemn': payment('09', label='indemnizaciones'), 'fees': payment(label='honorarios'),
        'mixed_pending': payment(),
        'pay_count': payment(select='COUNTIF(pago_gross != 0) AS registros'),
        'pay_mix': payment(select=(
            "SUM(IF(UPPER(TRIM(cod_estimacion)) IN ('HONORARIOS','HONORARIO','HONORARIOS Y GASTOS'), pago_gross,NULL)) AS honorarios,"
            "SUM(IF(UPPER(TRIM(cod_estimacion)) IN ('INDEMNIZACIONES','INDEMNIZACION','INDEMNIZACIÓN'), pago_gross,NULL)) AS indemnizaciones"
        )),
    }
    if earned:
        refs['earned'] = Reference(earned, table=PREMIUMS)
        refs['ratio'] = Reference(
            f"SELECT SAFE_DIVIDE(p.pagos_siniestros,e.prima_devengada)*100 AS siniestralidad_pagos "
            f"FROM ({refs['pay_oct'].sql}) p CROSS JOIN ({earned}) e")
    return refs


def earned_reference(adapter, zone):
    roles = routine_roles(adapter.inspect_routine_metadata(ROUTINE_ID))
    fields, _ = adapter.inspect_table(PREMIUMS)
    def day(field):
        return f"DATE({field}, '{zone}')" if fields.get(field) == 'TIMESTAMP' else f'CAST({field} AS DATE)'
    start, end = day('fecha_iniciovig'), day('fecha_finvig')
    values = {'start_coverage': start, 'end_coverage': end, 'start_analysis': "DATE '2026-10-01'",
              'end_analysis': "DATE '2026-10-31'", 'premium': 'vrprima'}
    args = ', '.join(values[r] for r in roles)
    return (f"SELECT SUM(`{ROUTINE_ID}`({args})) AS prima_devengada FROM `{PREMIUMS}` "
            f"WHERE {start}<DATE '2026-11-01' AND {end}>=DATE '2026-10-01'")


class ControlledBigQuery:
    location = 'us-east1'

    def __init__(self, catalog):
        self.catalog = catalog
        self.case = None
        self.calls = []

    def inspect_table_metadata(self, table_id):
        table = self.catalog.tables[table_id.lower()]
        return {'table_id': table_id, 'actual_schema': {n: {'name': n, 'data_type': c.data_type, 'description': c.description}
                                                  for n, c in table.columns.items()},
                'location': self.location, 'table_type': 'TABLE'}

    def dry_run(self, sql, **kwargs):
        return DryRunResult(True, 2**31 if self.case.number == 88 else 1024)

    def execute(self, sql, **kwargs):
        self.calls.append(sql)
        criterion = self.case.criterion
        if criterion not in {'issued', 'issued_branch', 'pay_oct', 'all_history'}:
            raise AssertionError('Consulta fuera del caso controlado')
        metric = 'pagos_siniestros' if criterion in {'pay_oct', 'all_history'} else 'prima_emitida'
        field = 'pago_gross' if metric == 'pagos_siniestros' else 'vrprima'
        if not re.search(r'SUM\s*\(\s*' + field, sql.replace('`', ''), re.I):
            raise AssertionError(f'Se esperaba SUM de {field}')
        row = {metric: 125, f'__rows_{metric}': 1, f'__values_{metric}': 1}
        if self.case.number in FIXTURE_DIMENSIONS:
            dimension, alias = FIXTURE_DIMENSIONS[self.case.number]
            if not re.search(r'\b' + dimension + r'\b', sql, re.I):
                raise AssertionError(f'Falta la dimensión {dimension}')
            match = re.search(r'`?' + dimension + r'`?\s+AS\s+`?(\w+)', sql, re.I)
            row[match[1] if match else alias] = 'Grupo de prueba'
        extra = {4: 'nombre_ramo_comercial', 5: 'cod_ramo_com', 65: 'big_account'}
        required = extra.get(self.case.number)
        if required and not re.search(r'\b' + required + r'\b', sql, re.I):
            raise AssertionError(f'Falta validar el filtro {required}')
        return QueryPage(str(self.case.number), tuple(row), (row,), False, 1, 1024)


def assert_answer(case, answer, reference, rows, controlled):
    key = case.criterion
    text = normalized(answer.answer + ' ' + ' '.join(answer.assumptions))
    if case.number in PENDING or key == 'mixed_pending':
        if answer.status != 'partial' or 'corte' not in text:
            raise AssertionError('Debe identificar el corte mensual pendiente')
        if case.number in PENDING and (answer.query_count or answer.summary_table):
            raise AssertionError('Se entregó o consultó una cifra de saldos sin corte')
    if reference:
        table = answer.summary_table
        if table is None:
            raise AssertionError('Falta tabla de resultado')
        if key == 'pay_count':
            cells = [v for row in table.rows for v in row.values()]
            if rows[0]['registros'] not in cells:
                raise AssertionError('El conteo no coincide con COUNTIF independiente')
        else:
            compare(rows, table.rows, reference.keys)
        if key == 'pay_zero' and 'cero' not in text:
            raise AssertionError('Falta distinguir cero de ausencia de registros')
    elif controlled and key in {'issued', 'issued_branch', 'pay_oct', 'all_history'}:
        metric = 'pagos_siniestros' if key in {'pay_oct', 'all_history'} else 'prima_emitida'
        if not answer.summary_table or answer.summary_table.rows[0].get(metric) != 125:
            raise AssertionError('El importe controlado de 125 no se presentó')
    elif key == 'ask_period':
        if answer.status != 'needs_clarification' or controlled.calls:
            raise AssertionError('Debe pedir periodo antes de ejecutar el SQL')
    elif case.number == 116:
        if controlled.calls or answer.query_count:
            raise AssertionError('La solicitud de escritura llegó al adaptador')
        if not any(v in text for v in ('solo lectura', 'no puedo', 'no se permite')):
            raise AssertionError('La respuesta debe indicar la restricción de escritura')
    elif key == 'claims_sources':
        if not any(v in text for v in ('concili', 'cobertura', 'confirm')) or 'mm_final' not in text:
            raise AssertionError('Debe identificar las fuentes y la conciliación pendiente')
    elif key not in {'pending', 'mixed_pending'}:
        missing = [part for part in key.split('|') if normalized(part) not in text]
        if missing:
            raise AssertionError('Faltan hechos de la rúbrica: ' + ', '.join(missing))


class FixtureClock(datetime):
    @classmethod
    def now(cls, tz=None):
        reference = datetime(2026, 11, 17, 12, tzinfo=ZoneInfo('America/Bogota'))
        return reference.astimezone(tz) if tz else reference.replace(tzinfo=None)


def run(args):
    load_dotenv(args.env_file)
    os.environ.pop('ANALYTICS_AGENT_CLAIMS_PERIOD_FILE', None)
    config = RuntimeConfig.from_env()
    config = replace(config, settings=replace(config.settings, engine='langgraph'))
    agent, adapter = _build_agent(config, Console(stderr=True))
    budget = BudgetClient(adapter.client, args.budget_bytes)
    adapter.client = budget
    reference_error = None
    try:
        earned = earned_reference(adapter, config.settings.timezone) if args.lane == 'live' else None
    except Exception as exc:
        earned = None
        reference_error = str(exc)
    refs = references(config.settings.timezone, earned)
    controlled = ControlledBigQuery(agent.catalog) if args.lane == 'fixture' else None
    if controlled:
        agent.bigquery = controlled
    report = {'status': 'running', 'lane': args.lane, 'cases': [], 'model': config.settings.llm_model,
              'financial_status': 'pending_reconciliation',
              'runner_sha256': sha256(Path(__file__).read_bytes()).hexdigest(),
              'corpus_sha256': sha256((Path(__file__).with_name('validation') / 'corpus.tsv').read_bytes()).hexdigest(),
              'prompt_sha256': sha256(load_default_agent_system_prompt().encode()).hexdigest()}
    cache = {}
    selected = set(args.case)
    if selected & set(range(92, 97)):
        selected.update(range(91, max(selected & set(range(92, 97))) + 1))
    if selected & set(range(98, 101)):
        selected.update(range(97, max(selected & set(range(98, 101))) + 1))
    if selected & {89, 90}:
        selected.add(88)
    zone = config.settings.bigquery_location or 'us-east1'
    with TemporaryDirectory(prefix='concept-validation-') as folder, SessionStore(folder) as memory:
        for case in corpus():
            if case.lane != args.lane or (selected and case.number not in selected):
                continue
            if case.number not in {*range(92, 97), *range(98, 101), 89}:
                session = memory.create_session(case.question)
            result = {'id': case.number, 'question': case.question, 'criterion': case.criterion}
            report['cases'].append(result)
            start = perf_counter()
            try:
                if controlled:
                    controlled.case = case
                    controlled.calls.clear()
                ref = refs.get(case.criterion) if args.lane == 'live' else None
                if case.criterion in {'earned', 'ratio'} and not ref:
                    result.update(status='reference_pending', reason=reference_error)
                    continue
                if case.number == 90:
                    controlled.case = corpus()[87]
                    agent.answer(controlled.case.question, session_store=memory, session_id=session)
                    controlled.case = case
                rows = []
                if ref:
                    if ref.sql not in cache:
                        job = budget.query(ref.sql, location=zone)
                        cache[ref.sql] = ([dict(row) for row in job.result()], job.job_id)
                    rows, result['reference_job'] = cache[ref.sql]
                    result['reference_sql'] = ref.sql
                    if case.criterion == 'seq_quincena':
                        for row in rows:
                            base, actual = row['prima_emitida_base'], row['prima_emitida_actual']
                            delta = None if base is None or actual is None else actual - base
                            row['prima_emitida_diferencia'] = delta
                            row['prima_emitida_variacion_pct'] = None if not base or delta is None else delta/base*100
                pending = memory.pending_turn(session) if case.number in {89, 90} else ''
                answer = agent.answer(case.question, session_store=memory, session_id=session,
                                      pending_turn_id=pending, clarification_response=case.question if pending else '')
                result.update(answer=answer.answer, assumptions=answer.assumptions, result_status=answer.status,
                              audit=answer.audit, timings=answer.timings, usage=answer.usage)
                if controlled:
                    result['sql'] = controlled.calls[:]
                assert_answer(case, answer, ref, rows, controlled)
                result['status'] = 'passed'
            except Exception as exc:
                result.update(status=failure_status(exc), error_type=type(exc).__name__, reason=str(exc))
            finally:
                result['seconds'] = round(perf_counter() - start, 3)
                report['estimated_bytes_reserved'] = budget.reserved
                write_report(args.report, report)
    report['status'] = 'finished'
    write_report(args.report, report)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--lane', choices=('live', 'fixture'), required=True)
    parser.add_argument('--execute', action='store_true')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--budget-bytes', type=int, default=MAX_BYTES)
    parser.add_argument('--report', required=True)
    parser.add_argument('--case', type=int, action='append', default=[])
    args = parser.parse_args()
    if not args.execute or not 1 <= args.budget_bytes <= MAX_BYTES:
        parser.error('Confirma con --execute y usa hasta 10 GiB')
    if args.lane == 'fixture':
        with patch('analytics_agent.turn_runtime.datetime', FixtureClock):
            run(args)
    else:
        run(args)


if __name__ == '__main__':
    main()
