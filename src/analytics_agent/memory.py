from __future__ import annotations

import getpass
import json
import math
import os
import re
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def runtime_owner() -> str:
    """Return a stable, local identity for this operating-system account."""
    try:
        return f"{getpass.getuser()}:{os.getuid()}"
    except AttributeError:  # pragma: no cover - Windows development environment
        return getpass.getuser()


def default_state_dir() -> Path:
    override = os.environ.get("ANALYTICS_AGENT_STATE_DIR", "").strip()
    if override:
        return Path(override).expanduser()
    data_home = os.environ.get("XDG_DATA_HOME", "").strip()
    root = Path(data_home).expanduser() if data_home else Path.home() / ".local" / "share"
    return root / "analytics-agent"


class SessionStore:
    """Private SQLite history and searchable evidence for one local user."""

    def __init__(self, path: str | Path | None = None, *, owner: str | None = None) -> None:
        self.owner = owner or runtime_owner()
        self.directory = (Path(path).expanduser() if path else default_state_dir())
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.directory.chmod(0o700)
        except OSError:
            pass
        self.path = self.directory / "state.sqlite3"
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self._initialize()
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "SessionStore":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def _initialize(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS sessions (
                session_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                title TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS turns (
                turn_id TEXT PRIMARY KEY,
                session_id TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
                turn_number INTEGER NOT NULL,
                question TEXT NOT NULL,
                answer TEXT NOT NULL DEFAULT '',
                assumptions_json TEXT NOT NULL DEFAULT '[]',
                summary_table_json TEXT NOT NULL DEFAULT 'null',
                status TEXT NOT NULL DEFAULT 'running',
                pending_clarification_json TEXT NOT NULL DEFAULT 'null',
                created_at TEXT NOT NULL,
                UNIQUE(session_id, turn_number)
            );
            CREATE TABLE IF NOT EXISTS actions (
                action_id TEXT PRIMARY KEY,
                turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
                sequence INTEGER NOT NULL,
                action TEXT NOT NULL,
                request_json TEXT NOT NULL,
                result_json TEXT NOT NULL,
                sql_text TEXT NOT NULL DEFAULT '',
                bq_job_id TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                UNIQUE(turn_id, sequence)
            );
            CREATE TABLE IF NOT EXISTS learnings (
                learning_id TEXT PRIMARY KEY,
                owner TEXT NOT NULL,
                kind TEXT NOT NULL,
                learning_key TEXT NOT NULL,
                title TEXT NOT NULL,
                content TEXT NOT NULL,
                trigger_text TEXT NOT NULL DEFAULT '',
                status TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 0,
                disable_reason TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS learning_evidence (
                learning_id TEXT NOT NULL REFERENCES learnings(learning_id) ON DELETE CASCADE,
                turn_id TEXT NOT NULL REFERENCES turns(turn_id) ON DELETE CASCADE,
                verified INTEGER NOT NULL DEFAULT 0,
                created_at TEXT NOT NULL,
                PRIMARY KEY(learning_id, turn_id)
            );
            CREATE TABLE IF NOT EXISTS learning_queue (
                turn_id TEXT PRIMARY KEY REFERENCES turns(turn_id) ON DELETE CASCADE,
                owner TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                last_error TEXT NOT NULL DEFAULT '',
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS business_definitions (
                owner TEXT NOT NULL,
                metric_key TEXT NOT NULL,
                proposed_rule TEXT NOT NULL,
                clarification_question TEXT NOT NULL,
                user_confirmation TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner, metric_key)
            );
            CREATE TABLE IF NOT EXISTS schema_cache (
                owner TEXT NOT NULL,
                table_id TEXT NOT NULL,
                metadata_json TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY(owner, table_id)
            );
            CREATE INDEX IF NOT EXISTS turns_session_idx ON turns(session_id, turn_number);
            CREATE INDEX IF NOT EXISTS actions_turn_idx ON actions(turn_id, sequence);
            CREATE INDEX IF NOT EXISTS learnings_owner_status_idx ON learnings(owner, status, kind);
            CREATE INDEX IF NOT EXISTS learning_evidence_turn_idx ON learning_evidence(turn_id);
            CREATE INDEX IF NOT EXISTS learning_queue_owner_status_idx ON learning_queue(owner, status, created_at);
            """
        )
        turn_columns = {
            str(row["name"])
            for row in self.connection.execute("PRAGMA table_info(turns)").fetchall()
        }
        if "status" not in turn_columns:
            self.connection.execute("ALTER TABLE turns ADD COLUMN status TEXT NOT NULL DEFAULT 'complete'")
        if "pending_clarification_json" not in turn_columns:
            self.connection.execute(
                "ALTER TABLE turns ADD COLUMN pending_clarification_json TEXT NOT NULL DEFAULT 'null'"
            )
        try:
            self.connection.execute(
                """CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
                    turn_id UNINDEXED, session_id UNINDEXED, owner UNINDEXED,
                    question, answer, sql_text, tokenize='unicode61 remove_diacritics 2'
                )"""
            )
            self.fts_available = True
        except sqlite3.OperationalError:
            self.fts_available = False
        self.connection.commit()

    def queue_learning_review(
        self,
        turn_id: str,
        *,
        question: str,
        prior_context: str,
        plan: Mapping[str, Any],
        answer: str,
        action_history: Sequence[Mapping[str, Any]],
        verified: bool,
        active_learnings: str = "",
    ) -> None:
        """Persist extraction work so it can run after the answer is returned."""
        payload = {
            "question": question,
            "prior_context": prior_context,
            "plan": dict(plan),
            "answer": answer,
            "action_history": list(action_history),
            "verified": bool(verified),
            "active_learnings": active_learnings,
        }
        now = _utc_now()
        self.connection.execute(
            """INSERT INTO learning_queue(turn_id, owner, payload_json, status, created_at, updated_at)
               VALUES (?, ?, ?, 'pending', ?, ?)
               ON CONFLICT(turn_id) DO NOTHING""",
            (turn_id, self.owner, json.dumps(payload, ensure_ascii=False, default=str), now, now),
        )
        self.connection.commit()

    def claim_learning_review(self) -> tuple[str, dict[str, Any]] | None:
        """Atomically claim one queued job; safe when ask/chat run concurrently."""
        self.connection.execute("BEGIN IMMEDIATE")
        try:
            lease_expired = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat(timespec="seconds")
            row = self.connection.execute(
                "SELECT turn_id, payload_json FROM learning_queue WHERE owner = ? AND "
                "(status = 'pending' OR (status = 'processing' AND updated_at < ?)) ORDER BY created_at LIMIT 1",
                (self.owner, lease_expired),
            ).fetchone()
            if row is None:
                self.connection.commit()
                return None
            self.connection.execute(
                "UPDATE learning_queue SET status = 'processing', updated_at = ?, last_error = '' "
                "WHERE turn_id = ? AND owner = ?",
                (_utc_now(), row["turn_id"], self.owner),
            )
            self.connection.commit()
            return str(row["turn_id"]), json.loads(row["payload_json"])
        except Exception:
            self.connection.rollback()
            raise

    def finish_learning_review(self, turn_id: str, *, error: str = "") -> None:
        status = "pending" if error else "complete"
        self.connection.execute(
            "UPDATE learning_queue SET status = ?, last_error = ?, updated_at = ? WHERE turn_id = ? AND owner = ?",
            (status, " ".join(error.split())[:500], _utc_now(), turn_id, self.owner),
        )
        self.connection.commit()

    def confirm_business_definition(
        self,
        metric_key: str,
        proposed_rule: str,
        clarification_question: str,
        user_confirmation: str,
    ) -> None:
        metric_key = metric_key.strip().lower()
        if not re.fullmatch(r"[a-z0-9_]{2,80}", metric_key):
            raise ValueError("La clave de métrica confirmada no es válida")
        proposed_rule = " ".join(proposed_rule.split())[:1200]
        clarification_question = " ".join(clarification_question.split())[:600]
        user_confirmation = " ".join(user_confirmation.split())[:1000]
        if len(proposed_rule) < 12 or not user_confirmation:
            raise ValueError("La definición requiere una regla concreta y confirmación del usuario")
        self.connection.execute(
            """INSERT INTO business_definitions(owner, metric_key, proposed_rule, clarification_question,
               user_confirmation, status, updated_at) VALUES (?, ?, ?, ?, ?, 'active', ?)
               ON CONFLICT(owner, metric_key) DO UPDATE SET proposed_rule = excluded.proposed_rule,
               clarification_question = excluded.clarification_question,
               user_confirmation = excluded.user_confirmation, status = 'active', updated_at = excluded.updated_at""",
            (self.owner, metric_key, proposed_rule, clarification_question, user_confirmation, _utc_now()),
        )
        self.connection.commit()

    def confirmed_business_definitions(self, query: str = "", *, limit: int = 8) -> str:
        tokens = re.findall(r"[\wÀ-ÿ]+", query.lower(), flags=re.UNICODE)
        rows = self.connection.execute(
            "SELECT metric_key, proposed_rule, clarification_question, user_confirmation "
            "FROM business_definitions WHERE owner = ? AND status = 'active' ORDER BY updated_at DESC LIMIT 100",
            (self.owner,),
        ).fetchall()
        relevant = []
        for row in rows:
            text = " ".join((row["metric_key"], row["proposed_rule"], row["clarification_question"])).lower()
            if not tokens or any(token in text for token in tokens if len(token) > 2):
                relevant.append(row)
        return "\n".join(
            f"[{row['metric_key']} · confirmación personal del usuario] {row['proposed_rule']} "
            f"(pregunta: {row['clarification_question']}; respuesta: {row['user_confirmation']})"
            for row in relevant[:limit]
        )

    def list_business_definitions(self, *, status: str = "active") -> list[dict[str, Any]]:
        return [dict(row) for row in self.connection.execute(
            "SELECT metric_key, proposed_rule, clarification_question, user_confirmation, status, updated_at "
            "FROM business_definitions WHERE owner = ? AND (? = '' OR status = ?) ORDER BY updated_at DESC",
            (self.owner, status, status),
        ).fetchall()]

    def set_business_definition_status(self, metric_key: str, status: str) -> bool:
        if status not in {"active", "disabled"}:
            raise ValueError("Estado de definición desconocido")
        cursor = self.connection.execute(
            "UPDATE business_definitions SET status = ?, updated_at = ? WHERE owner = ? AND metric_key = ?",
            (status, _utc_now(), self.owner, metric_key.strip().lower()),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def get_schema_metadata(self, table_id: str, *, max_age_seconds: int = 3600) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT metadata_json, updated_at FROM schema_cache WHERE owner = ? AND table_id = ?",
            (self.owner, table_id.strip().lower()),
        ).fetchone()
        if row is None:
            return None
        try:
            updated = datetime.fromisoformat(str(row["updated_at"]))
            if (datetime.now(timezone.utc) - updated).total_seconds() > max_age_seconds:
                return None
            value = json.loads(row["metadata_json"])
        except (ValueError, TypeError, json.JSONDecodeError):
            return None
        return value if isinstance(value, dict) else None

    def set_schema_metadata(self, table_id: str, metadata: Mapping[str, Any]) -> None:
        self.connection.execute(
            """INSERT INTO schema_cache(owner, table_id, metadata_json, updated_at)
               VALUES (?, ?, ?, ?)
               ON CONFLICT(owner, table_id) DO UPDATE SET metadata_json = excluded.metadata_json,
               updated_at = excluded.updated_at""",
            (self.owner, table_id.strip().lower(), json.dumps(metadata, ensure_ascii=False, default=str), _utc_now()),
        )
        self.connection.commit()

    def invalidate_schema_metadata(self, table_id: str) -> None:
        self.connection.execute(
            "DELETE FROM schema_cache WHERE owner = ? AND table_id = ?",
            (self.owner, table_id.strip().lower()),
        )
        self.connection.commit()

    def create_session(self, title: str = "") -> str:
        session_id = uuid.uuid4().hex
        now = _utc_now()
        self.connection.execute(
            "INSERT INTO sessions(session_id, owner, title, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
            (session_id, self.owner, title.strip()[:160] or "Nueva conversación", now, now),
        )
        self.connection.commit()
        return session_id

    def session_exists(self, session_id: str) -> bool:
        row = self.connection.execute(
            "SELECT 1 FROM sessions WHERE session_id = ? AND owner = ?", (session_id, self.owner)
        ).fetchone()
        return row is not None

    def begin_turn(self, session_id: str, question: str) -> str:
        if not self.session_exists(session_id):
            raise ValueError("La sesión no existe en la cuenta actual")
        row = self.connection.execute(
            "SELECT COALESCE(MAX(turn_number), 0) + 1 AS next_number FROM turns WHERE session_id = ?",
            (session_id,),
        ).fetchone()
        turn_id = uuid.uuid4().hex
        now = _utc_now()
        turn_number = int(row["next_number"])
        title_row = self.connection.execute(
            "SELECT title FROM sessions WHERE session_id = ?", (session_id,)
        ).fetchone()
        if turn_number == 1 and title_row and title_row["title"] == "Nueva conversación":
            title = question.strip().replace("\n", " ")[:160] or "Nueva conversación"
            self.connection.execute("UPDATE sessions SET title = ? WHERE session_id = ?", (title, session_id))
        self.connection.execute(
            "INSERT INTO turns(turn_id, session_id, turn_number, question, created_at) VALUES (?, ?, ?, ?, ?)",
            (turn_id, session_id, turn_number, question, now),
        )
        self.connection.execute("UPDATE sessions SET updated_at = ? WHERE session_id = ?", (now, session_id))
        self.connection.commit()
        return turn_id

    def log_action(
        self,
        turn_id: str,
        sequence: int,
        action: str,
        request: Mapping[str, Any],
        result: Mapping[str, Any],
        *,
        aggregate: bool = False,
    ) -> None:
        safe_result = self._persistable_result(result, aggregate=aggregate)
        safe_request = dict(request)
        # Final tables are stored once in turns.summary_table_json, only after
        # the agent confirms they came from aggregate query evidence.
        safe_request.pop("summary_table", None)
        sql_text = str(request.get("sql", ""))
        self.connection.execute(
            """INSERT INTO actions(action_id, turn_id, sequence, action, request_json,
               result_json, sql_text, bq_job_id, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                uuid.uuid4().hex,
                turn_id,
                sequence,
                action,
                json.dumps(safe_request, ensure_ascii=False, default=str),
                json.dumps(safe_result, ensure_ascii=False, default=str),
                sql_text,
                str(result.get("job_id", "") or ""),
                _utc_now(),
            ),
        )
        self.connection.commit()

    def next_action_sequence(self, turn_id: str) -> int:
        row = self.connection.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS next_sequence FROM actions WHERE turn_id = ?",
            (turn_id,),
        ).fetchone()
        return int(row["next_sequence"])

    def set_pending_clarification(self, turn_id: str, clarification: Mapping[str, Any]) -> None:
        self.connection.execute(
            "UPDATE turns SET status = 'needs_clarification', pending_clarification_json = ? WHERE turn_id = ?",
            (json.dumps(dict(clarification), ensure_ascii=False), turn_id),
        )
        self.connection.commit()

    def resume_pending_turn(self, turn_id: str, user_response: str) -> dict[str, Any]:
        row = self.connection.execute(
            """SELECT t.turn_id, t.session_id, t.question, t.status, t.pending_clarification_json
               FROM turns t JOIN sessions s USING(session_id)
               WHERE t.turn_id = ? AND s.owner = ?""",
            (turn_id, self.owner),
        ).fetchone()
        if row is None or row["status"] != "needs_clarification":
            raise ValueError("El turno no existe o no está esperando una aclaración")
        clarification = json.loads(row["pending_clarification_json"] or "null") or {}
        self.connection.execute(
            "UPDATE turns SET status = 'running', pending_clarification_json = 'null' WHERE turn_id = ?",
            (turn_id,),
        )
        self.connection.commit()
        return {
            "turn_id": str(row["turn_id"]),
            "session_id": str(row["session_id"]),
            "question": str(row["question"]),
            "clarification": clarification,
            "user_response": " ".join(user_response.split())[:2000],
        }

    @staticmethod
    def _persistable_result(result: Mapping[str, Any], *, aggregate: bool) -> dict[str, Any]:
        """Keep all metadata and aggregate rows, but never persist raw detail rows."""
        saved = {key: value for key, value in result.items() if key != "rows"}
        rows = result.get("rows")
        if isinstance(rows, Sequence) and not isinstance(rows, (str, bytes)):
            saved["row_count_in_page"] = len(rows)
            if aggregate:
                saved["aggregate_rows"] = list(rows)
        return saved

    def complete_turn(
        self,
        turn_id: str,
        answer: str,
        assumptions: Sequence[str] = (),
        summary_table: Mapping[str, Any] | None = None,
    ) -> None:
        self.connection.execute(
            "UPDATE turns SET answer = ?, assumptions_json = ?, summary_table_json = ?, status = 'complete', "
            "pending_clarification_json = 'null' WHERE turn_id = ?",
            (
                answer,
                json.dumps(list(assumptions), ensure_ascii=False),
                json.dumps(summary_table, ensure_ascii=False, default=str),
                turn_id,
            ),
        )
        row = self.connection.execute(
            "SELECT t.session_id, t.question, t.answer, s.owner FROM turns t JOIN sessions s USING(session_id) WHERE t.turn_id = ?",
            (turn_id,),
        ).fetchone()
        sql_text = "\n".join(
            record["sql_text"]
            for record in self.connection.execute(
                "SELECT sql_text FROM actions WHERE turn_id = ? AND sql_text <> '' ORDER BY sequence", (turn_id,)
            ).fetchall()
        )
        if row:
            if self.fts_available:
                self.connection.execute("DELETE FROM turns_fts WHERE turn_id = ?", (turn_id,))
                self.connection.execute(
                    "INSERT INTO turns_fts(turn_id, session_id, owner, question, answer, sql_text) VALUES (?, ?, ?, ?, ?, ?)",
                    (turn_id, row["session_id"], row["owner"], row["question"], answer, sql_text),
                )
            self.connection.execute(
                "UPDATE sessions SET updated_at = ? WHERE session_id = ?", (_utc_now(), row["session_id"])
            )
        self.connection.commit()

    def recent_context(self, session_id: str, *, limit: int = 6) -> str:
        rows = self.connection.execute(
            """SELECT question, answer, assumptions_json FROM turns
               WHERE session_id = ? AND answer <> '' ORDER BY turn_number DESC LIMIT ?""",
            (session_id, limit),
        ).fetchall()
        chunks = []
        for row in reversed(rows):
            assumptions = json.loads(row["assumptions_json"] or "[]")
            chunk = f"Usuario: {row['question'][:500]}\nAgente: {row['answer'][:2400]}"
            if assumptions:
                chunk += "\nSupuestos: " + "; ".join(str(value) for value in assumptions)
            chunks.append(chunk)
        return "\n\n".join(chunks)

    def search_context(self, query: str, *, limit: int = 5) -> str:
        tokens = re.findall(r"[\wÀ-ÿ]+", query, flags=re.UNICODE)[:12]
        if not tokens:
            return ""
        if self.fts_available:
            match = " OR ".join('"' + token.replace('"', '""') + '"' for token in tokens)
            try:
                rows = self.connection.execute(
                    """SELECT f.question, f.answer, f.sql_text, bm25(turns_fts) AS score
                       FROM turns_fts f WHERE turns_fts MATCH ? AND f.owner = ?
                       ORDER BY score LIMIT ?""",
                    (match, self.owner, limit),
                ).fetchall()
            except sqlite3.OperationalError:
                rows = []
        else:
            pattern = "%" + tokens[0] + "%"
            rows = self.connection.execute(
                """SELECT question, answer, '' AS sql_text FROM turns t JOIN sessions s USING(session_id)
                   WHERE s.owner = ? AND (question LIKE ? OR answer LIKE ?) ORDER BY t.created_at DESC LIMIT ?""",
                (self.owner, pattern, pattern, limit),
            ).fetchall()
        return "\n\n".join(
            f"Pregunta anterior: {row['question'][:500]}\nRespuesta anterior: {row['answer'][:2000]}"
            + (f"\nSQL anterior:\n{row['sql_text'][:1500]}" if row["sql_text"] else "")
            for row in rows
        )

    @staticmethod
    def _contains_secret(value: str) -> bool:
        return bool(re.search(
            r"(?i)(?:\bsk-[a-z0-9_-]{16,}\b|api[_ -]?key\s*[:=]|-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----)",
            value,
        ))

    @staticmethod
    def _contains_policy_override(value: str) -> bool:
        return bool(re.search(
            r"(?i)(?:\b(?:ignore|ignora|ignorar|bypass|omitir)\b.{0,60}\b(?:reglas|instrucciones|seguridad|guardrails|system|prompt|permisos|validaciones)\b|"
            r"\b(?:elimina|eliminar|borra|borrar|actualiza|actualizar)\b.{0,60}\b(?:tabla|datos|recursos)\b|"
            r"\b(?:revela|revelar|envía|envia|manda|publica)\b.{0,60}\b(?:clave|token|secreto|credencial)\b)",
            value,
        ))

    def apply_learning_review(
        self,
        turn_id: str,
        review: Mapping[str, Any],
        *,
        verified: bool,
    ) -> list[str]:
        """Persist bounded learnings with provenance and conservative promotion rules."""
        updates: list[str] = []
        explicit_correction = review.get("user_correction_detected", False) is True
        invalidations = review.get("invalidates", [])
        if explicit_correction and isinstance(invalidations, list):
            for item in invalidations:
                if not isinstance(item, Mapping):
                    continue
                key = str(item.get("key", "")).strip().lower()
                reason = " ".join(str(item.get("reason", "Corrección del usuario")).split())[:300]
                if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", key):
                    continue
                cursor = self.connection.execute(
                    """UPDATE learnings SET status = 'disabled', disable_reason = ?, updated_at = ?
                       WHERE owner = ? AND learning_key = ? AND status = 'active'""",
                    (reason, _utc_now(), self.owner, key),
                )
                if cursor.rowcount:
                    updates.append(f"Enseñanza desactivada: {key}")

        categories = (
            ("preferences", "preference"),
            ("procedures", "procedure"),
            ("business_proposals", "business_proposal"),
        )
        for field, kind in categories:
            records = review.get(field, [])
            if not isinstance(records, list):
                continue
            for item in records[:8]:
                if not isinstance(item, Mapping):
                    continue
                key = str(item.get("key", "")).strip().lower()
                title = " ".join(str(item.get("title", "")).split())[:160]
                content = " ".join(str(item.get("content", "")).split())[:1800]
                trigger = " ".join(str(item.get("trigger", "")).split())[:300]
                try:
                    confidence_value = float(item.get("confidence", 0))
                    confidence = min(1.0, max(0.0, confidence_value)) if math.isfinite(confidence_value) else 0.0
                except (TypeError, ValueError):
                    confidence = 0.0
                if not re.fullmatch(r"[a-z][a-z0-9_]{1,79}", key) or not title or not content:
                    continue
                learning_text = " ".join((title, content, trigger))
                if self._contains_secret(learning_text) or self._contains_policy_override(learning_text):
                    continue
                if kind == "procedure" and not verified:
                    continue
                now = _utc_now()
                explicit_preference = kind == "preference" and item.get("explicit_user_statement", False) is True and confidence >= 0.8
                if explicit_preference:
                    self.connection.execute(
                        """UPDATE learnings SET status = 'disabled',
                                  disable_reason = 'Reemplazado por una declaración explícita posterior',
                                  updated_at = ?
                           WHERE owner = ? AND kind = 'preference' AND learning_key = ?
                             AND content <> ? AND status = 'active'""",
                        (now, self.owner, key, content),
                    )

                existing = self.connection.execute(
                    """SELECT * FROM learnings WHERE owner = ? AND kind = ?
                       AND learning_key = ? AND content = ?""",
                    (self.owner, kind, key, content),
                ).fetchone()
                if existing is None:
                    learning_id = uuid.uuid4().hex
                    status = "proposed" if kind == "business_proposal" else "candidate"
                    self.connection.execute(
                        """INSERT INTO learnings(learning_id, owner, kind, learning_key, title, content,
                           trigger_text, status, confidence, created_at, updated_at)
                           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                        (learning_id, self.owner, kind, key, title, content, trigger, status,
                         confidence, now, now),
                    )
                else:
                    learning_id = str(existing["learning_id"])
                    # Manually disabled learnings stay disabled until restored by an explicit future command.
                    if existing["status"] == "disabled":
                        continue
                    self.connection.execute(
                        "UPDATE learnings SET confidence = MAX(confidence, ?), updated_at = ? WHERE learning_id = ?",
                        (confidence, now, learning_id),
                    )

                is_verified_evidence = int(kind == "procedure" and verified)
                self.connection.execute(
                    """INSERT OR IGNORE INTO learning_evidence(learning_id, turn_id, verified, created_at)
                       VALUES (?, ?, ?, ?)""",
                    (learning_id, turn_id, is_verified_evidence, now),
                )
                evidence = self.connection.execute(
                    "SELECT COUNT(*) AS n, SUM(verified) AS verified_n FROM learning_evidence WHERE learning_id = ?",
                    (learning_id,),
                ).fetchone()
                evidence_count = int(evidence["n"] or 0)
                verified_count = int(evidence["verified_n"] or 0)
                if kind == "preference":
                    if explicit_preference or evidence_count >= 2 and confidence >= 0.75:
                        new_status = "active"
                    else:
                        new_status = "candidate"
                elif kind == "procedure":
                    new_status = "active" if verified_count >= 2 else "candidate"
                else:
                    new_status = "proposed"
                previous_status = str(existing["status"]) if existing is not None else ""
                if new_status == "active":
                    superseded = self.connection.execute(
                        """UPDATE learnings SET status = 'disabled',
                                  disable_reason = 'Reemplazado por una enseñanza más reciente con la misma clave',
                                  updated_at = ?
                           WHERE owner = ? AND kind = ? AND learning_key = ?
                             AND content <> ? AND status = 'active'""",
                        (now, self.owner, kind, key, content),
                    )
                    if superseded.rowcount:
                        updates.append(f"Enseñanza anterior reemplazada: {key}")
                self.connection.execute(
                    "UPDATE learnings SET status = ?, updated_at = ? WHERE learning_id = ?",
                    (new_status, now, learning_id),
                )
                if new_status == "active" and previous_status != "active":
                    updates.append(f"Enseñanza activada: {title}")
                elif new_status == "candidate" and evidence_count == 1:
                    updates.append(f"Enseñanza en observación: {title}")
                elif new_status == "proposed" and kind == "business_proposal" and evidence_count == 1:
                    updates.append(f"Propuesta de definición pendiente: {title}")
        self.connection.commit()
        return list(dict.fromkeys(updates))

    def learning_context(self, query: str = "", *, limit: int = 8) -> str:
        """Return only active, relevant personal preferences and procedures."""
        tokens = re.findall(r"[\wÀ-ÿ]+", query.lower(), flags=re.UNICODE)[:16]
        rows = self.connection.execute(
            """SELECT learning_key, kind, title, content, trigger_text, updated_at FROM learnings
               WHERE owner = ? AND status = 'active' AND kind IN ('preference', 'procedure')
               ORDER BY updated_at DESC LIMIT 200""",
            (self.owner,),
        ).fetchall()
        selected: list[tuple[int, sqlite3.Row]] = []
        for row in rows:
            searchable = " ".join((row["learning_key"], row["title"], row["content"], row["trigger_text"])).lower()
            score = sum(1 for token in tokens if len(token) > 2 and token in searchable)
            if row["kind"] == "preference":
                selected.append((score + 100, row))
            elif not tokens or score:
                selected.append((score, row))
        selected.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        return "\n".join(
            f"[{row['kind']} · {row['learning_key']}] {row['title']}: {row['content']}"
            + (f" (aplica cuando: {row['trigger_text']})" if row["trigger_text"] else "")
            for _, row in selected[:limit]
        )

    def search_learning(self, query: str, *, limit: int = 8) -> list[dict[str, Any]]:
        tokens = re.findall(r"[\wÀ-ÿ]+", query.lower(), flags=re.UNICODE)[:16]
        if not tokens:
            return []
        rows = self.connection.execute(
            """SELECT learning_id, kind, learning_key, title, content, trigger_text, status,
                      confidence, disable_reason, updated_at FROM learnings
               WHERE owner = ? ORDER BY updated_at DESC LIMIT 500""",
            (self.owner,),
        ).fetchall()
        matches = []
        for row in rows:
            searchable = " ".join((row["learning_key"], row["title"], row["content"], row["trigger_text"])).lower()
            score = sum(1 for token in tokens if len(token) > 2 and token in searchable)
            if score:
                matches.append((score, dict(row)))
        matches.sort(key=lambda pair: (pair[0], pair[1]["updated_at"]), reverse=True)
        return [record for _, record in matches[:limit]]

    def list_learning(self, *, status: str = "", limit: int = 100) -> list[dict[str, Any]]:
        if status and status not in {"candidate", "active", "proposed", "disabled"}:
            raise ValueError("Estado de aprendizaje desconocido")
        sql = """SELECT l.learning_id, l.kind, l.learning_key, l.title, l.content,
                        l.trigger_text, l.status, l.confidence, l.disable_reason, l.updated_at,
                        COUNT(e.turn_id) AS evidence_count,
                        COALESCE(SUM(e.verified), 0) AS verified_evidence_count
                 FROM learnings l LEFT JOIN learning_evidence e USING(learning_id)
                 WHERE l.owner = ?"""
        params: list[Any] = [self.owner]
        if status:
            sql += " AND l.status = ?"
            params.append(status)
        sql += " GROUP BY l.learning_id ORDER BY l.updated_at DESC LIMIT ?"
        params.append(limit)
        return [dict(row) for row in self.connection.execute(sql, params).fetchall()]

    def get_learning(self, learning_id: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            """SELECT learning_id, kind, learning_key, title, content, trigger_text, status,
                      confidence, disable_reason, created_at, updated_at FROM learnings
               WHERE learning_id = ? AND owner = ?""",
            (learning_id, self.owner),
        ).fetchone()
        if row is None:
            return None
        item = dict(row)
        item["evidence"] = [dict(evidence) for evidence in self.connection.execute(
            """SELECT turn_id, verified, created_at FROM learning_evidence
               WHERE learning_id = ? ORDER BY created_at""",
            (learning_id,),
        ).fetchall()]
        return item

    def disable_learning(self, learning_id: str, reason: str = "Desactivado por el usuario") -> bool:
        cursor = self.connection.execute(
            """UPDATE learnings SET status = 'disabled', disable_reason = ?, updated_at = ?
               WHERE learning_id = ? AND owner = ? AND status <> 'disabled'""",
            (" ".join(reason.split())[:300], _utc_now(), learning_id, self.owner),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def enable_learning(self, learning_id: str) -> bool:
        """Restore a deliberately selected item; business definitions remain proposals."""
        cursor = self.connection.execute(
            """UPDATE learnings SET status = CASE WHEN kind = 'business_proposal'
                                                 THEN 'proposed' ELSE 'active' END,
                                  disable_reason = '', updated_at = ?
               WHERE learning_id = ? AND owner = ? AND status = 'disabled'""",
            (_utc_now(), learning_id, self.owner),
        )
        self.connection.commit()
        return cursor.rowcount > 0

    def list_sessions(self, *, limit: int = 20) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            """SELECT s.session_id, s.title, s.created_at, s.updated_at, COUNT(t.turn_id) AS turns
               FROM sessions s LEFT JOIN turns t USING(session_id) WHERE s.owner = ?
               GROUP BY s.session_id ORDER BY s.updated_at DESC LIMIT ?""",
            (self.owner, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_session(self, session_id: str) -> dict[str, Any] | None:
        session = self.connection.execute(
            "SELECT * FROM sessions WHERE session_id = ? AND owner = ?", (session_id, self.owner)
        ).fetchone()
        if session is None:
            return None
        turns = self.connection.execute(
            "SELECT * FROM turns WHERE session_id = ? ORDER BY turn_number", (session_id,)
        ).fetchall()
        result_turns = []
        for turn in turns:
            item = dict(turn)
            item["assumptions"] = json.loads(item.pop("assumptions_json"))
            item["summary_table"] = json.loads(item.pop("summary_table_json"))
            item["pending_clarification"] = json.loads(item.pop("pending_clarification_json", "null") or "null")
            item["actions"] = [
                {
                    **dict(action),
                    "request": json.loads(action["request_json"]),
                    "result": json.loads(action["result_json"]),
                }
                for action in self.connection.execute(
                    "SELECT * FROM actions WHERE turn_id = ? ORDER BY sequence", (turn["turn_id"],)
                ).fetchall()
            ]
            for action in item["actions"]:
                action.pop("request_json", None)
                action.pop("result_json", None)
            result_turns.append(item)
        return {"session": dict(session), "turns": result_turns}


def load_glossary(question: str = "", *, limit: int = 12) -> tuple[str, list[dict[str, Any]]]:
    """Load and rank the packaged, version-controlled business glossary."""
    from importlib.resources import files

    glossary_path = files("analytics_agent").joinpath("data", "business_glossary.json")
    try:
        payload = json.loads(glossary_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "", []
    terms = payload.get("terms", []) if isinstance(payload, dict) else []
    terms = [term for term in terms if isinstance(term, dict) and term.get("status") == "validated"]
    question_tokens = set(re.findall(r"[\wÀ-ÿ]+", question.lower(), flags=re.UNICODE))

    def score(term: Mapping[str, Any]) -> int:
        searchable = " ".join(
            [str(term.get("term", "")), str(term.get("definition", ""))]
            + [str(item) for item in term.get("aliases", [])]
            + [str(item) for item in term.get("tables", [])]
            + [str(item) for item in term.get("fields", [])]
        ).lower()
        return sum(1 for token in question_tokens if token and token in searchable)

    ranked = sorted(terms, key=score, reverse=True)
    if question_tokens:
        ranked = [term for term in ranked if score(term) > 0][:limit]
    else:
        ranked = ranked[:limit]
    return json.dumps(ranked, ensure_ascii=False, indent=2), ranked
