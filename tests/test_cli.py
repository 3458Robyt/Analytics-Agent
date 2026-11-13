import unittest
import os
import tempfile
from contextlib import redirect_stdout
from io import StringIO
from unittest.mock import patch

from rich.console import Console

from analytics_agent.cli import _as_bool, _build_parser, _format_cell, _render_answer, main
from analytics_agent.memory import SessionStore
from analytics_agent.execution_store import ExecutionStore
from analytics_agent.models import AgentAnswer, SummaryTable


class CliFormattingTests(unittest.TestCase):
    def test_runs_can_be_inspected_and_abandoned_without_provider_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            with SessionStore(folder) as memory:
                session = memory.create_session('Pregunta')
                turn = memory.begin_turn(session, 'Pregunta')
                owner = memory.owner
            with ExecutionStore(folder, owner=owner) as executions:
                run = executions.create(session_id=session, turn_id=turn, versions={})
            with patch.dict(os.environ, {'ANALYTICS_AGENT_STATE_DIR': folder}, clear=True), redirect_stdout(StringIO()):
                for args in (['list'], ['show', run], ['abandon', run]):
                    self.assertEqual(0, main(['runs', *args]))
            with ExecutionStore(folder, owner=owner) as executions:
                self.assertEqual('failed', executions.get(run)['status'])
            with SessionStore(folder) as memory:
                self.assertEqual('', memory.pending_turn(session))

    def test_engine_selection_and_execution_commands(self):
        args = _build_parser().parse_args(['ask', '--engine', 'langgraph', 'Hola'])
        self.assertEqual('langgraph', args.engine)
        args = _build_parser().parse_args(['chat', '--engine', 'legacy'])
        self.assertEqual('legacy', args.engine)
        for command in ('show', 'resume', 'abandon'):
            args = _build_parser().parse_args(['runs', command, 'run-1'])
            self.assertEqual(command, args.run_action)
            self.assertEqual('run-1', args.run_id)

    def test_execution_id_is_visible_only_in_audit(self):
        answer = AgentAnswer('Resultado', run_id='run-1', engine='langgraph')
        for presentation in ('audit', 'business'):
            console = Console(record=True, file=StringIO(), width=100)
            _render_answer(answer, console, presentation)
            self.assertEqual(presentation == 'audit', 'run-1' in console.export_text())

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

    def test_teams_feedback_list_cli_arguments(self):
        args = _build_parser().parse_args(["teams", "feedback", "list", "--limit", "25"])
        self.assertEqual("teams", args.command)
        self.assertEqual(25, args.limit)
        enable_args = _build_parser().parse_args(["learn", "enable", "abcd"])
        self.assertEqual("enable", enable_args.learning_action)
        evaluation_args = _build_parser().parse_args(["evaluate", "--candidate-prompt", "/tmp/prompt.txt"])
        self.assertEqual("evaluate", evaluation_args.command)
        self.assertEqual("/tmp/prompt.txt", evaluation_args.candidate_prompt)


if __name__ == "__main__":
    unittest.main()
