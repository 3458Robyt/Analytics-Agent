import unittest

from analytics_agent.agent import AnalyticsAgent
from analytics_agent.bigquery_adapter import BigQueryError, DryRunResult, QueryPage
from analytics_agent.models import AgentSettings
from tests.helpers import TABLE_ID, sample_catalog


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


class StubBigQuery:
    location = ""

    def __init__(self, *, dry_run_ok=True):
        self.dry_run_ok = dry_run_ok
        self.executions = 0
        self.pages_read = 0

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
        return DryRunResult(self.dry_run_ok, 2048, "No se pudo validar" if not self.dry_run_ok else "")

    def execute(self, sql, *, location=""):
        self.executions += 1
        return QueryPage(
            "query-1", ("ramo", "prima"), ({"ramo": "AUTO", "prima": 100},), True, 2, 4096
        )

    def read_page(self, query_id):
        self.pages_read += 1
        return QueryPage(
            query_id, ("ramo", "prima"), ({"ramo": "VIDA", "prima": 200},), False, 2, 4096
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
        self.assertEqual("VIDA tuvo la prima mayor, con 200.", result.answer)
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

    def test_repeat_with_identical_result_stops_loop(self):
        llm = StubLLM([
            action("search_tables", search_text="prima"),
            action("search_tables", search_text="prima"),
        ])
        result = make_agent(llm, StubBigQuery()).answer("Busca primas")
        self.assertIn("quedó incompleto", result.answer)

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

    def test_table_mismatch_triggers_revision_and_retry(self):
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
        self.assertEqual(2, len(llm.reviews))
        self.assertIn("no coincide", " ".join(llm.reviews[0]["deterministic_issues"]))
        self.assertEqual(2, len(result.summary_table.rows))
        self.assertNotIn("Revisión:", result.answer)


if __name__ == "__main__":
    unittest.main()
