import unittest

from analytics_agent.guardrails import extract_table_ids, validate_sql
from analytics_agent.models import ProposalError
from tests.helpers import TABLE_ID, sample_catalog


class SqlGuardrailTests(unittest.TestCase):
    def setUp(self):
        self.catalog = sample_catalog()

    def test_accepts_read_only_aggregate_on_a_table(self):
        result = validate_sql(f"SELECT SUM(vrprima) AS prima FROM `{TABLE_ID}`", self.catalog)
        self.assertEqual((TABLE_ID.lower(),), result.tables)
        self.assertEqual(64, len(result.sql_sha256))

    def test_accepts_joins_star_sensitive_and_undocumented_columns(self):
        other = "centralizacion-datos.analytics_curated.mm_final"
        sql = (
            f"SELECT p.*, m.numero_documento FROM `{TABLE_ID}` AS p "
            f"JOIN `{other}` AS m ON p.cod_sucursal = m.cod_sucursal"
        )
        result = validate_sql(sql, self.catalog)
        self.assertEqual(tuple(sorted((TABLE_ID.lower(), other.lower()))), result.tables)

    def test_rejects_mutation_and_multiple_statements(self):
        for sql in (
            f"DELETE FROM `{TABLE_ID}` WHERE cod_sucursal = 1",
            f"SELECT vrprima FROM `{TABLE_ID}`; DROP TABLE `project.dataset.t`",
            f"CREATE TABLE `project.dataset.t` AS SELECT vrprima FROM `{TABLE_ID}`",
        ):
            with self.subTest(sql=sql), self.assertRaises(ProposalError):
                validate_sql(sql, self.catalog)

    def test_rejects_unqualified_physical_table(self):
        with self.assertRaisesRegex(ProposalError, "project.dataset.table"):
            validate_sql("SELECT value FROM some_table", self.catalog)

    def test_rejects_external_query_function(self):
        sql = f"SELECT * FROM EXTERNAL_QUERY('connection', 'SELECT 1')"
        with self.assertRaisesRegex(ProposalError, "función externa"):
            validate_sql(sql, self.catalog)

    def test_accepts_cte_and_checks_underlying_source_table(self):
        sql = f"WITH base AS (SELECT SUM(vrprima) AS prima FROM `{TABLE_ID}`) SELECT prima FROM base"
        result = validate_sql(sql, self.catalog)
        self.assertEqual((TABLE_ID.lower(),), result.tables)

    def test_extracts_table_ids_for_view_checks(self):
        sql = f"SELECT p.vrprima FROM `{TABLE_ID}` AS p JOIN `centralizacion-datos.analytics_curated.mm_final` m ON TRUE"
        self.assertEqual(
            tuple(sorted((TABLE_ID.lower(), "centralizacion-datos.analytics_curated.mm_final"))),
            extract_table_ids(sql),
        )


if __name__ == "__main__":
    unittest.main()
