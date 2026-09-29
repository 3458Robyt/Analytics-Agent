import unittest

from analytics_agent.evaluation import FixtureBigQuery, evaluate_case, load_evaluation_cases
from analytics_agent.models import AgentSettings


class StubLLM:
    def __init__(self, actions):
        self.actions = list(actions)
        self.first = True

    def next_action(self, question, **kwargs):
        if self.first:
            self.first = False
            result = {
                "action": "plan", "notes": "", "plan": {
                    "metric": question, "period": "Según la consulta", "grain": "Según la pregunta",
                    "population": "Registros disponibles", "filters": [], "sources": [], "steps": [],
                    "assumptions": [],
                },
            }
        else:
            result = self.actions.pop(0)
        return result, {"total_tokens": 1}


def action(kind, **kwargs):
    return {
        "action": kind, "notes": "", "plan": {}, "search_text": "", "offset": 0, "page_size": 100,
        "table_ids": [], "sql": "", "query_id": "", "answer": "", "assumptions": [],
        "summary_table": None, "present_rows": False, **kwargs,
    }


class EvaluationTests(unittest.TestCase):
    def setUp(self):
        self.cases = load_evaluation_cases()
        self.settings = AgentSettings("https://example", "model", "secret", bigquery_location="us-east1")

    def test_fixture_dry_run_rejects_sql_that_does_not_match_expected_metric(self):
        fixture = FixtureBigQuery(self.cases[0])
        result = fixture.dry_run("SELECT COUNT(*) FROM `centralizacion-datos.analytics_curated.th_primas_final`")
        self.assertFalse(result.ok)
        self.assertIn("SUM(vrprima)", result.error)

    def test_aggregate_case_scores_period_sql_and_summary_result(self):
        case = self.cases[0]
        sql = (
            "SELECT nombre_ramo_comercial AS ramo, SUM(vrprima) AS prima_emitida "
            "FROM `centralizacion-datos.analytics_curated.th_primas_final` "
            "WHERE fecha_emision >= DATE '2026-01-01' AND fecha_emision < DATE '2026-07-01' "
            "GROUP BY ramo"
        )
        llm = StubLLM([
            action("run_select", sql=sql),
            action("finish", answer="La prima emitida por ramo está calculada para el primer semestre.",
                   summary_table={"title": "Prima por ramo", "columns": ["ramo", "prima_emitida"],
                                  "rows": case["result_rows"]}),
        ])
        result = evaluate_case(case, llm, self.settings)
        self.assertTrue(result.passed, result.failures)
        self.assertEqual(1, len(result.sql))

    def test_detail_case_reads_every_simulated_page(self):
        case = self.cases[1]
        sql = (
            "SELECT fecha_emision, nombre_ramo_comercial AS ramo, vrprima "
            "FROM `centralizacion-datos.analytics_curated.th_primas_final` "
            "WHERE fecha_emision >= DATE '2026-01-01' AND fecha_emision < DATE '2026-02-01'"
        )
        llm = StubLLM([
            action("run_select", sql=sql),
            action("finish", answer="Este es el detalle completo solicitado.", present_rows=True),
            action("finish", answer="Este es el detalle completo solicitado.", present_rows=True),
        ])
        result = evaluate_case(case, llm, self.settings)
        self.assertTrue(result.passed, result.failures)


if __name__ == "__main__":
    unittest.main()
