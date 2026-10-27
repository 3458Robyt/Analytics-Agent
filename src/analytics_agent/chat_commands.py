"""Deterministic employee commands; no BigQuery or language-model calls."""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from typing import Any

from .workspace_state import now_utc


@dataclass
class CommandResult:
    text: str
    session_id: str
    kind: str = "text"
    items: list[dict[str, Any]] = field(default_factory=list)
    changed: bool = False


def handle_command(store: Any, session_id: str, text: str, *, channel: str,
                   request_id: str = "") -> CommandResult | None:
    parts = text.strip().split(maxsplit=1)
    if not parts or not parts[0].startswith("/"):
        return None
    command, argument = parts[0].lower(), parts[1].strip() if len(parts) > 1 else ""
    if request_id:
        cached = store.connection.execute("SELECT result_json FROM chat_command_results WHERE request_id = ? AND owner = ?", (request_id, store.owner)).fetchone()
        if cached:
            return CommandResult(**json.loads(cached[0]))
    if command in {"/new", "/clear", "/nueva"}:
        result = CommandResult("Nueva conversación. Se conservan tu historial y preferencias.", store.create_session(), changed=True)
    elif command == "/sessions":
        page = max(0, int(argument or 0))
        rows = store.list_sessions(limit=(page + 1) * 10 + 1)
        items = rows[page * 10:page * 10 + 10]
        result = CommandResult(f"Conversaciones · página {page}. Usa /resume ID para continuar o /history ID para leer.", session_id, "sessions", items)
        if len(rows) > (page + 1) * 10:
            result.text += f" Más: /sessions {page + 1}"
    elif command in {"/resume", "/history"}:
        args = argument.split()
        key = args[0] if args else session_id
        record = store.get_session(key)
        if record is None:
            raise ValueError("La conversación no existe en tu historial o ya venció")
        if command == "/resume":
            result = CommandResult(f"Conversación retomada: {record['session']['title']}", key, changed=True)
        else:
            page = max(0, int(args[1])) if len(args) > 1 else 0
            turns = record["turns"][page:page + 1]
            items = [{"question": turn["question"], "answer": turn["answer"], "status": turn["status"],
                      "created_at": turn["created_at"], "turn_id": turn["turn_id"], "session_id": key,
                      "summary_table": turn["summary_table"]} for turn in turns]
            message = f"{record['session']['title']} · página {page}"
            if len(record["turns"]) > page + 1:
                message += f" · Más: /history {key} {page + 1}"
            result = CommandResult(message, session_id, "history", items)
    elif command == "/feedback":
        if not argument:
            result = CommandResult("Escribe /feedback seguido de tu comentario.", session_id, "feedback_form")
        else:
            key = store.add_feedback(argument, channel=channel, session_id=session_id,
                                     turn_id=store.latest_turn(session_id), external_key=request_id or None)
            result = CommandResult(f"Feedback registrado: {key}. Gracias por ayudarnos a mejorar.", session_id)
    elif command == "/cancel":
        result = CommandResult("Captura de feedback cancelada. Puedes continuar tu consulta.", session_id, "cancel")
    elif command == "/help":
        result = CommandResult("/new · /sessions [página] · /resume ID · /history ID [página] · /feedback comentario · /cancel", session_id)
    else:
        return CommandResult("Comando desconocido. Usa /help para ver los comandos disponibles.", session_id)
    if request_id and command in {"/new", "/clear", "/nueva", "/resume", "/feedback"}:
        store.connection.execute("INSERT OR IGNORE INTO chat_command_results VALUES (?, ?, ?, ?)", (request_id, store.owner, json.dumps(asdict(result)), now_utc()))
        store.connection.commit()
    return result
