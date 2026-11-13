"""Durable execution and BigQuery operation records for the graph runtime."""
from __future__ import annotations

import json
import sqlite3
import time
import uuid
from pathlib import Path
from typing import Any, Mapping, Sequence

from .bigquery_adapter import stable_query_job_id
from .text_encoding import dumps_json


class ExecutionStore:
    """Persist execution ownership and immutable query identities in SQLite."""

    def __init__(self, directory: str | Path, *, owner: str):
        if not owner.strip():
            raise ValueError("Se requiere el propietario de las ejecuciones")
        self.owner = owner.strip()
        folder = Path(directory).expanduser()
        folder.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.path = folder / "graph_executions.sqlite3"
        self.connection = sqlite3.connect(self.path, timeout=30)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 30000")
        self.connection.execute("PRAGMA journal_mode = WAL")
        self.connection.executescript("""
            CREATE TABLE IF NOT EXISTS executions (
                run_id TEXT PRIMARY KEY, owner TEXT NOT NULL, session_id TEXT NOT NULL,
                turn_id TEXT NOT NULL, status TEXT NOT NULL, stage TEXT NOT NULL,
                versions_json TEXT NOT NULL, created_at REAL NOT NULL, updated_at REAL NOT NULL,
                lease_token TEXT NOT NULL DEFAULT '', lease_until REAL NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS executions_owner_session ON executions(owner, session_id, updated_at);
            CREATE UNIQUE INDEX IF NOT EXISTS executions_one_active_session ON executions(owner, session_id)
                WHERE status IN ('running', 'interrupted');
            CREATE TABLE IF NOT EXISTS query_operations (
                run_id TEXT NOT NULL REFERENCES executions(run_id) ON DELETE CASCADE,
                operation_index INTEGER NOT NULL, sql TEXT NOT NULL, parameters_json TEXT NOT NULL,
                project TEXT NOT NULL, location TEXT NOT NULL, job_id TEXT NOT NULL,
                query_id TEXT NOT NULL, rows_saved INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'prepared', PRIMARY KEY(run_id, operation_index)
            );
            CREATE TABLE IF NOT EXISTS query_pages (
                run_id TEXT NOT NULL, operation_index INTEGER NOT NULL, start_index INTEGER NOT NULL,
                row_count INTEGER NOT NULL, rows_json TEXT NOT NULL, is_last INTEGER NOT NULL,
                PRIMARY KEY(run_id, operation_index, start_index),
                FOREIGN KEY(run_id, operation_index) REFERENCES query_operations(run_id, operation_index) ON DELETE CASCADE
            );
            CREATE TABLE IF NOT EXISTS query_metadata (
                run_id TEXT NOT NULL, operation_index INTEGER NOT NULL, metadata_json TEXT NOT NULL,
                PRIMARY KEY(run_id, operation_index),
                FOREIGN KEY(run_id, operation_index) REFERENCES query_operations(run_id, operation_index) ON DELETE CASCADE
            );
        """)
        try:
            self.path.chmod(0o600)
        except OSError:
            pass

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def create(self, *, session_id: str, turn_id: str, versions: Mapping[str, Any]) -> str:
        if not session_id or not turn_id:
            raise ValueError("Se requieren sesión y turno")
        run_id = uuid.uuid4().hex
        stamp = time.time()
        try:
            self.connection.execute("INSERT INTO executions VALUES (?, ?, ?, ?, 'running', 'created', ?, ?, ?, '', 0)",
                                    (run_id, self.owner, session_id, turn_id,
                                     dumps_json(dict(versions), sort_keys=True), stamp, stamp))
            self.connection.commit()
        except sqlite3.IntegrityError as exc:
            self.connection.rollback()
            raise ValueError("Ya existe una ejecución activa para esta sesión") from exc
        return run_id

    def get(self, run_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM executions WHERE run_id = ? AND owner = ?",
                                      (run_id, self.owner)).fetchone()
        if row is None:
            return None
        result = dict(row)
        result["versions"] = json.loads(result.pop("versions_json"))
        return result

    def list(self, *, session_id: str = "", limit: int = 50) -> list[dict[str, Any]]:
        if limit < 1 or limit > 500:
            raise ValueError("El límite debe estar entre 1 y 500")
        rows = self.connection.execute(
            "SELECT run_id, session_id, turn_id, status, stage, created_at, updated_at FROM executions "
            "WHERE owner = ? AND (? = '' OR session_id = ?) ORDER BY updated_at DESC LIMIT ?",
            (self.owner, session_id, session_id, limit)).fetchall()
        return [dict(row) for row in rows]

    def require_versions(self, run_id: str, versions: Mapping[str, Any]) -> None:
        """Reject a resume when any fingerprint differs from the original run."""
        run = self.get(run_id)
        if run is None:
            raise ValueError("La ejecución no existe o no pertenece al usuario")
        if run['versions'] != dict(versions):
            raise ValueError("La ejecución se creó con versiones distintas y no se puede reanudar")

    def acquire(self, run_id: str, token: str, *, seconds: int = 120, now: float | None = None) -> bool:
        if not token.strip() or seconds <= 0:
            raise ValueError("Se requieren un token y un plazo válido")
        now = time.time() if now is None else now
        cursor = self.connection.execute(
            "UPDATE executions SET lease_token = ?, lease_until = ?, updated_at = ? "
            "WHERE run_id = ? AND owner = ? AND status IN ('running', 'interrupted') "
            "AND (lease_until <= ? OR lease_token = ?)",
            (token, now + seconds, now, run_id, self.owner, now, token))
        self.connection.commit()
        return cursor.rowcount == 1

    def release(self, run_id: str, token: str) -> bool:
        cursor = self.connection.execute(
            "UPDATE executions SET lease_token = '', lease_until = 0 WHERE run_id = ? AND owner = ? AND lease_token = ?",
            (run_id, self.owner, token))
        self.connection.commit()
        return cursor.rowcount == 1

    def require_lease(self, run_id: str, token: str) -> None:
        row = self.connection.execute(
            "SELECT 1 FROM executions WHERE run_id = ? AND owner = ? AND lease_token = ? "
            "AND lease_until > ? AND status IN ('running', 'interrupted')",
            (run_id, self.owner, token, time.time())).fetchone()
        if row is None:
            raise ValueError("Ejecución inexistente o bloqueo vencido")

    def update(self, run_id: str, token: str, *, status: str, stage: str) -> None:
        if status not in {'running', 'interrupted', 'completed', 'failed'} or not stage:
            raise ValueError("Estado o etapa no válidos")
        now = time.time()
        cursor = self.connection.execute(
            "UPDATE executions SET status = ?, stage = ?, updated_at = ? WHERE run_id = ? AND owner = ? "
            "AND lease_token = ? AND lease_until > ?", (status, stage, now, run_id, self.owner, token, now))
        self.connection.commit()
        if cursor.rowcount != 1:
            raise ValueError("Ejecución inexistente o bloqueo vencido")

    def prepare_query(self, run_id: str, token: str, *, operation_index: int, sql: str,
                      parameters: Sequence[Mapping[str, Any]], project: str, location: str) -> dict[str, Any]:
        if operation_index < 0 or not project.strip() or not location.strip():
            raise ValueError("Se requieren operación, proyecto y región válidos")
        canonical = dumps_json(list(parameters), sort_keys=True, ensure_ascii=False, default=str)
        request_id = f"{run_id}:{operation_index}"
        job_id = stable_query_job_id(request_id, sql, parameters)
        query_id = uuid.uuid4().hex
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            row = self.connection.execute(
                "SELECT 1 FROM executions WHERE run_id = ? AND owner = ? AND lease_token = ? AND lease_until > ? "
                "AND status IN ('running', 'interrupted')", (run_id, self.owner, token, time.time())).fetchone()
            if row is None:
                raise ValueError("Ejecución inexistente o bloqueo vencido")
            self.connection.execute(
                "INSERT OR IGNORE INTO query_operations (run_id, operation_index, sql, parameters_json, project, location, job_id, query_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (run_id, operation_index, sql, canonical, project, location, job_id, query_id))
            operation = dict(self.connection.execute(
                "SELECT * FROM query_operations WHERE run_id = ? AND operation_index = ?", (run_id, operation_index)).fetchone())
            if (operation["sql"], operation["parameters_json"], operation["project"], operation["location"], operation["job_id"]) != (sql, canonical, project, location, job_id):
                raise ValueError("La operación ya existe con una consulta o alcance diferente")
            self.connection.commit()
            operation["request_id"] = request_id
            operation["parameters"] = json.loads(operation.pop("parameters_json"))
            return operation
        except Exception:
            self.connection.rollback()
            raise

    def save_page(self, run_id: str, token: str, *, operation_index: int, start_index: int,
                  rows: Sequence[Mapping[str, Any]], is_last: bool,
                  metadata: Mapping[str, Any] | None = None) -> int:
        """Atomically append a page and advance its offset; exact replays are harmless."""
        if start_index < 0 or (not rows and not is_last):
            raise ValueError("Página u offset no válidos")
        payload = dumps_json(list(rows), sort_keys=True, ensure_ascii=False, default=str)
        try:
            self.connection.execute("BEGIN IMMEDIATE")
            operation = self.connection.execute(
                "SELECT q.* FROM query_operations q JOIN executions e ON e.run_id = q.run_id "
                "WHERE q.run_id = ? AND q.operation_index = ? AND e.owner = ? AND e.lease_token = ? "
                "AND e.lease_until > ? AND e.status IN ('running', 'interrupted')",
                (run_id, operation_index, self.owner, token, time.time())).fetchone()
            if operation is None:
                raise ValueError("Operación inexistente o bloqueo vencido")
            if operation['status'] == 'expired':
                raise ValueError("Los resultados de esta operación caducaron")
            if metadata is not None:
                encoded_metadata = dumps_json(dict(metadata), sort_keys=True, ensure_ascii=False, default=str)
                previous = self.connection.execute(
                    "SELECT metadata_json FROM query_metadata WHERE run_id = ? AND operation_index = ?",
                    (run_id, operation_index)).fetchone()
                if previous and previous['metadata_json'] != encoded_metadata:
                    raise ValueError("Los metadatos de la consulta han cambiado")
                self.connection.execute("INSERT OR IGNORE INTO query_metadata VALUES (?, ?, ?)",
                                        (run_id, operation_index, encoded_metadata))
            existing = self.connection.execute(
                "SELECT * FROM query_pages WHERE run_id = ? AND operation_index = ? AND start_index = ?",
                (run_id, operation_index, start_index)).fetchone()
            if existing:
                if (existing['rows_json'], existing['is_last']) != (payload, int(is_last)):
                    raise ValueError("La página ya existe con contenido diferente")
            else:
                if operation['status'] == 'completed' or operation['rows_saved'] != start_index:
                    raise ValueError("La página no coincide con el offset esperado")
                self.connection.execute(
                    "INSERT INTO query_pages VALUES (?, ?, ?, ?, ?, ?)",
                    (run_id, operation_index, start_index, len(rows), payload, int(is_last)))
                self.connection.execute(
                    "UPDATE query_operations SET rows_saved = ?, status = ? WHERE run_id = ? AND operation_index = ?",
                    (start_index + len(rows), 'completed' if is_last else 'reading', run_id, operation_index))
            self.connection.commit()
            return int(operation['rows_saved']) if existing else start_index + len(rows)
        except Exception:
            self.connection.rollback()
            raise

    def query_result(self, run_id: str, operation_index: int) -> dict[str, Any] | None:
        """Read the persisted prefix, restricted to the owner of the execution."""
        operation = self.connection.execute(
            "SELECT q.* FROM query_operations q JOIN executions e ON e.run_id = q.run_id "
            "WHERE q.run_id = ? AND q.operation_index = ? AND e.owner = ?",
            (run_id, operation_index, self.owner)).fetchone()
        if operation is None:
            return None
        pages = self.connection.execute(
            "SELECT rows_json FROM query_pages WHERE run_id = ? AND operation_index = ? ORDER BY start_index",
            (run_id, operation_index)).fetchall()
        result = dict(operation)
        result['parameters'] = json.loads(result.pop('parameters_json'))
        result['rows'] = [row for page in pages for row in json.loads(page['rows_json'])]
        metadata = self.connection.execute(
            "SELECT metadata_json FROM query_metadata WHERE run_id = ? AND operation_index = ?",
            (run_id, operation_index)).fetchone()
        result['metadata'] = json.loads(metadata['metadata_json']) if metadata else {}
        return result

    def prune(self, *, result_ttl_seconds: int = 86400, run_ttl_seconds: int = 90 * 86400,
              now: float | None = None) -> dict[str, int]:
        """Expire stored rows and old executions for this owner, never leased work."""
        if result_ttl_seconds <= 0 or run_ttl_seconds <= 0 or run_ttl_seconds < result_ttl_seconds:
            raise ValueError("Los periodos de retención no son válidos")
        stamp = time.time() if now is None else now
        try:
            self.connection.execute('BEGIN IMMEDIATE')
            eligible = "SELECT run_id FROM executions WHERE owner = ? AND status IN ('completed', 'failed', 'interrupted') AND updated_at < ? AND lease_until <= ?"
            params = (self.owner, stamp - result_ttl_seconds, stamp)
            self.connection.execute(f'DELETE FROM query_pages WHERE run_id IN ({eligible})', params)
            self.connection.execute(f'DELETE FROM query_metadata WHERE run_id IN ({eligible})', params)
            expired = self.connection.execute(
                f"UPDATE query_operations SET status = 'expired' WHERE status != 'expired' AND run_id IN ({eligible})", params).rowcount
            removed = self.connection.execute(
                "DELETE FROM executions WHERE owner = ? AND status IN ('completed', 'failed', 'interrupted') "
                "AND updated_at < ? AND lease_until <= ?", (self.owner, stamp - run_ttl_seconds, stamp)).rowcount
            self.connection.commit()
            return {'expired_results': expired, 'deleted_runs': removed}
        except Exception:
            self.connection.rollback()
            raise
