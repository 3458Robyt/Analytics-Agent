import sys
import types
import unittest
from datetime import date
from decimal import Decimal
from unittest.mock import patch

from analytics_agent.bigquery_adapter import BigQueryAdapter, _to_json_value


class FakePage(list):
    def __init__(self, rows, next_page_token=None):
        super().__init__(rows)
        self.next_page_token = next_page_token


class FakeIterator:
    schema = [types.SimpleNamespace(name="value")]
    total_rows = 2

    @property
    def pages(self):
        return iter([
            FakePage([{"value": Decimal("1.25")}], "next"),
            FakePage([{"value": date(2026, 1, 1)}]),
        ])


class FakeJob:
    total_bytes_processed = 800
    schema = [types.SimpleNamespace(name="value")]
    referenced_routines = ()

    def result(self, *, page_size):
        self.page_size = page_size
        return FakeIterator()


class BigQueryAdapterTests(unittest.TestCase):
    def test_page_size_is_technical_and_has_no_total_row_ceiling(self):
        adapter = BigQueryAdapter(None, location="us-east1", page_size=20)
        self.assertEqual(20, adapter.page_size)
        with self.assertRaisesRegex(ValueError, "page_size"):
            BigQueryAdapter(None, page_size=0)

    def test_decimal_and_nested_values_keep_precision(self):
        self.assertEqual("12345678901234567890.12345678", _to_json_value(Decimal("12345678901234567890.12345678")))
        self.assertEqual({"d": "2026-01-01"}, _to_json_value({"d": date(2026, 1, 1)}))

    def test_query_results_are_read_page_by_page_until_complete(self):
        config_args = []

        class FakeQueryJobConfig:
            def __init__(self, **kwargs):
                config_args.append(kwargs)

        bigquery = types.ModuleType("google.cloud.bigquery")
        bigquery.QueryJobConfig = FakeQueryJobConfig
        cloud = types.ModuleType("google.cloud")
        cloud.bigquery = bigquery
        google = types.ModuleType("google")
        google.cloud = cloud
        job = FakeJob()
        client = types.SimpleNamespace(query=lambda *args, **kwargs: job)
        adapter = BigQueryAdapter(client, location="us-east1", page_size=1)
        with patch.dict(sys.modules, {"google": google, "google.cloud": cloud, "google.cloud.bigquery": bigquery}):
            first = adapter.execute("SELECT 1", location="us-east1")
            second = adapter.read_page(first.query_id)
        self.assertEqual(({"value": "1.25"},), first.rows)
        self.assertTrue(first.has_next_page)
        self.assertEqual(({"value": "2026-01-01"},), second.rows)
        self.assertFalse(second.has_next_page)
        self.assertNotIn("maximum_bytes_billed", config_args[0])
        self.assertNotIn("job_timeout_ms", config_args[0])

    def test_dry_run_has_no_byte_ceiling_and_rejects_user_routines(self):
        config_args = []

        class FakeQueryJobConfig:
            def __init__(self, **kwargs):
                config_args.append(kwargs)

        bigquery = types.ModuleType("google.cloud.bigquery")
        bigquery.QueryJobConfig = FakeQueryJobConfig
        cloud = types.ModuleType("google.cloud")
        cloud.bigquery = bigquery
        google = types.ModuleType("google")
        google.cloud = cloud

        normal_job = types.SimpleNamespace(total_bytes_processed=10**15, referenced_routines=())
        client = types.SimpleNamespace(query=lambda *args, **kwargs: normal_job)
        adapter = BigQueryAdapter(client, location="us-east1")
        with patch.dict(sys.modules, {"google": google, "google.cloud": cloud, "google.cloud.bigquery": bigquery}):
            dry_run = adapter.dry_run("SELECT 1")
        self.assertTrue(dry_run.ok)
        self.assertEqual(10**15, dry_run.bytes_processed)
        self.assertNotIn("maximum_bytes_billed", config_args[0])

        routine = types.SimpleNamespace(project_id="p", dataset_id="d", routine_id="custom")
        client.query = lambda *args, **kwargs: types.SimpleNamespace(
            total_bytes_processed=1,
            referenced_routines=[routine],
        )
        with patch.dict(sys.modules, {"google": google, "google.cloud": cloud, "google.cloud.bigquery": bigquery}):
            rejected = adapter.dry_run("SELECT custom()")
        self.assertFalse(rejected.ok)
        self.assertIn("rutinas definidas por el usuario", rejected.error)

    def test_discovery_lists_projects_datasets_and_tables(self):
        dataset = types.SimpleNamespace(project="visible-project", dataset_id="analytics", location="us-east1")
        table = types.SimpleNamespace(table_id="visible-project.analytics.sales", table_type="TABLE", description="Sales")

        class Client:
            def list_projects(self):
                return [types.SimpleNamespace(project_id="visible-project")]

            def list_datasets(self, *, project, include_all):
                self.assert_include_all = include_all
                if project != "visible-project":
                    raise PermissionError("forbidden")
                return [dataset]

            def list_tables(self, dataset_ref):
                return [table]

        report = BigQueryAdapter(Client()).discover_tables(["seed-project"])
        self.assertEqual({"visible-project", "seed-project"}, set(report.projects))
        self.assertEqual(1, report.datasets)
        self.assertEqual("visible-project.analytics.sales", report.tables[0]["table_id"])
        self.assertTrue(any("seed-project" in warning for warning in report.warnings))


if __name__ == "__main__":
    unittest.main()
