import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock

from scripts.validate_workbench import (
    BudgetClient, ValidationBudgetExceeded, cases, compare_reports, failure_status,
    provisional_claims_cases, select_cases, write_report,
)


class WorkbenchValidationTests(unittest.TestCase):
    def test_budget_rejects_mutation_before_bigquery(self):
        client = Mock()
        with self.assertRaisesRegex(ValueError, 'SELECT'):
            BudgetClient(client, 100).query('DELETE FROM `p.d.t` WHERE TRUE')
        client.query.assert_not_called()

    def test_budget_reserves_only_once_for_same_job_and_stops_at_limit(self):
        client = Mock()
        client.query.return_value = SimpleNamespace(total_bytes_processed=60)
        budget = BudgetClient(client, 100)
        budget.query('SELECT 1', job_id='stable')
        budget.query('SELECT 1', job_id='stable')
        self.assertEqual(budget.reserved, 60)
        self.assertEqual(client.query.call_count, 3)
        with self.assertRaises(ValidationBudgetExceeded):
            budget.query('SELECT 2')
        self.assertEqual(client.query.call_count, 4)

    def test_provisional_cases_are_unique_and_cover_context_followups(self):
        suite = [*cases(), *provisional_claims_cases('America/Bogota')]
        self.assertEqual(len({case[0] for case in suite}), 14)
        self.assertFalse(suite[4][4])
        self.assertTrue(suite[-1][4])
        selected = select_cases(suite, ["quitar_ramo"])
        self.assertEqual([case[0] for case in selected], ["ramo_201", "comparar_quincenas", "quitar_ramo"])

    def test_report_permissions_and_comparison(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'report.json'
            write_report(path, {'status': 'passed'})
            self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        base = {'cases': [{'name': 'case', 'status': 'passed', 'seconds': 2}]}
        result = compare_reports(base, {'cases': [{'name': 'case', 'status': 'regression'}]})
        self.assertEqual(result['common_passed'], 0)
        self.assertEqual(result['new_failures'], ['case'])
        self.assertEqual(failure_status(ConnectionError()), 'infrastructure')
