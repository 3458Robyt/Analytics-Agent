"""Durable driver for the shared turn stages; checkpoints contain references only."""
from __future__ import annotations

import hashlib
import json
import sqlite3
import threading
import time
import zlib
from dataclasses import asdict, fields, is_dataclass, replace
from datetime import datetime
from decimal import Decimal
from typing import TypedDict
from uuid import uuid4

from .execution_store import ExecutionStore
from .models import AgentAnswer, Clarification, SummaryTable
from .schema import ColumnSchema, TableSchema
from .text_encoding import dumps_json, repair_text
from .bigquery_adapter import QueryPage, current_request_id
from .graph_query import collect_prepared_query

_TYPES = {cls.__name__: cls for cls in (AgentAnswer, Clarification, SummaryTable, ColumnSchema, TableSchema)}


class DurableOperationError(BaseException):
    """Prevent the business repair loop from resubmitting an uncertain operation."""


def query_operation_index(action_index, sql, parameters, project, location):
    # Cache hits can skip auxiliary queries on replay. Operation identity must
    # depend on the request, never its position among those auxiliary calls.
    canonical = dumps_json([sql, list(parameters), project, location], sort_keys=True, default=str)
    fingerprint = int.from_bytes(hashlib.sha256(canonical.encode()).digest()[:6], 'big')
    return action_index * (1 << 48) + fingerprint


def encode(value):
    if is_dataclass(value):
        return {'$type': type(value).__name__, 'value': encode({f.name: getattr(value, f.name) for f in fields(value)})}
    if isinstance(value, (datetime, Decimal)):
        return {'$type': type(value).__name__, 'value': str(value)}
    if isinstance(value, (set, tuple)):
        return {'$type': type(value).__name__, 'value': [encode(x) for x in value]}
    if isinstance(value, list):
        return [encode(x) for x in value]
    if isinstance(value, dict):
        return {'$type': 'dict', 'value': [[encode(k), encode(v)] for k, v in value.items()]}
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f'Estado no serializable: {type(value).__name__}')


def decode(value):
    if isinstance(value, list):
        return [decode(x) for x in value]
    if not isinstance(value, dict):
        return value
    if '$type' not in value:
        return {k: decode(v) for k, v in value.items()}
    kind, item = value['$type'], value['value']
    if kind == 'dict':
        return {decode(k): decode(v) for k, v in item}
    if kind == 'datetime':
        return datetime.fromisoformat(item)
    if kind == 'Decimal':
        return Decimal(item)
    if kind in ('set', 'tuple'):
        return (set if kind == 'set' else tuple)(decode(x) for x in item)
    if kind not in _TYPES:
        raise ValueError('Tipo de estado no permitido')
    return _TYPES[kind](**decode(item))


class Payloads:
    def __init__(self, store, run_id, token):
        self.store, self.run_id, self.token = store, run_id, token
        store.connection.execute('''CREATE TABLE IF NOT EXISTS graph_payloads (
            run_id TEXT NOT NULL REFERENCES executions(run_id) ON DELETE CASCADE,
            name TEXT NOT NULL, payload BLOB NOT NULL, created REAL NOT NULL,
            PRIMARY KEY(run_id, name))''')
        store.connection.commit()

    def get(self, name):
        self.store.require_versions(self.run_id, self.store.get(self.run_id)['versions'])
        row = self.store.connection.execute('SELECT payload FROM graph_payloads WHERE run_id=? AND name=?', (self.run_id, name)).fetchone()
        return decode(json.loads(zlib.decompress(row[0]))) if row else None

    def put(self, name, value, *, commit=True):
        self.store.require_lease(self.run_id, self.token)
        blob = zlib.compress(dumps_json(encode(value), ensure_ascii=False).encode())
        self.store.connection.execute('INSERT OR REPLACE INTO graph_payloads VALUES (?, ?, ?, ?)', (self.run_id, name, blob, time.time()))
        if commit:
            self.store.connection.commit()
        return name


