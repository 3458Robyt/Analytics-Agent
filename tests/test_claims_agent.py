"""Run compiled BigQuery SELECTs against a separate in-memory SQL oracle."""
import re
import sqlite3
import tempfile
import time
import unittest
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from analytics_agent.agent import AnalyticsAgent
from analytics_agent.bigquery_adapter import DryRunResult, QueryPage
from analytics_agent.memory import SessionStore
from analytics_agent.models import AgentSettings
from analytics_agent.schema import SchemaCatalog
from tests.test_agent import StubLLM, action


TABLE = "centralizacion-datos.analytics_curated.mm_final"


class ClaimsBigQuery:
    location = "us-east1"

    def __init__(self):
        self.db = sqlite3.connect(":memory:")
        self.db.row_factory = sqlite3.Row
        self.db.execute("CREATE TABLE claims(tipo TEXT, clase_pago TEXT, incurrido_total REAL, fecha_pago TEXT, mes_mmalla TEXT, ramo_comercial TEXT, pago_moneda_local REAL, fecha_accidente TEXT)")
        def row(kind, cls, amount, paid=None, month="2026-09-01T00:00:00+00:00"):
            return (kind, cls, amount, paid, month, "AUTOS", 99999, paid)
        self.db.executemany("INSERT INTO claims VALUES(?,?,?,?,?,?,?,?)", [
            row("Pagos", "Indemnización", 100, "2026-09-01T05:00:00+00:00"),
            row("Pagos", "Indemnización", -10, "2026-09-15T12:00:00+00:00"),
            row(" pAgOs ", "Honorarios", 20, "2026-09-10T12:00:00+00:00"),
            row("Pagos", None, 5, "2026-09-11T12:00:00+00:00"),
            row("Pagos", "Indemnización", 60, "2026-08-31T23:00:00+00:00"),
            row("Pagos", "Indemnización", 40, "2026-10-01T04:59:00+00:00"),
            row("Pagos", "Honorarios", 500, "2026-10-01T05:00:00+00:00"),
            row("Avisado", None, 200),
            row("Avisado", None, 50, month="2026-10-01T00:00:00+00:00"),
            row("aviso de recobro", None, -12),
            row("avisado de salvamentos", None, -6),
        ])
        self.executed = []
        self.dry_runs = []
        self.metadata_calls = 0
        self.missing_value_field = False
        self.no_metadata = False

    def inspect_table_metadata(self, table_id):
        self.metadata_calls += 1
        if self.no_metadata:
            raise RuntimeError("Metadatos no disponibles")
        kinds = {"tipo": "STRING", "clase_pago": "STRING", "incurrido_total": "FLOAT64",
                 "fecha_pago": "TIMESTAMP", "fecha_accidente": "TIMESTAMP", "mes_mmalla": "TIMESTAMP", "ramo_comercial": "STRING", "pago_moneda_local": "FLOAT64"}
        if self.missing_value_field:
            kinds.pop("incurrido_total")
        return {"table_id": table_id, "actual_schema": {k: {"name": k, "data_type": v} for k, v in kinds.items()},
                "location": self.location, "table_type": "TABLE", "description": "", "view_query": ""}

    def _query(self, sql, parameters):
        local = sql.replace(f"`{TABLE}`", "claims")
        def bound(match):
            day, zone = match.groups()
            value = datetime.fromisoformat(day).replace(tzinfo=ZoneInfo(zone)).astimezone(timezone.utc)
            return "'" + value.isoformat() + "'"
        local = re.sub(r"TIMESTAMP\(CAST\('([0-9-]+)' AS DATE\), '([^']+)'\)", bound, local)
        bindings = {}
        for param in parameters:
            name, value = param["name"], param["value"]
            if param.get("array"):
                placeholders = []
                for i, item in enumerate(value):
                    key = f"{name}_{i}"
                    placeholders.append("@" + key)
                    bindings[key] = item
                local = local.replace(f"UNNEST(@{name})", "(" + ",".join(placeholders) + ")")
            else:
                bindings[name] = value
        return self.db.execute(local, bindings)

    def dry_run(self, sql, *, location="", parameters=()):
        self.dry_runs.append(sql)
        try:
            self._query(sql, parameters).fetchall()
            return DryRunResult(True, 100)
        except sqlite3.Error as error:
            return DryRunResult(False, 0, str(error))

    def execute(self, sql, *, location="", parameters=()):
        self.executed.append((sql, parameters))
        cursor = self._query(sql, parameters)
        columns = tuple(col[0] for col in cursor.description)
        rows = tuple(dict(row) for row in cursor.fetchall())
        return QueryPage("claim-" + str(len(self.executed)), columns, rows, False, len(rows), 100,
                         column_types={col: "NUMERIC" if col not in {"clase_pago", "tipo", "ramo", "ramo_comercial"} else "STRING" for col in columns})

    def read_page(self, query_id):
        raise AssertionError("No missing fixture pages")


