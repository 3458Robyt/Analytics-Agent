import unittest
import tempfile

from analytics_agent.agent import AnalyticsAgent
from analytics_agent.bigquery_adapter import BigQueryError, DryRunResult, QueryPage
from analytics_agent.models import AgentSettings
from tests.helpers import TABLE_ID, sample_catalog
from analytics_agent.memory import SessionStore


SQL = f"SELECT ramo, SUM(vrprima) AS prima FROM `{TABLE_ID}` GROUP BY ramo"


def action(kind, **kwargs):
    return {
        "action": kind,
        "search_text": "",
        "offset": 0,
        "page_size": 100,
        "table_ids": [],
        "sql": "",
        "query_id": "",
        "notes": "",
        "answer": "",
        "assumptions": [],
        "summary_table": None,
        "plan": {},
        "present_rows": False,
        **kwargs,
    }


class StubLLM:
    def __init__(self, actions):
        self.actions = list(actions)
        self.calls = []
        self.plan_sent = False

    def next_action(self, question, **kwargs):
        self.calls.append((question, kwargs))
        if not self.plan_sent:
            self.plan_sent = True
            return action("plan", plan={
                "metric": question,
                "period": "Según la pregunta",
                "grain": "Según la pregunta",
                "population": "Registros disponibles",
                "filters": [],
                "sources": [],
                "steps": ["Consultar y revisar evidencia"],
                "assumptions": [],
            }), {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}
        return self.actions.pop(0), {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}


class ReviewingStubLLM(StubLLM):
    def __init__(self, actions):
        super().__init__(actions)
        self.reviews = []

    def review_answer(self, question, **kwargs):
        self.reviews.append(kwargs)
        issues = list(kwargs.get("deterministic_issues", ()))
        return {"accepted": not issues, "issues": issues}, {"total_tokens": 4}


class LearningStubLLM(ReviewingStubLLM):
    def review_learning(self, question, **kwargs):
        return {
            "user_correction_detected": False,
            "invalidates": [],
            "preferences": [{
                "key": "answer_style", "title": "Respuestas directas",
                "content": "Responder directamente y resumir los datos principales.",
                "trigger": "En preguntas analíticas", "confidence": 0.9,
                "explicit_user_statement": True,
            }],
            "procedures": [],
            "business_proposals": [],
        }, {"total_tokens": 7}


class StubBigQuery:
    location = ""

    def __init__(self, *, dry_run_ok=True, rows=None, columns=("ramo", "prima"), column_types=None):
        self.dry_run_ok = dry_run_ok
        self.executions = 0
        self.pages_read = 0
        self.rows = list(rows) if rows is not None else [
            {"ramo": "AUTO", "prima": 100}, {"ramo": "VIDA", "prima": 200},
        ]
        self.columns = tuple(columns)
        self.column_types = dict(column_types or {})
        self.sql = []

    def inspect_table_metadata(self, table_id):
        catalog = sample_catalog().tables[TABLE_ID.lower()]
        return {
            "table_id": table_id,
            "actual_schema": {
                name: {"name": col.name, "data_type": col.data_type, "description": col.description}
                for name, col in catalog.columns.items()
            },
            "location": "us-east1",
            "table_type": "TABLE",
            "description": "Primas",
            "view_query": "",
        }

    def dry_run(self, sql, *, location=""):
        self.sql.append(sql)
        return DryRunResult(self.dry_run_ok, 2048, "No se pudo validar" if not self.dry_run_ok else "")

    def execute(self, sql, *, location=""):
        self.executions += 1
        return QueryPage(
            "query-1", self.columns, tuple(self.rows[:1]), len(self.rows) > 1, len(self.rows), 4096,
            column_types=self.column_types,
        )

    def read_page(self, query_id):
        self.pages_read += 1
        return QueryPage(
            query_id, self.columns, tuple(self.rows[1:]), False, len(self.rows), 4096,
            column_types=self.column_types,
        )


def make_agent(llm, bq):
    return AnalyticsAgent(
        catalog=sample_catalog(),
        llm=llm,
        bigquery=bq,
        settings=AgentSettings("https://example", "model", "secret", bigquery_location="us-east1"),
    )


