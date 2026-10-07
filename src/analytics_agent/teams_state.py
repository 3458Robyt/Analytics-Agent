from __future__ import annotations

import json
import os
import sqlite3
import time
from pathlib import Path
from typing import Any, Mapping


def default_teams_state_path() -> Path:
    configured = os.environ.get("ANALYTICS_AGENT_STATE_DIR", "").strip()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    root = (
        Path(configured).expanduser()
        if configured
        else (Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share") / "analytics-agent"
    )
    return root / "teams_state.sqlite3"


class TeamsWorkerStore:
    """Local durable queue state, conversation mapping, and expiring result pages."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path).expanduser() if path else default_teams_state_path()
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.path.parent.chmod(0o700)
        except OSError:
            pass
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA secure_delete = ON")
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS teams_conversations (
                owner TEXT NOT NULL,
                conversation_id TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '',
                pending_turn_id TEXT NOT NULL DEFAULT '',
                awaiting_feedback_result_id TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL,
                PRIMARY KEY(owner, conversation_id)
            );
            CREATE TABLE IF NOT EXISTS teams_requests (
                request_id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                response_json TEXT NOT NULL DEFAULT '',
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS teams_results (
                result_id TEXT NOT NULL,
                owner TEXT NOT NULL,
                record_json TEXT NOT NULL,
                expires_at REAL NOT NULL,
                PRIMARY KEY(result_id, owner)
            );
            CREATE TABLE IF NOT EXISTS teams_feedback (
                feedback_id INTEGER PRIMARY KEY AUTOINCREMENT,
                owner TEXT NOT NULL,
                result_id TEXT NOT NULL,
                feedback TEXT NOT NULL,
                created_at REAL NOT NULL
            );
            """
        )
        self.connection.commit()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "TeamsWorkerStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def conversation(self, owner: str, conversation_id: str) -> dict[str, str]:
        row = self.connection.execute(
            "SELECT * FROM teams_conversations WHERE owner = ? AND conversation_id = ?",
            (owner, conversation_id),
        ).fetchone()
        if row is None:
            return {"session_id": "", "pending_turn_id": "", "awaiting_feedback_result_id": ""}
        return {
            "session_id": str(row["session_id"]),
            "pending_turn_id": str(row["pending_turn_id"]),
            "awaiting_feedback_result_id": str(row["awaiting_feedback_result_id"]),
        }

    def save_conversation(
        self,
        owner: str,
        conversation_id: str,
        *,
        session_id: str,
        pending_turn_id: str = "",
        awaiting_feedback_result_id: str = "",
    ) -> None:
        self.connection.execute(
            """INSERT INTO teams_conversations(owner, conversation_id, session_id, pending_turn_id,
               awaiting_feedback_result_id, updated_at) VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(owner, conversation_id) DO UPDATE SET session_id = excluded.session_id,
               pending_turn_id = excluded.pending_turn_id,
               awaiting_feedback_result_id = excluded.awaiting_feedback_result_id,
               updated_at = excluded.updated_at""",
            (owner, conversation_id, session_id, pending_turn_id, awaiting_feedback_result_id, time.time()),
        )
        self.connection.commit()

    def clear_conversation(self, owner: str, conversation_id: str) -> None:
        self.connection.execute(
            "DELETE FROM teams_conversations WHERE owner = ? AND conversation_id = ?",
            (owner, conversation_id),
        )
        self.connection.commit()

    def claim_request(self, request_id: str) -> tuple[str, dict[str, Any] | None]:
        """Mark a request running or return its cached response after redelivery."""
        self.connection.execute("BEGIN IMMEDIATE")
        row = self.connection.execute(
            "SELECT status, response_json FROM teams_requests WHERE request_id = ?", (request_id,)
        ).fetchone()
        if row and row["status"] == "complete" and row["response_json"]:
            self.connection.commit()
            return "complete", json.loads(row["response_json"])
        self.connection.execute(
            """INSERT INTO teams_requests(request_id, status, response_json, updated_at)
               VALUES (?, 'running', '', ?) ON CONFLICT(request_id) DO UPDATE SET
               status = 'running', updated_at = excluded.updated_at""",
            (request_id, time.time()),
        )
        self.connection.commit()
        return "running", None

    def complete_request(self, request_id: str, response: Mapping[str, Any]) -> None:
        self.connection.execute(
            "UPDATE teams_requests SET status = 'complete', response_json = ?, updated_at = ? WHERE request_id = ?",
            (json.dumps(response, ensure_ascii=False, default=str), time.time(), request_id),
        )
        self.connection.commit()

    def save_result(
        self,
        result_id: str,
        owner: str,
        record: Mapping[str, Any],
        *,
        ttl_seconds: int = 24 * 60 * 60,
    ) -> None:
        now = time.time()
        self.connection.execute("DELETE FROM teams_results WHERE expires_at <= ?", (now,))
        self.connection.execute(
            """INSERT INTO teams_results(result_id, owner, record_json, expires_at) VALUES (?, ?, ?, ?)
               ON CONFLICT(result_id, owner) DO UPDATE SET record_json = excluded.record_json,
               expires_at = excluded.expires_at""",
            (result_id, owner, json.dumps(record, ensure_ascii=False, default=str), now + ttl_seconds),
        )
        self.connection.commit()

    def get_result(self, result_id: str, owner: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT record_json, expires_at FROM teams_results WHERE result_id = ? AND owner = ?",
            (result_id, owner),
        ).fetchone()
        if row is None:
            return None
        if float(row["expires_at"]) <= time.time():
            self.connection.execute(
                "DELETE FROM teams_results WHERE result_id = ? AND owner = ?", (result_id, owner)
            )
            self.connection.commit()
            return None
        return json.loads(row["record_json"])

    def set_feedback_wait(self, owner: str, conversation_id: str, result_id: str) -> None:
        conversation = self.conversation(owner, conversation_id)
        self.save_conversation(
            owner, conversation_id,
            session_id=conversation["session_id"],
            pending_turn_id=conversation["pending_turn_id"],
            awaiting_feedback_result_id=result_id,
        )

    def save_feedback(self, owner: str, result_id: str, feedback: str) -> None:
        self.connection.execute(
            "INSERT INTO teams_feedback(owner, result_id, feedback, created_at) VALUES (?, ?, ?, ?)",
            (owner, result_id, feedback[:2000], time.time()),
        )
        self.connection.commit()

    def list_feedback(self, *, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT owner, result_id, feedback, created_at FROM teams_feedback ORDER BY created_at DESC LIMIT ?",
            (max(1, min(int(limit), 1000)),),
        ).fetchall()
        return [dict(row) for row in rows]

    def prune(self, *, now: float | None = None) -> None:
        cutoff = time.time() if now is None else now
        self.connection.execute("DELETE FROM teams_results WHERE expires_at <= ?", (cutoff,))
        self.connection.execute("DELETE FROM teams_requests WHERE updated_at < ?", (cutoff - 7 * 86400,))
        self.connection.commit()
