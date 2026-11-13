#!/usr/bin/env python3
"""Measure local orchestration only; no model, network, SQL or GCP credentials."""
from __future__ import annotations

import argparse
import json
import math
import statistics
import tempfile
import time

from analytics_agent.agent import AnalyticsAgent
from analytics_agent.memory import SessionStore
from analytics_agent.models import AgentSettings
from analytics_agent.schema import SchemaCatalog


class Provider:
    def next_action(self, question, **kwargs):
        return {'action': 'finish', 'answer': 'Hola', 'notes': '', 'assumptions': [],
                'summary_table': None, 'present_rows': False}, {'total_tokens': 0}


class NoBigQuery:
    def __getattr__(self, name):
        raise AssertionError(f'El benchmark no permite BigQuery: {name}')


def measure(engine, samples):
    results = []
    for index in range(samples + 2):
        with tempfile.TemporaryDirectory() as folder, SessionStore(folder) as memory:
            agent = AnalyticsAgent(catalog=SchemaCatalog(tables={}), llm=Provider(), bigquery=NoBigQuery(),
                settings=AgentSettings('https://benchmark.invalid', 'simulated', '', engine=engine))
            started = time.perf_counter()
            answer = agent.answer('Hola', session_store=memory)
            elapsed = (time.perf_counter() - started) * 1000
            if answer.status != 'complete':
                raise AssertionError('El benchmark no terminó')
            if index >= 2:
                results.append(elapsed)
    return {'median_ms': round(statistics.median(results), 2),
            'p95_ms': round(sorted(results)[math.ceil(.95 * samples)-1], 2), 'samples': samples}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--samples', type=int, default=30)
    args = parser.parse_args()
    if args.samples < 5:
        parser.error('Usa al menos 5 muestras')
    result = {engine: measure(engine, args.samples) for engine in ('legacy', 'langgraph')}
    result['p95_overhead_ms'] = round(result['langgraph']['p95_ms'] - result['legacy']['p95_ms'], 2)
    print(json.dumps(result, indent=2))


if __name__ == '__main__':
    main()
