import base64
import json
import os
import tempfile
import time
import unittest
from concurrent.futures import Future

from analytics_agent.models import AgentAnswer, SummaryTable
from analytics_agent.teams_common import (
    result_page,
    stable_request_id,
    teams_owner,
)
from analytics_agent.teams_gateway import _activity_identity, _decode_push
from analytics_agent.teams_state import TeamsWorkerStore
from analytics_agent.teams_worker import TeamsRequestProcessor, _result_card, _text_card


class FakePublisher:
    def __init__(self):
        self.messages = []

    def publish(self, topic, data, **kwargs):
        self.messages.append((topic, json.loads(data.decode("utf-8"))))
        result = Future()
        result.set_result("message-id")
        return result


class FakeAgent:
    def __init__(self):
        self.calls = []

    def answer(self, question, *, session_store, session_id="", pending_turn_id="", clarification_response=""):
        self.calls.append((question, session_id, pending_turn_id, clarification_response))
        session_id = session_id or session_store.create_session(question)
        turn_id = session_store.begin_turn(session_id, question)
        rows = tuple({"ramo": f"RAMO {index}", "prima": index * 1000.25} for index in range(1, 13))
        session_store.complete_turn(turn_id, "Consulta terminada")
        return AgentAnswer(
            answer="La consulta terminó con 12 filas.",
            summary_table=SummaryTable(
                title="Prima por ramo",
                columns=("ramo", "prima"),
                rows=rows,
                numeric_columns=("prima",),
            ),
            query_count=1,
            session_id=session_id,
            turn_id=turn_id,
            audit=({"sql": "SELECT ramo, SUM(prima) FROM tabla GROUP BY ramo", "tables": ["p.d.t"]},),
        )


def make_request(text, *, tenant="tenant-1", user="user-1", conversation="conversation-1", activity="activity-1",
                 action="message", **extra):
    payload = {
        "request_id": stable_request_id(tenant, user, activity),
        "activity_id": activity,
        "tenant_id": tenant,
        "user_id": user,
        "conversation_id": conversation,
        "action": action,
        "text": text,
        "created_at": time.time(),
    }
    payload.update(extra)
    return payload


class TeamsCommonTests(unittest.TestCase):
    def test_owner_and_request_id_are_scoped_and_stable(self):
        self.assertEqual("teams:tenant-a:user-a", teams_owner("Tenant-A", "User-A"))
        first = stable_request_id("tenant-a", "user-a", "activity-1")
        self.assertEqual(first, stable_request_id("TENANT-A", "USER-A", "activity-1"))
        self.assertNotEqual(first, stable_request_id("tenant-a", "user-b", "activity-1"))

    def test_table_paginates_rows_and_columns_without_dropping_values(self):
        record = {"summary_table": {
            "title": "Resultados",
            "columns": [f"c{number}" for number in range(7)],
            "rows": [{f"c{number}": f"r{row}c{number}" for number in range(7)} for row in range(12)],
        }}
        first = result_page(record, 0, column_page=0)
        self.assertEqual(10, len(first["rows"]))
        self.assertEqual(["c0", "c1", "c2", "c3", "c4"], first["columns"])
        self.assertTrue(first["has_next"])
        self.assertTrue(first["has_next_columns"])
        next_rows = result_page(record, 1, column_page=0)
        self.assertEqual(2, len(next_rows["rows"]))
        next_columns = result_page(record, 0, column_page=1)
        self.assertEqual(["c5", "c6"], next_columns["columns"])
        self.assertEqual("r0c6", next_columns["rows"][0]["c6"])

    def test_result_table_formats_decimal_and_keeps_summary_and_detail_accessible(self):
        record = {
            "summary_table": {
                "title": "Resumen", "columns": ["total"], "numeric_columns": ["total"],
                "rows": [{"total": "1234567.89"}],
            },
            "detail_table": {
                "title": "Detalle", "columns": ["id"], "rows": [{"id": "A-1"}],
            },
            "audit": [{"sql": "SELECT 1"}],
        }
        page = result_page(record, 0)
        self.assertEqual("1.234.567,89", page["rows"][0]["total"])
        card = _result_card(record, "result-1")
        actions = {action["title"]: action["data"] for action in card["actions"]}
        self.assertEqual("detail", actions["Ver detalle"]["table_kind"])
        self.assertEqual("trace", actions["Ver SQL y trazabilidad"]["action"])
        detail_card = _result_card(record, "result-1", table_kind="detail")
        detail_actions = {action["title"]: action["data"] for action in detail_card["actions"]}
        self.assertEqual("summary", detail_actions["Volver al resumen"]["table_kind"])

    def test_clarification_card_has_a_new_question_action(self):
        card = _text_card("Aclaración", "¿Qué periodo?", allow_new=True)
        self.assertEqual("new", card["actions"][0]["data"]["action"])

    def test_activity_identity_accepts_teams_wire_shape_and_sdk_shape(self):
        wire_activity = {
            "channelData": {"tenant": {"id": "tenant-1"}},
            "from": {"aadObjectId": "user-1"},
            "conversation": {"id": "conversation-1"},
            "id": "activity-1",
        }
        self.assertEqual(
            ("tenant-1", "user-1", "conversation-1", "activity-1"),
            _activity_identity(wire_activity),
        )

    def test_pubsub_push_payload_requires_base64_json(self):
        encoded = base64.b64encode(b'{"kind":"heartbeat"}').decode("ascii")
        self.assertEqual({"kind": "heartbeat"}, _decode_push({"message": {"data": encoded}}))
        with self.assertRaises(ValueError):
            _decode_push({"message": {"data": "!"}})


