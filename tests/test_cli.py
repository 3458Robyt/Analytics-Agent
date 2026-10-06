import unittest
from io import StringIO

from rich.console import Console

from analytics_agent.cli import _as_bool, _build_parser, _format_cell, _render_answer
from analytics_agent.models import AgentAnswer, SummaryTable


class CliFormattingTests(unittest.TestCase):
    def test_boolean_configuration(self):
        self.assertTrue(_as_bool("true"))
        self.assertTrue(_as_bool("sí"))
        self.assertFalse(_as_bool("false"))
        with self.assertRaises(ValueError):
            _as_bool("maybe")

    def test_cell_formatting(self):
        self.assertEqual(_format_cell(1234567), "1.234.567")
        self.assertEqual(_format_cell(1234.5), "1.234,50")
        self.assertEqual(_format_cell(None), "—")
        self.assertEqual(_format_cell(True), "Sí")

    def test_answer_renders_summary_and_table(self):
        console = Console(record=True, file=StringIO(), width=100, color_system=None)
        answer = AgentAnswer(
            answer="El total fue **1.234,50**.",
            assumptions=("Se incluyeron todos los movimientos.",),
            learning_updates=("Enseñanza activada: Respuestas breves",),
            summary_table=SummaryTable(
                title="Prima por ramo",
                columns=("ramo", "prima"),
                rows=({"ramo": "AUTOMÓVILES", "prima": 1234.5},),
            ),
            query_count=1,
        )

        _render_answer(answer, console)
        rendered = console.export_text()
        self.assertIn("El total fue 1.234,50.", rendered)
        self.assertIn("Prima por ramo", rendered)
        self.assertIn("AUTOMÓVILES", rendered)
        self.assertIn("1.234,50", rendered)
        self.assertIn("Supuestos", rendered)
        self.assertIn("Aprendizaje actualizado", rendered)

    def test_business_presentation_hides_audit_and_timing_details(self):
        console = Console(record=True, file=StringIO(), width=100, color_system=None)
        answer = AgentAnswer(
            answer="El resultado se muestra en la tabla.",
            query_count=1,
            tables_used=("project.dataset.table",),
            bytes_processed=1024,
            query_jobs=({"job_id": "job-123", "project": "project", "location": "us-east1"},),
            audit=({"sql": "SELECT secreto FROM project.dataset.table", "tables": ["project.dataset.table"]},),
            timings={"model": 1.25, "query": 0.5},
        )
        _render_answer(answer, console, presentation="business")
        rendered = console.export_text()
        self.assertIn("El resultado", rendered)
        self.assertNotIn("SELECT secreto", rendered)
        self.assertNotIn("job-123", rendered)
        self.assertNotIn("Tiempos", rendered)

    def test_learning_cli_filter(self):
        args = _build_parser().parse_args(["learn", "list", "--status", "proposed"])
        self.assertEqual("learn", args.command)
        self.assertEqual("list", args.learning_action)
        self.assertEqual("proposed", args.status)
        enable_args = _build_parser().parse_args(["learn", "enable", "abcd"])
        self.assertEqual("enable", enable_args.learning_action)
        evaluation_args = _build_parser().parse_args(["evaluate", "--candidate-prompt", "/tmp/prompt.txt"])
        self.assertEqual("evaluate", evaluation_args.command)
        self.assertEqual("/tmp/prompt.txt", evaluation_args.candidate_prompt)


if __name__ == "__main__":
    unittest.main()