class DurableQueries:
    def __init__(self, base, payloads, project):
        self.base, self.payloads, self.project = base, payloads, project
        self.action_index, self.counter = 0, 0

    def __getattr__(self, name):
        return getattr(self.base, name)

    def execute(self, sql, *, location='', parameters=()):
        # Production uses BigQueryAdapter. Injected adapters in local evaluations
        # can retain their own query implementation without GCP credentials.
        if not hasattr(self.base, 'resume_query'):
            return self.base.execute(sql, location=location, **({'parameters': parameters} if parameters else {}))
        self.counter += 1
        if self.counter >= 1000000:
            raise RuntimeError('Demasiadas operaciones auxiliares')
        store = self.payloads.store
        location = location or self.base.location or 'us-east1'
        index = query_operation_index(self.action_index, sql, parameters, self.project, location)
        store.prepare_query(self.payloads.run_id, self.payloads.token, operation_index=index,
            sql=sql, parameters=parameters, project=self.project, location=location)
        try:
            result = collect_prepared_query(store, self.base, run_id=self.payloads.run_id,
                token=self.payloads.token, operation_index=index)
        except Exception as exc:
            raise DurableOperationError(str(exc)) from exc
        meta = result['metadata']
        return QueryPage(result['query_id'], tuple(meta['columns']), tuple(result['rows']), False,
            meta.get('total_rows'), meta.get('bytes_processed'), meta['job_id'], meta['job_project'],
            meta['location'], meta.get('column_types', {}))


class State(TypedDict, total=False):
    ref: str
    index: int
    done: bool
    action: str


def versions(agent):
    from .llm import load_default_agent_system_prompt
    return {'graph': 2, 'model': agent.settings.llm_model,
            'prompt': agent._fingerprint(getattr(agent.llm, 'agent_system_prompt', '') or load_default_agent_system_prompt()),
            'glossary': agent._fingerprint(agent.metric_definitions),
            'organization': agent._fingerprint(vars(agent.organization))}


def transient(exc):
    current = exc
    while current is not None:
        code = getattr(current, 'code', None)
        code = code() if callable(code) else code
        if code in {408, 429, 500, 502, 503, 504} or isinstance(current, (TimeoutError, ConnectionError)):
            return True
        current = current.__cause__
    return False


def retry(operation):
    for attempt in range(3):
        try:
            return operation()
        except Exception as exc:
            if attempt == 2 or not transient(exc):
                raise
            time.sleep(2 ** attempt)


