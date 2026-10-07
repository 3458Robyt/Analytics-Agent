from __future__ import annotations

import json
import hashlib
import logging
import os
import signal
import threading
import time
from typing import Any, Mapping

from .bigquery_adapter import bigquery_request_scope
from .teams_common import (
    answer_record,
    csv_env,
    required_env,
    result_page,
    stable_request_id,
    teams_owner,
)
from .teams_state import TeamsWorkerStore

LOGGER = logging.getLogger("analytics_agent.teams_worker")
PAGE_SIZE = 10
PAGE_COLUMNS = 5


def _response(request: Mapping[str, Any], *, text: str, card: Mapping[str, Any] | None = None,
              result_id: str = "", response_id: str = "") -> dict[str, Any]:
    return {
        "version": 1,
        "kind": "response",
        "response_id": response_id or str(request["request_id"]),
        "request_id": str(request["request_id"]),
        "tenant_id": str(request["tenant_id"]),
        "user_id": str(request["user_id"]),
        "conversation_id": str(request["conversation_id"]),
        "text": text,
        "card": dict(card) if card else None,
        "result_id": result_id,
    }


def _text_card(title: str, text: str, *, allow_new: bool = False) -> dict[str, Any]:
    card = {
        "type": "AdaptiveCard",
        "version": "1.5",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": [
            {"type": "TextBlock", "text": title, "weight": "Bolder", "size": "Medium", "wrap": True},
            {"type": "TextBlock", "text": text, "wrap": True},
        ],
    }
    if allow_new:
        card["actions"] = [{
            "type": "Action.Execute",
            "title": "Nueva consulta",
            "data": {"action": "new"},
        }]
    return card


def _result_card(record: Mapping[str, Any], result_id: str, page: int = 0,
                 column_page: int = 0, table_kind: str = "summary") -> dict[str, Any]:
    from .teams_common import render_cell

    page_data = result_page(record, page, page_size=PAGE_SIZE, column_page=column_page,
                            column_page_size=PAGE_COLUMNS, table_kind=table_kind)
    table_kind = page_data["table_kind"]
    body: list[dict[str, Any]] = []
    answer = str(record.get("answer", "")).strip()
    if answer:
        body.append({"type": "TextBlock", "text": answer[:3000], "wrap": True})
    assumptions = record.get("assumptions", [])
    if assumptions:
        body.append({"type": "TextBlock", "text": "Supuestos", "weight": "Bolder", "spacing": "Medium"})
        for assumption in assumptions[:5]:
            body.append({"type": "TextBlock", "text": f"• {str(assumption)[:400]}", "wrap": True, "spacing": "Small"})

    columns = page_data["columns"]
    rows = page_data["rows"]
    if columns:
        cells = [
            {"type": "TableCell", "items": [{"type": "TextBlock", "text": str(column), "weight": "Bolder", "wrap": True}]}
            for column in columns
        ]
        table_rows: list[dict[str, Any]] = [{"type": "TableRow", "cells": cells}]
        for row in rows:
            table_rows.append({
                "type": "TableRow",
                "cells": [
                    {"type": "TableCell", "items": [{"type": "TextBlock", "text": render_cell(row.get(column)), "wrap": True}]}
                    for column in columns
                ],
            })
        body.append({"type": "TextBlock", "text": page_data["title"], "weight": "Bolder", "size": "Medium", "spacing": "Medium"})
        body.append({
            "type": "TextBlock",
            "text": (
                f"Filas {page_data['shown_from']}–{page_data['shown_to']} de {page_data['row_count']} · "
                f"Columnas {page_data['column_start']}–{page_data['column_end']} de {page_data['column_count']}"
            ),
            "isSubtle": True,
            "wrap": True,
            "spacing": "Small",
        })
        body.append({"type": "Table", "firstRowAsHeader": True, "showGridLines": True,
                     "columns": [{"width": 1} for _ in columns], "rows": table_rows})
    elif record.get("status") == "complete":
        body.append({"type": "TextBlock", "text": "La consulta no devolvió filas.", "wrap": True})

    actions: list[dict[str, Any]] = []
    other_table = "detail" if table_kind == "summary" else "summary"
    if record.get(f"{other_table}_table"):
        actions.append({
            "type": "Action.Execute",
            "title": "Ver detalle" if other_table == "detail" else "Volver al resumen",
            "data": {"action": "page", "result_id": result_id, "page": 0,
                     "column_page": 0, "table_kind": other_table},
        })
    if page_data["has_next"]:
        actions.append({
            "type": "Action.Execute", "title": "Siguiente página",
            "data": {"action": "page", "result_id": result_id, "page": page + 1,
                     "column_page": column_page, "table_kind": table_kind},
        })
    if page > 0:
        actions.append({
            "type": "Action.Execute", "title": "Página anterior",
            "data": {"action": "page", "result_id": result_id, "page": page - 1,
                     "column_page": column_page, "table_kind": table_kind},
        })
    if page_data["has_next_columns"]:
        actions.append({
            "type": "Action.Execute", "title": "Más columnas",
            "data": {"action": "page", "result_id": result_id, "page": page,
                     "column_page": column_page + 1, "table_kind": table_kind},
        })
    if page_data["has_previous_columns"]:
        actions.append({
            "type": "Action.Execute", "title": "Columnas anteriores",
            "data": {"action": "page", "result_id": result_id, "page": page,
                     "column_page": column_page - 1, "table_kind": table_kind},
        })
    if record.get("audit"):
        actions.append({"type": "Action.Execute", "title": "Ver SQL y trazabilidad",
                        "data": {"action": "trace", "result_id": result_id,
                                 "trace_page": 0, "sql_offset": 0}})
    actions.append({"type": "Action.Execute", "title": "Reportar una corrección",
                    "data": {"action": "feedback", "result_id": result_id}})
    actions.append({"type": "Action.Execute", "title": "Nueva consulta", "data": {"action": "new"}})
    return {
        "type": "AdaptiveCard",
        "version": "1.5",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
        "actions": actions,
    }


