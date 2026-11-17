#!/usr/bin/env python3
"""Explicit, read-only live acceptance. Includes agent and independent references."""
from __future__ import annotations

import argparse
import copy
import json
import sys
import tempfile
from dataclasses import replace
from decimal import Decimal
from pathlib import Path

from dotenv import load_dotenv
from google.cloud import bigquery
from rich.console import Console

from analytics_agent.cli import RuntimeConfig, _build_agent
from analytics_agent.memory import SessionStore

PREMIUMS = 'centralizacion-datos.analytics_curated.th_primas_final'
CLAIMS = 'centralizacion-datos.analytics_curated.mm_final'
NET = 'centralizacion-datos.analytics_curated.mm_final_netos'


class BudgetClient:
    def __init__(self, client, limit):
        self.client, self.limit, self.reserved = client, limit, 0
        self.jobs = set()

    def __getattr__(self, name):
        return getattr(self.client, name)

    def query(self, sql, *, job_config=None, **kwargs):
        cfg = job_config or bigquery.QueryJobConfig()
        if not cfg.dry_run:
            identity = kwargs.get('job_id')
            if not identity or identity not in self.jobs:
                estimate_config = copy.deepcopy(cfg)
                estimate_config.dry_run, estimate_config.use_query_cache = True, False
                estimated = self.client.query(sql, job_config=estimate_config,
                    location=kwargs.get('location')).total_bytes_processed
                if estimated is None or self.reserved + estimated > self.limit:
                    raise RuntimeError('Se alcanzó el presupuesto acumulado de la validación')
                cfg.maximum_bytes_billed = self.limit - self.reserved
                self.reserved += estimated
                if identity:
                    self.jobs.add(identity)
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
        ('pagos', 'Total de pagos de siniestros de septiembre de 2026 por fecha de accidente, incluyendo todos los pagos.',
         f"SELECT SUM(incurrido_total) AS pagos_siniestros FROM `{CLAIMS}` WHERE tipo='Pagos' AND fecha_accidente >= DATE '2026-09-01' AND fecha_accidente < DATE '2026-10-01'", (), False),
        ('reservas', 'Total de reservas avisadas de septiembre de 2026 usando mes_mmalla.',
         f"SELECT SUM(incurrido_total) AS reservas_avisadas FROM `{CLAIMS}` WHERE tipo='Avisado' AND mes_mmalla >= DATE '2026-09-01' AND mes_mmalla < DATE '2026-10-01'", (), False),
        ('netos', f'Cuenta los registros de `{NET}` de septiembre de 2026 usando mes_mmalla. Devuelve una tabla con el alias registros.',
         f"SELECT COUNT(*) AS registros FROM `{NET}` WHERE mes_mmalla >= DATE '2026-09-01' AND mes_mmalla < DATE '2026-10-01'", (), False),
    ]


def compare(expected, actual, keys):
    indexed = {tuple(row.get(k) for k in keys): row for row in actual}
    if len(indexed) != len(actual):
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


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--execute', action='store_true', help='Confirma las consultas de lectura de esta batería')
    parser.add_argument('--env-file', default='.env')
    parser.add_argument('--report', default='workbench-validation.json')
    args = parser.parse_args()
    if not args.execute:
        parser.error('Revisa los casos del script y añade --execute para ejecutar la batería')
    load_dotenv(args.env_file)
    config = RuntimeConfig.from_env()
    config = replace(config, settings=replace(config.settings, engine='langgraph'))
    agent, adapter = _build_agent(config, Console(stderr=True))
    budget = BudgetClient(adapter.client, 10 * 1024**3)
    adapter.client = budget
    report = {'status': 'running', 'cases': [], 'maximum_estimated_bytes': budget.limit}
    try:
        with tempfile.TemporaryDirectory(prefix='agent-live-acceptance-') as folder, SessionStore(folder) as memory:
            session = ''
            for name, question, reference, keys, followup in cases():
                if not followup:
                    session = memory.create_session(question)
                job = budget.query(reference, job_config=bigquery.QueryJobConfig(use_legacy_sql=False),
                                   location=config.settings.bigquery_location or 'us-east1')
                expected = [dict(row) for row in job.result()]
                if name in {'comparar_quincenas', 'quitar_ramo'}:
                    for row in expected:
                        base, current = row['prima_emitida_base'], row['prima_emitida_actual']
                        row['prima_emitida_diferencia'] = None if base is None or current is None else current-base
                        row['prima_emitida_variacion_pct'] = None if not base or current is None else (current-base)/base*100
                answer = agent.answer(question, session_store=memory, session_id=session)
                if answer.status != 'complete' or answer.summary_table is None:
                    raise AssertionError(f'{name}: no se obtuvo una tabla completa')
                compare(expected, answer.summary_table.rows, keys)
                report['cases'].append({'name': name, 'status': 'passed', 'reference_job_id': job.job_id,
                    'reference_sql': reference, 'run_id': answer.run_id, 'audit': answer.audit,
                    'timings': answer.timings, 'usage': answer.usage})
        report['status'] = 'passed'
    except Exception as exc:
        report['status'], report['error_type'], report['error'] = 'failed', type(exc).__name__, str(exc)
    report['estimated_bytes_reserved'] = budget.reserved
    path = Path(args.report)
    path.write_text(json.dumps(report, ensure_ascii=False, default=str, indent=2))
    path.chmod(0o600)
    print(json.dumps({'status': report['status'], 'report': str(path), 'estimated_bytes_reserved': budget.reserved}))
    return 0 if report['status'] == 'passed' else 1


if __name__ == '__main__':
    raise SystemExit(main())
