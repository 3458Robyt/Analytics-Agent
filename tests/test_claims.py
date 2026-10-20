import unittest
from dataclasses import replace

from analytics_agent.claims import enforce_compiled_claims, explicit_class_choices, select_payment_classes
from analytics_agent.field_resolution import UnknownFieldError, resolve_sql_fields
from analytics_agent.schema import ColumnSchema, SchemaCatalog, TableSchema
from analytics_agent.semantics import MetricDefinitionError, compile_metric_query, load_metric_definitions
from analytics_agent.memory import load_glossary


TABLE = "centralizacion-datos.analytics_curated.mm_final"


class ClaimsRulesTests(unittest.TestCase):
    def setUp(self):
        self.metrics = load_metric_definitions()

    def test_all_four_categories_compile_distinct_protected_filters(self):
        for key, label, date in (
            ("pagos_siniestros", "pagos", "fecha_pago"),
            ("reservas_avisadas", "avisado", "mes_mmalla"),
            ("avisos_recobro", "aviso de recobro", "mes_mmalla"),
            ("avisados_salvamentos", "avisado de salvamentos", "mes_mmalla"),
        ):
            with self.subTest(key=key):
                query = compile_metric_query(self.metrics[key], start_date="2026-01-01", end_date="2026-07-01")
                self.assertIn("SUM(`incurrido_total`)", query.sql)
                self.assertIn(f"`{date}` >=", query.sql)
                self.assertIn("LOWER(TRIM(`tipo`)) = @metric_filter_0", query.sql)
                self.assertEqual(label, query.parameters[0]["value"])
                self.assertNotIn("ABS(", query.sql)
                self.assertNotIn("DISTINCT", query.sql)
                self.assertNotIn("LIMIT", query.sql)

    def test_fixed_type_cannot_be_expanded_or_replaced(self):
        for item in (
            {"field": "tipo", "value": "Avisado"},
            {"field": "tipo", "operator": "in", "value": ["Pagos", "Avisado"]},
            {"field": "tipo", "operator": "contains", "value": "Pagos"},
        ):
            with self.subTest(item=item), self.assertRaises(MetricDefinitionError):
                compile_metric_query(self.metrics["pagos_siniestros"], start_date="2026-09-01", end_date="2026-10-01", filters=[item])
        query = compile_metric_query(self.metrics["pagos_siniestros"], start_date="2026-09-01", end_date="2026-10-01",
                                     filters=[{"field": "tipo", "value": "  PAGOS  "}])
        self.assertEqual(["pagos", "pagos"], [item["value"] for item in query.parameters])

    def test_payment_dates_use_business_zone_but_month_labels_stay_in_month(self):
        payment = compile_metric_query(self.metrics["pagos_siniestros"], start_date="2026-09-01", end_date="2026-10-01")
        reserve = compile_metric_query(self.metrics["reservas_avisadas"], start_date="2026-09-01", end_date="2026-11-01")
        self.assertIn("'America/Bogota'", payment.sql)
        self.assertIn("'UTC'", reserve.sql)
        self.assertIn("2026-11-01", reserve.sql)

    def test_free_sql_cannot_bypass_claim_rules_even_via_ctes(self):
        for sql in (
            f"SELECT SUM(pago_moneda_local) FROM `{TABLE}`",
            f"SELECT SUM(incurrido_total) FROM `{TABLE}` WHERE tipo = 'Avisado'",
            f"WITH x AS (SELECT incurrido_total FROM `{TABLE}`) SELECT SUM(incurrido_total) FROM x",
            f"WITH x AS (SELECT incurrido_total AS importe FROM `{TABLE}`) SELECT SUM(importe) FROM x",
        ):
            with self.subTest(sql=sql), self.assertRaisesRegex(MetricDefinitionError, "run_metric"):
                enforce_compiled_claims(sql, "Total pagos de septiembre")
        enforce_compiled_claims(f"SELECT DISTINCT clase_pago FROM `{TABLE}`", "Honorarios pagados")
        enforce_compiled_claims(f"SELECT COUNT(*) FROM `{TABLE}`", "Número de pagos")
        enforce_compiled_claims(f"SELECT SUM(incurrido_neto) FROM `{TABLE}_netos`", "Incurrido neto")

    def test_payment_classes_must_be_observed_and_unambiguous(self):
        labels = [" Honorarios ", "Indemnización", "OTROS"]
        self.assertEqual([" Honorarios "], select_payment_classes(labels, "honorarios"))
        self.assertEqual(["Indemnización"], select_payment_classes(labels, "indemnizaciones"))
        for labels in (["Honorarios", "Honorarios ajustador"], ["OTROS"], ["No indemnización"]):
            with self.subTest(labels=labels), self.assertRaises(MetricDefinitionError):
                select_payment_classes(labels, "honorarios" if "Honorarios" in labels else "indemnizaciones")
        self.assertEqual(["Honorarios ajustador"], select_payment_classes(["Honorarios ajustador"], "honorarios", explicit=["honorarios ajustador"]))
        with self.assertRaises(MetricDefinitionError):
            select_payment_classes(labels, "honorarios", explicit=["clase inventada"])

    def test_inherited_glossary_context_contains_complete_current_rules(self):
        _, entries = load_glossary("reservas avisadas")
        rule = next(item for item in entries if item.get("key") == "reservas_avisadas")
        self.assertEqual("incurrido_total", rule["value_field"])
        self.assertEqual("Avisado", rule["required_filters"][0]["value"])
        self.assertEqual("mes_mmalla", rule["date_field"])

    def test_negated_class_name_is_not_misread_as_confirmation(self):
        labels = ["Honorarios ajustador", "Honorarios abogado"]
        self.assertEqual([], explicit_class_choices("No uses Honorarios ajustador", labels))
        self.assertEqual(labels, explicit_class_choices("Incluye Honorarios ajustador y Honorarios abogado", labels))


