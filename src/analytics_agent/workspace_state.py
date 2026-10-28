"""Local feedback, request diagnostics and retention shared by both channels."""
from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Mapping

from sqlglot import exp, parse_one
from .text_encoding import dumps_json, repair_text


def now_utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def positive_env(name: str, default: int) -> int:
    try:
        value = int(os.environ.get(name, str(default)))
    except ValueError as exc:
        raise ValueError(f"{name} debe ser un entero positivo") from exc
    if value < 1:
        raise ValueError(f"{name} debe ser un entero positivo")
    return value


def safe_text(value: Any, limit: int = 2000) -> str:
    text = repair_text(str(value))
    text = re.sub(r"\bsk-[A-Za-z0-9_-]{12,}", "[credencial omitida]", text)
    text = re.sub(r"(?i)(bearer\s+)[\w.~-]+", r"\1[omitido]", text)
    return text[:limit]


def query_pattern(sql: str) -> str:
    """Group the shape of queries without retaining their parameter values."""
    try:
        tree = parse_one(repair_text(sql), read="bigquery")
        tree = tree.transform(lambda node: exp.Var(this="?") if isinstance(node, exp.Literal) else node)
        return tree.sql(dialect="bigquery", normalize=True)
    except Exception:
        return "(SQL no normalizable)"


