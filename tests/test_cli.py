import unittest
from io import StringIO

from rich.console import Console

from analytics_agent.cli import _as_bool, _format_cell, _render_answer
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


if __name__ == "__main__":
    unittest.main()
