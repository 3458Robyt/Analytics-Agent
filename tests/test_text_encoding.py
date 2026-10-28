import json
import sqlite3
import tempfile
import unittest
from datetime import date
from decimal import Decimal
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from rich.console import Console

from analytics_agent.agent import AnalyticsAgent
from analytics_agent.bigquery_adapter import _to_json_value
from analytics_agent.cli import _read_clarification, _run_ask, _run_chat
from analytics_agent.llm import OpenAICompatibleProvider
from analytics_agent.memory import SessionStore
from analytics_agent.models import Clarification
from analytics_agent.teams_state import TeamsWorkerStore
from analytics_agent.teams_worker import TeamsRequestProcessor
from analytics_agent.text_encoding import TextConnection, dumps_json, repair_payload, repair_text
from tests.test_agent import SQL, StubBigQuery, StubLLM, action, make_agent
from tests.test_llm import FakeResponse
from tests.test_teams import FakeAgent, FakePublisher, make_request


BROKEN = "\udcc2"


class TextRepairTests(unittest.TestCase):
    def test_valid_unicode_and_literal_escape_notation_are_unchanged(self):
        valid = '¿Qué pasó? Año, Bogotá, indemnización, 😀, a\u0301,\u00a0espacio, \\udcc2\n'
        self.assertEqual(valid, repair_text(valid))

    def test_recoverable_utf8_bytes_and_surrogate_pairs_are_restored(self):
        valid = "¿Año? Bogotá 😀"
        escaped_bytes = valid.encode("utf-8").decode("ascii", errors="surrogateescape")
        self.assertEqual(valid, repair_text(escaped_bytes))
        self.assertEqual("😀", repair_text("\ud83d\ude00"))

    def test_unrecoverable_characters_are_visible_and_do_not_remove_other_text(self):
        for text, expected in ((BROKEN + "¿Pagos?", "�¿Pagos?"),
                               ("¿Pagos" + BROKEN + "?", "¿Pagos�?"),
                               ("¿Pagos?" + BROKEN, "¿Pagos?�"),
                               ("\ud800\udc00\udfff", "𐀀�")):
            with self.subTest(text=ascii(text)):
                repaired = repair_text(text)
                self.assertEqual(expected, repaired)
                repaired.encode("utf-8")
                self.assertEqual(repaired, repair_text(repaired))

    def test_nested_payload_preserves_values_and_repairs_keys(self):
        value = {"nombre" + BROKEN: ["¿Bogotá?", {"x": BROKEN}],
                 "values": (Decimal("123.45"), date(2026, 1, 1), b"\xc2", 123, False, None)}
        repaired = repair_payload(value)
        self.assertEqual(["¿Bogotá?", {"x": "�"}], repaired["nombre�"])
        self.assertEqual(value["values"], repaired["values"])
        encoded = dumps_json(value, ensure_ascii=False, default=str).encode("utf-8")
        self.assertEqual("123.45", json.loads(encoded)["values"][0])

    def test_json_repairs_strings_created_by_default_encoder(self):
        class ExternalValue:
            def __str__(self):
                return "valor" + BROKEN
        self.assertEqual('"valor�"', dumps_json(ExternalValue(), ensure_ascii=False, default=str))

    def test_sqlite_bindings_are_repaired_without_changing_numbers_or_blobs(self):
        with sqlite3.connect(":memory:", factory=TextConnection) as conn:
            conn.execute("CREATE TABLE samples (text, amount, blob)")
            conn.executemany("INSERT INTO samples VALUES (?, ?, ?)",
                             ((text, 123.45, b"\xc2") for text in (BROKEN, "¿Año?")))
            self.assertEqual([("�", 123.45, b"\xc2"), ("¿Año?", 123.45, b"\xc2")],
                             conn.execute("SELECT * FROM samples").fetchall())
            self.assertEqual("�", conn.execute("SELECT :text", {"text": BROKEN}).fetchone()[0])

    def test_fingerprints_keep_valid_hashes_and_accept_nested_damaged_text(self):
        value = {"question": "¿Qué primas hubo?", "x": 123.45}
        import hashlib
        expected = hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
        self.assertEqual(expected, AnalyticsAgent._fingerprint(value))
        self.assertEqual(AnalyticsAgent._fingerprint({"question": "�"}),
                         AnalyticsAgent._fingerprint({"question": BROKEN}))

    def test_bigquery_nested_values_remain_usable(self):
        result = _to_json_value({"nombre" + BROKEN: [BROKEN, Decimal("12.50"), date(2026, 1, 1)], "n": 123})
        self.assertEqual({"nombre�": ["�", "12.50", "2026-01-01"], "n": 123}, result)
        json.dumps(result, ensure_ascii=False).encode("utf-8")


class EncodingBoundaryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.output = StringIO()
        self.console = Console(file=self.output, width=120, color_system=None)
        self.config = SimpleNamespace(state_dir=self.temp.name, presentation="business")
        for target in ("_launch_learning_worker", "_run_learning_jobs"):
            worker = patch("analytics_agent.cli." + target, return_value=0)
            worker.start()
            self.addCleanup(worker.stop)

    def querying_agent(self, *extra_actions):
        llm = StubLLM([action("run_select", sql=SQL), action("finish", answer="Resultado consultado."), *extra_actions])
        return make_agent(llm, StubBigQuery())

    def test_session_storage_repairs_all_text_and_compressed_result_rows(self):
        with SessionStore(self.temp.name) as store:
            session = store.create_session("¿Primas?" + BROKEN)
            turn = store.begin_turn(session, "Total" + BROKEN + "?")
            store.log_action(turn, 1, "run_select", {"sql": "SELECT '" + BROKEN + "'"},
                             {"rows": [{"nombre": BROKEN, "total": 123.45}]}, aggregate=True)
            store.complete_turn(turn, "Respuesta " + BROKEN, [BROKEN],
                {"title": "Resumen", "columns": ["nombre"], "rows": [{"nombre": BROKEN}]})
            store.save_query_result("q1", session_id=session, turn_id=turn, question=BROKEN,
                columns=["nombre", "total"], column_types={}, rows=[{"nombre": BROKEN, "total": 123.45}])
            record = store.get_session(session)
            self.assertEqual("¿Primas?�", record["session"]["title"])
            self.assertEqual("Total�?", record["turns"][0]["question"])
            self.assertEqual("Respuesta �", record["turns"][0]["answer"])
            self.assertEqual([{"nombre": "�", "total": 123.45}], store.get_query_result("q1", session_id=session)["rows"])
            json.dumps(record, ensure_ascii=False).encode("utf-8")

    def test_original_error_is_handled_before_request_metrics_and_model(self):
        agent = self.querying_agent()
        progress = []
        with SessionStore(self.temp.name) as store:
            answer = agent.answer("Dame las primas por sucursal de " + BROKEN + " septiembre?",
                                  session_store=store, on_progress=progress.append)
            self.assertEqual("complete", answer.status)
            self.assertNotIn(BROKEN, agent.llm.calls[0][0])
            self.assertTrue(any("caracteres dañados" in message for message in progress))
            self.assertEqual(1, store.usage_report()["totals"]["requests"])
            self.assertIn("�", store.get_session(answer.session_id)["turns"][0]["question"])

    def test_cli_ask_repairs_before_creating_session_title(self):
        with patch("analytics_agent.cli._build_agent", return_value=(self.querying_agent(), None)):
            result = _run_ask("¿Prima?" + BROKEN, self.config, self.console, self.console)
        self.assertEqual(0, result)
        self.assertIn("caracteres dañados", self.output.getvalue())
        with SessionStore(self.temp.name) as store:
            self.assertEqual("¿Prima?�", store.list_sessions()[0]["title"])

    def test_chat_continues_after_damaged_question_and_feedback(self):
        agent = self.querying_agent(action("finish", answer="Hola, ¿en qué puedo ayudarte?"))
        inputs = ["¿Prima" + BROKEN + "?", "/feedback Revisar " + BROKEN, "Hola?", "/exit"]
        with patch("analytics_agent.cli._build_agent", return_value=(agent, None)), patch("builtins.input", side_effect=inputs):
            result = _run_chat(self.config, self.console, self.console)
        self.assertEqual(0, result)
        self.assertNotIn("No se pudo responder", self.output.getvalue())
        self.assertIn("Hola,", self.output.getvalue())
        self.assertEqual(2, self.output.getvalue().count("caracteres dañados"))
        with SessionStore(self.temp.name) as store:
            self.assertEqual("Revisar �", store.list_feedback_records()[0]["comment"])
            session = store.list_sessions()[0]["session_id"]
            self.assertEqual(2, len(store.get_session(session)["turns"]))

    def test_clarification_input_is_repaired_with_a_notice(self):
        with patch("builtins.input", return_value="Cuenta todo" + BROKEN):
            reply = _read_clarification(Clarification("¿Qué movimientos?"), self.console)
        self.assertEqual("Cuenta todo�", reply)
        self.assertIn("caracteres dañados", self.output.getvalue())

    def test_sdk_clarification_callback_does_not_reintroduce_bad_text(self):
        agent = make_agent(StubLLM([
            action("clarify", clarification={"question": "¿Qué fecha?", "metric_key": "", "proposed_rule": ""}),
            action("run_select", sql=SQL), action("finish", answer="Resultado consultado."),
        ]), StubBigQuery())
        notices = []
        with SessionStore(self.temp.name) as store:
            answer = agent.answer("Primas de enero de 2026", session_store=store,
                on_clarification=lambda request: "Usa fecha_emision" + BROKEN, on_progress=notices.append)
            self.assertEqual("complete", answer.status)
            self.assertTrue(any("caracteres dañados" in message for message in notices))
            record = store.get_session(answer.session_id)
            json.dumps(record, ensure_ascii=False).encode("utf-8")

    def test_pending_clarification_is_repaired_when_resumed(self):
        with SessionStore(self.temp.name) as store:
            first = make_agent(StubLLM([action("clarify", clarification={
                "question": "¿Qué fecha?", "metric_key": "", "proposed_rule": "",
            })]), StubBigQuery())
            pending = first.answer("Primas de enero de 2026", session_store=store)
            notices = []
            answer = self.querying_agent().answer("Usa fecha_emision" + BROKEN,
                session_store=store, pending_turn_id=pending.turn_id,
                clarification_response="Usa fecha_emision" + BROKEN, on_progress=notices.append)
            self.assertEqual("complete", answer.status)
            self.assertEqual(pending.turn_id, answer.turn_id)
            self.assertEqual(1, sum("caracteres dañados" in message for message in notices))
            received = [event for event in store.get_session(answer.session_id)["turns"][0]["actions"]
                        if event["action"] == "clarification_received"]
            self.assertEqual("Usa fecha_emision�", received[-1]["result"]["user_answer"])

    def test_diagnostics_can_store_an_error_containing_damaged_text(self):
        with SessionStore(self.temp.name) as store:
            key = store.begin_request("Primas" + BROKEN, "", {})
            store.record_query("SELECT '" + BROKEN + "'", 0, "error", BROKEN)
            store.finish_request(key, error=ValueError("Error externo " + BROKEN))
            report = store.usage_report()
            self.assertEqual(1, report["statuses"]["error"])
            json.dumps(report, ensure_ascii=False).encode("utf-8")

    def test_teams_cache_and_transport_repair_nested_card_text(self):
        with TeamsWorkerStore(Path(self.temp.name) / "teams.sqlite3") as store:
            record = {"answer": "Resumen " + BROKEN, "rows": [{"nombre": BROKEN, "n": 123}]}
            store.save_result("result-1", "user", record)
            self.assertEqual("�", store.get_result("result-1", "user")["rows"][0]["nombre"])
            publisher = FakePublisher()
            processor = TeamsRequestProcessor(agent=FakeAgent(), bigquery=object(), store=store,
                publisher=publisher, response_topic="test", project="test",
                allowed_users=frozenset({"user-1"}), allowed_tenant="tenant-1")
            processor._publish_response({"tenant_id": "t", "user_id": "u", "conversation_id": "c", "card": record})
            self.assertEqual("Resumen �", publisher.messages[0][1]["card"]["answer"])
            processor.process(make_request("¿Prima" + BROKEN + "?"))
            self.assertEqual("¿Prima�?", processor.agent.calls[0][0])

    def test_provider_requests_and_inner_json_are_clean_in_both_protocols(self):
        for wire in ("responses", "chat_completions"):
            with self.subTest(wire=wire):
                sent = []
                def opener(request, timeout):
                    sent.append(json.loads(request.data))
                    text = json.dumps({"action": "finish", "answer": "Consulta " + BROKEN})
                    payload = {"output_text": text} if wire == "responses" else {"choices": [{"message": {"content": text}}]}
                    return FakeResponse(payload)
                provider = OpenAICompatibleProvider(base_url="https://example.invalid", model="test", api_key="unused",
                                                     wire_api=wire, opener=opener)
                result, _ = provider.next_action("¿Prima" + BROKEN + "?")
                self.assertEqual("Consulta �", result["answer"])
                json.dumps(sent, ensure_ascii=False).encode("utf-8")
                self.assertIn("�", str(sent))

    def test_provider_plain_completion_repairs_malformed_response_text(self):
        for wire in ("responses", "chat_completions"):
            with self.subTest(wire=wire):
                def opener(request, timeout):
                    text = "¿Resultado? " + BROKEN
                    return FakeResponse({"output_text": text} if wire == "responses" else
                                        {"choices": [{"message": {"content": text}}]})
                provider = OpenAICompatibleProvider(base_url="https://example.invalid", model="test", api_key="unused",
                                                     wire_api=wire, opener=opener)
                result = provider.complete([{"role": "user", "content": "¿Resultado?"}])
                self.assertEqual("¿Resultado? �", result.text)