class TeamsWorkerStoreTests(unittest.TestCase):
    def test_result_cache_is_owner_scoped_and_expires(self):
        with tempfile.TemporaryDirectory() as directory:
            with TeamsWorkerStore(os.path.join(directory, "teams.sqlite3")) as store:
                record = {"summary_table": {"columns": ["x"], "rows": [{"x": 1}]}}
                store.save_result("result-1", "teams:t:u1", record, ttl_seconds=3600)
                self.assertEqual(record, store.get_result("result-1", "teams:t:u1"))
                self.assertIsNone(store.get_result("result-1", "teams:t:u2"))
                store.connection.execute("UPDATE teams_results SET expires_at = ?", (time.time() - 1,))
                store.connection.commit()
                self.assertIsNone(store.get_result("result-1", "teams:t:u1"))

    def test_completed_request_returns_saved_response_for_redelivery(self):
        with tempfile.TemporaryDirectory() as directory:
            with TeamsWorkerStore(os.path.join(directory, "teams.sqlite3")) as store:
                self.assertEqual(("running", None), store.claim_request("req-1"))
                response = {"response_id": "req-1", "text": "resultado"}
                store.complete_request("req-1", response)
                self.assertEqual(("complete", response), store.claim_request("req-1"))

    def test_feedback_is_listed_for_review(self):
        with tempfile.TemporaryDirectory() as directory:
            with TeamsWorkerStore(os.path.join(directory, "teams.sqlite3")) as store:
                store.save_feedback("teams:t:u1", "result-1", "La cifra no coincide con el reporte aprobado")
                rows = store.list_feedback()
                self.assertEqual(1, len(rows))
                self.assertEqual("teams:t:u1", rows[0]["owner"])


class TeamsRequestProcessorTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.previous_state_dir = os.environ.get("ANALYTICS_AGENT_STATE_DIR")
        os.environ["ANALYTICS_AGENT_STATE_DIR"] = self.tempdir.name
        self.store = TeamsWorkerStore(os.path.join(self.tempdir.name, "teams.sqlite3"))
        self.agent = FakeAgent()
        self.publisher = FakePublisher()
        self.processor = TeamsRequestProcessor(
            agent=self.agent,
            bigquery=object(),
            store=self.store,
            publisher=self.publisher,
            response_topic="projects/test/topics/responses",
            project="test",
            allowed_users=frozenset({"user-1"}),
            allowed_tenant="tenant-1",
        )

    def tearDown(self):
        self.store.close()
        if self.previous_state_dir is None:
            os.environ.pop("ANALYTICS_AGENT_STATE_DIR", None)
        else:
            os.environ["ANALYTICS_AGENT_STATE_DIR"] = self.previous_state_dir
        self.tempdir.cleanup()

    def test_duplicate_activity_does_not_repeat_agent_or_sql_work(self):
        request = make_request("prima por ramo")
        self.processor.process(request)
        self.processor.process(request)
        self.assertEqual(1, len(self.agent.calls))
        self.assertEqual(2, len(self.publisher.messages))
        self.assertEqual(self.publisher.messages[0][1], self.publisher.messages[1][1])

    def test_pagination_uses_cached_result_without_calling_agent(self):
        original = make_request("prima por ramo")
        self.processor.process(original)
        result_id = original["request_id"]
        page_request = make_request(
            "", activity="activity-page", action="page", result_id=result_id,
            page=1, column_page=0,
        )
        self.processor.process(page_request)
        self.assertEqual(1, len(self.agent.calls))
        page_response = self.publisher.messages[-1][1]
        rows = page_response["card"]["body"][-1]["rows"]
        self.assertEqual(3, len(rows))  # One header plus two remaining rows.

    def test_outside_pilot_user_is_rejected_before_agent_call(self):
        self.processor.process(make_request("consulta", user="otro"))
        self.assertEqual([], self.agent.calls)
        self.assertIn("no está habilitada", self.publisher.messages[-1][1]["text"])

    def test_old_request_expires_without_calling_agent(self):
        request = make_request("consulta")
        request["created_at"] = time.time() - 1000
        self.processor.max_request_age = 900
        self.processor.process(request)
        self.assertEqual([], self.agent.calls)
        self.assertIn("venció", self.publisher.messages[-1][1]["text"])


if __name__ == "__main__":
    unittest.main()
