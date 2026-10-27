import json
import os
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from analytics_agent.chat_commands import handle_command
from analytics_agent.cli import _build_parser
from analytics_agent.knowledge import KnowledgeGraph
from analytics_agent.memory import SessionStore
from analytics_agent.models import AgentAnswer
from analytics_agent.query_scope import all_history, has_period
from analytics_agent.semantics import compile_metric_query, load_metric_definitions
from analytics_agent.teams_state import TeamsWorkerStore
from analytics_agent.teams_worker import _command_card, _feedback_card
from analytics_agent.teams_worker import TeamsRequestProcessor
from analytics_agent.teams_common import teams_owner
from analytics_agent.workspace_state import query_pattern
from analytics_agent.bigquery_adapter import DryRunResult
from analytics_agent.llm import LLMError
from tests.test_agent import StubBigQuery, StubLLM, action, make_agent
from tests.helpers import TABLE_ID
from tests.test_teams import FakeAgent, FakePublisher, make_request


def past(days=0, hours=0):
    return (datetime.now(timezone.utc) - timedelta(days=days, hours=hours)).isoformat(timespec="seconds")


class WorkspaceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = SessionStore(self.temp.name, owner="alice")
        self.addCleanup(self.store.close)
        self.session = self.store.create_session()

    def test_feedback_general_and_last_answer_have_one_store(self):
        general = handle_command(self.store, self.session, "/feedback Quiero gráficos", channel="cli")
        turn = self.store.begin_turn(self.session, "Primas enero 2026")
        self.store.complete_turn(turn, "Total 100")
        correction = handle_command(self.store, self.session, "/feedback Revisar la cifra", channel="cli")
        records = self.store.list_feedback_records()
        self.assertEqual(2, len(records))
        self.assertEqual({"", turn}, {row["turn_id"] for row in records})
        self.assertIn("Feedback registrado", general.text)
        self.assertIn("Feedback registrado", correction.text)

    def test_duplicate_feedback_command_is_idempotent(self):
        a = handle_command(self.store, self.session, "/feedback Error", channel="teams", request_id="request-1")
        b = handle_command(self.store, self.session, "/feedback Error", channel="teams", request_id="request-1")
        self.assertEqual(a, b)
        self.assertEqual(1, len(self.store.list_feedback_records()))

    def test_new_is_idempotent_and_keeps_old_history(self):
        turn = self.store.begin_turn(self.session, "Enero 2026")
        self.store.complete_turn(turn, "Resultado")
        first = handle_command(self.store, self.session, "/new", channel="teams", request_id="new-1")
        again = handle_command(self.store, self.session, "/new", channel="teams", request_id="new-1")
        self.assertEqual(first.session_id, again.session_id)
        self.assertNotEqual(self.session, first.session_id)
        self.assertTrue(self.store.get_session(self.session)["turns"])
        self.assertEqual("", self.store.recent_context(first.session_id))
        self.assertEqual("", self.store.temporal_context(first.session_id))

    def test_resume_and_history_are_owner_scoped(self):
        with SessionStore(self.temp.name, owner="bob") as other:
            for command in (f"/resume {self.session}", f"/history {self.session}"):
                with self.assertRaises(ValueError):
                    handle_command(other, "", command, channel="teams")
            listed = handle_command(other, "", "/sessions", channel="teams")
            self.assertEqual([], listed.items)

    def test_commands_do_not_consume_pending_clarification(self):
        turn = self.store.begin_turn(self.session, "¿Total?")
        self.store.set_pending_clarification(turn, {"question": "¿Fechas?", "reason": "date_scope"})
        handle_command(self.store, self.session, "/sessions", channel="cli")
        handle_command(self.store, self.session, "/feedback Debería ser más rápido", channel="cli")
        self.assertEqual(turn, self.store.pending_turn(self.session))
        handle_command(self.store, self.session, "/resume " + self.session, channel="cli")
        self.assertEqual(turn, self.store.pending_turn(self.session))

    def test_feedback_resolution_is_a_local_operator_action(self):
        key = self.store.add_feedback("Revisar", channel="cli")
        self.assertTrue(self.store.resolve_feedback(key, "Corregido"))
        self.assertEqual("resolved", self.store.feedback_record(key)["status"])
        self.assertEqual([], self.store.list_feedback_records(status="new"))
        self.assertIn("desconocido", handle_command(self.store, self.session, "/feedback-admin", channel="teams").text)

    def test_feedback_ownership_validation(self):
        with SessionStore(self.temp.name, owner="bob") as other:
            with self.assertRaises(ValueError):
                other.add_feedback("Comentario", channel="teams", session_id=self.session)

    def test_legacy_feedback_is_migrated_once_and_preserved(self):
        path = Path(self.temp.name) / "teams_state.sqlite3"
        conn = sqlite3.connect(path)
        conn.execute("CREATE TABLE teams_feedback (feedback_id INTEGER PRIMARY KEY, owner TEXT, result_id TEXT, feedback TEXT, created_at REAL)")
        conn.execute("INSERT INTO teams_feedback VALUES (1, 'teams:t:bob', 'old-result', 'Cifra incorrecta', ?)", (time.time(),))
        conn.commit()
        conn.close()
        self.store.migrate_legacy_feedback()
        self.store.migrate_legacy_feedback()
        rows = self.store.list_feedback_records()
        self.assertEqual(1, len(rows))
        self.assertEqual("teams:t:bob", rows[0]["owner"])
        self.assertEqual("old-result", rows[0]["result_id"])

    def test_results_expire_at_24_hours_without_losing_conversation(self):
        turn = self.store.begin_turn(self.session, "Pregunta")
        self.store.complete_turn(turn, "Respuesta")
        self.store.save_query_result("q1", session_id=self.session, turn_id=turn,
            question="Pregunta", columns=["x"], column_types={}, rows=[{"x": 1}])
        self.store.connection.execute("UPDATE query_results SET created_at = ?", (past(hours=25),))
        self.store.connection.commit()
        self.assertIsNone(self.store.get_query_result("q1", session_id=self.session))
        self.assertEqual("Respuesta", self.store.get_session(self.session)["turns"][0]["answer"])

    def test_result_copies_expire_in_action_traces_but_sql_is_retained(self):
        turn = self.store.begin_turn(self.session, "Pregunta")
        self.store.log_action(turn, 1, "run_select", {"sql": "SELECT 1"},
            {"rows": [{"x": 99}], "sample_rows": {"first": [{"x": 99}]}, "query_id": "q1"}, aggregate=True)
        self.store.connection.execute("UPDATE actions SET created_at = ?", (past(hours=25),))
        self.store.connection.commit()
        self.store.prune_history()
        trace = self.store.get_session(self.session)["turns"][0]["actions"][0]
        self.assertEqual("SELECT 1", trace["sql_text"])
        self.assertTrue(trace["result"]["results_expired"])
        self.assertNotIn("aggregate_rows", trace["result"])
        self.assertNotIn("sample_rows", trace["result"])

    def test_history_retention_cascades_and_preserves_learning(self):
        turn = self.store.begin_turn(self.session, "Expirable")
        self.store.complete_turn(turn, "Expirable")
        review = {"preferences": [{"key": "format", "title": "Breve", "content": "Prefiero respuestas cortas",
            "confidence": 0.9, "explicit_user_statement": True}]}
        self.store.apply_learning_review(turn, review, verified=True)
        self.store.connection.execute("UPDATE turns SET created_at = ? WHERE turn_id = ?", (past(days=91), turn))
        self.store.connection.execute("UPDATE sessions SET updated_at = ?", (past(days=91),))
        self.store.connection.commit()
        self.store.prune_history()
        self.assertIsNone(self.store.get_session(self.session))
        self.assertEqual("", self.store.search_context("Expirable"))
        learning = self.store.list_learning()[0]
        self.assertEqual("active", learning["status"])
        self.assertEqual(1, self.store.get_learning(learning["learning_id"])["archived_evidence"]["evidence_count"])

    def test_retention_configuration_is_validated(self):
        with patch.dict(os.environ, {"ANALYTICS_AGENT_HISTORY_RETENTION_DAYS": "0"}):
            with self.assertRaises(ValueError):
                self.store.prune_history()

    def test_usage_report_groups_literals_and_tracks_errors(self):
        for period in ("2026-01-01", "2026-02-01"):
            key = self.store.begin_request("Pregunta", self.session, {"model": "test"})
            self.store.record_query(f"SELECT SUM(x) FROM p.d.t WHERE fecha >= DATE '{period}'", 2000, "executed")
            self.store.finish_request(key, AgentAnswer("ok", query_count=1, bytes_processed=2000,
                usage={"total_tokens": 10}, session_id=self.session, timings={"model": 1}), elapsed=2)
        key = self.store.begin_request("Falla", self.session, {})
        self.store.finish_request(key, elapsed=1, error=LLMError("Sin respuesta"))
        report = self.store.usage_report()
        self.assertEqual(3, report["totals"]["requests"])
        self.assertEqual(20, report["totals"]["tokens"])
        self.assertEqual(1, len(report["patterns"]))
        self.assertEqual(2, report["patterns"][0]["executions"])
        self.assertEqual(1, report["statuses"]["error"])
        self.assertEqual("LLMError", report["errors"][0]["error_type"])

    def test_knowledge_has_provenance_and_financial_filters(self):
        result = self.store.knowledge("pagos de siniestros")
        payment = next(item for item in result["matches"] if item["metric_key"] == "pagos_siniestros")
        edges = payment["relationships"]
        self.assertTrue(any(item["relation"] == "aggregates" and item["label"] == "incurrido_total" for item in edges))
        self.assertTrue(any(item["relation"] == "requires_filter" and item["details"]["value"] == "Pagos" for item in edges))
        self.assertTrue(all(item["evidence"]["status"] == "validated" for item in edges))

    def test_query_metrics_keep_one_event_per_attempt_and_count_retries(self):
        self.store.begin_request("Pagos", self.session, {})
        self.store.record_query("SELECT SUM(x) FROM p.d.t", 200, "error", "dry run fallido", attempt_key="1")
        self.store.record_query("SELECT SUM(x) FROM p.d.t", None, "error", "dry run fallido", attempt_key="1")
        self.store.record_query("SELECT SUM(x) FROM p.d.t", 200, "executed", attempt_key="2")
        report = self.store.usage_report()
        self.assertEqual(1, report["totals"]["query_retries"])
        self.assertEqual(2, report["patterns"][0]["attempts"])
        self.assertEqual(1, report["patterns"][0]["errors"])

    def test_proposed_financial_rule_never_enters_graph(self):
        turn = self.store.begin_turn(self.session, "Cambiar pagos")
        self.store.apply_learning_review(turn, {"business_proposals": [{"key": "pagos_incorrectos", "title": "Pagos",
            "content": "Sumar una columna inventada", "confidence": 1}]}, verified=True)
        graph = KnowledgeGraph(self.store.connection)
        self.assertNotIn("inventada", json.dumps(graph.search("pagos"), ensure_ascii=False))
        self.assertEqual("proposed", self.store.list_learning()[0]["status"])

    def test_session_and_feedback_cards(self):
        command = handle_command(self.store, self.session, "/sessions", channel="teams")
        card = _command_card(command)
        actions = [item for item in card["body"] if item["type"] == "ActionSet"]
        self.assertEqual("resume", actions[0]["actions"][0]["data"]["action"])
        form = _feedback_card(session_id=self.session)
        self.assertEqual("submit_feedback", form["actions"][0]["data"]["action"])
        self.assertTrue(any(item["type"] == "Input.Text" for item in form["body"]))

    def test_operator_parser_does_not_require_llm_settings(self):
        parser = _build_parser()
        self.assertEqual("feedback", parser.parse_args(["feedback", "list", "--json"]).command)
        self.assertEqual(7, parser.parse_args(["usage", "report", "--days", "7"]).days)
        self.assertEqual("knowledge", parser.parse_args(["knowledge", "search", "pagos"]).command)


