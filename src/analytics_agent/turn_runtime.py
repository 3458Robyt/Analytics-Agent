from __future__ import annotations

import hashlib
import json
import re
import time
from collections import Counter
from dataclasses import asdict, replace
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from sqlglot import exp, parse

from .bigquery_adapter import BigQueryAdapter, BigQueryError
from .guardrails import extract_table_ids, validate_sql
from .llm import LLMError, OpenAICompatibleProvider
from .models import AgentAnswer, AgentSettings, Clarification, SummaryTable, TABLE_IDS
from .semantics import MetricDefinitionError, compile_metric_query, load_metric_definitions
from .schema import ColumnSchema, SchemaCatalog, TableSchema, merge_bigquery_schema
from .excel_export import export_results_to_excel
from .claims import (
    enforce_compiled_claims, explicit_class_choices,
    observed_class_filter, requested_payment_class, select_payment_classes,
)
from .field_resolution import UnknownFieldError, resolve_field, resolve_sql_fields
from .query_scope import DateScopeRequired, all_history, has_period, needs_period
from .text_encoding import INPUT_REPAIR_NOTICE, dumps_json, repair_text
from .analysis_context import branch_scope_filter, requested_metrics, resolve_analysis, wants_branch_code, removes_branch_filter
from .analysis_sql import ROUTINE_ID, compile_analysis, routine_roles
from .organization import OrganizationCatalog
from .presentation import analysis_presentation

