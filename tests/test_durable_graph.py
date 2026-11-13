"""Integration coverage for persisted graph stages and real adapter job identities."""
import tempfile
import types
import unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch

from analytics_agent.bigquery_adapter import BigQueryAdapter, DryRunResult, QueryPage, bigquery_request_scope, current_request_id, stable_query_job_id
from analytics_agent.execution_store import ExecutionStore
from analytics_agent.memory import SessionStore
from analytics_agent.durable_graph import encode, decode, query_operation_index, DurableQueries, Payloads
from analytics_agent.models import AgentAnswer, SummaryTable
from tests.test_agent import action, make_agent, StubLLM, StubBigQuery, SQL


class DurableGraphTests(unittest.TestCase):
    def test_initial_snapshot_failure_releases_lease(self):
        with tempfile.TemporaryDirectory() as folder, SessionStore(folder, owner='alice') as memory:
            agent = make_agent(StubLLM([]), StubBigQuery())
            with patch.object(Payloads, 'put', side_effect=OSError('disk unavailable')):
                with self.assertRaisesRegex(OSError, 'disk unavailable'):
                    agent.answer('Hola', session_store=memory)
            with ExecutionStore(folder, owner='alice') as executions:
                run = executions.list()[0]['run_id']
                self.assertEqual(0, executions.get(run)['lease_until'])
                self.assertEqual('failed', executions.get(run)['status'])

    def test_query_replay_survives_auxiliary_cache_hit(self):
        class Adapter:
            client = types.SimpleNamespace(project='billing')
            location = 'us-east1'
            calls = 0
            def resume_query(self, *args, **kwargs):
                raise AssertionError('A completed query must use stored pages')
            def execute(self, sql, *, parameters=(), location=''):
                self.calls += 1
                return QueryPage('ephemeral', ('n',), ({'n': 1},), False, 1, 1,
                    stable_query_job_id(current_request_id(), sql, parameters), 'billing', location)

        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner='alice') as store:
            run = store.create(session_id='session', turn_id='turn', versions={})
            store.acquire(run, 'worker')
            adapter = Adapter()
            first = DurableQueries(adapter, Payloads(store, run, 'worker'), 'billing')
            first.action_index = 2
            first.execute('SELECT 1 AS auxiliary', location='us-east1')
            result = first.execute('SELECT 1 AS n', location='us-east1')
            second = DurableQueries(adapter, Payloads(store, run, 'worker'), 'billing')
            second.action_index = 2
            # The auxiliary lookup is now cached and is deliberately skipped.
            self.assertEqual(result, second.execute('SELECT 1 AS n', location='us-east1'))
            self.assertEqual(2, adapter.calls)

    def test_external_request_redelivery_returns_original_answer(self):
        with tempfile.TemporaryDirectory() as folder, SessionStore(folder, owner='alice') as memory:
            provider = StubLLM([action('finish', answer='Hola')])
            agent = make_agent(provider, StubBigQuery())
            with bigquery_request_scope('teams-request-1'):
                first = agent.answer('Hola', session_store=memory)
            count = len(provider.calls)
            with bigquery_request_scope('teams-request-1'):
                second = agent.answer('Hola', session_store=memory)
            self.assertEqual(first, second)
            self.assertEqual(count, len(provider.calls))
            self.assertEqual(1, len(memory.list_sessions()))

    def test_restart_after_model_failure_reuses_previous_result_and_completed_answer(self):
        class LLM(StubLLM):
            fail = True
            def next_action(self, question, **kwargs):
                if self.plan_sent and not self.actions and self.fail:
                    raise RuntimeError('simulated provider outage')
                return super().next_action(question, **kwargs)

        bq = StubBigQuery()
        llm = LLM([action('run_select', sql=SQL)])
        with tempfile.TemporaryDirectory() as folder:
            with SessionStore(folder, owner='alice') as memory:
                agent = make_agent(llm, bq)
                with self.assertRaisesRegex(RuntimeError, 'provider outage'):
                    agent.answer('Resumen de enero de 2026', session_store=memory)
                with ExecutionStore(folder, owner='alice') as executions:
                    run = executions.list()[0]['run_id']
                self.assertEqual(1, bq.executions)
            # Fresh agent, memory and provider objects model a restarted process.
            provider = StubLLM([action('finish', answer='Resumen recuperado')])
            provider.plan_sent = True
            with SessionStore(folder, owner='alice') as memory:
                agent = make_agent(provider, bq)
                result = agent.resume(run, session_store=memory)
                self.assertEqual(run, result.run_id)
                self.assertEqual('langgraph', result.engine)
                self.assertEqual(1, bq.executions)
                calls = len(provider.calls)
                repeated = agent.resume(run, session_store=memory)
                self.assertEqual(result, repeated)
                self.assertEqual(calls, len(provider.calls))
            with SessionStore(folder, owner='bob') as memory:
                with self.assertRaisesRegex(ValueError, 'no pertenece'):
                    agent.resume(run, session_store=memory)

    def test_persisted_clarification_can_resume_with_new_agent(self):
        with tempfile.TemporaryDirectory() as folder:
            with SessionStore(folder, owner='alice') as memory:
                agent = make_agent(StubLLM([action('clarify', clarification={
                    'question': '¿Qué periodo?', 'metric_key': '', 'proposed_rule': ''})]), StubBigQuery())
                pending = agent.answer('Necesito un resumen', session_store=memory)
                self.assertEqual('needs_clarification', pending.status)
            provider = StubLLM([action('finish', answer='Se usará enero de 2026')])
            provider.plan_sent = True
            with SessionStore(folder, owner='alice') as memory:
                agent = make_agent(provider, StubBigQuery())
                result = agent.resume(pending.run_id, session_store=memory, clarification_response='Enero de 2026')
                self.assertEqual(pending.turn_id, result.turn_id)
                self.assertEqual('complete', result.status)

    def test_version_change_blocks_resume_before_another_model_call(self):
        with tempfile.TemporaryDirectory() as folder, SessionStore(folder, owner='alice') as memory:
            agent = make_agent(StubLLM([]), StubBigQuery())
            with self.assertRaises(IndexError):
                agent.answer('Resumen', session_store=memory)
            with ExecutionStore(folder, owner='alice') as executions:
                run = executions.list()[0]['run_id']
            agent.settings = replace(agent.settings, llm_model='different')
            with self.assertRaisesRegex(ValueError, 'versiones distintas'):
                agent.resume(run, session_store=memory)

    def test_typed_answer_roundtrip_preserves_numeric_and_table_types(self):
        answer = AgentAnswer('ok', summary_table=SummaryTable('table', ('value',), ({'value': 7},)),
                             run_id='r', engine='langgraph')
        self.assertEqual(answer, decode(encode(answer)))
        source = {'$type': 'datetime', 'value': 'arbitrary user data', 'nested': {'$type': 'unknown'}}
        self.assertEqual(source, decode(encode(source)))

    def test_real_adapter_collects_pages_under_stable_job_identity(self):
        class Page(list):
            def __init__(self, rows, more):
                super().__init__(rows)
                self.next_page_token = 'more' if more else None

        class Job:
            project, location, total_bytes_processed = 'sbscol-dbreplication-prd', 'us-east1', 4096
            schema = [types.SimpleNamespace(name='ramo', field_type='STRING'), types.SimpleNamespace(name='prima', field_type='INTEGER')]
            def __init__(self, sql, job_id):
                self.query, self.job_id = sql, job_id
            def result(self, *, page_size, start_index=0):
                rows = [{'ramo': 'AUTO', 'prima': 100}, {'ramo': 'VIDA', 'prima': 200}][start_index:]
                return types.SimpleNamespace(schema=self.schema, total_rows=2,
                    pages=iter([Page([r], i < len(rows)-1) for i, r in enumerate(rows)]))

        class Client:
            project = 'sbscol-dbreplication-prd'
            def __init__(self):
                self.jobs = {}
            def query(self, sql, *, job_id, **kwargs):
                if job_id in self.jobs:
                    raise AssertionError('unexpected duplicate submission')
                self.jobs[job_id] = Job(sql, job_id)
                return self.jobs[job_id]
            def get_job(self, job_id, **kwargs):
                return self.jobs[job_id]

        class Adapter(BigQueryAdapter):
            fail_next_page = False
            def inspect_table_metadata(self, table_id):
                return StubBigQuery().inspect_table_metadata(table_id)
            def dry_run(self, sql, **kwargs):
                return DryRunResult(True, 2048)
            def read_page(self, query_id):
                if self.fail_next_page:
                    self.fail_next_page = False
                    raise RuntimeError('page connection lost')
                return super().read_page(query_id)

        for lose_connection in (False, True):
            with self.subTest(lose_connection=lose_connection):
                client = Client()
                with tempfile.TemporaryDirectory() as folder, SessionStore(folder, owner='alice') as memory:
                    adapter = Adapter(client)
                    adapter.fail_next_page = lose_connection
                    agent = make_agent(StubLLM([action('run_select', sql=SQL), action('finish', answer='Resumen')]), adapter)
                    if lose_connection:
                        with self.assertRaisesRegex(RuntimeError, 'se puede reanudar'):
                            agent.answer('Resumen de enero de 2026', session_store=memory)
                        with ExecutionStore(folder, owner='alice') as executions:
                            run = executions.list()[0]['run_id']
                            index = executions.connection.execute('SELECT operation_index FROM query_operations WHERE run_id=?', (run,)).fetchone()[0]
                            self.assertEqual(1, executions.query_result(run, index)['rows_saved'])
                        provider = StubLLM([action('finish', answer='Resumen')])
                        provider.plan_sent = True
                        agent = make_agent(provider, Adapter(client))
                        result = agent.resume(run, session_store=memory)
                    else:
                        result = agent.answer('Resumen de enero de 2026', session_store=memory)
                    self.assertEqual(1, len(client.jobs))
                    self.assertEqual(2, len(result.summary_table.rows))
                    with ExecutionStore(folder, owner='alice') as executions:
                        # Plan is decision 1; the financial query belongs to decision 2.
                        index = executions.connection.execute('SELECT operation_index FROM query_operations WHERE run_id=?', (result.run_id,)).fetchone()[0]
                        stored = executions.query_result(result.run_id, index)
                        self.assertEqual(2, stored['rows_saved'])
                        self.assertEqual('completed', stored['status'])


if __name__ == '__main__':
    unittest.main()
