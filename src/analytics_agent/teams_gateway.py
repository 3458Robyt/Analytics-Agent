from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from .teams_common import csv_env, required_env, stable_request_id, value_at

LOGGER = logging.getLogger("analytics_agent.teams_gateway")
_SAFE_ID = re.compile(r"^[a-zA-Z0-9_-]{1,128}$")


def _activity_identity(activity: Any) -> tuple[str, str, str, str]:
    tenant_id = str(value_at(activity, "channel_data", "tenant", "id") or
                    value_at(activity, "conversation", "tenant_id") or "").strip()
    sender = value_at(activity, "from_", default=None)
    if sender is None:
        sender = value_at(activity, "from", default=None)
    user_id = str(value_at(sender, "aad_object_id") or "").strip()
    conversation_id = str(value_at(activity, "conversation", "id") or "").strip()
    activity_id = str(value_at(activity, "id") or "").strip()
    return tenant_id, user_id, conversation_id, activity_id


def _decode_push(body: Mapping[str, Any]) -> dict[str, Any]:
    message = body.get("message")
    if not isinstance(message, Mapping) or not isinstance(message.get("data"), str):
        raise ValueError("El mensaje push de Pub/Sub no tiene message.data")
    try:
        payload = json.loads(base64.b64decode(message["data"], validate=True).decode("utf-8"))
    except Exception as exc:
        raise ValueError("El cuerpo Pub/Sub no contiene JSON válido") from exc
    if not isinstance(payload, dict):
        raise ValueError("El cuerpo de Pub/Sub debe ser un objeto JSON")
    return payload


def _verify_pubsub_token(authorization: str, *, audience: str, service_account: str) -> None:
    if not authorization.startswith("Bearer "):
        raise PermissionError("Se requiere el token OIDC de Pub/Sub")
    from google.auth.transport.requests import Request
    from google.oauth2 import id_token

    claims = id_token.verify_oauth2_token(authorization.removeprefix("Bearer ").strip(), Request(), audience)
    if claims.get("email") != service_account or claims.get("email_verified") is not True:
        raise PermissionError("La suscripción Pub/Sub no tiene la identidad autorizada")