class TurnRuntime:

    def __init__(self, agent, question, **kwargs):
        self.agent = agent
        self.bigquery = agent.bigquery
        self.question = question
        defaults = dict(context='', on_progress=None, on_clarification=None, session_store=None, session_id='', pending_turn_id='', clarification_response='')
        defaults.update(kwargs)
        self.__dict__.update(defaults)

    def run(self):
        initial = self.initialize()
        if initial is not None:
            return initial
        while True:
            self.decide()
            answer = self.perform()
            if answer is not None:
                return answer

    def run_graph(self):
        """Execute the shared decision/action stages through LangGraph.

        Used by callers that explicitly omit SessionStore. CLI and Teams use
        the persistent driver in durable_graph instead.
        """
        from langgraph.graph import StateGraph, START, END

        def initialize(state):
            answer = self.initialize()
            if answer is not None:
                self.graph_answer = answer
            return {'done': answer is not None}

        def decide(state):
            self.decide()
            return {'action': self.action['action']}

        def perform(state):
            answer = self.perform()
            if answer is not None:
                self.graph_answer = answer
            return {'done': answer is not None}

        graph = StateGraph(dict)
        graph.add_node('load_context', initialize)
        graph.add_node('decide', decide)
        for name in ('query', 'tool', 'review', 'clarify'):
            graph.add_node(name, perform)
            graph.add_conditional_edges(name, lambda s: 'done' if s['done'] else 'next',
                                        {'done': END, 'next': 'decide'})
        graph.add_edge(START, 'load_context')
        graph.add_conditional_edges('load_context', lambda s: 'done' if s['done'] else 'next',
                                    {'done': END, 'next': 'decide'})
        graph.add_conditional_edges('decide', lambda s:
            'query' if s['action'] in {'run_select', 'run_metric', 'run_analysis', 'read_page'} else
            'review' if s['action'] == 'finish' else
            'clarify' if s['action'] == 'clarify' else 'tool')
        graph.compile().invoke({}, {'recursion_limit': 512})
        return self.graph_answer

    def export_results(self, *args, **kwargs):
        if getattr(self, 'graph_run_id', ''):
            kwargs['export_key'] = f'{self.graph_run_id}:{self.graph_action_index}'
        return export_results_to_excel(*args, **kwargs)

    def collect_query_pages(self, page, jobs):
        rows = list(page.rows)
        self.agent._record_page_job(page, jobs)
        while page.has_next_page:
            page = self.bigquery.read_page(page.query_id)
            rows.extend(page.rows)
            self.agent._record_page_job(page, jobs)
        return page, rows

    def progress(self, message: str):
        if self.on_progress is not None:
            self.on_progress(message)

    def persist_query_result(self, query_id: str):
        if self.session_store is None or not self.turn_id or query_id not in self.query_data:
            return
        data = self.query_data[query_id]
        self.session_store.save_query_result(query_id, session_id=self.session_id, turn_id=self.turn_id, question=self.question, columns=data.get('columns', []), column_types=data.get('column_types', {}), rows=data.get('rows', []), sql=self.query_sql.get(query_id, ''), tables=self.query_tables.get(query_id, ()), metadata={'result_role': data.get('result_role', 'answer'), 'parameters': list(self.query_parameter_values.get(query_id, ())), **data.get('metadata', {})})

    def ensure_schema(self, table_id: str, *, refresh: bool=False):
        key = table_id.lower()
        if key in self.unavailable_metadata:
            return None
        try:
            return self.agent._ensure_table(table_id, session_store=self.session_store, force_refresh=refresh)
        except Exception:
            self.unavailable_metadata.add(key)
            if refresh and key in self.agent.catalog.tables:
                self.agent.catalog.tables[key] = replace(self.agent.catalog.tables[key], metadata_loaded=False)
            return None

    def check_date_scope(self, sql: str, estimated_bytes: int | None, *, analysis_scope: Mapping[str, Any] | None=None):
        supplied = '\n'.join([self.question, *self.explicit_scope_inputs])
        inherited = self.session_store.temporal_context(self.session_id) if self.session_store is not None and hasattr(self.session_store, 'temporal_context') else ''
        if analysis_scope is not None:
            if analysis_scope.get('start_date') or any(('todo el histórico' in note for note in analysis_scope.get('assumptions', ()))):
                return
            supplied = inherited = ''
        elif all_history(supplied) or has_period(supplied) or has_period(inherited) or all_history(inherited) or self.active_analysis.get('start_date') or any(('todo el histórico' in note for note in self.active_analysis.get('assumptions', ()))):
            return
        tables = {str(rule['table_id']).lower() for rule in self.agent.metric_definitions.values()}
        tables.update((key for key, table in self.agent.catalog.tables.items() if any((col.data_type.upper() in {'DATE', 'TIMESTAMP', 'DATETIME'} for col in table.columns.values()))))
        if needs_period(sql, self.question, temporal_tables=tables) and (estimated_bytes is None or estimated_bytes > self.agent.settings.unscoped_query_threshold_bytes):
            self.record_query(sql, estimated_bytes, 'needs_date_scope')
            raise DateScopeRequired(estimated_bytes)

    def record_query(self, sql: str, estimate: int | None, status: str, error: str=''):
        if self.session_store is not None and hasattr(self.session_store, 'record_query'):
            self.session_store.record_query(sql, estimate, status, error, attempt_key=str(self.sequence))

    def discover_payment_classes(self, table_id: str, location: str):
        cached = self.agent._class_cache.get(table_id.lower())
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        checked = validate_sql(f"SELECT DISTINCT clase_pago FROM `{table_id}` WHERE LOWER(TRIM(tipo)) = 'pagos' AND clase_pago IS NOT NULL ORDER BY clase_pago", self.agent.catalog)
        started = time.perf_counter()
        dry = self.bigquery.dry_run(checked.sql, location=location)
        self.stage_timings['dry_run'] = self.stage_timings.get('dry_run', 0.0) + time.perf_counter() - started
        if not dry.ok:
            self.record_query(checked.sql, dry.bytes_processed, 'error', dry.error or 'dry run fallido')
            raise BigQueryError(dry.error or 'No se pudo descubrir clase_pago')
        self.check_date_scope(checked.sql, dry.bytes_processed)
        started = time.perf_counter()
        page = first = self.bigquery.execute(checked.sql, location=location)
        self.record_query(checked.sql, dry.bytes_processed, 'executed')
        self.query_count += 1
        self.query_bytes[first.query_id] = int(first.bytes_processed or dry.bytes_processed or 0)
        self.bytes_processed = sum(self.query_bytes.values())
        page, rows = self.collect_query_pages(page, self.query_jobs)
        self.stage_timings['query'] = self.stage_timings.get('query', 0.0) + time.perf_counter() - started
        self.query_sql[first.query_id] = checked.sql
        self.query_tables[first.query_id] = checked.tables
        self.tables_used.update(checked.tables)
        values = tuple(dict.fromkeys((str(row['clase_pago']) for row in rows if row.get('clase_pago') is not None)))
        self.agent._class_cache[table_id.lower()] = (time.monotonic(), values)
        self.sequence += 1
        result = {'action': 'payment_class_values', 'table_id': table_id, 'values': values, 'sql': checked.sql, 'query_id': first.query_id, 'all_pages_read': True}
        if self.session_store is not None:
            self.session_store.log_action(self.turn_id, self.sequence, 'discover_payment_classes', {'sql': checked.sql}, result)
        self.action_history.append(result)
        return values

    def verified_routine(self):
        cached = self.agent._routine_cache.get(ROUTINE_ID)
        if cached and time.monotonic() - cached[0] < 3600:
            return cached[1]
        cache_key = 'routine:' + ROUTINE_ID
        metadata = self.session_store.get_schema_metadata(cache_key) if self.session_store is not None else None
        if metadata is not None:
            routine_roles(metadata)
        else:
            try:
                metadata = self.bigquery.inspect_routine_metadata(ROUTINE_ID)
            except Exception:
                checked = validate_sql("SELECT p.parameter_name, p.data_type, p.ordinal_position, r.routine_type, r.routine_body, r.external_language FROM `analytics-487218.functions.INFORMATION_SCHEMA.PARAMETERS` AS p JOIN `analytics-487218.functions.INFORMATION_SCHEMA.ROUTINES` AS r ON p.specific_name = r.specific_name WHERE p.specific_name = 'prima_devengada' AND p.ordinal_position > 0 ORDER BY p.ordinal_position")
                started = time.perf_counter()
                dry = self.bigquery.dry_run(checked.sql, location=self.agent.settings.bigquery_location or 'us-east1')
                self.stage_timings['dry_run'] = self.stage_timings.get('dry_run', 0.0) + time.perf_counter() - started
                if not dry.ok:
                    raise MetricDefinitionError('No se pudo verificar la firma de la función de devengada: ' + str(dry.error))
                started = time.perf_counter()
                first = page = self.bigquery.execute(checked.sql, location=self.agent.settings.bigquery_location or 'us-east1')
                self.record_query(checked.sql, dry.bytes_processed, 'executed')
                self.query_count += 1
                page, rows = self.collect_query_pages(page, self.query_jobs)
                self.stage_timings['query'] = self.stage_timings.get('query', 0.0) + time.perf_counter() - started
                self.query_bytes[first.query_id] = int(first.bytes_processed or dry.bytes_processed or 0)
                self.bytes_processed = sum(self.query_bytes.values())
                self.query_sql[first.query_id], self.query_tables[first.query_id] = (checked.sql, checked.tables)
                self.tables_used.update(checked.tables)
                if not rows or any((row.get('routine_type') != 'FUNCTION' or row.get('routine_body') != 'SQL' or row.get('external_language') for row in rows)):
                    raise MetricDefinitionError('No se pudo verificar que prima_devengada sea una función SQL interna')
                metadata = {'routine_id': ROUTINE_ID, 'routine_type': 'SCALAR_FUNCTION', 'language': 'SQL', 'arguments': [{'name': row['parameter_name'], 'data_type': row['data_type']} for row in sorted(rows, key=lambda row: int(row['ordinal_position']))]}
                routine_roles(metadata)
                self.sequence += 1
                event = {'action': 'routine_signature', 'result_role': 'metadata', 'query_id': first.query_id, 'signature': metadata}
                if self.session_store is not None:
                    self.session_store.log_action(self.turn_id, self.sequence, 'routine_signature', {'sql': checked.sql}, event)
                self.action_history.append(event)
            routine_roles(metadata)
            if self.session_store is not None:
                self.session_store.set_schema_metadata(cache_key, metadata)
        self.agent._routine_cache[ROUTINE_ID] = (time.monotonic(), metadata)
        return metadata

    def prepare_analysis(self, action: Mapping[str, Any], failed_components: Mapping[str, str] | None=None):
        employee_input = '\n'.join(dict.fromkeys([self.question, *self.explicit_scope_inputs]))
        spec = resolve_analysis(action, employee_input, today=self.local_now.date(), active=self.active_analysis, organization=self.agent.organization)
        if has_period(employee_input) and (not spec.start_date) and (not all_history(employee_input)):
            raise MetricDefinitionError('Convierte el periodo pedido a start_date y end_date exclusivo; no ejecutes todo el histórico')
        if any((key not in self.agent.metric_definitions for key in spec.metric_keys)):
            raise MetricDefinitionError('Una de las métricas no tiene una definición revisada')
        self.active_analysis = spec.to_dict()
        components = []
        for key in spec.metric_keys:
            rule = self.agent.metric_definitions[key]
            components.extend((rule['numerator'], rule['denominator']) if rule.get('kind') == 'ratio' else (key,))
        definitions = {key: dict(value) for key, value in self.agent.metric_definitions.items()}
        unavailable = dict(failed_components or {})
        signature = None
        for key in dict.fromkeys(components):
            rule = definitions[key]
            table = self.ensure_schema(rule['table_id'])
            if rule.get('kind') == 'routine':
                try:
                    signature = self.verified_routine()
                except Exception as error:
                    unavailable[key] = str(error)
            if rule.get('payment_class') or (key in {'pagos_siniestros', 'honorarios', 'indemnizaciones'} and any((item.get('field') == 'clase_pago' for item in spec.filters))):
                try:
                    values = self.discover_payment_classes(rule['table_id'], (table.location if table else '') or self.agent.settings.bigquery_location)
                    goal = rule.get('payment_class', '')
                    explicit = []
                    for reply in self.class_replies.get(goal, []):
                        explicit = explicit_class_choices(reply, values) or explicit
                    reader = getattr(self.session_store, 'confirmed_payment_classes', None)
                    if not explicit and callable(reader):
                        explicit = reader(rule['table_id'], goal)
                    if goal:
                        labels = select_payment_classes(values, goal, explicit=explicit)
                        rule['required_filters'] = [*rule.get('required_filters', []), {'field': 'clase_pago', 'operator': 'in', 'value': labels}]
                    if any((item.get('field') == 'clase_pago' for item in spec.filters)):
                        spec = replace(spec, filters=tuple(observed_class_filter(values, spec.filters)))
                except (MetricDefinitionError, BigQueryError) as error:
                    unavailable[key] = str(error)
        self.active_analysis = spec.to_dict()
        return compile_analysis(spec, definitions, tables=self.agent.catalog.tables, timezone=self.agent.settings.timezone, routine_metadata=signature, unavailable=unavailable)

    def save_explicit_definition(self, clarification: Clarification, reply: str):
        if clarification.reason == 'date_scope':
            return
        if not (self.session_store is not None and clarification.metric_key and clarification.proposed_rule):
            return
        normalized_reply = ' '.join(reply.lower().split())
        confirmations = ('sí', 'si', 'confirmo', 'correcto', 'de acuerdo', 'cuenta todo', 'incluye todos', 'incluir todos', 'usa esa regla', 'aplica esa regla')
        if not any((normalized_reply == prefix or normalized_reply.startswith(prefix + ' ') or normalized_reply.startswith(prefix + ',') or normalized_reply.startswith(prefix + ';') for prefix in confirmations)):
            return
        try:
            self.session_store.confirm_business_definition(clarification.metric_key, clarification.proposed_rule, clarification.question, reply)
        except ValueError:
            return

    def pending_clarification_answer(self, clarification: Clarification):
        if self.session_store is not None and self.turn_id:
            self.session_store.set_pending_clarification(self.turn_id, {'question': clarification.question, 'metric_key': clarification.metric_key, 'proposed_rule': clarification.proposed_rule, 'reason': clarification.reason, 'estimated_bytes': clarification.estimated_bytes})
        return AgentAnswer(answer='La consulta espera la aclaración de negocio para continuar.', assumptions=tuple(dict.fromkeys([*(str(item) for item in (self.analysis_plan or {}).get('assumptions', []) if item), *self.field_notices])), query_count=self.query_count, tables_used=tuple(sorted(self.tables_used)), bytes_processed=self.bytes_processed, usage=self.total_usage, session_id=self.session_id, status='needs_clarification', clarification=clarification, audit=self.agent._audit_records(self.query_sql, self.query_tables, self.query_jobs, self.query_bytes, self.query_parameter_values), timings={**self.stage_timings, 'total': time.perf_counter() - self.turn_started}, turn_id=self.turn_id)

    def best_effort_answer(self, reason: str):
        for pending_id in tuple(self.pending_queries):
            try:
                while pending_id in self.pending_queries:
                    page = self.bigquery.read_page(pending_id)
                    if not page.has_next_page:
                        self.pending_queries.discard(pending_id)
                    page_result = self.agent._page_result(page)
                    self.query_data.setdefault(pending_id, {'columns': list(page.columns), 'rows': []})['rows'].extend(page_result['rows'])
                    self.bytes_processed += int(page.bytes_processed or 0)
                    self.agent._record_page_job(page, self.query_jobs)
            except Exception as page_error:
                self.action_history.append({'action': 'read_page', 'request': {'query_id': pending_id}, 'result': {'error': str(page_error)}})
                self.pending_queries.discard(pending_id)
        metric = str((self.analysis_plan or {}).get('metric') or self.question).strip()
        period = str((self.analysis_plan or {}).get('period') or 'el periodo pedido').strip()
        verified_summary: SummaryTable | None = None
        for query_id in reversed(list(self.query_data)):
            data = self.query_data[query_id]
            rows = data.get('rows', [])
            columns = tuple((str(item) for item in data.get('columns', [])))
            if query_id in self.aggregate_query_ids and query_id not in self.pending_queries and rows and columns and (data.get('result_role', 'answer') == 'answer'):
                verified_summary = self.agent._summary_from_evidence(None, self.query_data, self.aggregate_query_ids, query_id)
                break
        latest_error = ''
        attempted_sql = ''
        for entry in reversed(self.action_history):
            result = entry.get('result', {})
            if not latest_error and result.get('error'):
                latest_error = str(result['error'])
            if not attempted_sql:
                attempted_sql = str(entry.get('request', {}).get('sql') or '')
            if latest_error and attempted_sql:
                break
        if verified_summary is not None:
            answer_text = self.agent._result_sentence(f'BigQuery ejecutó una consulta agregada para **{metric}** ({period}) y devolvió {len(verified_summary.rows)} filas. La tabla contiene los valores calculados directamente por SQL.', verified_summary)
            answer = AgentAnswer(answer=answer_text, assumptions=tuple(dict.fromkeys([*(str(item) for item in (self.analysis_plan or {}).get('assumptions', []) if item), *self.field_notices])), summary_table=verified_summary, query_count=self.query_count, tables_used=tuple(sorted(self.tables_used)), bytes_processed=self.bytes_processed, usage=self.total_usage, session_id=self.session_id, query_jobs=tuple(self.query_jobs.values()), audit=self.agent._audit_records(self.query_sql, self.query_tables, self.query_jobs, self.query_bytes, self.query_parameter_values), timings={**self.stage_timings, 'total': time.perf_counter() - self.turn_started}, turn_id=self.turn_id, artifacts=tuple(self.artifacts))
            if self.session_store is not None and self.turn_id:
                stored_summary = asdict(verified_summary)
                self.session_store.complete_turn(self.turn_id, answer.answer, answer.assumptions, stored_summary)
                if self.active_analysis:
                    self.session_store.set_analysis_state(self.session_id, self.active_analysis)
            return answer
        details = f' BigQuery informó: {latest_error}.' if latest_error else ''
        sql_note = f'\n\nÚltimo SQL probado:\n```sql\n{attempted_sql}\n```' if attempted_sql else ''
        answer_text = f'**Interpretación:** {metric}; {period}.\n\nNo pude verificar una cifra en esta ejecución porque {reason}.{details} La fuente candidata más probable está entre `mm_final_netos`, `mm_final` y `th_primas_final`; el valor requiere que BigQuery acepte la consulta. No invento un resultado numérico.{sql_note}'
        answer = AgentAnswer(answer=answer_text, assumptions=tuple((str(item) for item in (self.analysis_plan or {}).get('assumptions', []) if item)), status='failed', query_count=self.query_count, tables_used=tuple(sorted(self.tables_used)), bytes_processed=self.bytes_processed, usage=self.total_usage, session_id=self.session_id, query_jobs=tuple(self.query_jobs.values()), audit=self.agent._audit_records(self.query_sql, self.query_tables, self.query_jobs, self.query_bytes, self.query_parameter_values), timings={**self.stage_timings, 'total': time.perf_counter() - self.turn_started}, turn_id=self.turn_id, artifacts=tuple(self.artifacts))
        if self.session_store is not None and self.turn_id:
            self.session_store.complete_turn(self.turn_id, answer.answer, answer.assumptions)
        return answer

    def initialize(self):
        self.question = self.question.strip()
        self.explicit_scope_inputs = [self.question, self.clarification_response]
        self.turn_started = time.perf_counter()
        self.resumed_turn: dict[str, Any] | None = None
        if self.pending_turn_id:
            if self.session_store is None or not self.clarification_response.strip():
                raise ValueError('Reanudar un turno requiere SessionStore y una aclaración no vacía')
            self.resumed_turn = self.session_store.resume_pending_turn(self.pending_turn_id, self.clarification_response)
            self.question = str(self.resumed_turn['question'])
        if not self.question:
            return AgentAnswer('Escribe una pregunta para iniciar el análisis.')
        self.turn_id = ''
        if self.session_store is not None:
            if self.resumed_turn is not None:
                self.session_id = str(self.resumed_turn['session_id'])
                self.turn_id = str(self.resumed_turn['turn_id'])
            else:
                if not self.session_id:
                    self.session_id = self.session_store.create_session(self.question)
                self.turn_id = self.session_store.begin_turn(self.session_id, self.question)
            self.session_store.active_turn_id = self.turn_id
        self.retrieved_memory = ''
        self.glossary_context = ''
        self.local_now = datetime.now(ZoneInfo(self.agent.settings.timezone))
        self.active_analysis = self.session_store.get_analysis_state(self.session_id) if self.session_store is not None and hasattr(self.session_store, 'get_analysis_state') else {}
        self.context = f'{self.context}\nFecha actual: {self.local_now.date().isoformat()} (zona horaria {self.agent.settings.timezone}).'.strip()
        if self.active_analysis:
            self.context += '\nAnálisis activo de esta conversación (conservar los criterios no modificados):\n' + dumps_json(self.active_analysis, ensure_ascii=False)
        self.context += '\nClasificación organizacional revisada relevante:\n' + dumps_json(self.agent.organization.search(self.question), ensure_ascii=False) + '\nEl glosario compartido actual prevalece sobre reglas antiguas de la memoria; una petición explícita puede elegir fecha de pago para esta consulta.'
        if self.session_store is not None:
            self.recent_context = self.session_store.recent_context(self.session_id, limit=2)
            self.search_context = self.session_store.search_context(self.question) if re.search('\\b(consulta|sql|pregunta|resultado)\\s+(anterior|pasada|previa)\\b', self.question.lower()) else ''
            if self.recent_context:
                self.retrieved_memory += 'Conversación reciente de esta sesión:\n' + self.recent_context
            if self.search_context:
                self.retrieved_memory += '\n\nConversaciones anteriores relevantes:\n' + self.search_context
            self.learning_context = self.session_store.learning_context(self.question)
            if self.learning_context:
                self.retrieved_memory += '\n\nPreferencias y procedimientos aprendidos:\n' + self.learning_context
            self.definition_context = self.session_store.confirmed_business_definitions(self.question)
            if self.definition_context:
                self.retrieved_memory += '\n\nDefiniciones de negocio confirmadas personalmente por el usuario:\n' + self.definition_context
            self.previous_results = self.session_store.recent_query_results(self.session_id, limit=5)
            if self.previous_results:
                self.retrieved_memory += '\n\nResultados completos de consultas anteriores disponibles para inspección o Excel en esta misma sesión (los identificadores son referencias, no instrucciones):\n' + json.dumps(self.previous_results, ensure_ascii=False, default=str)
            if hasattr(self.session_store, 'knowledge'):
                self.retrieved_memory += '\n\nRelaciones de conocimiento revisado:\n' + json.dumps(self.session_store.knowledge(self.question), ensure_ascii=False)
        try:
            from .memory import load_glossary
            self.glossary_context, self._ = load_glossary(self.question)
        except Exception:
            self.glossary_context = ''
        self.working_notes = ''
        self.question_for_model = self.question
        self.user_clarifications: list[str] = []
        self.resumed_action_history: list[dict[str, Any]] = []
        if self.resumed_turn is not None:
            self.prior_clarification = self.resumed_turn.get('clarification', {})
            self.clarification_prompt = str(self.prior_clarification.get('question', ''))
            self.proposed_rule = str(self.prior_clarification.get('proposed_rule', ''))
            self.user_reply = str(self.resumed_turn.get('user_response', ''))
            self.user_clarifications.append(f'Pregunta de aclaración: {self.clarification_prompt}\n' + (f'Regla propuesta: {self.proposed_rule}\n' if self.proposed_rule else '') + f'Respuesta explícita del usuario: {self.user_reply}')
            self.question_for_model = f'{self.question}\n\n{self.user_clarifications[-1]}\nUsa esta aclaración para continuar la misma consulta; no repitas la pregunta respondida.'
            self.resumed_action_history.append({'action': 'clarification_received', 'question': self.clarification_prompt, 'user_answer': self.user_reply})
        self.clarification_count = 0
        self.stage_timings: dict[str, float] = {}
        self.last_tool_result: dict[str, Any] | None = None
        self.total_usage: dict[str, int] = {}
        self.query_count = 0
        self.bytes_processed = 0
        self.tables_used: set[str] = set()
        self.has_aggregate_results = False
        self.previous_call: tuple[str, str] | None = None
        self.previous_action_id: str | None = None
        self.seen_inspections: set[str] = set()
        self.executed_selects: set[str] = set()
        self.pending_queries: set[str] = set()
        self.action_history: list[dict[str, Any]] = list(self.resumed_action_history)
        self.query_data: dict[str, dict[str, Any]] = {}
        self.aggregate_query_ids: set[str] = set()
        self.query_jobs: dict[str, dict[str, str]] = {}
        self.query_sql: dict[str, str] = {}
        self.query_tables: dict[str, tuple[str, ...]] = {}
        self.query_bytes: dict[str, int] = {}
        self.query_parameter_values: dict[str, tuple[dict[str, Any], ...]] = {}
        self.metric_query_ids: set[str] = set()
        self.artifacts: list[dict[str, Any]] = []
        self.field_notices: list[str] = []
        self.unavailable_metadata: set[str] = set()
        self.refreshed_schema: set[str] = set()
        self.class_replies: dict[str, list[str]] = {}
        if self.resumed_turn is not None:
            self.key = str(self.resumed_turn.get('clarification', {}).get('metric_key', ''))
            self.class_replies[self.key] = [str(self.resumed_turn.get('user_response', ''))]
        self.analysis_plan: dict[str, Any] | None = {'metric': self.question, 'period': 'El indicado por el usuario; si hay ambigüedad, aplicar y declarar un supuesto razonable.', 'grain': 'El solicitado; si no se indica, devolver el total del periodo.', 'population': 'Los registros accesibles de la fuente más probable.', 'filters': [], 'sources': list(TABLE_IDS.values()), 'steps': ['Probar una consulta SELECT directa y corregirla con la validación de BigQuery.'], 'assumptions': []}
        self.review_feedback = ''
        self.review_attempts = 0
        self.review_limit = 2
        self.plan_count = 0
        self.sequence = self.session_store.next_action_sequence(self.turn_id) if self.session_store is not None and self.turn_id else 0
        if self.resumed_turn is not None:
            self.prior_clarification = self.resumed_turn.get('clarification', {})
            self.clarification = Clarification(question=str(self.prior_clarification.get('question', '')), metric_key=str(self.prior_clarification.get('metric_key', '')), proposed_rule=str(self.prior_clarification.get('proposed_rule', '')), reason=str(self.prior_clarification.get('reason', 'business_definition')), estimated_bytes=self.prior_clarification.get('estimated_bytes'))
            self.reply = str(self.resumed_turn.get('user_response', ''))
            self.save_explicit_definition(self.clarification, self.reply)
            self.confirmation_event = {'action': 'clarification_received', 'question': self.clarification.question, 'user_answer': self.reply}
            if self.session_store is not None:
                self.session_store.log_action(self.turn_id, self.sequence, 'clarification_received', {}, self.confirmation_event)
                self.sequence += 1

    def decide(self):
        try:
            return self._decide()
        except Exception as exc:
            if self.session_store is not None and self.turn_id:
                if hasattr(self.session_store, 'snapshot_request'):
                    self.session_store.snapshot_request(self.session_id, self.turn_id, self.stage_timings,
                        self.total_usage, self.query_count, self.bytes_processed)
                self.session_store.complete_turn(self.turn_id,
                    f'La pregunta quedó incompleta por un error: {type(exc).__name__}.')
            raise

    def _decide(self):
        self.llm_started = time.perf_counter()
        try:
            self.action, self.usage = self.agent.llm.next_action(self.question_for_model, context=self.context, working_notes=self.working_notes, last_tool_result=self.last_tool_result, action_history=self.action_history[-20:], analysis_plan=self.analysis_plan, retrieved_memory=self.retrieved_memory, glossary_context=self.glossary_context, review_feedback=self.review_feedback, catalog_context=self.agent.catalog.prompt_text(query=self.question) if not self.action_history else '', user_clarification='\n'.join(self.user_clarifications))
        finally:
            self.stage_timings['model'] = self.stage_timings.get('model', 0.0) + time.perf_counter() - self.llm_started
        self.agent._add_usage(self.total_usage, self.usage)

    def perform(self):
        try:
            if self.action['notes'].strip():
                self.working_notes = self.action['notes'].strip()
            if self.action['action'] == 'plan':
                self.plan_count += 1
                self.analysis_plan = self.action['plan']
                if self.plan_count == 1:
                    self.progress('Interpretación inicial: ' + '; '.join((value for value in (str(self.analysis_plan.get('metric', '')).strip(), str(self.analysis_plan.get('period', '')).strip(), str(self.analysis_plan.get('grain', '')).strip()) if value)))
                    self.tool_result = {'action': 'plan', 'plan': self.analysis_plan}
                    self.sequence += 1
                    if self.session_store is not None:
                        self.session_store.log_action(self.turn_id, self.sequence, 'plan', self.action, self.tool_result)
                    self.action_history.append({'action': 'plan', 'plan': self.analysis_plan})
                    self.last_tool_result = self.tool_result
                    return None
                if self.plan_count > 3:
                    return self.best_effort_answer('el agente repitió la planificación y no avanzó a una consulta')
                self.metric_text = f"{self.question} {self.analysis_plan.get('metric', '')}".lower()
                if self.plan_count == 2:
                    self.action = {**self.action, 'action': 'search_tables', 'search_text': self.metric_text, 'offset': 0, 'page_size': 100, 'table_ids': []}
                else:
                    if any((term in self.metric_text for term in ('prima', 'vrprima', 'emisi', 'ramo'))):
                        self.candidate = TABLE_IDS['th_primas_final']
                    elif re.search('\\bnet[oa]s?\\b', self.metric_text):
                        self.candidate = TABLE_IDS['mm_final_netos']
                    else:
                        self.candidate = TABLE_IDS['mm_final']
                    self.action = {**self.action, 'action': 'run_select', 'sql': f'SELECT * FROM `{self.candidate}` LIMIT 0', 'table_ids': []}
                self.last_tool_result = {'action': 'plan_already_recorded', 'instruction': 'No vuelvas a planificar. Usa las fuentes candidatas, prueba SQL directamente y acepta supuestos razonables.'}
                self.progress('El plan ya está registrado; ' + ('busco la fuente candidata.' if self.plan_count == 2 else 'consulto directamente el esquema con SQL para poder continuar.'))
            if self.action['action'] == 'clarify':
                self.raw_clarification = self.action['clarification']
                self.clarification = Clarification(question=self.raw_clarification['question'].strip(), metric_key=self.raw_clarification['metric_key'].strip(), proposed_rule=self.raw_clarification['proposed_rule'].strip())
                self.sequence += 1
                self.clarification_event = {'action': 'clarify', 'question': self.clarification.question, 'metric_key': self.clarification.metric_key, 'proposed_rule': self.clarification.proposed_rule}
                self.action_history.append(self.clarification_event)
                if self.session_store is not None:
                    self.session_store.log_action(self.turn_id, self.sequence, 'clarify', self.action, self.clarification_event)
                if self.on_clarification is None:
                    return self.pending_clarification_answer(self.clarification)
                self.clarification_count += 1
                if self.clarification_count > 2:
                    return self.pending_clarification_answer(self.clarification)
                self.progress(self.clarification.question)
                try:
                    self.reply = self.on_clarification(self.clarification).strip()
                except (EOFError, KeyboardInterrupt):
                    return self.pending_clarification_answer(self.clarification)
                if not self.reply:
                    return self.pending_clarification_answer(self.clarification)
                self.save_explicit_definition(self.clarification, self.reply)
                self.explicit_scope_inputs.append(self.reply)
                self.class_replies.setdefault(self.clarification.metric_key, []).append(self.reply)
                self.previous_action_id = None
                self.previous_call = None
                self.sequence += 1
                self.confirmation_event = {'action': 'clarification_received', 'question': self.clarification.question, 'user_answer': self.reply}
                self.action_history.append(self.confirmation_event)
                if self.session_store is not None:
                    self.session_store.log_action(self.turn_id, self.sequence, 'clarification_received', {}, self.confirmation_event)
                self.user_clarifications.append(f'Pregunta de aclaración: {self.clarification.question}\nRespuesta explícita del usuario: {self.reply}')
                self.question_for_model = f'{self.question}\n\n{self.user_clarifications[-1]}\nUsa esta aclaración para continuar la misma consulta; no repitas la pregunta respondida.'
                self.last_tool_result = {'action': 'clarification_received', 'question': self.clarification.question, 'user_answer': self.reply, 'instruction': 'Continúa con la consulta o solicita únicamente otra decisión material aún pendiente.'}
                if self.session_store is not None:
                    self.recent_definitions = self.session_store.confirmed_business_definitions(self.question)
                    if self.recent_definitions:
                        self.retrieved_memory += '\n\nDefiniciones de negocio confirmadas personalmente por el usuario:\n' + self.recent_definitions
                return None
            if self.action['action'] == 'finish':
                if self.pending_queries:
                    self.query_id = sorted(self.pending_queries)[0]
                    self.progress('Completando páginas pendientes antes de cerrar el análisis.')
                    self.page = self.bigquery.read_page(self.query_id)
                    if not self.page.has_next_page:
                        self.pending_queries.discard(self.query_id)
                    self.page_result = self.agent._page_result(self.page)
                    self.query_data.setdefault(self.query_id, {'columns': list(self.page.columns), 'rows': []})['rows'].extend(self.page_result['rows'])
                    self.agent._record_page_job(self.page, self.query_jobs)
                    self.tool_result = {'action': 'query_page', **self.page_result}
                    self.sequence += 1
                    if self.session_store is not None:
                        self.session_store.log_action(self.turn_id, self.sequence, 'read_page', {'query_id': self.query_id}, self.tool_result, aggregate=self.query_id in self.aggregate_query_ids)
                    self.action_history.append(self.agent._history_entry('read_page', {'query_id': self.query_id}, self.tool_result))
                    self.last_tool_result = self.tool_result
                    return None
                self.summary = self.agent._summary_from_evidence(self.action.get('summary_table'), self.query_data, self.aggregate_query_ids, str(self.action.get('summary_query_id', ''))) if self.has_aggregate_results else None
                self.deterministic_issues = self.agent._verify_summary_table(self.summary, self.query_data, self.aggregate_query_ids)
                self.present_rows = bool(self.action.get('present_rows', False))
                self.detail_table = self.agent._detail_table(self.query_data, self.aggregate_query_ids) if self.present_rows else None
                if self.present_rows and self.detail_table is None:
                    self.deterministic_issues.append('Se solicitaron filas detalladas, pero no hay un resultado tabular disponible para mostrarlas.')
                self.reviewer = getattr(self.agent.llm, 'review_answer', None)
                self.review_result = {'accepted': True, 'issues': []}
                self.needs_review = any((query_id not in self.metric_query_ids for query_id in self.aggregate_query_ids))
                if callable(self.reviewer) and self.review_attempts < self.review_limit and (self.needs_review or self.deterministic_issues):
                    self.progress('Revisando que la respuesta coincida con el plan y la evidencia.')
                    try:
                        self.review_result, self.review_usage = self.reviewer(self.question, plan=self.analysis_plan, draft=self.action, action_history=self.action_history, evidence=self.agent._review_evidence(self.query_data, self.aggregate_query_ids, self.query_jobs), deterministic_issues=self.deterministic_issues)
                        self.agent._add_usage(self.total_usage, self.review_usage)
                    except Exception as review_error:
                        self.review_result = {'accepted': False, 'issues': [f'No se pudo completar la revisión final ({type(review_error).__name__}).']}
                self.issues = [*self.deterministic_issues, *self.review_result.get('issues', [])]
                self.accepted = bool(self.review_result.get('accepted', False)) and (not self.deterministic_issues)
                self.learning_eligible = self.accepted and (not any((entry.get('result', {}).get('action') == 'tool_error' for entry in self.action_history)))
                if not self.accepted and self.review_attempts < self.review_limit:
                    self.review_attempts += 1
                    self.review_feedback = '\n'.join(dict.fromkeys(self.issues)) or 'La respuesta necesita una comprobación adicional.'
                    self.review_event = {'accepted': False, 'issues': self.issues, 'attempt': self.review_attempts}
                    self.sequence += 1
                    if self.session_store is not None:
                        self.session_store.log_action(self.turn_id, self.sequence, 'review', self.action, self.review_event)
                    self.action_history.append({'action': 'review', **self.review_event})
                    self.last_tool_result = {'action': 'review_feedback', **self.review_event}
                    return None
                self.has_rows = any((result.get('rows') for result in self.query_data.values()))
                self.answer_text = 'BigQuery no devolvió filas para el criterio consultado.' if self.query_data and (not self.has_rows) else self.agent._result_sentence(self.action['answer'].strip(), self.summary, self.detail_table)
                if self.query_data and self.summary is None and (self.detail_table is None) and requested_metrics(self.question) and all((data.get('result_role', 'answer') != 'answer' for data in self.query_data.values())):
                    self.answer_text = 'No se obtuvo el cálculo solicitado; solo se recopilaron datos de diagnóstico o metadatos.'
                if not self.accepted:
                    self.issue_text = '; '.join(dict.fromkeys(self.issues)) or 'la respuesta no quedó verificada'
                    self.answer_text += f'\n\n**Revisión:** no pude confirmar todos los puntos: {self.issue_text}'
                else:
                    self.sequence += 1
                    self.review_event = {'accepted': True, 'issues': []}
                    if self.session_store is not None:
                        self.session_store.log_action(self.turn_id, self.sequence, 'review', self.action, self.review_event)
                self.answer = AgentAnswer(answer=self.answer_text, assumptions=tuple(dict.fromkeys([*self.action['assumptions'], *self.field_notices])), summary_table=self.summary, detail_table=self.detail_table, query_count=self.query_count, tables_used=tuple(sorted(self.tables_used)), bytes_processed=self.bytes_processed, usage=self.total_usage, session_id=self.session_id, query_jobs=tuple(self.query_jobs.values()), audit=self.agent._audit_records(self.query_sql, self.query_tables, self.query_jobs, self.query_bytes, self.query_parameter_values), timings={**self.stage_timings, 'total': time.perf_counter() - self.turn_started}, turn_id=self.turn_id, artifacts=tuple(self.artifacts))
                if self.session_store is not None:
                    self.stored_summary = None
                    if self.summary is not None:
                        self.stored_summary = asdict(self.summary)
                    self.session_store.complete_turn(self.turn_id, self.answer.answer, self.answer.assumptions, self.stored_summary)
                    if self.active_analysis and self.has_aggregate_results and self.accepted:
                        self.session_store.set_analysis_state(self.session_id, self.active_analysis)
                    self.learning_updates: list[str] = []
                    self.queue_learning = getattr(self.session_store, 'queue_learning_review', None)
                    if callable(self.queue_learning):
                        try:
                            self.queue_learning(self.turn_id, question=self.question, prior_context=self.retrieved_memory, plan=self.analysis_plan or {}, answer=self.answer.answer, action_history=self.action_history, verified=self.learning_eligible, active_learnings=self.session_store.learning_context(self.question))
                            self.learning_updates.append('Preferencias y procedimientos pendientes de extracción en segundo plano.')
                        except Exception as learning_error:
                            self.progress(f'No se pudo encolar el aprendizaje ({type(learning_error).__name__}).')
                    self.answer = AgentAnswer(answer=self.answer.answer, assumptions=self.answer.assumptions, summary_table=self.answer.summary_table, detail_table=self.answer.detail_table, query_count=self.answer.query_count, tables_used=self.answer.tables_used, bytes_processed=self.answer.bytes_processed, usage=self.answer.usage, session_id=self.answer.session_id, query_jobs=self.answer.query_jobs, learning_updates=tuple(self.learning_updates), audit=self.answer.audit, timings={**self.stage_timings, 'total': time.perf_counter() - self.turn_started}, turn_id=self.turn_id, artifacts=self.answer.artifacts)
                return self.answer
            self.action_id = self.agent._fingerprint({key: value for key, value in self.action.items() if key not in {'notes'}})
            if self.action['action'] == 'inspect_result':
                self.inspection_key = self.agent._fingerprint({key: self.action.get(key) for key in ('query_id', 'operation', 'column', 'offset', 'limit')})
                if self.inspection_key in self.seen_inspections:
                    return self.best_effort_answer('la inspección ya se realizó y no aportaría evidencia nueva')
                self.seen_inspections.add(self.inspection_key)
            if self.previous_action_id == self.action_id:
                return self.best_effort_answer('el agente repitió exactamente la misma acción y no aportó evidencia nueva')
            self.progress('Buscando en la memoria de conversaciones y procedimientos.' if self.action['action'] == 'search_memory' else 'Buscando tablas en el catálogo.' if self.action['action'] == 'search_tables' else 'Consultando el esquema vivo y las descripciones disponibles.' if self.action['action'] == 'describe_tables' else 'Ejecutando el análisis confirmado.' if self.action['action'] == 'run_analysis' else 'Ejecutando la métrica confirmada.' if self.action['action'] == 'run_metric' else 'Ejecutando una consulta de lectura.' if self.action['action'] == 'run_select' else 'Analizando el resultado completo.' if self.action['action'] == 'inspect_result' else 'Creando el archivo Excel local.' if self.action['action'] == 'create_excel' else 'Leyendo la siguiente página de resultados.')
            try:
                if self.action['action'] == 'search_knowledge':
                    self.tool_result = self.session_store.knowledge(self.action['search_text']) if self.session_store is not None else {'matches': [], 'note': 'El glosario validado sigue disponible en el contexto.'}
                elif self.action['action'] == 'search_memory':
                    if self.session_store is None:
                        self.tool_result = {'action': 'search_memory', 'matches': [], 'note': 'No hay memoria persistente en esta ejecución.'}
                    else:
                        self.query = self.action['search_text']
                        self.sessions = self.session_store.search_context(self.query)
                        self.learnings = [item for item in self.session_store.search_learning(self.query) if item.get('status') == 'active']
                        self.tool_result = {'action': 'search_memory', 'search_text': self.query, 'conversation_matches': self.sessions, 'learning_matches': self.learnings}
                elif self.action['action'] == 'inspect_result':
                    self.requested_query_id = self.action['query_id']
                    self.result_data = self.query_data.get(self.requested_query_id)
                    if self.result_data is None and self.session_store is not None:
                        self.result_data = self.session_store.get_query_result(self.requested_query_id, session_id=self.session_id)
                    if self.result_data is None:
                        raise BigQueryError('Ese resultado no está disponible en esta sesión local')
                    self.tool_result = self.agent._inspect_result(self.requested_query_id, self.result_data, operation=self.action['operation'], column=self.action.get('column', ''), offset=self.action.get('offset', 0), limit=self.action.get('limit', 20))
                elif self.action['action'] == 'create_excel':
                    self.explicit_excel_request = re.search('\\b(excel|xlsx|hoja de c[aá]lculo|archivo descargable|exporta|exportar|exportaci[oó]n)\\b', self.question, flags=re.IGNORECASE)
                    if not self.explicit_excel_request:
                        raise BigQueryError('Solo se crea el archivo cuando el usuario lo solicita explícitamente')
                    self.requested_ids = list(dict.fromkeys(self.action.get('query_ids', [])))
                    if not self.requested_ids:
                        self.requested_ids = list(self.query_data)[-1:]
                        if not self.requested_ids and self.session_store is not None:
                            self.requested_ids = [item['query_id'] for item in self.session_store.recent_query_results(self.session_id, limit=1)]
                    self.result_items = []
                    for self.requested_query_id in self.requested_ids:
                        self.result_data = self.query_data.get(self.requested_query_id)
                        if self.result_data is None and self.session_store is not None:
                            self.result_data = self.session_store.get_query_result(self.requested_query_id, session_id=self.session_id)
                        if self.result_data is None:
                            raise BigQueryError('No se encontró uno de los resultados seleccionados en esta sesión')
                        self.result_items.append({**self.result_data, 'query_id': self.requested_query_id, 'question': self.result_data.get('question', self.question), 'sql': self.result_data.get('sql') or self.query_sql.get(self.requested_query_id, ''), 'tables': self.result_data.get('tables') or self.query_tables.get(self.requested_query_id, ()), 'title': str(self.result_data.get('question') or f'Resultado {self.requested_query_id}')})
                    if not self.result_items:
                        raise BigQueryError('Todavía no hay resultados que se puedan exportar')
                    self.export_title = self.action.get('title', '').strip() or self.question[:120]
                    self.artifact_path = self.export_results(self.result_items, title=self.export_title, export_dir=self.agent.settings.export_dir, timezone_name=self.agent.settings.timezone)
                    self.artifact = {'type': 'excel', 'path': str(self.artifact_path), 'name': self.artifact_path.name, 'query_ids': self.requested_ids, 'row_count': sum((len(item.get('rows', [])) for item in self.result_items))}
                    self.artifacts.append(self.artifact)
                    self.tool_result = {'action': 'create_excel', 'artifact': self.artifact}
                elif self.action['action'] == 'search_tables':
                    self.matches, self.total = self.agent.catalog.search_tables(self.action['search_text'], offset=self.action['offset'], page_size=self.action['page_size'])
                    self.tool_result = {'action': 'search_tables', 'search_text': self.action['search_text'], 'offset': self.action['offset'], 'page_size': self.action['page_size'], 'total_matches': self.total, 'next_offset': self.action['offset'] + len(self.matches) if self.action['offset'] + len(self.matches) < self.total else None, 'tables': [{'table_id': item.table_id, 'description': item.description, 'table_type': item.table_type, 'location': item.location} for item in self.matches]}
                elif self.action['action'] == 'describe_tables':
                    if not self.action['table_ids']:
                        raise BigQueryError('Indica al menos una tabla completa para describir')
                    self.descriptions = []
                    for self.table_id in self.action['table_ids']:
                        self.normalized_id = str(self.table_id).replace(':', '.').lower()
                        try:
                            self.table = self.agent._ensure_table(str(self.table_id), session_store=self.session_store)
                        except Exception as schema_error:
                            self.descriptions.append({'table_id': str(self.table_id), 'columns': [], 'note': f'No se pudo leer el esquema vivo ({type(schema_error).__name__}); usa el dry run como alternativa.'})
                            continue
                        if self.table is None:
                            self.descriptions.append({'table_id': str(self.table_id), 'columns': [], 'note': 'No hay descripción local; intenta SQL directamente y usa el dry run para validar columnas.'})
                            continue
                        self.descriptions.append({'table_id': self.table.table_id, 'description': self.table.description, 'table_type': self.table.table_type, 'location': self.table.location, 'source': 'esquema actual de BigQuery; descripciones del diccionario solo en coincidencias exactas', 'columns': [{'name': column.name, 'type': column.data_type, 'description': column.description} for column in self.table.columns.values()], 'note': 'Si faltan columnas o definiciones, prueba una consulta SQL candidata; no es necesario tener el esquema completo.'})
                    self.tool_result = {'action': 'describe_tables', 'tables': self.descriptions, 'metadata_api_used': True, 'instruction': 'Los nombres y tipos vienen del esquema actual; una descripción ausente no impide continuar con SQL.'}
                elif self.action['action'] in {'run_select', 'run_metric', 'run_analysis'}:
                    self.metric_definition: Mapping[str, Any] | None = None
                    self.requested_sql = self.action.get('sql', '')
                    self.query_parameters: tuple[dict[str, Any], ...] = ()
                    self.compiled_analysis = None
                    if self.action['action'] == 'run_metric' and (self.agent.metric_definitions.get(self.action.get('metric_key', ''), {}).get('kind', 'base') != 'base' or len(requested_metrics(self.question)) > 1 or self.agent.organization.from_question(self.question) or (branch_scope_filter(self.question) is not None) or wants_branch_code(self.question) or self.active_analysis or removes_branch_filter(self.question)):
                        self.action = {**self.action, 'action': 'run_analysis'}
                    if self.action['action'] == 'run_analysis':
                        self.compiled_analysis = self.prepare_analysis(self.action)
                        self.requested_sql = self.compiled_analysis.query.sql
                        self.query_parameters = self.compiled_analysis.query.parameters
                        self.action['sql'] = self.requested_sql
                    elif self.action['action'] == 'run_metric':
                        self.scope_text = '\n'.join([self.question, *self.explicit_scope_inputs])
                        self.inherited_scope = self.session_store.temporal_context(self.session_id) if self.session_store is not None and hasattr(self.session_store, 'temporal_context') else ''
                        self.current_period = has_period(self.scope_text)
                        if all_history(self.scope_text) and (not self.current_period) or (not self.current_period and (not has_period(self.inherited_scope))):
                            self.action['start_date'] = self.action['end_date'] = ''
                        elif not self.action.get('start_date') or not self.action.get('end_date'):
                            raise MetricDefinitionError('El usuario indicó un periodo: conviértelo a start_date y end_date, sin omitir el filtro temporal')
                        if self.action.get('metric_key') == 'pagos_siniestros':
                            self.requested_class = requested_payment_class(self.question)
                            if self.requested_class:
                                self.action['metric_key'] = self.requested_class
                        self.metric_definition = self.agent.metric_definitions.get(str(self.action.get('metric_key', '')))
                        if self.metric_definition is None:
                            raise MetricDefinitionError(f"No hay una definición confirmada para `{self.action.get('metric_key', '')}`.")
                        self.metric_definition = dict(self.metric_definition)
                        self.legacy_spec = resolve_analysis(self.action, self.question, today=self.local_now.date(), active=self.active_analysis, organization=self.agent.organization)
                        if str(self.action.get('metric_key')) in {'pagos_siniestros', 'honorarios', 'indemnizaciones'}:
                            self.metric_definition['date_field'] = 'fecha_pago' if self.legacy_spec.payment_date_basis == 'payment' else 'fecha_accidente'
                        self.table_id = str(self.metric_definition['table_id'])
                        self.table = self.ensure_schema(self.table_id)
                        self.required_fields = {self.metric_definition['value_field'], self.metric_definition['date_field']}
                        self.required_fields.update((item['field'] for item in self.metric_definition.get('required_filters', [])))
                        if self.metric_definition.get('payment_class'):
                            self.required_fields.add('clase_pago')
                        if self.table and self.table.metadata_loaded:
                            self.aliases = {cfg['alias']: field for field, cfg in self.metric_definition.get('dimensions', {}).items() if isinstance(cfg, Mapping)}
                            self.requested_fields = [*self.action.get('group_by', []), *(item.get('field', '') for item in self.action.get('filters', []))]
                            self.missing = self.required_fields - set(self.table.columns)
                            self.unknown = [field for field in self.requested_fields if field.lower() not in self.table.columns and self.aliases.get(field) not in self.table.columns]
                            if (self.missing or self.unknown) and self.table_id not in self.refreshed_schema:
                                self.refreshed_schema.add(self.table_id)
                                self.table = self.ensure_schema(self.table_id, refresh=True)
                        if self.table and self.table.metadata_loaded:
                            self.missing = self.required_fields - set(self.table.columns)
                            if self.missing:
                                raise MetricDefinitionError('Faltan campos necesarios para la regla confirmada: ' + ', '.join(sorted(self.missing)) + '. No sustituyas incurrido_total por otro importe.')
                            self.dimensions = dict(self.metric_definition.get('dimensions', {}))
                            self.filter_fields = dict(self.metric_definition.get('filter_fields', {}))
                            for self.field, self.column in self.table.columns.items():
                                self.dtype = self.column.data_type.split()[0]
                                if re.fullmatch('[A-Za-z_][A-Za-z0-9_]*', self.field) and self.dtype in {'STRING', 'INT64', 'INTEGER', 'NUMERIC', 'BIGNUMERIC', 'FLOAT', 'FLOAT64', 'DATE', 'BOOL', 'BOOLEAN'}:
                                    self.dimensions.setdefault(self.field, {'alias': self.field})
                                    self.filter_fields[self.field] = {**self.filter_fields.get(self.field, {}), 'type': self.dtype, 'operators': self.filter_fields.get(self.field, {}).get('operators', ['eq', 'in'])}
                            self.metric_definition.update(dimensions=self.dimensions, filter_fields=self.filter_fields)
                            self.dtype = self.table.columns[self.metric_definition['date_field']].data_type.split()[0]
                            if self.dtype not in {'DATE', 'TIMESTAMP', 'DATETIME'}:
                                raise MetricDefinitionError('El campo temporal de la métrica no tiene un tipo compatible')
                            self.metric_definition['date_field_type'] = self.dtype
                            self.aliases = {cfg['alias']: field for field, cfg in self.dimensions.items() if isinstance(cfg, Mapping)}

                            def resolve_metric_field(field):
                                corrected = resolve_field(field, self.table, self.aliases)
                                if corrected != field:
                                    self.field_notices.append(f'Interpreté `{field}` como el campo comprobado `{corrected}`.')
                                return corrected
                            self.action['group_by'] = [resolve_metric_field(field) for field in self.action.get('group_by', [])]
                            self.action['filters'] = [{**item, 'field': resolve_metric_field(item.get('field', ''))} for item in self.action.get('filters', [])]
                        self.filters = self.action.get('filters', [])
                        self.class_goal = self.metric_definition.get('payment_class', '')
                        if self.class_goal or any((item.get('field') == 'clase_pago' for item in self.filters)):
                            self.values = self.discover_payment_classes(self.table_id, (self.table.location if self.table else '') or self.agent.settings.bigquery_location)
                            self.filters = observed_class_filter(self.values, self.filters)
                            if self.class_goal:
                                try:
                                    self.selected = select_payment_classes(self.values, self.class_goal)
                                except MetricDefinitionError:
                                    self.explicit = []
                                    for self.reply in self.class_replies.get(str(self.class_goal), []):
                                        self.explicit = explicit_class_choices(self.reply, self.values) or self.explicit
                                    self.reader = getattr(self.session_store, 'confirmed_payment_classes', None)
                                    if not self.explicit and callable(self.reader):
                                        self.explicit = self.reader(self.table_id, str(self.class_goal))
                                    if not self.explicit:
                                        raise
                                    self.selected = select_payment_classes(self.values, self.class_goal, explicit=self.explicit)
                                    if self.session_store is not None and self.class_replies.get(str(self.class_goal)):
                                        self.session_store.confirm_business_definition(str(self.class_goal) + '_clase_pago', json.dumps({'kind': 'payment_class_mapping', 'table_id': self.table_id, 'goal': self.class_goal, 'labels': self.selected}, ensure_ascii=False), 'Etiquetas exactas de clase_pago comprobadas en BigQuery', self.class_replies[str(self.class_goal)][-1])
                                self.metric_definition['required_filters'] = [*self.metric_definition.get('required_filters', []), {'field': 'clase_pago', 'operator': 'in', 'value': self.selected}]
                            self.action['filters'] = self.filters
                        self.metric_query = compile_metric_query(self.metric_definition, start_date=self.action['start_date'], end_date=self.action['end_date'], group_by=self.action['group_by'], filters=self.filters, timezone=self.agent.settings.timezone, allow_unbounded=True, payment_date_basis=self.legacy_spec.payment_date_basis)
                        self.requested_sql = self.metric_query.sql
                        self.query_parameters = self.metric_query.parameters
                        self.action['sql'] = self.requested_sql
                        self.active_analysis = replace(self.legacy_spec, start_date=self.action['start_date'], end_date=self.action['end_date']).to_dict()
                    else:
                        enforce_compiled_claims(self.requested_sql, self.question_for_model, str(self.action.get('metric_key', '')))
                        for self.table_id in extract_table_ids(self.requested_sql):
                            self.ensure_schema(self.table_id)
                        try:
                            self.requested_sql, self.notices = resolve_sql_fields(self.requested_sql, self.agent.catalog, allow_typo=False)
                        except UnknownFieldError as field_error:
                            if field_error.table_id in self.refreshed_schema:
                                raise
                            self.refreshed_schema.add(field_error.table_id)
                            self.ensure_schema(field_error.table_id, refresh=True)
                            self.requested_sql, self.notices = resolve_sql_fields(self.requested_sql, self.agent.catalog)
                        self.field_notices.extend(self.notices)
                        self.action['sql'] = self.requested_sql
                    self.validated = validate_sql(self.requested_sql, self.agent.catalog)
                    self.locations = {self.agent.catalog.tables[table_id].location.strip().lower() for table_id in self.validated.tables if table_id in self.agent.catalog.tables and self.agent.catalog.tables[table_id].location.strip()}
                    if len(self.locations) > 1:
                        raise BigQueryError('La consulta combina tablas de regiones distintas. Separa las consultas por región.')
                    self.location = next(iter(self.locations), self.agent.settings.bigquery_location or self.bigquery.location)
                    self.execution_key = self.agent._fingerprint({'sql': self.validated.sql, 'parameters': self.query_parameters, 'location': self.location})
                    if self.execution_key in self.executed_selects:
                        return self.best_effort_answer('la consulta y sus parámetros ya se ejecutaron en este turno')
                    self.stage_started = time.perf_counter()
                    if self.query_parameters:
                        self.dry_run = self.bigquery.dry_run(self.validated.sql, location=self.location, parameters=self.query_parameters)
                    else:
                        self.dry_run = self.bigquery.dry_run(self.validated.sql, location=self.location)
                    self.stage_timings['dry_run'] = self.stage_timings.get('dry_run', 0.0) + time.perf_counter() - self.stage_started
                    if not self.dry_run.ok and self.compiled_analysis is not None and ('prima_devengada' in self.compiled_analysis.components) and ('prima_devengada' in str(self.dry_run.error).lower()):
                        self.record_query(self.validated.sql, self.dry_run.bytes_processed, 'error', self.dry_run.error or '')
                        self.compiled_analysis = self.prepare_analysis(self.action, {'prima_devengada': str(self.dry_run.error)})
                        self.query_parameters = self.compiled_analysis.query.parameters
                        self.validated = validate_sql(self.compiled_analysis.query.sql, self.agent.catalog)
                        self.action['sql'] = self.validated.sql
                        self.execution_key = self.agent._fingerprint({'sql': self.validated.sql, 'parameters': self.query_parameters, 'location': self.location})
                        self.stage_started = time.perf_counter()
                        self.dry_run = self.bigquery.dry_run(self.validated.sql, location=self.location, parameters=self.query_parameters)
                        self.stage_timings['dry_run'] += time.perf_counter() - self.stage_started
                    if not self.dry_run.ok:
                        self.record_query(self.validated.sql, self.dry_run.bytes_processed, 'error', self.dry_run.error or 'dry run fallido')
                        if any((token in (self.dry_run.error or '').lower() for token in ('unrecognized name', 'not found inside', 'no such field'))):
                            for self.table_id in self.validated.tables:
                                if self.table_id not in self.refreshed_schema:
                                    self.refreshed_schema.add(self.table_id)
                                    self.ensure_schema(self.table_id, refresh=True)
                        raise BigQueryError(self.dry_run.error or 'El dry run de BigQuery no fue aprobado')
                    self.check_date_scope(self.validated.sql, self.dry_run.bytes_processed, analysis_scope=self.compiled_analysis.spec.to_dict() if self.compiled_analysis else None)
                    self.stage_started = time.perf_counter()
                    if self.query_parameters:
                        self.page = self.bigquery.execute(self.validated.sql, location=self.location, parameters=self.query_parameters)
                    else:
                        self.page = self.bigquery.execute(self.validated.sql, location=self.location)
                    self.record_query(self.validated.sql, self.dry_run.bytes_processed, 'executed')
                    self.executed_selects.add(self.execution_key)
                    self.query_count += 1
                    self.query_bytes[self.page.query_id] = int(self.page.bytes_processed or self.dry_run.bytes_processed or 0)
                    self.bytes_processed = sum(self.query_bytes.values())
                    if self.metric_definition is not None and (not self.action.get('start_date')):
                        self.field_notices.append('Se consultó todo el histórico disponible; no se aplicó un filtro de fechas.')
                    self.first_page = self.page
                    self.page, self.rows = self.collect_query_pages(self.page, self.query_jobs)
                    self.stage_timings['query'] = self.stage_timings.get('query', 0.0) + time.perf_counter() - self.stage_started
                    self.tables_used.update(self.validated.tables)
                    self.is_aggregate = self.agent._is_aggregate(self.validated.sql)
                    self.has_aggregate_results = self.has_aggregate_results or self.is_aggregate
                    if self.is_aggregate:
                        self.aggregate_query_ids.add(self.page.query_id)
                    if self.metric_definition is not None or self.compiled_analysis is not None:
                        self.metric_query_ids.add(self.page.query_id)
                    self.expected_rows = getattr(self.first_page, 'total_rows', None)
                    self.completeness_warning = ''
                    if self.expected_rows is not None and len(self.rows) != int(self.expected_rows):
                        self.warning = f'BigQuery reportó {self.expected_rows} filas y el agente recibió {len(self.rows)}. La tabla puede estar incompleta.'
                        self.analysis_plan.setdefault('assumptions', []).append(self.warning)
                        self.completeness_warning = self.warning
                    self.role = 'answer'
                    if any(('information_schema.' in table.lower() for table in self.validated.tables)) or re.search('\\bLIMIT\\s+0\\b', self.validated.sql, re.I):
                        self.role = 'metadata'
                    elif self.compiled_analysis is None and requested_metrics(self.question) and (not re.search('\\b(?:cu[aá]nt[oa]s?|cantidad de|n[uú]mero de|conteo)\\b', self.question, re.I)):
                        self.tree = parse(self.validated.sql, read='bigquery')[0]
                        if self.tree.find(exp.Count) and (not self.tree.find(exp.Sum)):
                            self.role = 'diagnostic'
                    self.query_data[self.page.query_id] = {'columns': list(self.first_page.columns), 'column_types': dict(getattr(self.first_page, 'column_types', {}) or {}), 'rows': self.rows, 'result_role': self.role, 'metadata': {'analysis': self.compiled_analysis.spec.to_dict()} if self.compiled_analysis else {}}
                    self.query_sql[self.page.query_id] = self.validated.sql
                    self.query_tables[self.page.query_id] = self.validated.tables
                    self.query_parameter_values[self.page.query_id] = self.query_parameters
                    try:
                        self.persist_query_result(self.page.query_id)
                    except Exception as storage_error:
                        self.analysis_plan.setdefault('assumptions', []).append('No se pudo guardar una copia local del resultado para exportarlo en otra pregunta: ' + type(storage_error).__name__)
                    self.tool_result = {'action': 'query_page', 'query_id': self.page.query_id, 'job_id': getattr(self.first_page, 'job_id', ''), 'job_project': getattr(self.first_page, 'job_project', ''), 'location': getattr(self.first_page, 'location', ''), 'columns': list(self.first_page.columns), 'column_types': dict(getattr(self.first_page, 'column_types', {}) or {}), 'rows': self.rows, 'rows_read': len(self.rows), 'total_rows': self.first_page.total_rows, 'all_pages_read': True, 'bytes_processed': self.query_bytes[self.page.query_id], 'estimated_bytes': self.dry_run.bytes_processed, 'sql': self.validated.sql, 'tables': list(self.validated.tables), 'field_corrections': list(dict.fromkeys(self.field_notices)), 'query_parameters': list(self.query_parameters), 'metric_key': self.action.get('metric_key', ''), 'result_role': self.role}
                    if self.compiled_analysis is not None:
                        self.answer_text, self.summary, self.notes = analysis_presentation(self.query_data[self.page.query_id], self.compiled_analysis)
                        self.status = 'partial' if self.compiled_analysis.unavailable else 'complete'
                        if self.completeness_warning:
                            self.status = 'partial'
                            self.notes = (*self.notes, self.completeness_warning)
                            self.answer_text += '\n\n**Resultado parcial.** No se recibieron todas las filas informadas por BigQuery.'
                        if re.search('\\b(?:excel|xlsx)\\b', self.question, re.I):
                            try:
                                self.export_rows = [{self.summary.column_labels.get(key, key): value for key, value in row.items()} for row in self.summary.rows]
                                self.path = self.export_results([{'query_id': self.page.query_id, 'columns': [self.summary.column_labels.get(key, key) for key in self.summary.columns], 'rows': self.export_rows, 'sql': self.validated.sql, 'tables': self.validated.tables, 'title': self.summary.title}], title=self.question[:120], export_dir=self.agent.settings.export_dir, timezone_name=self.agent.settings.timezone)
                                self.artifacts.append({'type': 'excel', 'path': str(self.path), 'name': self.path.name, 'query_ids': [self.page.query_id], 'row_count': len(self.summary.rows)})
                            except Exception as export_error:
                                self.status = 'partial'
                                self.notes = (*self.notes, f'Los datos se calcularon, pero no se pudo crear el Excel ({type(export_error).__name__}).')
                        self.sequence += 1
                        if self.session_store is not None:
                            self.session_store.log_action(self.turn_id, self.sequence, 'run_analysis', self.action, self.tool_result, aggregate=True)
                            self.session_store.complete_turn(self.turn_id, self.answer_text, self.notes, asdict(self.summary), status=self.status)
                            if self.active_analysis:
                                self.session_store.set_analysis_state(self.session_id, self.active_analysis)
                            try:
                                self.session_store.queue_learning_review(self.turn_id, question=self.question, prior_context=self.retrieved_memory, plan={**self.analysis_plan, 'analysis': self.compiled_analysis.spec.to_dict()}, answer=self.answer_text, action_history=[*self.action_history, self.tool_result], verified=self.status == 'complete', active_learnings=self.session_store.learning_context(self.question))
                            except Exception as learning_error:
                                self.progress(f'No se pudo encolar el aprendizaje ({type(learning_error).__name__}).')
                        return AgentAnswer(self.answer_text, assumptions=tuple(dict.fromkeys([*self.notes, *self.field_notices])), summary_table=self.summary, query_count=self.query_count, tables_used=tuple(sorted(self.tables_used)), bytes_processed=self.bytes_processed, usage=self.total_usage, session_id=self.session_id, query_jobs=tuple(self.query_jobs.values()), status=self.status, audit=self.agent._audit_records(self.query_sql, self.query_tables, self.query_jobs, self.query_bytes, self.query_parameter_values), timings={**self.stage_timings, 'total': time.perf_counter() - self.turn_started}, turn_id=self.turn_id, artifacts=tuple(self.artifacts))
                else:
                    self.requested_query_id = self.action['query_id']
                    if self.requested_query_id in self.query_data and self.requested_query_id not in self.pending_queries:
                        self.existing_rows = self.query_data[self.requested_query_id].get('rows', [])
                        self.tool_result = {'action': 'query_already_complete', 'query_id': self.requested_query_id, 'rows_read': len(self.existing_rows), 'instruction': 'Ya se leyeron todas las páginas por código. Continúa con finish; no pidas otra página.'}
                    else:
                        self.page = self.bigquery.read_page(self.requested_query_id)
                        if not self.page.has_next_page:
                            self.pending_queries.discard(self.page.query_id)
                        self.query_data.setdefault(self.page.query_id, {'columns': list(self.page.columns), 'rows': []})['rows'].extend(self.page.rows)
                        self.agent._record_page_job(self.page, self.query_jobs)
                        self.tool_result = {'action': 'query_page', **self.agent._page_result(self.page)}
            except DateScopeRequired:
                raise
            except Exception as exc:
                if self.action['action'] in {'run_select', 'run_metric', 'run_analysis'} and self.action.get('sql'):
                    self.record_query(self.action['sql'], None, 'error', str(exc))
                self.tool_result = {'action': 'tool_error', 'requested_action': self.action['action'], 'error': str(exc), 'instruction': 'Corrige el paso o busca otra fuente y continúa; si no hay alternativa, explica la limitación.'}
            self.sequence += 1
            if self.session_store is not None:
                self.session_store.log_action(self.turn_id, self.sequence, self.action['action'], self.action, self.tool_result, aggregate=self.action['action'] in {'run_select', 'run_metric', 'run_analysis'} and self.agent._is_aggregate(str(self.action.get('sql', ''))) or (self.action['action'] == 'read_page' and self.action.get('query_id') in self.aggregate_query_ids))
            self.result_id = self.agent._fingerprint({key: value for key, value in self.tool_result.items() if key != 'rows'})
            if self.previous_call == (self.action_id, self.result_id):
                return self.best_effort_answer('BigQuery o el modelo repitieron el mismo paso sin aportar evidencia nueva')
            self.previous_call = (self.action_id, self.result_id)
            self.previous_action_id = self.action_id
            self.action_history.append(self.agent._history_entry(self.action['action'], self.action, self.tool_result))
            self.last_tool_result = self.agent._model_tool_result(self.tool_result)
        except DateScopeRequired as exc:
            return self.pending_clarification_answer(Clarification(str(exc), reason='date_scope', estimated_bytes=exc.estimated_bytes))
        except Exception as exc:
            if self.session_store is not None and self.turn_id:
                if hasattr(self.session_store, 'snapshot_request'):
                    self.session_store.snapshot_request(self.session_id, self.turn_id, self.stage_timings, self.total_usage, self.query_count, self.bytes_processed)
                self.with_context = f'La pregunta quedó incompleta por un error: {type(exc).__name__}.'
                self.session_store.complete_turn(self.turn_id, self.with_context)
            raise
