import unittest
from datetime import date
from tempfile import TemporaryDirectory
from unittest.mock import patch

from analytics_agent.agent import AnalyticsAgent
from analytics_agent.analysis_context import resolve_analysis
from analytics_agent.analysis_sql import compile_analysis
from analytics_agent.memory import SessionStore
from analytics_agent.execution_store import ExecutionStore
from analytics_agent.turn_runtime import TurnRuntime
from analytics_agent.models import TABLE_IDS
from analytics_agent.presentation import analysis_presentation
from analytics_agent.schema import ColumnSchema, TableSchema
from analytics_agent.semantics import load_metric_definitions
from tests.test_agent import StubLLM, action
from tests.test_rules_v6 import PaymentAdapter, make_agent

TODAY = date(2026, 11, 17)
NET = TABLE_IDS["mm_final_netos"]


class ContextRepairTests(unittest.TestCase):
    def test_total_preserves_period_and_filters(self):
        first = resolve_analysis({}, "Pagos de octubre de 2026 por ramo", today=TODAY)
        for question in ("Y en total cuánto fue?", "Dame el total de pagos, no desglosado"):
            actual = resolve_analysis({"group_by": ["ramo"]}, question, today=TODAY, active=first.to_dict())
            self.assertEqual((first.start_date, first.end_date, ()),
                             (actual.start_date, actual.end_date, actual.group_by))
        grouped = resolve_analysis({}, "Pagos de octubre por ramo, incluyendo total", today=TODAY)
        self.assertEqual(("ramo",), grouped.group_by)

    def test_mixed_claims_have_independent_amount_basis(self):
        spec = resolve_analysis({}, "Pagos brutos y reservas netas de octubre", today=TODAY)
        self.assertEqual({"pagos_siniestros": "gross", "reservas_avisadas": "net"}, spec.metric_bases)

    def test_pending_incurrido_cannot_fall_back_to_payments(self):
        for engine in ("legacy", "langgraph"):
            with self.subTest(engine=engine), TemporaryDirectory() as folder, SessionStore(folder) as store:
                from dataclasses import replace
                bq = PaymentAdapter()
                wrong_sql = f"SELECT SUM(pago_gross) FROM `{NET}`"
                provider = StubLLM([
                    action("run_analysis", metric_keys=["pagos_siniestros"]),
                    action("run_select", sql=wrong_sql),
                    action("run_analysis", metric_keys=["pagos_siniestros"]),
                ])
                agent = make_agent(provider, bq)
                agent.settings = replace(agent.settings, engine=engine)
                first = agent.answer("Pagos de siniestros de octubre de 2026", session_store=store)
                session = first.session_id
                for q in ("Cuál es el incurrido neto en octubre neto de 2026", "fecha_pago"):
                    answer = agent.answer(q, session_store=store, session_id=session)
                    self.assertEqual("partial", answer.status)
                    self.assertIn("corte mensual", answer.answer)
                    intent = store.get_analysis_intent(session)
                    self.assertEqual(["incurrido_siniestros"], intent["metric_keys"])
                    self.assertEqual("net", intent["metric_bases"]["incurrido_siniestros"])
                self.assertEqual(1, bq.executions)
                self.assertEqual(["pagos_siniestros"], store.get_analysis_state(session)["metric_keys"])
                answer = agent.answer("Explícame de dónde sale ese número", session_store=store, session_id=session)
                self.assertIn("SUM(`pago_gross`)", answer.answer)
                self.assertEqual(1, bq.executions)

    def test_resume_verification_without_repeating_query(self):
        with TemporaryDirectory() as folder, SessionStore(folder) as store:
            adapter = PaymentAdapter()
            agent = make_agent(StubLLM([action("run_analysis")]), adapter)
            with patch.object(TurnRuntime, "finish_financial", side_effect=OSError("simulated interruption")):
                with self.assertRaisesRegex(OSError, "simulated interruption"):
                    agent.answer("Pagos de septiembre de 2026", session_store=store)
            with ExecutionStore(folder, owner=store.owner) as executions:
                run = executions.list()[0]["run_id"]
            restarted = make_agent(StubLLM([]), adapter)
            answer = restarted.resume(run, session_store=store)
            self.assertEqual(100, answer.summary_table.rows[0]["pagos_siniestros"])
            self.assertEqual(1, adapter.executions)

    def test_year_does_not_hide_explanation(self):
        text = "Falta el corte mensual de octubre de 2026."
        self.assertEqual(text, AnalyticsAgent._result_sentence(text, None))
        self.assertIn("No se obtuvo", AnalyticsAgent._result_sentence("El pago ascendió a 35.000 pesos.", None))

    def test_zero_diagnostics_come_from_same_aggregate(self):
        spec = resolve_analysis({}, "Pagos en septiembre de 2026", today=TODAY)
        compiled = compile_analysis(spec, load_metric_definitions(), tables={}, timezone="America/Bogota")
        self.assertIn("COUNTIF", compiled.query.sql)
        row = {"pagos_siniestros": 0, "__rows_pagos_siniestros": 5, "__values_pagos_siniestros": 4,
               "__positive_pagos_siniestros": 1, "__negative_pagos_siniestros": 1, "__zero_pagos_siniestros": 2}
        _, _, notes = analysis_presentation({"columns": list(row), "rows": [row]}, compiled)
        self.assertIn("1 importes positivos, 1 negativos y 2 iguales a cero", " ".join(notes))

    def test_monthly_balance_contract_preserves_months_and_netted_field(self):
        definitions = load_metric_definitions()
        contract = {"mode": "monthly_closes", "period_field": "month_of_close",
                    "start": "2026-09-01", "end": "2026-11-01"}
        definitions["reservas_avisadas"]["claims_period"] = contract
        tables = {NET: TableSchema("", NET, {
            "month_of_close": ColumnSchema("month_of_close", "DATE"),
            "reserva_actual_neto": ColumnSchema("reserva_actual_neto", "FLOAT64"),
        }, metadata_loaded=True)}
        spec = resolve_analysis({}, "Reservas netas de septiembre a octubre de 2026", today=TODAY)
        sql = compile_analysis(spec, definitions, tables=tables, timezone="America/Bogota").query.sql
        self.assertIn("SUM(`reserva_actual_neto`)", sql)
        self.assertIn("GROUP BY `mes`", sql)
        self.assertNotIn("fecha_pago", sql)


if __name__ == "__main__":
    unittest.main()