def metric(key, **kwargs):
    return action("run_metric", metric_key=key, start_date="2026-09-01", end_date="2026-10-01", group_by=[], filters=[], **kwargs)


def agent_for(actions, bq):
    llm = StubLLM(actions)
    agent = AnalyticsAgent(catalog=SchemaCatalog({}), llm=llm, bigquery=bq,
                           settings=AgentSettings("https://example", "model", "secret", bigquery_location="us-east1"))
    return agent, llm


class ClaimsAgentTests(unittest.TestCase):
    def setUp(self):
        self.bq = ClaimsBigQuery()
        self.addCleanup(self.bq.db.close)

    def test_small_unscoped_metrics_sum_all_history_without_inventing_dates(self):
        for key, expected in (("pagos_siniestros", 715), ("reservas_avisadas", 250)):
            with self.subTest(key=key):
                agent, _ = agent_for([metric(key), action("finish", answer="Resultado consultado.")], self.bq)
                result = agent.answer(f"Total de {key}")
                self.assertEqual(expected, result.summary_table.rows[0][key])
                self.assertNotIn("2026-09-01", self.bq.executed[-1][0])
                self.assertTrue(any("todo el histórico" in note for note in result.assumptions))

    def test_latest_all_history_scope_overrides_older_dates_for_followup(self):
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory) as store:
            session = store.create_session()
            for question in ("Septiembre de 2026", "Ahora todo el histórico"):
                turn = store.begin_turn(session, question)
                store.complete_turn(turn, "Resultado")
            agent, _ = agent_for([metric("pagos_siniestros"), action("finish", answer="Pagos consultados.")], self.bq)
            result = agent.answer("¿Y los pagos?", session_store=store, session_id=session)
            self.assertEqual(715, result.summary_table.rows[0]["pagos_siniestros"])

    def test_correct_totals_and_date_boundaries_for_all_four_types(self):
        for key, expected in (("pagos_siniestros", 155), ("reservas_avisadas", 200),
                              ("avisos_recobro", -12), ("avisados_salvamentos", -6)):
            with self.subTest(key=key):
                agent, _ = agent_for([metric(key), action("finish", answer="Resultado consultado.")], self.bq)
                result = agent.answer(f"{key} de septiembre de 2026")
                self.assertEqual(expected, result.summary_table.rows[0][key])

    def test_multimonth_reserves_are_summed_instead_of_last_cut(self):
        run = metric("reservas_avisadas")
        run["end_date"] = "2026-11-01"
        agent, _ = agent_for([run, action("finish", answer="Reservas consultadas.")], self.bq)
        result = agent.answer("Reservas de septiembre y octubre")
        self.assertEqual(250, result.summary_table.rows[0]["reservas_avisadas"])

    def test_honorarios_and_indemnizaciones_are_subsets_and_discovery_is_cached(self):
        agent, _ = agent_for([metric("honorarios"), action("finish", answer="Honorarios consultados."),
                              metric("indemnizaciones"), action("finish", answer="Indemnizaciones consultadas.")], self.bq)
        first = agent.answer("Honorarios de septiembre")
        second = agent.answer("Indemnizaciones de septiembre")
        self.assertEqual(20, first.summary_table.rows[0]["honorarios"])
        self.assertEqual(130, second.summary_table.rows[0]["indemnizaciones"])
        self.assertEqual(1, sum("SELECT DISTINCT" in sql for sql, _ in self.bq.executed))
        self.assertEqual(1, self.bq.metadata_calls)
        self.assertEqual(2, first.query_count)  # discovery is included in job/byte accounting
        self.assertEqual(1, second.query_count)

    def test_generic_payment_action_cannot_erase_an_explicit_honorarios_request(self):
        agent, _ = agent_for([metric("pagos_siniestros"), action("finish", answer="Honorarios consultados.")], self.bq)
        result = agent.answer("Total de pagos de honorarios en septiembre")
        self.assertEqual(20, result.summary_table.rows[0]["honorarios"])

    def test_including_honorarios_does_not_restrict_all_payments_to_honorarios(self):
        agent, _ = agent_for([metric("pagos_siniestros"), action("finish", answer="Todos los pagos.")], self.bq)
        result = agent.answer("Todos los pagos de septiembre, incluyendo honorarios")
        self.assertEqual(155, result.summary_table.rows[0]["pagos_siniestros"])

    def test_free_sql_is_repaired_in_same_turn_before_execution(self):
        agent, llm = agent_for([
            action("run_select", sql=f"SELECT SUM(pago_moneda_local) FROM `{TABLE}`"),
            metric("pagos_siniestros"), action("finish", answer="Pagos consultados."),
        ], self.bq)
        result = agent.answer("Total de pagos de septiembre")
        self.assertEqual(155, result.summary_table.rows[0]["pagos_siniestros"])
        self.assertEqual(1, len(self.bq.executed))
        self.assertIn("run_metric", llm.calls[2][1]["last_tool_result"]["error"])

    def test_contradictory_type_and_missing_amount_never_execute_wrong_totals(self):
        wrong = metric("pagos_siniestros")
        wrong["filters"] = [{"field": "tipo", "value": "Avisado"}]
        agent, _ = agent_for([wrong, action("finish", answer="No fue posible aplicar ese filtro.")], self.bq)
        agent.answer("Pagos de septiembre de 2026")
        self.assertEqual([], self.bq.executed)
        self.bq.missing_value_field = True
        agent, llm = agent_for([metric("pagos_siniestros"), action("finish", answer="Falta el importe requerido.")], self.bq)
        agent.answer("Pagos de septiembre de 2026")
        self.assertEqual([], self.bq.executed)
        self.assertIn("incurrido_total", llm.calls[-1][1]["last_tool_result"]["error"])

    def test_typo_is_corrected_against_live_schema_and_notice_is_preserved(self):
        agent, _ = agent_for([
            action("run_select", sql=f"SELECT incurrido_totall FROM `{TABLE}` LIMIT 1"),
            action("finish", answer="Detalle disponible.", present_rows=True),
        ], self.bq)
        result = agent.answer("Muestra incurrido_totall")
        self.assertIn("incurrido_total", self.bq.executed[0][0])
        self.assertTrue(any("incurrido_totall" in item for item in result.assumptions))

    def test_metric_dimension_uses_documented_alias_not_a_field_from_primas(self):
        wrong = metric("pagos_siniestros")
        wrong["group_by"] = ["nombre_ramo_comercial"]  # exists in primas, not in mm_final
        corrected = metric("pagos_siniestros")
        corrected["group_by"] = ["ramo"]  # documented alias of ramo_comercial in claims
        agent, llm = agent_for([wrong, corrected, action("finish", answer="Pagos por ramo.")], self.bq)
        result = agent.answer("Pagos por ramo de septiembre")
        self.assertEqual(({"ramo": "AUTOS", "pagos_siniestros": 155},), result.summary_table.rows)
        self.assertEqual(1, len(self.bq.executed))
        self.assertIn("no existe", llm.calls[2][1]["last_tool_result"]["error"])

    def test_metadata_failure_uses_dry_run_and_does_not_block_confirmed_metric(self):
        self.bq.no_metadata = True
        agent, _ = agent_for([metric("pagos_siniestros"), action("finish", answer="Pagos consultados.")], self.bq)
        result = agent.answer("Pagos de septiembre")
        self.assertEqual(155, result.summary_table.rows[0]["pagos_siniestros"])

    def test_stale_schema_is_refreshed_and_classes_cache_expires(self):
        agent, _ = agent_for([metric("honorarios"), action("finish", answer="Honorarios consultados."),
                              metric("honorarios"), action("finish", answer="Honorarios consultados.")], self.bq)
        agent.answer("Honorarios de septiembre de 2026")
        age, labels = agent._class_cache[TABLE]
        agent._class_cache[TABLE] = (age - 3601, labels)
        from dataclasses import replace
        table = agent.catalog.tables[TABLE]
        agent.catalog.tables[TABLE] = replace(table, columns={k: v for k, v in table.columns.items() if k != "incurrido_total"})
        result = agent.answer("Honorarios de septiembre de 2026")
        self.assertEqual(20, result.summary_table.rows[0]["honorarios"])
        self.assertEqual(2, self.bq.metadata_calls)
        self.assertEqual(2, sum("SELECT DISTINCT" in sql for sql, _ in self.bq.executed))

    def test_ambiguous_class_needs_explicit_reply_not_an_unverified_model_filter(self):
        self.bq.db.execute("UPDATE claims SET clase_pago = 'Honorarios ajustador' WHERE clase_pago = 'Honorarios'")
        run = metric("honorarios")
        agent, _ = agent_for([run, action("clarify", clarification={
            "question": "¿Qué etiqueta de honorarios incluyo?", "metric_key": "honorarios", "proposed_rule": ""}),
            dict(run), action("finish", answer="Honorarios consultados.")], self.bq)
        result = agent.answer("Honorarios de septiembre", on_clarification=lambda _: "Honorarios ajustador")
        self.assertEqual(20, result.summary_table.rows[0]["honorarios"])
        self.assertEqual(2, len(self.bq.executed))

    def test_parameters_and_compiled_metric_rows_are_logged_for_audit(self):
        agent, _ = agent_for([metric("pagos_siniestros"), action("finish", answer="Pagos consultados.")], self.bq)
        with tempfile.TemporaryDirectory() as directory, SessionStore(directory, owner="david") as store:
            result = agent.answer("Pagos de septiembre de 2026", session_store=store)
            actions = store.get_session(result.session_id)["turns"][0]["actions"]
            saved = next(item for item in actions if item["action"] == "run_metric")
            self.assertEqual("pagos", saved["result"]["query_parameters"][0]["value"])
            self.assertEqual(155, saved["result"]["aggregate_rows"][0]["pagos_siniestros"])

    def test_model_cannot_replace_honorarios_by_another_observed_class(self):
        run = metric("honorarios")
        run["filters"] = [{"field": "clase_pago", "value": "Indemnización"}]
        agent, llm = agent_for([run, action("finish", answer="Ese filtro contradice honorarios.")], self.bq)
        result = agent.answer("Honorarios de septiembre de 2026")
        self.assertIsNone(result.summary_table)
        self.assertEqual(1, len(self.bq.executed))  # only discovery, no financial query
        self.assertIn("contradice", llm.calls[-1][1]["last_tool_result"]["error"])
        self.assertNotIn("no devolvió filas", result.answer)

    def test_class_confirmation_is_reusable_and_scoped_to_the_owner(self):
        self.bq.db.execute("UPDATE claims SET clase_pago = 'Honorarios ajustador' WHERE clase_pago = 'Honorarios'")
        agent, _ = agent_for([
            metric("honorarios"), action("clarify", clarification={"question": "¿Qué clase incluyo?", "metric_key": "honorarios", "proposed_rule": ""}),
            metric("honorarios"), action("finish", answer="Resultado consultado."),
            metric("honorarios"), action("finish", answer="Resultado consultado."),
        ], self.bq)
        with tempfile.TemporaryDirectory() as directory:
            with SessionStore(directory, owner="david") as store:
                agent.answer("Honorarios de septiembre de 2026", session_store=store, on_clarification=lambda _: "Honorarios ajustador")
                second = agent.answer("Honorarios de septiembre de 2026", session_store=store)
                self.assertEqual(20, second.summary_table.rows[0]["honorarios"])
                self.assertEqual(["Honorarios ajustador"], store.confirmed_payment_classes(TABLE, "honorarios"))
            with SessionStore(directory, owner="otro") as other:
                self.assertEqual([], other.confirmed_payment_classes(TABLE, "honorarios"))

    def test_new_similarly_named_field_is_refreshed_before_typo_correction(self):
        agent, _ = agent_for([action("run_select", sql=f"SELECT incurrido_totall FROM `{TABLE}` LIMIT 1"),
                               action("finish", answer="Resultado consultado.", present_rows=True)], self.bq)
        metadata = self.bq.inspect_table_metadata(TABLE)
        from analytics_agent.schema import ColumnSchema, TableSchema
        agent.catalog.tables[TABLE] = TableSchema("mm_final", TABLE, {
            key: ColumnSchema(key, value["data_type"]) for key, value in metadata["actual_schema"].items()
        }, metadata_loaded=True, metadata_loaded_at=time.monotonic())
        self.bq.db.execute("ALTER TABLE claims ADD COLUMN incurrido_totall REAL DEFAULT 777")
        original = self.bq.inspect_table_metadata
        def new_metadata(table_id):
            result = original(table_id)
            result["actual_schema"]["incurrido_totall"] = {"name": "incurrido_totall", "data_type": "FLOAT64"}
            return result
        self.bq.inspect_table_metadata = new_metadata
        result = agent.answer("Muestra incurrido_totall")
        self.assertIn("incurrido_totall", self.bq.executed[0][0])
        self.assertEqual(777, result.detail_table.rows[0]["incurrido_totall"])
        self.assertEqual((), result.assumptions)


if __name__ == "__main__":
    unittest.main()