def _trace_response(request: Mapping[str, Any], record: Mapping[str, Any]) -> dict[str, Any]:
    audit = record.get("audit", [])
    if not audit:
        return _response(request, text="Esta respuesta no tiene consultas registradas.")
    query_index = max(0, min(int(request.get("trace_page", 0)), len(audit) - 1))
    item = audit[query_index]
    sql = str(item.get("sql", "")).strip()
    sql_offset = max(0, min(int(request.get("sql_offset", 0)), len(sql)))
    sql_chunk_size = 4000
    sql_chunk = sql[sql_offset:sql_offset + sql_chunk_size] or "(SQL no disponible)"
    tables = ", ".join(item.get("tables", [])) or "fuente no indicada"
    body: list[dict[str, Any]] = [
        {"type": "TextBlock", "text": f"Consulta {query_index + 1} de {len(audit)}", "weight": "Bolder", "size": "Medium", "wrap": True},
        {"type": "TextBlock", "text": f"Tablas: {tables}", "wrap": True},
        {"type": "TextBlock", "text": "SQL", "weight": "Bolder", "spacing": "Medium"},
        {"type": "TextBlock", "text": sql_chunk, "fontType": "Monospace", "wrap": True},
    ]
    bytes_processed = item.get("bytes_processed")
    job = item.get("job", {})
    details = []
    if bytes_processed:
        details.append("Bytes procesados: " + f"{int(bytes_processed):,}".replace(",", "."))
    if job.get("job_id"):
        details.append("Job: " + str(job["job_id"]))
    if details:
        body.append({"type": "TextBlock", "text": " · ".join(details), "isSubtle": True, "wrap": True})
    actions: list[dict[str, Any]] = []
    if sql_offset + sql_chunk_size < len(sql):
        actions.append({
            "type": "Action.Execute", "title": "Siguiente parte del SQL",
            "data": {"action": "trace", "result_id": str(request.get("result_id", "")),
                     "trace_page": query_index, "sql_offset": sql_offset + sql_chunk_size},
        })
    if query_index + 1 < len(audit):
        actions.append({
            "type": "Action.Execute", "title": "Siguiente consulta",
            "data": {"action": "trace", "result_id": str(request.get("result_id", "")),
                     "trace_page": query_index + 1, "sql_offset": 0},
        })
    card = {
        "type": "AdaptiveCard",
        "version": "1.5",
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json",
        "body": body,
        "actions": actions,
    }
    return _response(request, text=f"Trazabilidad · consulta {query_index + 1} de {len(audit)}",
                     card=card, response_id=f"{request['request_id']}-trace-{query_index}-{sql_offset}")


