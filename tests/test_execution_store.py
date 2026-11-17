import tempfile
import time
import unittest

from analytics_agent.bigquery_adapter import stable_query_job_id
from analytics_agent.execution_store import ExecutionStore


class ExecutionStoreTests(unittest.TestCase):
    def test_one_active_run_per_owner_and_session(self):
        with tempfile.TemporaryDirectory() as folder:
            with ExecutionStore(folder, owner='alice') as alice, ExecutionStore(folder, owner='bob') as bob:
                run = alice.create(session_id='s', turn_id='t', versions={})
                with self.assertRaisesRegex(ValueError, 'ejecución activa'):
                    alice.create(session_id='s', turn_id='other', versions={})
                bob.create(session_id='s', turn_id='t', versions={})
                alice.acquire(run, 'worker')
                alice.update(run, 'worker', status='completed', stage='finished')
                self.assertNotEqual(run, alice.create(session_id='s', turn_id='new', versions={}))

    def test_pages_survive_restart_without_duplicates_and_are_owner_scoped(self):
        with tempfile.TemporaryDirectory() as folder:
            with ExecutionStore(folder, owner='alice') as store:
                run = store.create(session_id='s', turn_id='t', versions={})
                self.assertTrue(store.acquire(run, 'first'))
                store.prepare_query(run, 'first', operation_index=0, sql='SELECT 1', parameters=[], project='p', location='us-east1')
                self.assertEqual(2, store.save_page(run, 'first', operation_index=0, start_index=0,
                                                    rows=[{'v': 1}, {'v': 2}], is_last=False))
                store.release(run, 'first')
            with ExecutionStore(folder, owner='alice') as store, ExecutionStore(folder, owner='bob') as other:
                self.assertIsNone(other.query_result(run, 0))
                self.assertEqual(2, store.query_result(run, 0)['rows_saved'])
                self.assertTrue(store.acquire(run, 'second'))
                self.assertEqual(2, store.save_page(run, 'second', operation_index=0, start_index=0,
                                                    rows=[{'v': 1}, {'v': 2}], is_last=False))
                with self.assertRaisesRegex(ValueError, 'contenido diferente'):
                    store.save_page(run, 'second', operation_index=0, start_index=0, rows=[{'v': 3}], is_last=False)
                with self.assertRaisesRegex(ValueError, 'offset esperado'):
                    store.save_page(run, 'second', operation_index=0, start_index=3, rows=[{'v': 4}], is_last=True)
                self.assertEqual(3, store.save_page(run, 'second', operation_index=0, start_index=2,
                                                    rows=[{'v': 3}], is_last=True))
                self.assertEqual([{'v': 1}, {'v': 2}, {'v': 3}], store.query_result(run, 0)['rows'])
                self.assertEqual('completed', store.query_result(run, 0)['status'])
                with self.assertRaisesRegex(ValueError, 'offset esperado'):
                    store.save_page(run, 'second', operation_index=0, start_index=3, rows=[], is_last=True)

    def test_empty_final_page_is_idempotent(self):
        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner='alice') as store:
            run = store.create(session_id='s', turn_id='t', versions={})
            store.acquire(run, 'worker')
            store.prepare_query(run, 'worker', operation_index=0, sql='SELECT 1 WHERE FALSE', parameters=[], project='p', location='us-east1')
            for _ in range(2):
                self.assertEqual(0, store.save_page(run, 'worker', operation_index=0, start_index=0, rows=[], is_last=True))
            self.assertEqual([], store.query_result(run, 0)['rows'])
            self.assertEqual('completed', store.query_result(run, 0)['status'])

    def test_owner_isolation_leases_and_query_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            with ExecutionStore(folder, owner="alice") as alice, ExecutionStore(folder, owner="bob") as bob:
                run = alice.create(session_id="session-1", turn_id="turn-1", versions={"prompt": "v1"})
                alice.require_versions(run, {"prompt": "v1"})
                with self.assertRaisesRegex(ValueError, 'versiones distintas'):
                    alice.require_versions(run, {"prompt": "v2"})
                with self.assertRaisesRegex(ValueError, 'no pertenece'):
                    bob.require_versions(run, {"prompt": "v1"})
                self.assertIsNone(bob.get(run))
                self.assertEqual([], bob.list())
                self.assertFalse(bob.acquire(run, "bob-worker"))
                self.assertTrue(alice.acquire(run, "worker-1"))
                self.assertFalse(alice.acquire(run, "worker-2"))
                args = dict(operation_index=0, sql="SELECT @p", parameters=[{"name": "p", "value": 7, "type": "INT64"}],
                            project="project", location="us-east1")
                first = alice.prepare_query(run, "worker-1", **args)
                second = alice.prepare_query(run, "worker-1", **args)
                self.assertEqual(first["query_id"], second["query_id"])
                self.assertEqual(first["job_id"], stable_query_job_id(run + ":0", args["sql"], args["parameters"]))
                with self.assertRaisesRegex(ValueError, "diferente"):
                    alice.prepare_query(run, "worker-1", **{**args, "sql": "SELECT 99"})
                self.assertFalse(alice.release(run, "worker-2"))
                self.assertTrue(alice.release(run, "worker-1"))
                self.assertTrue(alice.acquire(run, "worker-2"))
                alice.update(run, "worker-2", status="completed", stage="finished")
                self.assertFalse(alice.acquire(run, "worker-3"))
                self.assertEqual("completed", alice.get(run)["status"])

    def test_expired_lease_can_be_taken_over_but_not_used_by_old_worker(self):
        with tempfile.TemporaryDirectory() as folder, ExecutionStore(folder, owner="alice") as store:
            run = store.create(session_id="session", turn_id="turn", versions={})
            self.assertTrue(store.acquire(run, "old", seconds=1, now=time.time() - 2))
            self.assertTrue(store.acquire(run, "new"))
            with self.assertRaisesRegex(ValueError, "bloqueo vencido"):
                store.update(run, "old", status="running", stage="query")
            with self.assertRaisesRegex(ValueError, "bloqueo vencido"):
                store.prepare_query(run, "old", operation_index=0, sql="SELECT 1", parameters=[], project="p", location="us-east1")
            self.assertTrue(store.release(run, "new"))


if __name__ == "__main__":
    unittest.main()