class WorkspaceStateMixin:
    def initialize_workspace(self) -> None:
        self.active_request_id = ""
        self.active_turn_id = ""
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.execute("PRAGMA secure_delete = ON")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS feedback (
                feedback_id TEXT PRIMARY KEY, owner TEXT NOT NULL, channel TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '', turn_id TEXT NOT NULL DEFAULT '',
                result_id TEXT NOT NULL DEFAULT '', category TEXT NOT NULL, comment TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'new', resolution TEXT NOT NULL DEFAULT '',
                external_key TEXT UNIQUE, created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS request_metrics (
                request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, channel TEXT NOT NULL,
                session_id TEXT NOT NULL DEFAULT '', turn_id TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL DEFAULT 'running', question TEXT NOT NULL,
                elapsed_seconds REAL NOT NULL DEFAULT 0, timings_json TEXT NOT NULL DEFAULT '{}',
                usage_json TEXT NOT NULL DEFAULT '{}', query_count INTEGER NOT NULL DEFAULT 0,
                bytes_processed INTEGER NOT NULL DEFAULT 0, error_type TEXT NOT NULL DEFAULT '',
                error_message TEXT NOT NULL DEFAULT '', versions_json TEXT NOT NULL,
                created_at TEXT NOT NULL, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS query_metrics (
                event_id TEXT PRIMARY KEY, request_id TEXT NOT NULL, owner TEXT NOT NULL,
                pattern_key TEXT NOT NULL, pattern TEXT NOT NULL, status TEXT NOT NULL,
                estimated_bytes INTEGER, error TEXT NOT NULL DEFAULT '', created_at TEXT NOT NULL
            );
            CREATE INDEX IF NOT EXISTS feedback_created_idx ON feedback(created_at);
            CREATE INDEX IF NOT EXISTS request_metrics_owner_idx ON request_metrics(owner, created_at);
            CREATE INDEX IF NOT EXISTS query_metrics_pattern_idx ON query_metrics(pattern_key, created_at);
            CREATE INDEX IF NOT EXISTS actions_created_idx ON actions(created_at);
            CREATE TABLE IF NOT EXISTS learning_evidence_summaries (
                learning_id TEXT PRIMARY KEY, evidence_count INTEGER NOT NULL DEFAULT 0,
                verified_count INTEGER NOT NULL DEFAULT 0, archived_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chat_command_results (
                request_id TEXT PRIMARY KEY, owner TEXT NOT NULL, result_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS result_links (
                result_id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, created_at TEXT NOT NULL
            );
        """)
        self.connection.commit()
        self.prune_history()

    def prune_history(self) -> None:
        stamp = datetime.now(timezone.utc)
        history_cutoff = (stamp - timedelta(days=positive_env("ANALYTICS_AGENT_HISTORY_RETENTION_DAYS", 90))).isoformat(timespec="seconds")
        result_cutoff = (stamp - timedelta(hours=positive_env("ANALYTICS_AGENT_RESULT_RETENTION_HOURS", 24))).isoformat(timespec="seconds")
        # Archive counts, never expired conversation text, before cascading evidence deletion.
        self.connection.execute("""
            INSERT INTO learning_evidence_summaries(learning_id, evidence_count, verified_count, archived_at)
            SELECT e.learning_id, COUNT(*), COALESCE(SUM(e.verified), 0), ?
            FROM learning_evidence e JOIN turns t USING(turn_id) WHERE t.created_at < ?
            GROUP BY e.learning_id
            ON CONFLICT(learning_id) DO UPDATE SET
                evidence_count = evidence_count + excluded.evidence_count,
                verified_count = verified_count + excluded.verified_count, archived_at = excluded.archived_at
        """, (now_utc(), history_cutoff))
        if self.fts_available:
            self.connection.execute("DELETE FROM turns_fts WHERE turn_id IN (SELECT turn_id FROM turns WHERE created_at < ?)", (history_cutoff,))
        self.connection.execute("DELETE FROM turns WHERE created_at < ?", (history_cutoff,))
        self.connection.execute("DELETE FROM sessions WHERE updated_at < ? AND NOT EXISTS (SELECT 1 FROM turns WHERE turns.session_id = sessions.session_id)", (history_cutoff,))
        self.connection.execute("DELETE FROM query_results WHERE created_at < ?", (result_cutoff,))
        # Action traces can also contain copies of aggregate rows or samples.
        # Retain their SQL and metadata, while expiring those copies with the
        # full result cache. The final answer/summary remains in the turn.
        for row in self.connection.execute("""SELECT action_id, result_json FROM actions WHERE created_at < ?
            AND (result_json LIKE '%"aggregate_rows"%' OR result_json LIKE '%"sample_rows"%'
                 OR result_json LIKE '%"rows"%')""", (result_cutoff,)).fetchall():
            def without_rows(value: Any) -> Any:
                if isinstance(value, dict):
                    return {key: without_rows(item) for key, item in value.items()
                            if key not in {"aggregate_rows", "sample_rows", "rows"}}
                if isinstance(value, list):
                    return [without_rows(item) for item in value]
                return value
            data = without_rows(json.loads(row["result_json"]))
            data["results_expired"] = True
            self.connection.execute("UPDATE actions SET result_json = ? WHERE action_id = ?",
                                    (dumps_json(data, ensure_ascii=False), row["action_id"]))
        for table in ("feedback", "query_metrics", "request_metrics", "chat_command_results", "result_links"):
            self.connection.execute(f"DELETE FROM {table} WHERE created_at < ?", (history_cutoff,))
        self.connection.commit()

    def link_result(self, result_id: str, session_id: str, turn_id: str) -> None:
        if session_id and not self.session_exists(session_id):
            raise ValueError("El resultado no pertenece a esta cuenta")
        self.connection.execute("INSERT OR IGNORE INTO result_links VALUES (?, ?, ?, ?, ?)", (result_id, self.owner, session_id, turn_id, now_utc()))
        self.connection.commit()

    def result_link(self, result_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM result_links WHERE result_id = ? AND owner = ?", (result_id, self.owner)).fetchone()
        return dict(row) if row else None

    def latest_turn(self, session_id: str) -> str:
        row = self.connection.execute("""SELECT t.turn_id FROM turns t JOIN sessions s USING(session_id)
            WHERE t.session_id = ? AND s.owner = ? ORDER BY t.turn_number DESC LIMIT 1""", (session_id, self.owner)).fetchone()
        return str(row[0]) if row else ""

    def migrate_legacy_feedback(self, path: Any = None) -> None:
        path = path or self.directory / "teams_state.sqlite3"
        if not path.is_file():
            return
        source = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
        source.row_factory = sqlite3.Row
        try:
            exists = source.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'teams_feedback'").fetchone()
            if not exists:
                return
            cutoff = datetime.now(timezone.utc) - timedelta(days=positive_env("ANALYTICS_AGENT_HISTORY_RETENTION_DAYS", 90))
            for row in source.execute("SELECT * FROM teams_feedback WHERE created_at >= ?", (cutoff.timestamp(),)):
                key = f"legacy-teams:{hashlib.sha256(str(path.resolve()).encode()).hexdigest()[:12]}:{row['feedback_id']}"
                stamp = datetime.fromtimestamp(row["created_at"], timezone.utc).isoformat(timespec="seconds")
                self.connection.execute("""INSERT OR IGNORE INTO feedback(feedback_id, owner, channel,
                    result_id, category, comment, external_key, created_at, updated_at)
                    VALUES (?, ?, 'teams', ?, 'correction', ?, ?, ?, ?)""",
                    (uuid.uuid4().hex, row["owner"], row["result_id"], safe_text(row["feedback"]), key, stamp, stamp))
            self.connection.commit()
        finally:
            source.close()

    def knowledge(self, query: str) -> dict[str, Any]:
        from .knowledge import KnowledgeGraph
        return KnowledgeGraph(self.connection).search(query)

    def pending_turn(self, session_id: str) -> str:
        row = self.connection.execute("""SELECT t.turn_id FROM turns t JOIN sessions s USING(session_id)
            WHERE t.session_id = ? AND s.owner = ? ORDER BY t.turn_number DESC LIMIT 1""", (session_id, self.owner)).fetchone()
        if row:
            status = self.connection.execute("SELECT status FROM turns WHERE turn_id = ?", (row[0],)).fetchone()
            if status[0] == "needs_clarification":
                return str(row[0])
        return ""

    def temporal_context(self, session_id: str) -> str:
        from .query_scope import all_history, has_period

        if not self.session_exists(session_id):
            return ""
        rows = self.connection.execute("SELECT question, turn_id FROM turns WHERE session_id = ? ORDER BY turn_number DESC LIMIT 6", (session_id,)).fetchall()
        for row in rows:
            replies = [str(json.loads(action[0]).get("user_answer", "")) for action in self.connection.execute(
                "SELECT result_json FROM actions WHERE turn_id = ? AND action = 'clarification_received' ORDER BY sequence DESC", (row["turn_id"],))]
            for supplied in [*replies, row["question"]]:
                if has_period(supplied) or all_history(supplied):
                    return supplied
        return ""

    def add_feedback(self, comment: str, *, channel: str, session_id: str = "", turn_id: str = "",
                     result_id: str = "", category: str = "suggestion", external_key: str | None = None,
                     created_at: str = "") -> str:
        if not comment.strip():
            raise ValueError("Escribe un comentario para registrar el feedback")
        if category not in {"suggestion", "error", "correction", "other"}:
            raise ValueError("Categoría de feedback desconocida")
        if session_id and not self.session_exists(session_id):
            raise ValueError("La sesión no pertenece a esta cuenta")
        if turn_id:
            row = self.connection.execute("SELECT 1 FROM turns t JOIN sessions s USING(session_id) WHERE turn_id = ? AND s.owner = ?", (turn_id, self.owner)).fetchone()
            if not row:
                raise ValueError("La respuesta no pertenece a esta cuenta")
        feedback_id = uuid.uuid4().hex
        stamp = created_at or now_utc()
        self.connection.execute("""INSERT OR IGNORE INTO feedback(feedback_id, owner, channel, session_id,
            turn_id, result_id, category, comment, external_key, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (feedback_id, self.owner, channel, session_id, turn_id, result_id, category, safe_text(comment), external_key, stamp, stamp))
        self.connection.commit()
        if external_key:
            row = self.connection.execute("SELECT feedback_id FROM feedback WHERE external_key = ? AND owner = ?", (external_key, self.owner)).fetchone()
            if not row:
                raise ValueError("Identificador de feedback de otra cuenta")
            return str(row[0])
        return feedback_id

    # These operator methods are intentionally available only through the local CLI.
    # They are never exposed as tools or employee Teams commands.
    def list_feedback_records(self, *, limit: int = 100, status: str = "") -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM feedback WHERE (? = '' OR status = ?) ORDER BY created_at DESC LIMIT ?", (status, status, max(1, min(limit, 1000)))).fetchall()
        return [dict(row) for row in rows]

    def feedback_record(self, feedback_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM feedback WHERE feedback_id = ?", (feedback_id,)).fetchone()
        return dict(row) if row else None

    def resolve_feedback(self, feedback_id: str, resolution: str = "") -> bool:
        cursor = self.connection.execute("UPDATE feedback SET status = 'resolved', resolution = ?, updated_at = ? WHERE feedback_id = ?", (safe_text(resolution), now_utc(), feedback_id))
        self.connection.commit()
        return cursor.rowcount > 0

    def begin_request(self, question: str, session_id: str, versions: Mapping[str, Any]) -> str:
        key = uuid.uuid4().hex
        stamp = now_utc()
        self.connection.execute("""INSERT INTO request_metrics(request_id, owner, channel, session_id, question,
            versions_json, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (key, self.owner, "teams" if self.owner.startswith("teams:") else "cli", session_id,
             safe_text(question, 8000), dumps_json(dict(versions)), stamp, stamp))
        self.connection.commit()
        self.active_request_id, self.active_turn_id = key, ""
        return key

    def finish_request(self, request_id: str, answer: Any = None, *, elapsed: float = 0, error: BaseException | None = None) -> None:
        turn_id = getattr(answer, "turn_id", "") or self.active_turn_id
        saved = self.connection.execute("SELECT * FROM request_metrics WHERE request_id = ? AND owner = ?", (request_id, self.owner)).fetchone()
        status = "error" if error else getattr(answer, "status", "complete")
        if not error and status == "complete" and turn_id:
            errors = self.connection.execute("SELECT result_json FROM actions WHERE turn_id = ?", (turn_id,)).fetchall()
            has_error = any(json.loads(row[0]).get("error") for row in errors)
            if has_error:
                status = "recovered" if getattr(answer, "query_count", 0) else "failed"
        self.connection.execute("""UPDATE request_metrics SET session_id = ?, turn_id = ?, status = ?,
            elapsed_seconds = ?, timings_json = ?, usage_json = ?, query_count = ?, bytes_processed = ?,
            error_type = ?, error_message = ?, updated_at = ? WHERE request_id = ? AND owner = ?""",
            (getattr(answer, "session_id", saved["session_id"]), turn_id, status, elapsed,
             dumps_json(getattr(answer, "timings", json.loads(saved["timings_json"]))), dumps_json(getattr(answer, "usage", json.loads(saved["usage_json"]))),
             getattr(answer, "query_count", saved["query_count"]), getattr(answer, "bytes_processed", saved["bytes_processed"]),
             type(error).__name__ if error else "", safe_text(error, 600) if error else "", now_utc(), request_id, self.owner))
        if turn_id and status in {"error", "failed"}:
            self.connection.execute("UPDATE turns SET status = ? WHERE turn_id = ?", (status, turn_id))
        self.connection.commit()

    def snapshot_request(self, session_id: str, turn_id: str, timings: Mapping[str, Any], usage: Mapping[str, Any], queries: int, processed: int) -> None:
        self.connection.execute("""UPDATE request_metrics SET session_id = ?, turn_id = ?, timings_json = ?,
            usage_json = ?, query_count = ?, bytes_processed = ? WHERE request_id = ? AND owner = ?""",
            (session_id, turn_id, dumps_json(dict(timings)), dumps_json(dict(usage)), queries, processed, self.active_request_id, self.owner))
        self.connection.commit()

    def record_query(self, sql: str, estimated_bytes: int | None, status: str, error: str = "", *, attempt_key: str = "") -> None:
        pattern = query_pattern(sql)
        key = hashlib.sha256(f"{self.active_request_id}:{attempt_key}:{pattern}".encode()).hexdigest() if attempt_key else uuid.uuid4().hex
        self.connection.execute("""INSERT INTO query_metrics VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(event_id) DO UPDATE SET status = excluded.status,
                estimated_bytes = COALESCE(excluded.estimated_bytes, query_metrics.estimated_bytes), error = excluded.error""",
            (key, self.active_request_id, self.owner, hashlib.sha256(pattern.encode()).hexdigest()[:24],
             pattern, status, estimated_bytes, safe_text(error, 600), now_utc()))
        self.connection.commit()

    def usage_report(self, *, days: int = 30) -> dict[str, Any]:
        if not 1 <= days <= 3660:
            raise ValueError("days debe estar entre 1 y 3660")
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat(timespec="seconds")
        rows = [dict(row) for row in self.connection.execute("SELECT * FROM request_metrics WHERE created_at >= ?", (cutoff,))]
        totals = {"requests": len(rows), "queries": sum(row["query_count"] for row in rows),
                  "bytes_processed": sum(row["bytes_processed"] for row in rows), "tokens": 0}
        states: dict[str, int] = {}
        stages: dict[str, float] = {}
        for row in rows:
            states[row["status"]] = states.get(row["status"], 0) + 1
            usage = json.loads(row["usage_json"])
            totals["tokens"] += int(usage.get("total_tokens", usage.get("input_tokens", 0) + usage.get("output_tokens", 0)))
            for key, value in json.loads(row["timings_json"]).items():
                stages[key] = stages.get(key, 0.0) + float(value)
        patterns = [dict(row) for row in self.connection.execute("""SELECT pattern_key, pattern, COUNT(*) AS attempts,
            SUM(CASE WHEN status = 'executed' THEN 1 ELSE 0 END) AS executions,
            SUM(CASE WHEN status = 'error' THEN 1 ELSE 0 END) AS errors,
            MAX(estimated_bytes) AS max_estimated_bytes FROM query_metrics WHERE created_at >= ?
            GROUP BY pattern_key, pattern ORDER BY attempts DESC LIMIT 30""", (cutoff,))]
        errors = [dict(row) for row in self.connection.execute("""SELECT error_type, error_message, COUNT(*) AS occurrences
            FROM request_metrics WHERE created_at >= ? AND error_type <> ''
            GROUP BY error_type, error_message ORDER BY occurrences DESC LIMIT 20""", (cutoff,))]
        repeated = self.connection.execute("""SELECT COALESCE(SUM(n - 1), 0) FROM
            (SELECT COUNT(*) AS n FROM query_metrics WHERE created_at >= ?
             AND status IN ('executed', 'error') GROUP BY request_id, pattern_key HAVING COUNT(*) > 1)""", (cutoff,)).fetchone()
        totals["query_retries"] = int(repeated[0])
        return {"days": days, "totals": totals, "statuses": states, "stage_seconds": stages,
                "mean_seconds": sum(row["elapsed_seconds"] for row in rows) / max(1, len(rows)),
                "patterns": patterns, "errors": errors}
