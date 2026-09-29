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
        self.assertEqual(_format_cell(1234567), "1,234,567")
        self.assertEqual(_format_cell(1234.5), "1,234.50")
        self.assertEqual(_format_cell(None), "—")
        self.assertEqual(_format_cell(True), "Sí")

    def test_answer_renders_summary_and_table(self):
        console = Console(record=True, file=StringIO(), width=100, color_system=None)
        answer = AgentAnswer(
            answer="El total fue **1,234.50**.",
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
        self.assertIn("El total fue 1,234.50.", rendered)
        self.assertIn("Prima por ramo", rendered)
        self.assertIn("AUTOMÓVILES", rendered)
        self.assertIn("1,234.50", rendered)
        self.assertIn("Supuestos", rendered)
        self.assertIn("Aprendizaje actualizado", rendered)

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