class FieldResolutionTests(unittest.TestCase):
    def setUp(self):
        self.table = TableSchema("mm_final", TABLE, {
            name: ColumnSchema(name, kind) for name, kind in (
                ("incurrido_total", "FLOAT"), ("ramo_comercial", "STRING"),
                ("fecha_pago", "TIMESTAMP"), ("fecha_aviso", "TIMESTAMP"),
                ("tipo", "STRING"), ("clase_pago", "STRING"),
            )
        }, metadata_loaded=True)
        self.catalog = SchemaCatalog({TABLE: self.table})

    def test_corrects_verified_alias_or_clear_typo_and_reports_it(self):
        sql, notices = resolve_sql_fields(f"SELECT ramo, incurrido_totall FROM `{TABLE}`", self.catalog)
        self.assertIn("ramo_comercial", sql)
        self.assertIn("incurrido_total", sql)
        self.assertEqual(2, len(notices))

    def test_ambiguous_or_invented_field_is_not_silently_replaced(self):
        for field in ("fecha", "valor_imaginario"):
            with self.subTest(field=field), self.assertRaises(UnknownFieldError):
                resolve_sql_fields(f"SELECT {field} FROM `{TABLE}`", self.catalog)

    def test_cte_output_alias_and_order_alias_are_not_treated_as_physical_fields(self):
        sql, _ = resolve_sql_fields(
            f"WITH x AS (SELECT SUM(incurrido_total) AS total FROM `{TABLE}`) SELECT total FROM x ORDER BY total",
            self.catalog,
        )
        self.assertIn("SELECT total FROM x", sql)
        resolve_sql_fields(f"SELECT tipo AS etiqueta FROM `{TABLE}` ORDER BY etiqueta", self.catalog)

    def test_unverified_dictionary_does_not_certify_a_missing_field(self):
        self.catalog.tables[TABLE] = replace(self.table, metadata_loaded=False)
        sql, notices = resolve_sql_fields(f"SELECT campo_candidato FROM `{TABLE}`", self.catalog)
        self.assertIn("campo_candidato", sql)  # BigQuery's dry run remains authoritative
        self.assertEqual([], notices)


if __name__ == "__main__":
    unittest.main()
