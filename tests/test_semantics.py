import unittest

from analytics_agent.semantics import (
    MetricDefinitionError,
    compile_metric_sql,
    load_metric_definitions,
)


class MetricSemanticsTests(unittest.TestCase):
    def setUp(self):
        self.definition = load_metric_definitions()["prima_emitida"]

    def test_compiles_confirmed_metric_with_exclusive_end_and_dimension_alias(self):
        sql = compile_metric_sql(
            self.definition,
            start_date="2026-01-01",
            end_date="2026-07-01",
            group_by=["nombre_ramo_comercial"],
        )
        self.assertIn("SUM(`vrprima`) AS `prima_emitida`", sql)
        self.assertIn("`nombre_ramo_comercial` AS `ramo`", sql)
        self.assertIn("`fecha_emision` >= DATE '2026-01-01'", sql)
        self.assertIn("`fecha_emision` < DATE '2026-07-01'", sql)
        self.assertIn("GROUP BY `nombre_ramo_comercial`", sql)

    def test_rejects_invalid_period_and_unapproved_dimension(self):
        with self.assertRaises(MetricDefinitionError):
            compile_metric_sql(self.definition, start_date="2026-02-01", end_date="2026-01-01")
        with self.assertRaises(MetricDefinitionError):
            compile_metric_sql(
                self.definition,
                start_date="2026-01-01",
                end_date="2026-02-01",
                group_by=["nombre_ramo_comercial`, `documento"],
            )


if __name__ == "__main__":
    unittest.main()