class VolumeBigQuery(StubBigQuery):
    def __init__(self, volume):
        super().__init__(rows=[{"total": 2}], columns=("total",))
        self.volume = volume

    def dry_run(self, sql, *, location="", parameters=()):
        self.sql.append(sql)
        return DryRunResult(True, self.volume)

    def execute(self, sql, *, location="", parameters=()):
        return super().execute(sql, location=location)


COUNT_SQL = f"SELECT COUNT(*) AS total FROM `{TABLE_ID}`"


class DateScopeTests(unittest.TestCase):
    def test_large_unscoped_query_waits_without_execution(self):
        bq = VolumeBigQuery(2 * 1024**3)
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            agent = make_agent(StubLLM([action("run_select", sql=COUNT_SQL)]), bq)
            result = agent.answer("¿Cuántos registros hay?", session_store=store)
            self.assertEqual("needs_clarification", result.status)
            self.assertEqual("date_scope", result.clarification.reason)
            self.assertEqual(0, bq.executions)
            self.assertEqual(2 * 1024**3, result.clarification.estimated_bytes)

    def test_small_query_runs_and_has_telemetry(self):
        bq = VolumeBigQuery(1024)
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            agent = make_agent(StubLLM([action("run_select", sql=COUNT_SQL), action("finish", answer="Hay 2 registros.")]), bq)
            result = agent.answer("¿Cuántos registros hay?", session_store=store)
            self.assertEqual(1, bq.executions)
            self.assertEqual(1, store.usage_report()["totals"]["queries"])

    def test_explicit_dates_and_all_history_allow_large_reads(self):
        for question in ("¿Cuántos registros hay en enero de 2026?", "Cuenta todo el histórico"):
            with self.subTest(question=question):
                bq = VolumeBigQuery(2 * 1024**3)
                agent = make_agent(StubLLM([action("run_select", sql=COUNT_SQL), action("finish", answer="Hay 2 registros.")]), bq)
                result = agent.answer(question)
                self.assertEqual(1, bq.executions)

    def test_inherited_period_is_used_only_in_same_session(self):
        bq = VolumeBigQuery(2 * 1024**3)
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            session = store.create_session()
            turn = store.begin_turn(session, "Enero de 2026")
            store.complete_turn(turn, "Resultados")
            agent = make_agent(StubLLM([action("run_select", sql=COUNT_SQL), action("finish", answer="Hay 2 registros.")]), bq)
            agent.answer("¿Y cuántos registros hay?", session_store=store, session_id=session)
            self.assertEqual(1, bq.executions)
            other = store.create_session()
            agent = make_agent(StubLLM([action("run_select", sql=COUNT_SQL)]), bq)
            result = agent.answer("¿Cuántos registros hay?", session_store=store, session_id=other)
            self.assertEqual("needs_clarification", result.status)
            self.assertEqual(1, bq.executions)

    def test_callback_can_confirm_all_history_without_business_learning(self):
        bq = VolumeBigQuery(2 * 1024**3)
        llm = StubLLM([action("run_select", sql=COUNT_SQL), action("run_select", sql=COUNT_SQL), action("finish", answer="Hay 2 registros.")])
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            result = make_agent(llm, bq).answer("¿Cuántos registros hay?", session_store=store,
                on_clarification=lambda request: "Todo el histórico")
            self.assertEqual("complete", result.status)
            self.assertEqual(1, bq.executions)
            self.assertEqual([], store.list_business_definitions())

    def test_invalid_reply_does_not_authorize_all_history_from_question_text(self):
        bq = VolumeBigQuery(2 * 1024**3)
        llm = StubLLM([action("run_select", sql=COUNT_SQL) for _ in range(3)])
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            result = make_agent(llm, bq).answer("¿Cuántos registros hay?", session_store=store,
                on_clarification=lambda request: "No entiendo")
            self.assertEqual("needs_clarification", result.status)
            self.assertEqual(0, bq.executions)

    def test_invented_metric_period_is_removed_before_estimation(self):
        bq = VolumeBigQuery(2 * 1024**3)
        llm = StubLLM([action("run_metric", metric_key="prima_emitida", start_date="2026-01-01", end_date="2026-02-01", group_by=[], filters=[])])
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            result = make_agent(llm, bq).answer("Total de primas", session_store=store)
            self.assertEqual("needs_clarification", result.status)
            self.assertNotIn("2026", bq.sql[-1])
            self.assertEqual(0, bq.executions)

    def test_no_date_metric_compilation_retains_business_filters(self):
        definitions = load_metric_definitions()
        compiled = compile_metric_query(definitions["pagos_siniestros"], start_date="", end_date="", allow_unbounded=True)
        self.assertIn("SUM(`incurrido_total`)", compiled.sql)
        self.assertTrue(any(param["value"] == "pagos" for param in compiled.parameters))
        self.assertNotIn("TIMESTAMP(", compiled.sql)

    def test_provider_failure_is_persisted_as_error(self):
        class BrokenLLM:
            def next_action(self, *args, **kwargs):
                raise LLMError("HTTP 400")
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            with self.assertRaises(LLMError):
                make_agent(BrokenLLM(), VolumeBigQuery(0)).answer("Hola", session_store=store)
            self.assertEqual(1, store.usage_report()["statuses"]["error"])
            row = store.connection.execute("SELECT status FROM turns").fetchone()
            self.assertEqual("error", row[0])

    def test_query_consumption_is_retained_if_the_following_model_call_fails(self):
        class FailsAfterQuery(StubLLM):
            def next_action(self, *args, **kwargs):
                if self.plan_sent and not self.actions:
                    raise LLMError("HTTP 400")
                return super().next_action(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            bq = VolumeBigQuery(2000)
            with self.assertRaises(LLMError):
                make_agent(FailsAfterQuery([action("run_select", sql=COUNT_SQL)]), bq).answer(
                    "¿Cuántos registros hay?", session_store=store)
            report = store.usage_report()
            self.assertEqual(1, report["totals"]["queries"])
            self.assertGreater(report["totals"]["bytes_processed"], 0)
            self.assertGreater(report["stage_seconds"]["model"], 0)

    def test_period_detection_does_not_confuse_movement_policy(self):
        self.assertFalse(all_history("Cuenta todos los movimientos y endosos"))
        self.assertFalse(has_period("Usa fecha_emision"))
        for phrase in ("mes pasado", "año en curso", "últimos 30 días", "enero", "01/09/26"):
            self.assertTrue(has_period(phrase), phrase)


class TeamsExperienceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = TeamsWorkerStore(Path(self.temp.name) / "teams.sqlite3")
        self.addCleanup(self.store.close)
        self.agent, self.publisher = FakeAgent(), FakePublisher()
        self.processor = TeamsRequestProcessor(agent=self.agent, bigquery=object(), store=self.store,
            publisher=self.publisher, response_topic="projects/test/topics/responses", project="test",
            allowed_users=frozenset({"user-1", "user-2"}), allowed_tenant="tenant-1")
        self.owner = teams_owner("tenant-1", "user-1")
        with SessionStore(self.temp.name, owner=self.owner) as state:
            self.session = state.create_session("Primas de enero")
            self.turn = state.begin_turn(self.session, "Enero de 2026")
            state.complete_turn(self.turn, "Resumen de primas")
        self.store.save_conversation(self.owner, "conversation-1", session_id=self.session)

    def test_inline_feedback_is_saved_once_without_agent(self):
        request = make_request("/feedback Quiero gráficos")
        self.processor.process(request)
        self.processor.process(request)
        self.assertEqual([], self.agent.calls)
        with SessionStore(self.temp.name) as state:
            rows = state.list_feedback_records()
            self.assertEqual(1, len(rows))
            self.assertEqual(self.turn, rows[0]["turn_id"])

    def test_expired_response_receipt_does_not_reexecute_or_keep_card_rows(self):
        request = make_request("/sessions")
        self.processor.process(request)
        self.store.connection.execute("UPDATE teams_requests SET updated_at = ?", (time.time() - 25 * 3600,))
        self.store.connection.commit()
        self.processor.process(request)
        status, cached = self.store.claim_request(request["request_id"])
        self.assertEqual("complete", status)
        self.assertTrue(cached["cache_expired"])
        self.assertIsNone(cached["card"])
        self.assertNotIn("Primas de enero", json.dumps(cached))
        self.assertEqual([], self.agent.calls)

    def test_history_summary_can_be_opened_only_by_its_owner(self):
        summary = {"columns": ["ramo", "total"], "rows": [{"ramo": "AUTOS", "total": 123}], "title": "Primas"}
        with SessionStore(self.temp.name, owner=self.owner) as state:
            state.connection.execute("UPDATE turns SET summary_table_json = ? WHERE turn_id = ?", (json.dumps(summary), self.turn))
            state.connection.commit()
        history = self.processor._process_new(make_request("/history " + self.session))
        self.assertIn("history_table", json.dumps(history))
        table = self.processor._process_new(make_request("", action="history_table", activity="table-1",
            session_id=self.session, turn_id=self.turn))
        self.assertIn("AUTOS", json.dumps(table))
        other = self.processor._process_new(make_request("", user="user-2", action="history_table", activity="table-2",
            session_id=self.session, turn_id=self.turn))
        self.assertNotIn("AUTOS", json.dumps(other))
        self.assertEqual([], self.agent.calls)

    def test_feedback_form_and_submit_share_storage(self):
        form = self.processor._process_new(make_request("/feedback"))
        self.assertEqual("Input.Text", form["card"]["body"][2]["type"])
        submitted = self.processor._process_new(make_request("", activity="submit-1", action="submit_feedback",
            feedback_comment="La cifra necesita revisión", feedback_category="correction", session_id=self.session))
        self.assertIn("Feedback registrado", submitted["text"])
        self.assertEqual([], self.agent.calls)
        with SessionStore(self.temp.name) as state:
            self.assertEqual("correction", state.list_feedback_records()[0]["category"])

    def test_session_buttons_and_resume_preserve_pending_turn(self):
        with SessionStore(self.temp.name, owner=self.owner) as state:
            state.set_pending_clarification(self.turn, {"question": "¿Fechas?"})
        fresh = self.processor._process_new(make_request("/new", activity="new-1"))
        self.assertIn("Nueva conversación", fresh["text"])
        listed = self.processor._process_new(make_request("/sessions", activity="sessions-1"))
        self.assertIn("Primas de enero", json.dumps(listed, ensure_ascii=False))
        resumed = self.processor._process_new(make_request("", action="resume", activity="resume-1", session_id=self.session))
        conversation = self.store.conversation(self.owner, "conversation-1")
        self.assertEqual(self.session, conversation["session_id"])
        self.assertEqual(self.turn, conversation["pending_turn_id"])
        self.assertEqual([], self.agent.calls)

    def test_employee_cannot_resume_or_read_someone_elses_session(self):
        with SessionStore(self.temp.name, owner=teams_owner("tenant-1", "user-2")) as state:
            other = state.create_session("Privada de otra persona")
        for command in ("/resume ", "/history "):
            result = self.processor._process_new(make_request(command + other))
            self.assertIn("no existe", result["text"])
            self.assertNotIn("Privada de otra persona", result["text"])
        self.assertEqual([], self.agent.calls)

    def test_feedback_can_reference_a_response_after_result_rows_expire(self):
        result_id = "a" * 40
        with SessionStore(self.temp.name, owner=self.owner) as state:
            state.link_result(result_id, self.session, self.turn)
        form = self.processor._process_new(make_request("", action="feedback", result_id=result_id))
        self.assertEqual(result_id, form["card"]["actions"][0]["data"]["result_id"])
        response = self.processor._process_new(make_request("", activity="submit-linked", action="submit_feedback",
            result_id=result_id, feedback_comment="Revisar", feedback_category="error"))
        self.assertIn("Feedback registrado", response["text"])

    def test_feedback_rejects_someone_elses_result(self):
        result_id = "b" * 40
        with SessionStore(self.temp.name, owner=teams_owner("tenant-1", "user-2")) as state:
            session = state.create_session("Otra sesión")
            state.link_result(result_id, session, "")
        result = self.processor._process_new(make_request("", activity="submit-foreign", action="submit_feedback",
            result_id=result_id, feedback_comment="No debería guardarse", feedback_category="error"))
        self.assertIn("no está disponible", result["text"])
        with SessionStore(self.temp.name) as state:
            self.assertEqual([], state.list_feedback_records())

    def test_commands_are_not_mistaken_for_pending_legacy_feedback(self):
        self.store.set_feedback_wait(self.owner, "conversation-1", "old-result")
        self.processor._process_new(make_request("/sessions"))
        self.assertEqual([], self.store.list_feedback())
        self.processor._process_new(make_request("/cancel", activity="cancel-1"))
        self.assertEqual("", self.store.conversation(self.owner, "conversation-1")["awaiting_feedback_result_id"])


if __name__ == "__main__":
    unittest.main()
