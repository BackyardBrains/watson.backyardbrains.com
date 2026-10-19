"""
Durable Watson idea/artifact overlay.

Memory API owns facts, tasks, projects, and captures. This store owns a smaller
UX contract: "Watson proactively made or suggested this thing." The UI can show
these rows with an AI Star badge, and Greg can accept, dismiss, or mark them
wrong without mutating the underlying evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


IDEA_STATUSES = {"pending", "accepted", "dismissed", "wrong", "archived"}
TERMINAL_STATUSES = {"accepted", "dismissed", "wrong", "archived"}
IDEA_KINDS = {
    "artifact",
    "document",
    "presentation",
    "draft",
    "email_draft",
    "research_report",
    "decision_brief",
    "data_map",
    "task_suggestion",
    "project_suggestion",
    "idea",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def slugify(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", str(value or "").lower()).strip("-")
    return slug[:96] or "idea"


def stable_idea_id(payload: dict[str, Any]) -> str:
    raw_id = str(payload.get("id") or "").strip()
    if raw_id:
        return slugify(raw_id)
    source_type = str(payload.get("source_type") or "").strip()
    source_id = str(payload.get("source_id") or "").strip()
    kind = str(payload.get("kind") or "idea").strip()
    if source_type and source_id:
        return slugify(f"{kind}-{source_type}-{source_id}")
    material = "\n".join(
        [
            kind,
            str(payload.get("project") or ""),
            str(payload.get("title") or ""),
            str(payload.get("artifact_url") or ""),
            str(payload.get("summary") or ""),
        ]
    )
    digest = hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()[:12]
    return slugify(f"{kind}-{payload.get('title') or digest}-{digest}")


def normalize_kind(value: str | None) -> str:
    raw = str(value or "idea").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"report", "research"}:
        raw = "research_report"
    if raw in {"brief"}:
        raw = "decision_brief"
    if raw not in IDEA_KINDS:
        raw = "artifact"
    return raw


def normalize_status(value: str | None) -> str:
    raw = str(value or "pending").strip().lower().replace("-", "_").replace(" ", "_")
    if raw in {"accept", "keep", "kept"}:
        raw = "accepted"
    if raw in {"dismiss", "reject", "rejected", "wont_do", "won_t_do", "wontdo"}:
        raw = "dismissed"
    if raw in {"incorrect", "bad_source", "not_real"}:
        raw = "wrong"
    if raw not in IDEA_STATUSES:
        raw = "pending"
    return raw


def normalize_evidence(value: Any) -> list[dict[str, Any]]:
    if value is None:
        return []
    items = value if isinstance(value, list) else [value]
    normalized: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, dict):
            normalized.append(
                {
                    "label": str(item.get("label") or item.get("title") or item.get("source") or item.get("url") or "Source"),
                    "url": str(item.get("url") or item.get("source_url") or "") or None,
                    "note": str(item.get("note") or item.get("description") or item.get("evidence") or "") or None,
                }
            )
        else:
            normalized.append({"label": str(item), "url": None, "note": None})
    return normalized


def idea_to_row_payload(payload: dict[str, Any], existing: dict[str, Any] | None = None) -> dict[str, Any]:
    now = utc_now()
    normalized_status = normalize_status(str(payload.get("status") or "pending"))
    if existing and existing.get("status") in TERMINAL_STATUSES and normalized_status == "pending":
        normalized_status = str(existing["status"])
    confidence = payload.get("confidence")
    try:
        confidence_value = None if confidence in (None, "") else max(0.0, min(1.0, float(confidence)))
    except (TypeError, ValueError):
        confidence_value = None
    return {
        "id": stable_idea_id(payload),
        "kind": normalize_kind(str(payload.get("kind") or "")),
        "title": str(payload.get("title") or "Watson idea").strip()[:240],
        "summary": str(payload.get("summary") or "").strip(),
        "project": str(payload.get("project") or "").strip() or None,
        "status": normalized_status,
        "confidence": confidence_value,
        "artifact_url": str(payload.get("artifact_url") or payload.get("url") or "").strip() or None,
        "artifact_path": str(payload.get("artifact_path") or "").strip() or None,
        "source_type": str(payload.get("source_type") or "watson").strip() or "watson",
        "source_id": str(payload.get("source_id") or "").strip() or None,
        "source_url": str(payload.get("source_url") or "").strip() or None,
        "created_by": str(payload.get("created_by") or "watson").strip() or "watson",
        "created_at": str(existing.get("created_at")) if existing else str(payload.get("created_at") or now),
        "updated_at": now,
        "expires_at": str(payload.get("expires_at") or "").strip() or None,
        "evidence_json": json.dumps(normalize_evidence(payload.get("evidence")), sort_keys=True),
        "metadata_json": json.dumps(payload.get("metadata") or {}, sort_keys=True),
    }


class WatsonIdeaStore:
    def __init__(self, db_path: Path):
        self.db_path = Path(db_path)
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
                CREATE TABLE IF NOT EXISTS watson_ideas (
                    id TEXT PRIMARY KEY,
                    kind TEXT NOT NULL,
                    title TEXT NOT NULL,
                    summary TEXT,
                    project TEXT,
                    status TEXT NOT NULL,
                    confidence REAL,
                    artifact_url TEXT,
                    artifact_path TEXT,
                    source_type TEXT,
                    source_id TEXT,
                    source_url TEXT,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    expires_at TEXT,
                    evidence_json TEXT,
                    metadata_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watson_ideas_status ON watson_ideas(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watson_ideas_project ON watson_ideas(project)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watson_ideas_source ON watson_ideas(source_type, source_id)")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS watson_idea_feedback (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    idea_id TEXT NOT NULL,
                    disposition TEXT NOT NULL,
                    reason TEXT,
                    actor TEXT,
                    created_at TEXT NOT NULL,
                    metadata_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_watson_idea_feedback_idea ON watson_idea_feedback(idea_id)")

    def get(self, idea_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM watson_ideas WHERE id = ?", (idea_id,)).fetchone()
        return self._row_to_dict(row)

    def upsert(self, payload: dict[str, Any]) -> dict[str, Any]:
        idea_id = stable_idea_id(payload)
        existing = self.get(idea_id)
        row = idea_to_row_payload({**payload, "id": idea_id}, existing=existing)
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watson_ideas(
                    id, kind, title, summary, project, status, confidence, artifact_url, artifact_path,
                    source_type, source_id, source_url, created_by, created_at, updated_at, expires_at,
                    evidence_json, metadata_json
                )
                VALUES(
                    :id, :kind, :title, :summary, :project, :status, :confidence, :artifact_url, :artifact_path,
                    :source_type, :source_id, :source_url, :created_by, :created_at, :updated_at, :expires_at,
                    :evidence_json, :metadata_json
                )
                ON CONFLICT(id) DO UPDATE SET
                    kind=excluded.kind,
                    title=excluded.title,
                    summary=excluded.summary,
                    project=excluded.project,
                    status=excluded.status,
                    confidence=excluded.confidence,
                    artifact_url=excluded.artifact_url,
                    artifact_path=excluded.artifact_path,
                    source_type=excluded.source_type,
                    source_id=excluded.source_id,
                    source_url=excluded.source_url,
                    created_by=excluded.created_by,
                    updated_at=excluded.updated_at,
                    expires_at=excluded.expires_at,
                    evidence_json=excluded.evidence_json,
                    metadata_json=excluded.metadata_json
                """,
                row,
            )
        return self.get(row["id"]) or row

    def list(self, status: str | None = "pending", project: str | None = None, limit: int = 50) -> list[dict[str, Any]]:
        clauses: list[str] = []
        params: list[Any] = []
        if status and status != "all":
            clauses.append("status = ?")
            params.append(normalize_status(status))
        if project:
            clauses.append("LOWER(project) = ?")
            params.append(project.strip().lower())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(max(1, min(int(limit), 200)))
        with self._connect() as conn:
            rows = conn.execute(
                f"SELECT * FROM watson_ideas {where} ORDER BY updated_at DESC, created_at DESC LIMIT ?",
                params,
            ).fetchall()
        return [row for row in (self._row_to_dict(row) for row in rows) if row]

    def record_feedback(
        self,
        idea_id: str,
        disposition: str,
        reason: str | None = None,
        actor: str = "Greg",
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        normalized = normalize_status(disposition)
        if normalized == "pending":
            raise ValueError("disposition must be accepted, dismissed, wrong, or archived")
        if not self.get(idea_id):
            raise ValueError(f"Watson idea {idea_id!r} not found")
        now = utc_now()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO watson_idea_feedback(idea_id, disposition, reason, actor, created_at, metadata_json)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (idea_id, normalized, reason, actor, now, json.dumps(metadata or {}, sort_keys=True)),
            )
            conn.execute(
                "UPDATE watson_ideas SET status = ?, updated_at = ? WHERE id = ?",
                (normalized, now, idea_id),
            )
        return self.get(idea_id) or {}

    @staticmethod
    def _row_to_dict(row: sqlite3.Row | None) -> dict[str, Any] | None:
        if not row:
            return None
        data = dict(row)
        try:
            data["evidence"] = json.loads(data.pop("evidence_json") or "[]")
        except json.JSONDecodeError:
            data["evidence"] = []
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        data["badge"] = "ai_star"
        data["badge_label"] = "AI Star"
        data["origin"] = "watson_background" if data.get("source_type") == "background_task" else "watson"
        data["is_ai_generated"] = True
        return data