def execute(runtime, *, run_id=''):
    from langgraph.graph import StateGraph, START, END
    from langgraph.checkpoint.sqlite import SqliteSaver
    from langgraph.types import interrupt, Command
    from .query_scope import has_period, all_history

    memory = runtime.session_store
    if memory is None:
        # Callers without a SessionStore explicitly use an ephemeral conversation.
        return replace(runtime.run_graph(), engine='langgraph')
    token = uuid4().hex
    with ExecutionStore(memory.directory, owner=memory.owner) as store:
        store.connection.execute('CREATE TABLE IF NOT EXISTS graph_external (owner TEXT, external_id TEXT, run_id TEXT REFERENCES executions(run_id) ON DELETE CASCADE, PRIMARY KEY(owner, external_id))')
        store.connection.commit()
        external = current_request_id()
        if not run_id and external:
            linked = store.connection.execute('SELECT run_id FROM graph_external WHERE owner=? AND external_id=?', (memory.owner, external)).fetchone()
            if linked:
                run_id = linked[0]
        resume = bool(run_id)
        if resume:
            store.require_versions(run_id, versions(runtime.agent))
            record = store.get(run_id)
            if record['status'] == 'completed':
                return Payloads(store, run_id, token).get('answer')
            if record['status'] == 'failed':
                raise ValueError('La ejecución fue abandonada; inicia una nueva pregunta')
        else:
            if runtime.session_id:
                active = [r for r in store.list(session_id=runtime.session_id) if r['status'] in {'running', 'interrupted'}]
                if active:
                    raise ValueError(f"Hay una ejecución pendiente: /retry {active[0]['run_id']} o /new")
            initial = runtime.initialize()
            if initial is not None:
                return replace(initial, engine='langgraph')
            run_id = store.create(session_id=runtime.session_id, turn_id=runtime.turn_id, versions=versions(runtime.agent))
            if external:
                store.connection.execute('INSERT INTO graph_external VALUES (?, ?, ?)', (memory.owner, external, run_id))
                store.connection.commit()
        if not store.acquire(run_id, token):
            raise ValueError('Otra instancia está ejecutando esta conversación')
        stop = threading.Event()
        thread = None
        initialized = resume
        try:
            payloads = Payloads(store, run_id, token)
            stamp = time.time()
            from .workspace_state import positive_env
            result_ttl = positive_env('ANALYTICS_AGENT_RESULT_RETENTION_HOURS', 24) * 3600
            history_ttl = positive_env('ANALYTICS_AGENT_HISTORY_RETENTION_DAYS', 90) * 86400
            store.connection.execute("DELETE FROM graph_payloads WHERE run_id IN (SELECT run_id FROM executions WHERE owner=? AND lease_until<=?) AND name != 'answer' AND created < ?", (memory.owner, stamp, stamp-result_ttl))
            expired_runs = store.connection.execute("SELECT run_id FROM executions WHERE owner=? AND lease_until<=? AND updated_at<?", (memory.owner, stamp, stamp-history_ttl)).fetchall()
            store.connection.commit()
            stop = threading.Event()
            lost = threading.Event()

            def heartbeat():
                while not stop.wait(30):
                    try:
                        with ExecutionStore(memory.directory, owner=memory.owner) as heartbeat_store:
                            if not heartbeat_store.acquire(run_id, token):
                                lost.set()
                                return
                    except Exception:
                        lost.set()
                        return

            thread = threading.Thread(target=heartbeat, daemon=True)
            thread.start()
            deps = {'agent', 'bigquery', 'session_store', 'on_progress', 'on_clarification'}
            if not resume:
                state_keys = sorted(set(runtime.__dict__) - deps)
                payloads.put('keys', state_keys)
            else:
                state_keys = payloads.get('keys')
            state_keys = set(state_keys) | {'action', 'usage'}
            queries = DurableQueries(runtime.agent.bigquery, payloads, runtime.agent.settings.bigquery_project)
            runtime.bigquery = queries

            def save(name, *, commit=True):
                return payloads.put(name, {'runtime': {k: v for k, v in runtime.__dict__.items() if k in state_keys},
                    'catalog': runtime.agent.catalog.tables,
                    'class_cache': {k: v[1] for k, v in runtime.agent._class_cache.items()},
                    'routine_cache': {k: v[1] for k, v in runtime.agent._routine_cache.items()}}, commit=commit)

            def restore(ref):
                if lost.is_set():
                    raise RuntimeError('Se perdió el bloqueo de ejecución; vuelve a intentar')
                saved = payloads.get(ref)
                if saved is None:
                    raise ValueError('El estado necesario para reanudar ya caducó')
                runtime.__dict__.update(saved['runtime'])
                runtime.agent.catalog.tables.update(saved['catalog'])
                runtime.agent._class_cache.update({k: (time.monotonic(), v) for k, v in saved.get('class_cache', {}).items()})
                runtime.agent._routine_cache.update({k: (time.monotonic(), v) for k, v in saved.get('routine_cache', {}).items()})
                memory.active_turn_id = runtime.turn_id

            if not resume:
                save('initial')
                initialized = True

            def decide(state):
                restore(state['ref'])
                store.update(run_id, token, status='running', stage='decide')
                index = state.get('index', 0) + 1
                if index > 24:
                    answer = replace(runtime.best_effort_answer('se alcanzó el límite de decisiones'), run_id=run_id, engine='langgraph')
                    payloads.put('answer', answer)
                    return {'done': True}
                receipt = f'decide:{index}'
                if payloads.get(receipt) is None:
                    retry(runtime.decide)
                    save(receipt)
                restore(receipt)
                return {'ref': receipt, 'index': index, 'done': False, 'action': runtime.action['action']}

            def perform(state):
                restore(state['ref'])
                store.update(run_id, token, status='running', stage=state['action'])
                index = state['index']
                receipt = f'perform:{index}'
                cached = payloads.get(receipt)
                if cached is not None:
                    return cached
                queries.action_index, queries.counter = index, 0
                runtime.graph_run_id, runtime.graph_action_index = run_id, index
                # The interrupt is before effects. Resume re-enters this node safely.
                original_callback = runtime.on_clarification
                if runtime.action['action'] == 'clarify':
                    clarification = runtime.action['clarification']
                    reply = interrupt(clarification)
                    runtime.on_clarification = lambda _: repair_text(reply)
                pending_ref = f'clarification:{index}'
                if payloads.get(pending_ref) is not None:
                    restore(pending_ref)
                    answer = payloads.get(f'pending:{index}')
                else:
                    try:
                        answer = runtime.perform()
                    finally:
                        runtime.on_clarification = original_callback
                if answer is not None and answer.clarification is not None:
                    save(pending_ref)
                    payloads.put(f'pending:{index}', answer)
                    reply = repair_text(interrupt(asdict(answer.clarification)))
                    runtime.explicit_scope_inputs.append(reply)
                    runtime.user_clarifications.append(f'Pregunta: {answer.clarification.question}\nRespuesta: {reply}')
                    runtime.question_for_model += '\nAclaración explícita: ' + reply
                    runtime.save_explicit_definition(answer.clarification, reply)
                    memory.resume_pending_turn(runtime.turn_id, reply)
                    answer = None
                if answer is not None:
                    answer = replace(answer, run_id=run_id, engine='langgraph')
                    payloads.put('answer', answer, commit=False)
                ref = save(f'after:{index}', commit=False)
                result = {'ref': ref, 'done': answer is not None}
                payloads.put(receipt, result)
                return result

            graph = StateGraph(State)
            graph.add_node('decide', decide)
            for node in ('query', 'tool', 'review', 'clarify'):
                graph.add_node(node, perform)
                graph.add_conditional_edges(node, lambda s: END if s['done'] else 'decide')
            graph.add_edge(START, 'decide')
            graph.add_conditional_edges('decide', lambda s: END if s['done'] else
                'query' if s['action'] in {'run_select', 'run_metric', 'run_analysis', 'read_page'} else
                'review' if s['action'] == 'finish' else 'clarify' if s['action'] == 'clarify' else 'tool')
            checkpoint_path = memory.directory / 'graph_checkpoints.sqlite3'
            config = {'configurable': {'thread_id': memory.owner + ':' + run_id}, 'recursion_limit': 512}
            with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
                for old in expired_runs:
                    saver.delete_thread(memory.owner + ':' + old[0])
                store.prune(result_ttl_seconds=result_ttl, run_ttl_seconds=history_ttl)
                compiled = graph.compile(checkpointer=saver)
                invocation = None if resume else {'ref': 'initial', 'index': 0, 'done': False}
                if resume and runtime.clarification_response:
                    previous = compiled.get_state(config)
                    questions = [i.value for t in previous.tasks for i in t.interrupts]
                    reply = repair_text(runtime.clarification_response)
                    if not questions or questions[0].get('reason') != 'date_scope' or has_period(reply) or all_history(reply):
                        invocation = Command(resume=reply)
                result = compiled.invoke(invocation, config, durability='sync')
                while result.get('__interrupt__') and runtime.on_clarification is not None:
                    question = Clarification(**result['__interrupt__'][0].value)
                    reply = runtime.on_clarification(question)
                    if not reply.strip() or (question.reason == 'date_scope' and not has_period(reply) and not all_history(reply)):
                        break
                    result = compiled.invoke(Command(resume=reply), config, durability='sync')
            checkpoint_path.chmod(0o600)
            if result.get('__interrupt__'):
                clarification = Clarification(**result['__interrupt__'][0].value)
                memory.set_pending_clarification(runtime.turn_id, asdict(clarification))
                store.update(run_id, token, status='interrupted', stage='clarify')
                return AgentAnswer(clarification.question, status='needs_clarification', clarification=clarification,
                    session_id=runtime.session_id, turn_id=runtime.turn_id, run_id=run_id, engine='langgraph')
            answer = payloads.get('answer')
            store.update(run_id, token, status='completed', stage='finished')
            return answer
        except BaseException as exc:
            store.connection.rollback()
            store.update(run_id, token, status='interrupted' if initialized else 'failed',
                         stage='retry' if initialized else 'initialize')
            if isinstance(exc, DurableOperationError):
                raise RuntimeError(f'La ejecución {run_id} se puede reanudar: {exc}') from exc
            raise
        finally:
            stop.set()
            if thread is not None:
                thread.join(timeout=1)
            store.release(run_id, token)
