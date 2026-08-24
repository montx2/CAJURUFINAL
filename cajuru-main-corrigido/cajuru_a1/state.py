"""Checkpoints SQLite sem senhas para observabilidade e recuperação."""

from __future__ import annotations

import json
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sanitize_metadata(value):
    if isinstance(value, dict):
        return {
            str(key): _sanitize_metadata(item)
            for key, item in value.items()
            if not any(word in str(key).casefold() for word in ("senha", "password", "secret", "token"))
        }
    if isinstance(value, list):
        return [_sanitize_metadata(item) for item in value]
    return value


class StateStore:
    def __init__(self, path: Path):
        self.path = Path(path).expanduser().resolve(strict=False)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.path, timeout=15)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute("PRAGMA foreign_keys=ON")
        self._lock = threading.Lock()
        self._schema()
        self.run_id = ""

    def _schema(self) -> None:
        self.connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                started_utc TEXT NOT NULL,
                finished_utc TEXT,
                status TEXT NOT NULL,
                source_root TEXT NOT NULL,
                inventory_digest TEXT NOT NULL,
                stats_json TEXT NOT NULL DEFAULT '{}'
            );
            CREATE TABLE IF NOT EXISTS files (
                run_id TEXT NOT NULL,
                relative_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                kind TEXT NOT NULL,
                status TEXT NOT NULL,
                attempts INTEGER NOT NULL DEFAULT 0,
                error_code TEXT NOT NULL DEFAULT '',
                metadata_json TEXT NOT NULL DEFAULT '{}',
                updated_utc TEXT NOT NULL,
                PRIMARY KEY (run_id, relative_path),
                FOREIGN KEY (run_id) REFERENCES runs(run_id)
            );
            CREATE INDEX IF NOT EXISTS files_hash_idx ON files(sha256);
            """
        )
        columns = {row[1] for row in self.connection.execute("PRAGMA table_info(files)")}
        if "metadata_json" not in columns:
            self.connection.execute("ALTER TABLE files ADD COLUMN metadata_json TEXT NOT NULL DEFAULT '{}'")
        self.connection.commit()

    def start(self, source_root: str, inventory_digest: str) -> str:
        with self._lock:
            # Uma execução que permaneceu running foi interrompida; preservamos
            # o histórico em vez de fingir sucesso.
            self.connection.execute(
                "UPDATE runs SET status='interrompida', finished_utc=? WHERE status='running'",
                (_now(),),
            )
            self.run_id = uuid.uuid4().hex
            self.connection.execute(
                "INSERT INTO runs(run_id, started_utc, status, source_root, inventory_digest) VALUES(?,?,?,?,?)",
                (self.run_id, _now(), "running", source_root, inventory_digest),
            )
            self.connection.commit()
        return self.run_id

    def file(self, relative_path: str, sha256: str, kind: str, status: str, *, attempts: int = 0, error_code: str = "", metadata: dict | None = None) -> None:
        if not self.run_id:
            return
        metadata_json = json.dumps(_sanitize_metadata(metadata or {}), ensure_ascii=False, default=str)
        with self._lock:
            self.connection.execute(
                """INSERT INTO files(run_id, relative_path, sha256, kind, status, attempts, error_code, metadata_json, updated_utc)
                   VALUES(?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(run_id, relative_path) DO UPDATE SET
                     sha256=excluded.sha256, kind=excluded.kind, status=excluded.status,
                     attempts=excluded.attempts, error_code=excluded.error_code,
                     metadata_json=excluded.metadata_json, updated_utc=excluded.updated_utc""",
                (self.run_id, relative_path, sha256, kind, status, int(attempts), str(error_code), metadata_json, _now()),
            )
            self.connection.commit()

    def previous_file(self, relative_path: str, sha256: str, kind: str) -> dict | None:
        """Recupera somente metadados não secretos de execução anterior."""
        row = self.connection.execute(
            """SELECT metadata_json FROM files
               WHERE run_id <> ? AND relative_path=? AND sha256=? AND kind=?
                 AND status IN ('aberto','bloqueado') AND metadata_json <> '{}'
               ORDER BY updated_utc DESC LIMIT 1""",
            (self.run_id, relative_path, sha256, kind),
        ).fetchone()
        if not row:
            return None
        try:
            value = json.loads(row[0])
            return value if isinstance(value, dict) else None
        except (TypeError, json.JSONDecodeError):
            return None

    def finish(self, status: str, stats: dict | None = None) -> None:
        if not self.run_id:
            return
        with self._lock:
            self.connection.execute(
                "UPDATE runs SET status=?, finished_utc=?, stats_json=? WHERE run_id=?",
                (str(status), _now(), json.dumps(stats or {}, ensure_ascii=False), self.run_id),
            )
            self.connection.commit()

    def list_recent_runs(self, limit: int = 50) -> list[dict]:
        rows = self.connection.execute(
            "SELECT run_id, started_utc, finished_utc, status, source_root, stats_json "
            "FROM runs ORDER BY started_utc DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        result = []
        for run_id, started, finished, status, source_root, stats_json in rows:
            try:
                stats = json.loads(stats_json) if stats_json else {}
            except (TypeError, json.JSONDecodeError):
                stats = {}
            result.append({
                "run_id": run_id,
                "started_utc": started,
                "finished_utc": finished,
                "status": status,
                "source_root": source_root,
                "stats": stats,
            })
        return result

    def file_history(self, relative_path: str, limit: int = 10) -> list[dict]:
        """Histórico de um arquivo (PFX/PDF) em execuções anteriores.

        Útil para o relatório de diagnóstico: mostra 'o que tinha antes',
        quantas tentativas de senha foram feitas, qual o erro, etc.
        """
        rows = self.connection.execute(
            "SELECT run_id, updated_utc, sha256, status, attempts, error_code, metadata_json "
            "FROM files WHERE relative_path=? ORDER BY updated_utc DESC LIMIT ?",
            (relative_path, int(limit)),
        ).fetchall()
        result = []
        for run_id, updated, sha256, status, attempts, error_code, metadata_json in rows:
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            result.append({
                "run_id": run_id,
                "updated_utc": updated,
                "sha256": sha256,
                "status": status,
                "attempts": attempts,
                "error_code": error_code,
                "metadata": metadata,
            })
        return result

    def all_files_current(self) -> list[dict]:
        if not self.run_id:
            return []
        rows = self.connection.execute(
            "SELECT relative_path, sha256, kind, status, attempts, error_code, metadata_json "
            "FROM files WHERE run_id=? ORDER BY relative_path",
            (self.run_id,),
        ).fetchall()
        result = []
        for relative_path, sha256, kind, status, attempts, error_code, metadata_json in rows:
            try:
                metadata = json.loads(metadata_json) if metadata_json else {}
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            result.append({
                "relative_path": relative_path,
                "sha256": sha256,
                "kind": kind,
                "status": status,
                "attempts": attempts,
                "error_code": error_code,
                "metadata": metadata,
            })
        return result

    def close(self) -> None:
        self.connection.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, _exc, _tb):
        if exc_type and self.run_id:
            self.finish("falha")
        self.close()
