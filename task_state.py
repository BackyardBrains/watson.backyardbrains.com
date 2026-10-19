"""
Durable Watson task disposition overlay.

Memory API owns Task rows and exposes canonical task attention fields. Watson
keeps this tiny structured side table as a temporary rollout fallback for
Done / won't-do / wrong / snooze feedback and for pre-migration task-current
reseeds.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


DATA_PATH = Path(os.environ.get("DATA_PATH", Path(__file__).resolve().parent.parent.parent))
DEFAULT_TASK_STATE_DB = DATA_PATH / "memory" / "watson_task_state.sqlite3"
TASK_DISPOSITIONS = {"done", "wont_do", "wrong", "snoozed", "deferred", "not_today"}
SUPPRESSING_DISPOSITIONS = TASK_DISPOSITIONS


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def today_local_date() -> datetime.date:
    return datetime.now(ZoneInfo("America/Detroit")).date()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:96] or "task"


def project_slug(value: str | None) -> str:
    raw = str(value or "").strip()
    if raw.startswith("byb:project/"):
        raw = raw.split("/", 1)[1]
    return slugify(raw or "general")


def stable_task_key(project: str | None, task_id_or_description: str | None) -> str:
    return f"{project_slug(project)}:{slugify(task_id_or_description or '')}"


def source_fingerprint(source_pointer: str | None, description: str | None = None) -> str | None:
    material = "\n".join((str(source_pointer or "").strip(), str(description or "").strip())).strip()
    if not material:
        return None
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def memory_task_numeric_id(task_id: Any) -> str | None:
    raw = str(task_id or "").strip()
    if not raw:
        return None
    match = re.search(r"(\d+)$", raw)
    return match.group(1) if match else raw


class TaskDispositionStore:
    def __init__(self, db_path: Path | None = None):
        self.db_path = Path(db_path or os.environ.get("WATSON_TASK_STATE_DB", DEFAULT_TASK_STATE_DB))
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS task_dispositions (
                    task_key TEXT PRIMARY KEY,
                    task_id TEXT,
                    source_fingerprint TEXT,
                    disposition TEXT NOT NULL,
                    reason TEXT,
                    actor TEXT,
                    source TEXT,
                    project_slug TEXT,
                    description TEXT,
                    source_pointer TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_task_dispositions_task_id ON task_dispositions(task_id)")
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_dispositions_source_fp ON task_dispositions(source_fingerprint)"
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_task_dispositions_disposition ON task_dispositions(disposition)"
            )

    def record(
        self,
        *,
        task_key: str,
        disposition: str,
        task_id: Any = None,
        reason: str | None = None,
        actor: str | None = "Greg",
        source: str | None = "dashboard",
        project: str | None = None,
        description: str | None = None,
        source_pointer: str | None = None,
        source_fingerprint_value: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        disposition_norm = str(disposition or "").strip().lower().replace("-", "_")
        if disposition_norm in {"wontdo", "won_t_do", "won't_do", "rejected"}:
            disposition_norm = "wont_do"
        if disposition_norm in {"snooze", "snoozed_until"}:
            disposition_norm = "snoozed"
        if disposition_norm in {"delay", "delayed", "later"}:
            disposition_norm = "deferred"
        if disposition_norm in {"not-today", "not today", "not_now", "not-now"}:
            disposition_norm = "not_today"
        if disposition_norm not in TASK_DISPOSITIONS:
            raise ValueError(f"Invalid disposition {disposition!r}; expected one of {sorted(TASK_DISPOSITIONS)}")
        now = utc_now()
        existing = self.get(task_key)
        created_at = str(existing.get("created_at")) if existing else now
        payload = {
            "task_key": task_key,
            "task_id": memory_task_numeric_id(task_id),
            "source_fingerprint": source_fingerprint_value or source_fingerprint(source_pointer, description),
            "disposition": disposition_norm,
            "reason": reason,
            "actor": actor,
            "source": source,
            "project_slug": project_slug(project),
            "description": description,
            "source_pointer": source_pointer,
            "created_at": created_at,
            "updated_at": now,
            "metadata_json": json.dumps(metadata or {}, sort_keys=True),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO task_dispositions(
                    task_key, task_id, source_fingerprint, disposition, reason, actor, source,
                    project_slug, description, source_pointer, created_at, updated_at, metadata_json
                )
                VALUES(
                    :task_key, :task_id, :source_fingerprint, :disposition, :reason, :actor, :source,
                    :project_slug, :description, :source_pointer, :created_at, :updated_at, :metadata_json
                )
                ON CONFLICT(task_key) DO UPDATE SET
                    task_id=COALESCE(excluded.task_id, task_dispositions.task_id),
                    source_fingerprint=COALESCE(excluded.source_fingerprint, task_dispositions.source_fingerprint),
                    disposition=excluded.disposition,
                    reason=excluded.reason,
                    actor=excluded.actor,
                    source=excluded.source,
                    project_slug=excluded.project_slug,
                    description=excluded.description,
                    source_pointer=excluded.source_pointer,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                payload,
            )
        payload["metadata"] = metadata or {}
        payload.pop("metadata_json", None)
        return payload

    def get(self, task_key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM task_dispositions WHERE task_key = ?", (task_key,)).fetchone()
        return self._row_to_dict(row)

    def disposition_for(
        self,
        *,
        task_key: str | None = None,
        task_id: Any = None,
        source_fingerprint_value: str | None = None,
    ) -> dict[str, Any] | None:
        clauses = []
        params: list[Any] = []
        if task_key:
            clauses.append("task_key = ?")
            params.append(task_key)
        numeric_id = memory_task_numeric_id(task_id)
        if numeric_id:
            clauses.append("task_id = ?")
            params.append(numeric_id)
        if source_fingerprint_value:
            clauses.append("source_fingerprint = ?")
            params.append(source_fingerprint_value)
        if not clauses:
            return None
        with self._connect() as conn:
            row = conn.execute(
                f"SELECT * FROM task_dispositions WHERE {' OR '.join(clauses)} ORDER BY updated_at DESC LIMIT 1",
                params,
            ).fetchone()
        return self._row_to_dict(row)

    def is_suppressed(
        self,
        *,
        task_key: str | None = None,
        task_id: Any = None,
        source_fingerprint_value: str | None = None,
    ) -> bool:
        record = self.disposition_for(
            task_key=task_key,
            task_id=task_id,
            source_fingerprint_value=source_fingerprint_value,
        )
        if not record or record.get("disposition") not in SUPPRESSING_DISPOSITIONS:
            return False
        if record.get("disposition") == "snoozed":
            metadata = record.get("metadata") if isinstance(record.get("metadata"), dict) else {}
            snooze_until = metadata.get("snooze_until_date")
            if snooze_until:
                try:
                    return datetime.fromisoformat(str(snooze_until)[:10]).date() > today_local_date()
                except ValueError:
                    return True
        return True

    def list(self, disposition: str | None = None, limit: int = 500) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 2000))
        sql = "SELECT * FROM task_dispositions"
        params: list[Any] = []
        if disposition:
            sql += " WHERE disposition = ?"
            params.append(disposition)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [row for row in (self._row_to_dict(row) for row in rows) if row]

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data
