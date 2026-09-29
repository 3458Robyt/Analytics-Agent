import json
import tempfile
import unittest
from pathlib import Path

from analytics_agent.memory import SessionStore, load_glossary


class SessionStoreTests(unittest.TestCase):
    def test_session_persists_sql_aggregates_and_job_references_without_detail_rows(self):
        with tempfile.TemporaryDirectory() as directory:
            with SessionStore(directory, owner="david:1001") as store:
                session_id = store.create_session("Primas por ramo")
                turn_id = store.begin_turn(session_id, "¿Cuál fue la prima por ramo?")
                store.log_action(
                    turn_id,
                    1,
                    "run_select",
                    {"sql": "SELECT ramo, SUM(prima) AS total FROM proyecto.dataset.tabla GROUP BY ramo"},
                    {
                        "action": "query_page",
                        "query_id": "local-query-1",
                        "job_id": "bq-job-1",
                        "columns": ["ramo", "total"],
                        "rows": [{"ramo": "AUTOS", "total": "100.25"}],
                        "total_rows": 1,
                    },
                    aggregate=True,
                )
                store.log_action(
                    turn_id,
                    2,
                    "run_select",
                    {"sql": "SELECT documento FROM proyecto.dataset.tabla"},
                    {
                        "action": "query_page",
                        "columns": ["documento"],
                        "rows": [{"documento": "dato-detallado"}],
                        "total_rows": 1,
                    },
                )
                store.complete_turn(
                    turn_id,
                    "La prima de AUTOS fue 100.25.",
                    ["Se incluyeron todos los movimientos."],
                    {"title": "Prima por ramo", "columns": ["ramo", "total"],
                     "rows": [{"ramo": "AUTOS", "total": "100.25"}]},
                )

                record = store.get_session(session_id)
                self.assertIsNotNone(record)
                saved_actions = record["turns"][0]["actions"]
                aggregate_result = saved_actions[0]["result"]
                detail_result = saved_actions[1]["result"]
                self.assertEqual([{"ramo": "AUTOS", "total": "100.25"}], aggregate_result["aggregate_rows"])
                self.assertEqual("bq-job-1", saved_actions[0]["bq_job_id"])
                self.assertNotIn("dato-detallado", json.dumps(detail_result))
                self.assertIn("GROUP BY ramo", saved_actions[0]["sql_text"])
                self.assertIn("AUTOS", store.search_context("prima ramo"))
                self.assertIn("prima", store.recent_context(session_id))

            with SessionStore(directory, owner="david:1001") as resumed_user:
                self.assertTrue(resumed_user.session_exists(session_id))
                resumed = resumed_user.get_session(session_id)
                self.assertEqual("La prima de AUTOS fue 100.25.", resumed["turns"][0]["answer"])

            with SessionStore(directory, owner="otro:2002") as other_user:
                self.assertFalse(other_user.session_exists(session_id))
                self.assertEqual([], other_user.list_sessions())

    def test_load_glossary_returns_only_relevant_validated_terms(self):
        text, terms = load_glossary("prima emitida por ramo en th_primas_final")
        self.assertTrue(terms)
        self.assertEqual("prima emitida", terms[0]["term"])
        self.assertIn("vrprima", text)


if __name__ == "__main__":
    unittest.main()