class TeamsRequestProcessor:
    def __init__(self, *, agent: Any, bigquery: Any, store: TeamsWorkerStore,
                 publisher: Any, response_topic: str, project: str,
                 allowed_users: frozenset[str], allowed_tenant: str,
                 max_request_age: int = 900, runtime_config: Any = None) -> None:
        self.agent = agent
        self.bigquery = bigquery
        self.store = store
        self.publisher = publisher
        self.response_topic = response_topic
        self.project = project
        self.allowed_users = allowed_users
        self.allowed_tenant = allowed_tenant.lower()
        self.max_request_age = max_request_age
        self.runtime_config = runtime_config

    def _publish_response(self, response: Mapping[str, Any]) -> None:
        payload = json.dumps(response, ensure_ascii=False, separators=(",", ":"), default=str).encode("utf-8")
        key_source = f"{response.get('tenant_id', '')}:{response.get('user_id', '')}:{response.get('conversation_id', '')}"
        ordering_key = hashlib.sha256(key_source.encode("utf-8")).hexdigest()[:40]
        self.publisher.publish(self.response_topic, payload, ordering_key=ordering_key).result(timeout=60)

    def process(self, request: Mapping[str, Any]) -> None:
        request_id = str(request.get("request_id", ""))
        if not request_id:
            raise ValueError("La solicitud no tiene request_id")
        self.store.prune()
        status, cached_response = self.store.claim_request(request_id)
        if status == "complete" and cached_response:
            self._publish_response(cached_response)
            return

        try:
            response = self._process_new(request)
            self.store.complete_request(request_id, response)
            self._publish_response(response)
        except Exception:
            # Leave the request incomplete so Pub/Sub can retry it.
            LOGGER.exception("Falló la solicitud Teams %s", request_id)
            raise

    def _process_new(self, request: Mapping[str, Any]) -> dict[str, Any]:
        tenant_id = str(request.get("tenant_id", ""))
        user_id = str(request.get("user_id", ""))
        conversation_id = str(request.get("conversation_id", ""))
        activity_id = str(request.get("activity_id", ""))
        expected_id = stable_request_id(tenant_id, user_id, activity_id)
        if expected_id != str(request.get("request_id", "")):
            raise ValueError("La identidad de la solicitud no coincide con su huella")
        if tenant_id.lower() != self.allowed_tenant or user_id.lower() not in self.allowed_users:
            return _response(request, text="Esta cuenta no está habilitada para el piloto de Analytics Agent.")
        created_at = float(request.get("created_at", 0) or 0)
        if not created_at or time.time() - created_at > self.max_request_age:
            return _response(request, text="La solicitud venció antes de llegar a Workbench. Envíala otra vez.")

        owner = teams_owner(tenant_id, user_id)
        action = str(request.get("action", "message"))
        if action == "page":
            return self._page(request, owner)
        if action == "trace":
            record = self.store.get_result(str(request.get("result_id", "")), owner)
            if record is None:
                return _response(request, text="Ese resultado ya venció. Vuelve a enviar la consulta.")
            return _trace_response(request, record)
        if action == "feedback":
            self.store.set_feedback_wait(owner, conversation_id, str(request.get("result_id", "")))
            return _response(request, text="Escribe en el chat la corrección que debería revisar el equipo.")
        if action == "new":
            self.store.clear_conversation(owner, conversation_id)
            return _response(request, text="Conversación reiniciada. ¿Qué necesitas consultar?")

        conversation = self.store.conversation(owner, conversation_id)
        text = str(request.get("text", "")).strip()
        if not text:
            return _response(request, text="Escribe una pregunta para iniciar el análisis.")
        if text.strip().lower() in {"/nueva", "/new", "nueva consulta", "iniciar de nuevo"}:
            self.store.clear_conversation(owner, conversation_id)
            return _response(request, text="Conversación reiniciada. ¿Qué necesitas consultar?")
        if conversation["awaiting_feedback_result_id"]:
            self.store.save_feedback(owner, conversation["awaiting_feedback_result_id"], text)
            self.store.save_conversation(
                owner, conversation_id,
                session_id=conversation["session_id"],
                pending_turn_id=conversation["pending_turn_id"],
            )
            return _response(request, text="Gracias. Guardé la observación para revisarla.")

        session_id = conversation["session_id"]
        pending_turn_id = conversation["pending_turn_id"]
        from .memory import SessionStore

        with bigquery_request_scope(str(request["request_id"])), SessionStore(owner=owner) as session_store:
            answer = self.agent.answer(
                text,
                session_store=session_store,
                session_id=session_id,
                pending_turn_id=pending_turn_id,
                clarification_response=text if pending_turn_id else "",
            )
        if answer.learning_updates and self.runtime_config is not None:
            from .cli import _run_learning_jobs

            threading.Thread(
                target=_run_learning_jobs,
                args=(self.runtime_config, self.runtime_config.state_dir, None),
                kwargs={"owner": owner},
                name="teams-learning-review",
                daemon=True,
            ).start()
        record = answer_record(answer)
        session_id = str(record.get("session_id") or session_id)
        pending = str(record.get("turn_id", "")) if record.get("status") == "needs_clarification" else ""
        self.store.save_conversation(owner, conversation_id, session_id=session_id, pending_turn_id=pending)
        if record.get("status") == "needs_clarification":
            clarification = record.get("clarification") or {}
            question = str(clarification.get("question", "El agente necesita una aclaración."))
            return _response(
                request,
                text=question + "\n\nResponde aquí. Para empezar otra consulta primero usa la acción «Nueva consulta».",
                card=_text_card(
                    "Aclaración necesaria",
                    question + "\n\nResponde aquí para continuar o usa «Nueva consulta» para empezar otra pregunta.",
                    allow_new=True,
                ),
            )

        result_id = str(request["request_id"])
        self.store.save_result(result_id, owner, record)
        card = _result_card(record, result_id)
        return _response(
            request,
            text=str(record.get("answer", "Consulta terminada.")),
            card=card,
            result_id=result_id,
        )

    def _page(self, request: Mapping[str, Any], owner: str) -> dict[str, Any]:
        result_id = str(request.get("result_id", ""))
        record = self.store.get_result(result_id, owner)
        if record is None:
            return _response(request, text="Ese resultado ya venció. Vuelve a enviar la consulta.")
        page = max(0, int(request.get("page", 0)))
        column_page = max(0, int(request.get("column_page", 0)))
        table_kind = str(request.get("table_kind", "summary"))
        card = _result_card(record, result_id, page=page, column_page=column_page, table_kind=table_kind)
        return _response(
            request,
            text=str(record.get("answer", "Resultados")),
            card=card,
            result_id=result_id,
        )