class AgentLoopTests(unittest.TestCase):
    def test_runs_selects_reads_all_pages_and_returns_no_raw_rows(self):
        llm = StubLLM([
            action("run_select", sql=SQL),
            action("read_page", query_id="query-1", notes="AUTO 100; falta una página"),
            action(
                "finish",
                answer="VIDA tuvo la prima mayor, con 200.",
                assumptions=["La suma incluye los registros de la tabla."],
                summary_table={"title": "Prima por ramo", "columns": ["ramo", "prima"], "rows": [["AUTO", 100], ["VIDA", 200]]},
            ),
        ])
        bq = StubBigQuery()
        result = make_agent(llm, bq).answer("Prima por ramo")
        self.assertEqual(1, bq.executions, result.answer)
        self.assertEqual(1, bq.pages_read)
        self.assertEqual("El mayor valor de **prima** corresponde a **ramo: VIDA**, con **200**.", result.answer)
        self.assertEqual(1, result.query_count)
        self.assertEqual(4096, result.bytes_processed)
        self.assertNotIn("rows", result.__dict__)
        self.assertEqual(2, len(result.summary_table.rows))

    def test_search_and_describe_are_actions_in_the_same_run(self):
        llm = StubLLM([
            action("search_tables", search_text="prima"),
            action("describe_tables", table_ids=[TABLE_ID]),
            action("finish", answer="No se requiere consulta para explicar el esquema."),
        ])
        result = make_agent(llm, StubBigQuery()).answer("¿Qué tabla tiene primas?")
        self.assertIn("esquema", result.answer)
        self.assertEqual(4, len(llm.calls))

    def test_dry_run_failure_prevents_query_and_agent_can_finish(self):
        llm = StubLLM([
            action("run_select", sql=SQL),
            action("finish", answer="BigQuery no permitió validar la consulta."),
        ])
        bq = StubBigQuery(dry_run_ok=False)
        result = make_agent(llm, bq).answer("Prima por ramo")
        self.assertEqual(0, bq.executions)
        self.assertEqual("BigQuery no permitió validar la consulta.", result.answer)

    def test_empty_bigquery_result_does_not_reuse_model_claims(self):
        llm = StubLLM([
            action("run_select", sql=SQL),
            action("finish", answer="El total fue 999 para enero de 2026."),
        ])
        result = make_agent(llm, StubBigQuery(rows=[])).answer("Prima por ramo")
        self.assertEqual("BigQuery no devolvió filas para el criterio consultado.", result.answer)
        self.assertIsNone(result.summary_table)

    def test_repeat_with_identical_result_stops_loop(self):
        llm = StubLLM([
            action("search_tables", search_text="prima"),
            action("search_tables", search_text="prima"),
        ])
        result = make_agent(llm, StubBigQuery()).answer("Busca primas")
        self.assertIn("repitió exactamente la misma acción", result.answer)

    def test_repeated_identical_sql_is_not_submitted_to_bigquery_twice(self):
        llm = StubLLM([
            action("run_select", sql=SQL),
            action("run_select", sql=SQL),
        ])
        bq = StubBigQuery()
        result = make_agent(llm, bq).answer("Prima por ramo")
        self.assertEqual(1, bq.executions)
        self.assertEqual(1, result.query_count)
        self.assertEqual(2, len(result.summary_table.rows))
        self.assertIn("200", result.answer)

    def test_mutating_sql_is_rejected_before_dry_run(self):
        llm = StubLLM([
            action("run_select", sql=f"DELETE FROM `{TABLE_ID}` WHERE TRUE"),
            action("finish", answer="No ejecuté una instrucción de escritura."),
        ])
        bq = StubBigQuery()
        result = make_agent(llm, bq).answer("Elimina filas")
        self.assertEqual(0, bq.executions)
        self.assertEqual("No ejecuté una instrucción de escritura.", result.answer)

    def test_requested_detail_rows_include_all_pages(self):
        self.assertFalse(AnalyticsAgent._is_aggregate(f"SELECT ramo FROM `{TABLE_ID}`"))
        llm = StubLLM([
            action("run_select", sql=f"SELECT ramo FROM `{TABLE_ID}`"),
            action("read_page", query_id="query-1"),
            action("finish", answer="Estos son los ramos consultados.", present_rows=True),
        ])
        result = make_agent(llm, StubBigQuery()).answer("Muestra todos los ramos")
        self.assertIsNotNone(result.detail_table)
        self.assertEqual(2, len(result.detail_table.rows))
        self.assertEqual("AUTO", result.detail_table.rows[0]["ramo"])
        self.assertEqual("VIDA", result.detail_table.rows[1]["ramo"])

    def test_model_cannot_replace_bigquery_rows_and_custom_query_is_reviewed_once(self):
        llm = ReviewingStubLLM([
            action("run_select", sql=SQL),
            action("read_page", query_id="query-1"),
            action("finish", answer="La suma está verificada.", summary_table={
                "title": "Prima por ramo", "columns": ["ramo", "prima"], "rows": [["INVENTADO", 999]],
            }),
            action("finish", answer="AUTO tuvo 100 y VIDA tuvo 200.", summary_table={
                "title": "Prima por ramo", "columns": ["ramo", "prima"], "rows": [["AUTO", 100], ["VIDA", 200]],
            }),
        ])
        result = make_agent(llm, StubBigQuery()).answer("Prima por ramo")
        self.assertEqual(1, len(llm.reviews))
        self.assertEqual([], llm.reviews[0]["deterministic_issues"])
        self.assertEqual(2, len(result.summary_table.rows))
        self.assertEqual("VIDA", result.summary_table.rows[1]["ramo"])
        self.assertIn("200", result.answer)

    def test_model_cannot_hide_numeric_evidence_from_summary_table(self):
        summary = AnalyticsAgent._summary_from_evidence(
            {"title": "Totales", "columns": ["ramo"], "rows": []},
            {"query-1": {
                "columns": ["ramo", "prima"],
                "column_types": {"ramo": "STRING", "prima": "NUMERIC"},
                "rows": [{"ramo": "AUTO", "prima": "100.25"}],
            }},
            {"query-1"},
        )
        self.assertEqual(("ramo", "prima"), summary.columns)
        self.assertEqual(("prima",), summary.numeric_columns)
        self.assertEqual("100.25", summary.rows[0]["prima"])

    def test_search_memory_action_reads_past_turns(self):
        llm = StubLLM([
            action("search_memory", search_text="prima por ramo"),
            action("finish", answer="Usé el contexto anterior."),
        ])
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory, owner="user:1") as store:
            prior_session = store.create_session("Consulta previa")
            prior_turn = store.begin_turn(prior_session, "Prima emitida por ramo")
            store.complete_turn(prior_turn, "Se usa vrprima por fecha_emision.")
            active_session = store.create_session("Seguimiento")
            result = make_agent(llm, StubBigQuery()).answer(
                "¿Cómo calculo la prima por ramo?", session_store=store, session_id=active_session
            )
            self.assertIn("contexto anterior", result.answer)
            self.assertIn("prima emitida", str(llm.calls[-1][1]["last_tool_result"]).lower())

    def test_learning_review_is_queued_after_answer_and_can_be_processed_separately(self):
        llm = LearningStubLLM([action("finish", answer="Respuesta directa.")])
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory, owner="user:1") as store:
            session_id = store.create_session("Pregunta")
            result = make_agent(llm, StubBigQuery()).answer(
                "Prefiero respuestas breves", session_store=store, session_id=session_id
            )
            self.assertIn("segundo plano", " ".join(result.learning_updates))
            self.assertEqual(2, len(llm.calls))  # Plan y respuesta; curación fuera de la ruta crítica.
            job = store.claim_learning_review()
            self.assertIsNotNone(job)
            turn_id, payload = job
            review, _usage = llm.review_learning(
                payload["question"], prior_context=payload["prior_context"], plan=payload["plan"],
                answer=payload["answer"], action_history=payload["action_history"],
                verified=payload["verified"], active_learnings=payload["active_learnings"],
            )
            store.apply_learning_review(turn_id, review, verified=payload["verified"])
            store.finish_learning_review(turn_id)
            self.assertIn("resumir los datos", store.learning_context("preguntas analíticas"))
            self.assertEqual("complete", store.connection.execute(
                "SELECT status FROM learning_queue WHERE turn_id = ?", (turn_id,)
            ).fetchone()["status"])

    def test_confirmed_metric_compiles_sql_and_skips_llm_reviewer(self):
        llm = ReviewingStubLLM([
            action("run_metric", metric_key="prima_emitida", start_date="2026-01-01",
                   end_date="2026-02-01", group_by=["nombre_ramo_comercial"]),
            action("finish", answer="La prima quedó consultada."),
        ])
        rows = [{"ramo": f"RAMO {index:03d}", "prima_emitida": index * 10} for index in range(75)]
        bq = StubBigQuery(
            rows=rows, columns=("ramo", "prima_emitida"),
            column_types={"ramo": "STRING", "prima_emitida": "NUMERIC"},
        )
        result = make_agent(llm, bq).answer("Prima emitida por ramo en enero de 2026")
        self.assertEqual(1, bq.executions)
        self.assertIn("SUM(`vrprima`)", bq.sql[0])
        self.assertIn("`fecha_emision` >= CAST('2026-01-01' AS DATE)", bq.sql[0])
        self.assertEqual(75, len(result.summary_table.rows))
        self.assertEqual([], llm.reviews)

    def test_clarification_is_collected_in_the_same_turn_and_confirmed_definition_is_personal(self):
        clarification = {
            "question": "¿Incluyo todos los movimientos del periodo?",
            "metric_key": "prima_emitida",
            "proposed_rule": "Incluir todos los registros y movimientos del periodo, sin excluir endosos ni ajustes.",
        }
        llm = StubLLM([
            action("clarify", clarification=clarification),
            action("run_select", sql=SQL),
            action("finish", answer="Resultado consultado."),
        ])
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory, owner="user:1") as store:
            session_id = store.create_session("Pregunta")
            captured = []
            result = make_agent(llm, StubBigQuery()).answer(
                "Prima emitida por ramo en enero de 2026", session_store=store,
                session_id=session_id,
                on_clarification=lambda prompt: (captured.append(prompt.question) or "Cuenta todo"),
            )
            self.assertEqual([clarification["question"]], captured)
            self.assertEqual("complete", result.status)
            definitions = store.list_business_definitions()
            self.assertEqual("prima_emitida", definitions[0]["metric_key"])
            self.assertIn("confirmación personal", store.confirmed_business_definitions("prima emitida"))

    def test_pending_clarification_can_resume_the_same_persisted_turn(self):
        clarification = {
            "question": "¿Qué campo de fecha debe delimitar el periodo?",
            "metric_key": "incurrido_neto",
            "proposed_rule": "Usar fecha_pago para delimitar el periodo de cálculo.",
        }
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory, owner="user:1") as store:
            session_id = store.create_session("Pregunta")
            first = make_agent(StubLLM([action("clarify", clarification=clarification)]), StubBigQuery())
            waiting = first.answer("Incurrido neto de enero", session_store=store, session_id=session_id)
            self.assertEqual("needs_clarification", waiting.status)
            self.assertEqual(waiting.turn_id, store.get_session(session_id)["turns"][0]["turn_id"])

            resumed_agent = make_agent(StubLLM([
                action("run_select", sql=SQL),
                action("finish", answer="Resultado consultado."),
            ]), StubBigQuery())
            resumed = resumed_agent.answer(
                "", session_store=store, pending_turn_id=waiting.turn_id,
                clarification_response="Usa fecha_pago y suma neto",
            )
            self.assertEqual("complete", resumed.status)
            self.assertEqual(waiting.turn_id, resumed.turn_id)
            self.assertEqual("complete", store.get_session(session_id)["turns"][0]["status"])


if __name__ == "__main__":
    unittest.main()
