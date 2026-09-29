from __future__ import annotations

import getpass
import json
import os
import re
import sqlite3
import uuid
from datetime import datetime, timezone
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
            CREATE INDEX IF NOT EXISTS turns_session_idx ON turns(session_id, turn_number);
            CREATE INDEX IF NOT EXISTS actions_turn_idx ON actions(turn_id, sequence);
            """
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
            "UPDATE turns SET answer = ?, assumptions_json = ?, summary_table_json = ? WHERE turn_id = ?",
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