def _heartbeat_loop(publisher: Any, topic: str, instance_id: str, stop: threading.Event) -> None:
    while not stop.is_set():
        try:
            payload = json.dumps({"kind": "heartbeat", "worker_id": instance_id, "seen_at": time.time()}).encode()
            publisher.publish(topic, payload).result(timeout=30)
        except Exception:
            LOGGER.exception("No se pudo enviar el heartbeat de Workbench")
        stop.wait(30)


def run_worker() -> int:
    try:
        from google.cloud import pubsub_v1
    except ImportError as exc:  # pragma: no cover - optional runtime dependency
        raise RuntimeError('Instala el extra de Workbench: pip install -e ".[teams-worker]"') from exc

    from rich.console import Console
    from .cli import RuntimeConfig, _build_agent

    project = os.environ.get("TEAMS_GCP_PROJECT", "").strip()
    if not project:
        project = RuntimeConfig.from_env().settings.bigquery_project
    tenant = required_env("TEAMS_TENANT_ID").lower()
    allowed_users = csv_env("TEAMS_ALLOWED_USER_IDS")
    if not allowed_users:
        raise ValueError("TEAMS_ALLOWED_USER_IDS debe incluir al menos un aadObjectId aprobado")
    request_subscription_name = required_env("TEAMS_REQUEST_SUBSCRIPTION")
    request_subscription = (
        request_subscription_name if request_subscription_name.startswith("projects/")
        else f"projects/{project}/subscriptions/{request_subscription_name}"
    )
    response_topic_name = os.environ.get("TEAMS_RESPONSE_TOPIC", "analytics-agent-teams-responses").strip()
    response_topic = (
        response_topic_name if response_topic_name.startswith("projects/")
        else f"projects/{project}/topics/{response_topic_name}"
    )
    max_age = int(os.environ.get("TEAMS_REQUEST_MAX_AGE_SECONDS", "900"))
    if not 60 <= max_age <= 86400:
        raise ValueError("TEAMS_REQUEST_MAX_AGE_SECONDS debe estar entre 60 y 86400")

    console = Console()
    config = RuntimeConfig.from_env()
    agent, bigquery = _build_agent(config, console)
    subscriber = pubsub_v1.SubscriberClient()
    publisher = pubsub_v1.PublisherClient(
        publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
    )
    store = TeamsWorkerStore()
    processor = TeamsRequestProcessor(
        agent=agent, bigquery=bigquery, store=store, publisher=publisher,
        response_topic=response_topic, project=project,
        allowed_users=allowed_users, allowed_tenant=tenant, max_request_age=max_age,
        runtime_config=config,
    )

    instance_id = "workbench"
    stop = threading.Event()
    heartbeat = threading.Thread(
        target=_heartbeat_loop,
        args=(publisher, response_topic, instance_id, stop),
        name="teams-worker-heartbeat",
        daemon=True,
    )
    heartbeat.start()

    def receive(message: Any) -> None:
        try:
            request = json.loads(message.data.decode("utf-8"))
            if request.get("action") == "heartbeat":
                message.ack()
                return
            processor.process(request)
            message.ack()
        except Exception:
            LOGGER.exception("Error procesando un mensaje Pub/Sub; se reintentará")
            message.nack()

    subscription = subscriber.subscribe(
        request_subscription,
        callback=receive,
        flow_control=pubsub_v1.types.FlowControl(max_messages=1),
    )
    LOGGER.info("Teams worker activo; suscripción %s", request_subscription)
    Console().print(f"Teams worker activo · suscripción {request_subscription}")

    def request_stop(*_: Any) -> None:
        stop.set()
        subscription.cancel()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    try:
        subscription.result()
    except KeyboardInterrupt:
        request_stop()
    except Exception:
        if not stop.is_set():
            raise
    finally:
        stop.set()
        subscriber.close()
        publisher.transport.close()
        store.close()
    return 0
