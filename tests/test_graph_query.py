import tempfile
import sys
import types
import unittest
from unittest.mock import patch

from analytics_agent.bigquery_adapter import BigQueryAdapter, BigQueryError, QueryPage
from analytics_agent.execution_store import ExecutionStore
from analytics_agent.graph_query import collect_prepared_query


class GraphQueryTests(unittest.TestCase):
    def test_stale_worker_cannot_submit_a_prepared_query(self):
        class Adapter:
            client = types.SimpleNamespace(project='billing')
            def execute(self, *args, **kwargs):
                raise AssertionError('No se debería enviar la consulta')

        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner='a') as store:
            run = store.create(session_id='s', turn_id='t', versions={})
            store.acquire(run, 'one')
            store.prepare_query(run, 'one', operation_index=0, sql='SELECT 1', parameters=[], project='billing', location='us-east1')
            store.release(run, 'one')
            store.acquire(run, 'two')
            with self.assertRaisesRegex(ValueError, 'bloqueo vencido'):
                collect_prepared_query(store, Adapter(), run_id=run, token='one', operation_index=0)

    def test_expired_results_are_not_silently_reexecuted(self):
        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner='a') as store:
            run = store.create(session_id='s', turn_id='t', versions={})
            store.acquire(run, 'worker')
            store.prepare_query(run, 'worker', operation_index=0, sql='SELECT 1', parameters=[], project='billing', location='us-east1')
            store.save_page(run, 'worker', operation_index=0, start_index=0, rows=[{'value': 1}], is_last=True)
            store.update(run, 'worker', status='completed', stage='finished')
            store.release(run, 'worker')
            stamp = store.get(run)['updated_at']
            self.assertEqual({'expired_results': 1, 'deleted_runs': 0},
                             store.prune(result_ttl_seconds=10, run_ttl_seconds=100, now=stamp + 11))
            self.assertEqual([], store.query_result(run, 0)['rows'])
            with self.assertRaisesRegex(ValueError, 'caducaron'):
                collect_prepared_query(store, object(), run_id=run, token='worker', operation_index=0)
            self.assertEqual(1, store.prune(result_ttl_seconds=10, run_ttl_seconds=100, now=stamp + 101)['deleted_runs'])
            self.assertIsNone(store.get(run))

    def test_unknown_submission_outcome_reuses_real_adapter_job_id(self):
        class Conflict(Exception):
            code = 409

        class Page(list):
            next_page_token = None

        class Iterator:
            total_rows = 1
            schema = [types.SimpleNamespace(name='value', field_type='INTEGER')]
            pages = iter([Page([{'value': 42}])])

        class Job:
            project = 'billing'
            location = 'us-east1'
            total_bytes_processed = 10
            schema = Iterator.schema
            attempts = 0

            def __init__(self, job_id, sql):
                self.job_id, self.query = job_id, sql

            def result(self, *, page_size):
                self.attempts += 1
                if self.attempts == 1:
                    raise RuntimeError('conexión perdida después de enviar')
                return Iterator()

        class Client:
            project = 'billing'
            job = None
            submissions = 0

            def query(self, sql, *, job_id, **kwargs):
                self.submissions += 1
                if self.job is not None:
                    self.assert_same(job_id)
                    raise Conflict('ya existe')
                self.job = Job(job_id, sql)
                return self.job

            def assert_same(self, job_id):
                if self.job.job_id != job_id:
                    raise AssertionError('se creó otro identificador')

            def get_job(self, job_id, **kwargs):
                self.assert_same(job_id)
                return self.job

        fake_bq = types.ModuleType('google.cloud.bigquery')
        fake_bq.QueryJobConfig = lambda **kwargs: types.SimpleNamespace(**kwargs)
        fake_cloud = types.ModuleType('google.cloud')
        fake_cloud.bigquery = fake_bq
        fake_google = types.ModuleType('google')
        fake_google.cloud = fake_cloud
        client = Client()
        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner='a') as store:
            run = store.create(session_id='s', turn_id='t', versions={})
            store.acquire(run, 'worker')
            store.prepare_query(run, 'worker', operation_index=0, sql='SELECT 42', parameters=[], project='billing', location='us-east1')
            with patch.dict(sys.modules, {'google': fake_google, 'google.cloud': fake_cloud, 'google.cloud.bigquery': fake_bq}):
                with self.assertRaises(BigQueryError):
                    collect_prepared_query(store, BigQueryAdapter(client), run_id=run, token='worker', operation_index=0)
                result = collect_prepared_query(store, BigQueryAdapter(client), run_id=run, token='worker', operation_index=0)
            self.assertEqual([{'value': 42}], result['rows'])
            self.assertEqual(2, client.submissions)
            self.assertEqual(2, client.job.attempts)

    def test_restart_resumes_at_committed_offset_and_never_reexecutes_completed_result(self):
        class Adapter:
            client = types.SimpleNamespace(project='billing')
            submits = 0
            resumes = []
            job_id = ''

            def page(self, rows, more):
                return QueryPage('page-id', ('value',), tuple({'value': n} for n in rows), more,
                                 3, 100, self.job_id, 'billing', 'us-east1', {'value': 'INTEGER'})

            def execute(self, *args, **kwargs):
                self.submits += 1
                return self.page([1, 2], True)

            def read_page(self, query_id):
                raise RuntimeError('proceso interrumpido después de guardar primera página')

            def resume_query(self, request_id, sql, **kwargs):
                self.resumes.append(kwargs['start_index'])
                return self.page([3], False)

        adapter = Adapter()
        with tempfile.TemporaryDirectory() as folder:
            with ExecutionStore(folder, owner='alice') as store:
                run = store.create(session_id='s', turn_id='t', versions={})
                store.acquire(run, 'one')
                operation = store.prepare_query(run, 'one', operation_index=0, sql='SELECT value FROM t',
                                                parameters=[], project='billing', location='us-east1')
                adapter.job_id = operation['job_id']
                with self.assertRaisesRegex(RuntimeError, 'interrumpido'):
                    collect_prepared_query(store, adapter, run_id=run, token='one', operation_index=0)
                self.assertEqual(2, store.query_result(run, 0)['rows_saved'])
                store.release(run, 'one')
            with ExecutionStore(folder, owner='alice') as store:
                store.acquire(run, 'two')
                result = collect_prepared_query(store, adapter, run_id=run, token='two', operation_index=0)
                self.assertEqual([{'value': 1}, {'value': 2}, {'value': 3}], result['rows'])
                self.assertEqual(['value'], result['metadata']['columns'])
                self.assertEqual('completed', result['status'])
                collect_prepared_query(store, adapter, run_id=run, token='two', operation_index=0)
                self.assertEqual(1, adapter.submits)
                self.assertEqual([2], adapter.resumes)

    def test_incomplete_final_page_fails_instead_of_being_marked_complete(self):
        class Adapter:
            client = types.SimpleNamespace(project='billing')
            job_id = ''
            def execute(self, *args, **kwargs):
                return QueryPage('x', ('value',), ({'value': 1},), False, 2, 0, self.job_id, 'billing', 'us-east1', {})

        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner='a') as store:
            run = store.create(session_id='s', turn_id='t', versions={})
            store.acquire(run, 'worker')
            operation = store.prepare_query(run, 'worker', operation_index=0, sql='SELECT 1', parameters=[], project='billing', location='us-east1')
            adapter = Adapter()
            adapter.job_id = operation['job_id']
            with self.assertRaisesRegex(ValueError, 'cantidad de filas'):
                collect_prepared_query(store, adapter, run_id=run, token='worker', operation_index=0)
            self.assertEqual('prepared', store.query_result(run, 0)['status'])