def _build_gateway():
    try:
        from fastapi import FastAPI, Header, HTTPException, Request
        from fastapi.responses import JSONResponse
        from google.api_core.exceptions import AlreadyExists
        from google.cloud import firestore, pubsub_v1
        from microsoft_teams.api import (
            AdaptiveCardActionMessageResponse,
            AdaptiveCardInvokeActivity,
            AdaptiveCardInvokeResponse,
            MessageActivity,
            MessageActivityInput,
        )
        from microsoft_teams.apps import ActivityContext, App, FastAPIAdapter
        from microsoft_teams.cards import AdaptiveCard
    except ImportError as exc:  # pragma: no cover - optional Cloud Run dependencies
        raise RuntimeError('Instala el extra del receptor: pip install -e ".[teams-gateway]"') from exc

    tenant = required_env("TEAMS_TENANT_ID").lower()
    allowed_users = csv_env("TEAMS_ALLOWED_USER_IDS")
    if not allowed_users:
        raise ValueError("TEAMS_ALLOWED_USER_IDS debe incluir al menos un aadObjectId aprobado")
    project = os.environ.get("TEAMS_GCP_PROJECT", os.environ.get("BQ_JOB_PROJECT", "")).strip()
    if not project:
        raise ValueError("Configura TEAMS_GCP_PROJECT")
    request_topic_name = os.environ.get("TEAMS_REQUEST_TOPIC", "analytics-agent-teams-requests").strip()
    request_topic = request_topic_name if request_topic_name.startswith("projects/") else f"projects/{project}/topics/{request_topic_name}"
    push_audience = required_env("TEAMS_PUBSUB_PUSH_AUDIENCE")
    push_service_account = required_env("TEAMS_PUBSUB_PUSH_SERVICE_ACCOUNT")
    # CLIENT_ID is optional for the first Cloud Run deployment, which exposes
    # /healthz so Teams can be registered against its final URL. Keep the bot
    # endpoint closed until the SDK can authenticate the application.
    bot_credentials_configured = bool(
        os.environ.get("CLIENT_ID", "").strip()
        and os.environ.get("CLIENT_SECRET", "").strip()
    )
    os.environ.setdefault("TENANT_ID", tenant)

    publisher = pubsub_v1.PublisherClient(
        publisher_options=pubsub_v1.types.PublisherOptions(enable_message_ordering=True)
    )
    topic_path = request_topic
    documents = firestore.Client(project=project).collection("analytics_agent_teams")
    http = FastAPI(title="Analytics Agent Teams Gateway")

    @http.middleware("http")
    async def require_bot_credentials(request: Request, call_next: Any) -> Any:
        if request.url.path == "/api/messages" and not bot_credentials_configured:
            return JSONResponse(
                status_code=503,
                content={"detail": "El registro de Teams aún no está configurado."},
            )
        return await call_next(request)

    bot_app = App(http_server_adapter=FastAPIAdapter(app=http))

    async def publish_request(request_payload: dict[str, Any]) -> None:
        doc = documents.document("requests").collection("items").document(request_payload["request_id"])
        request_payload["expires_at"] = datetime.now(timezone.utc) + timedelta(days=7)
        try:
            await asyncio.to_thread(doc.create, request_payload)
        except AlreadyExists:
            # Re-publish a duplicate activity. The Workbench worker uses the same
            # idempotency key and returns its stored response without rerunning SQL.
            pass
        key_source = f"{request_payload['tenant_id']}:{request_payload['user_id']}:{request_payload['conversation_id']}"
        ordering_key = hashlib.sha256(key_source.encode()).hexdigest()[:40]
        data = json.dumps(
            request_payload, ensure_ascii=False, separators=(",", ":"),
            default=lambda value: value.isoformat() if isinstance(value, datetime) else str(value),
        ).encode("utf-8")
        future = publisher.publish(topic_path, data, ordering_key=ordering_key)
        await asyncio.to_thread(future.result, 60)

    async def workbench_is_online() -> bool:
        ref = documents.document("workers").collection("items").document("workbench")
        snapshot = await asyncio.to_thread(ref.get)
        if not snapshot.exists:
            return False
        try:
            return time.time() - float((snapshot.to_dict() or {}).get("seen_at", 0)) <= 120
        except (TypeError, ValueError):
            return False

    def make_request(activity: Any, *, action: str = "message", text: str = "",
                     action_data: Mapping[str, Any] | None = None) -> dict[str, Any]:
        tenant_id, user_id, conversation_id, activity_id = _activity_identity(activity)
        conversation_type = str(value_at(activity, "conversation", "conversation_type") or "").lower()
        if conversation_type != "personal":
            raise PermissionError("El piloto acepta únicamente conversaciones privadas uno a uno")
        if tenant_id.lower() != tenant or user_id.lower() not in allowed_users:
            raise PermissionError("El usuario no pertenece al grupo aprobado para el piloto")
        if not (conversation_id and activity_id):
            raise ValueError("El mensaje no incluye identificadores de conversación y actividad")
        payload = {
            "version": 1,
            "request_id": stable_request_id(tenant_id, user_id, activity_id),
            "activity_id": activity_id,
            "tenant_id": tenant_id,
            "user_id": user_id,
            "conversation_id": conversation_id,
            "action": action,
            "text": text[:8000],
            "created_at": time.time(),
        }
        if action_data:
            payload.update({
                key: action_data[key]
                for key in ("result_id", "page", "column_page", "table_kind", "trace_page", "sql_offset")
                if key in action_data
            })
        if action in {"page", "trace", "feedback"}:
            result_id = str(payload.get("result_id", ""))
            if not re.fullmatch(r"[a-f0-9]{40}", result_id):
                raise ValueError("La tarjeta no contiene un identificador de resultado válido")
        if action == "page":
            for field in ("page", "column_page"):
                try:
                    number = int(payload.get(field, 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("La tarjeta contiene un número de página no válido") from exc
                if not 0 <= number <= 100000:
                    raise ValueError("La tarjeta contiene un número de página no válido")
                payload[field] = number
            if payload.get("table_kind", "summary") not in {"summary", "detail"}:
                raise ValueError("La tarjeta solicita una tabla no válida")
        if action == "trace":
            for field in ("trace_page", "sql_offset"):
                try:
                    number = int(payload.get(field, 0))
                except (TypeError, ValueError) as exc:
                    raise ValueError("La tarjeta contiene una página de trazabilidad no válida") from exc
                if not 0 <= number <= 100000:
                    raise ValueError("La tarjeta contiene una página de trazabilidad no válida")
                payload[field] = number
        return payload

    @bot_app.on_message
    async def on_message(ctx: ActivityContext[MessageActivity]) -> None:
        text = str(value_at(ctx.activity, "text") or "").strip()
        try:
            payload = make_request(ctx.activity, text=text)
            if not await workbench_is_online():
                await ctx.send("El worker de Workbench está desconectado. Inícialo con `analytics-agent teams-worker` y vuelve a enviar la pregunta.")
                return
            await publish_request(payload)
            await ctx.send("Recibí tu pregunta. Estoy consultando los datos y te compartiré el resultado aquí.")
        except PermissionError:
            await ctx.send("Este agente está habilitado únicamente para el grupo aprobado del piloto.")
        except Exception as exc:
            LOGGER.exception("No se pudo aceptar una actividad de Teams")
            detail = "El servicio no pudo aceptar la solicitud. Inténtalo otra vez."
            if isinstance(exc, ValueError):
                detail = str(exc)
            await ctx.send(detail)

    @bot_app.on_card_action_execute("page")
    @bot_app.on_card_action_execute("trace")
    @bot_app.on_card_action_execute("feedback")
    @bot_app.on_card_action_execute("new")
    async def on_card_action(ctx: ActivityContext[AdaptiveCardInvokeActivity]) -> AdaptiveCardInvokeResponse:
        data = value_at(ctx.activity, "value", "action", "data", default={})
        action = str(value_at(data, "action") or "")
        if action not in {"page", "trace", "feedback", "new"}:
            await ctx.send("La acción de esta tarjeta no es válida. Envía la pregunta otra vez.")
            return AdaptiveCardActionMessageResponse(
                status_code=200,
                type="application/vnd.microsoft.activity.message",
                value="Acción no válida",
            )
        try:
            payload = make_request(ctx.activity, action=action, action_data=data)
            if not await workbench_is_online():
                await ctx.send("El worker de Workbench está desconectado. Inícialo con `analytics-agent teams-worker` y vuelve a intentarlo.")
                return AdaptiveCardActionMessageResponse(
                    status_code=200,
                    type="application/vnd.microsoft.activity.message",
                    value="Workbench está desconectado",
                )
            await publish_request(payload)
            await ctx.send("Estoy preparando esa información.")
        except PermissionError:
            await ctx.send("Este agente está habilitado únicamente para el grupo aprobado del piloto.")
        except Exception:
            LOGGER.exception("No se pudo procesar una acción de tarjeta")
            await ctx.send("No pude completar esa acción. Vuelve a intentarlo.")
        return AdaptiveCardActionMessageResponse(
            status_code=200,
            type="application/vnd.microsoft.activity.message",
            value="Acción recibida",
        )

    @http.get("/healthz")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @http.post("/internal/pubsub/results")
    async def receive_result(request: Request, authorization: str | None = Header(default=None)) -> dict[str, str]:
        try:
            await asyncio.to_thread(
                _verify_pubsub_token,
                authorization or "",
                audience=push_audience,
                service_account=push_service_account,
            )
        except Exception as exc:
            raise HTTPException(status_code=401, detail="Pub/Sub OIDC inválido") from exc
        try:
            payload = _decode_push(await request.json())
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

        if payload.get("kind") == "heartbeat":
            worker_id = str(payload.get("worker_id", ""))
            if _SAFE_ID.fullmatch(worker_id):
                worker_ref = documents.document("workers").collection("items").document("workbench")
                await asyncio.to_thread(worker_ref.set, {"seen_at": time.time()})
            return {"status": "ok"}

        response_id = str(payload.get("response_id", ""))
        request_id = str(payload.get("request_id", ""))
        if not _SAFE_ID.fullmatch(response_id) or not _SAFE_ID.fullmatch(request_id):
            raise HTTPException(status_code=400, detail="La respuesta no tiene identificadores válidos")
        request_ref = documents.document("requests").collection("items").document(request_id)
        request_snapshot = await asyncio.to_thread(request_ref.get)
        if not request_snapshot.exists:
            return {"status": "orphan"}
        original = request_snapshot.to_dict() or {}
        for field in ("tenant_id", "user_id", "conversation_id"):
            if str(payload.get(field, "")) != str(original.get(field, "")):
                raise HTTPException(status_code=403, detail="La respuesta no coincide con la conversación original")

        response_ref = documents.document("responses").collection("items").document(response_id)
        response_snapshot = await asyncio.to_thread(response_ref.get)
        if response_snapshot.exists and (response_snapshot.to_dict() or {}).get("sent_at"):
            return {"status": "duplicate"}
        card_data = payload.get("card")
        message_text = str(payload.get("text", ""))[:7000]
        if card_data:
            try:
                card = AdaptiveCard.model_validate(card_data)
                message = MessageActivityInput(text="").add_card(card)
            except Exception as exc:
                LOGGER.exception("La tarjeta generada por Workbench no es válida")
                raise HTTPException(status_code=400, detail="Tarjeta no válida") from exc
        else:
            message = MessageActivityInput(text=message_text)
        try:
            await bot_app.send(str(original["conversation_id"]), message)
        except Exception as exc:
            LOGGER.exception("Teams no aceptó la respuesta %s", response_id)
            raise HTTPException(status_code=502, detail="Teams no aceptó la respuesta") from exc
        await asyncio.to_thread(response_ref.set, {
            "request_id": request_id,
            "sent_at": time.time(),
            "expires_at": datetime.now(timezone.utc) + timedelta(days=7),
        })
        return {"status": "sent"}

    return http, bot_app


def run_gateway() -> int:
    http, bot_app = _build_gateway()
    import uvicorn

    async def start() -> None:
        await bot_app.initialize()
        config = uvicorn.Config(http, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")), log_level="info")
        await uvicorn.Server(config).serve()

    asyncio.run(start())
    return 0
