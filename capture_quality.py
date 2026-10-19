"""
Reversible quality controls for Memory API captures.

The production Memory API is the source of truth for captures, but it does not
guarantee a reversible quarantine field on every row. Watson keeps this small
local overlay so noisy semantic-memory rows can be hidden from Watson search and
dashboard synthesis without deleting the underlying evidence.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


CAPTURE_STATUSES = {"active", "quarantined", "superseded", "deleted", "wrong_task_source"}
DEFAULT_JUNK_QUERIES = (
    "All quiet",
    "0 tasks processed",
    "0 emails",
    "HEARTBEAT_OK",
    "status page updated",
    "dashboard sync no changes",
    "Business hours sync",
    "Delta since",
    "SENT MAIL RECONCILIATION",
)

TAG_RE = re.compile(r"\[([a-zA-Z0-9:_-]+)(?:[^\]]*)\]")
LEADING_TAG_RE = re.compile(r"^\s*\[([^\]]+)\]")
JUNK_RULES: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("idle-all-quiet", re.compile(r"\ball quiet\b", re.IGNORECASE)),
    ("idle-zero-tasks", re.compile(r"\b0\s+tasks?\s+processed\b", re.IGNORECASE)),
    (
        "idle-zero-email",
        re.compile(r"\b0\s+(?:new\s+)?emails?\s+(?:captured|summarized|processed|found)\b", re.IGNORECASE),
    ),
    ("heartbeat-ok", re.compile(r"^\s*(?:\[heartbeat[^\]]*\]\s*)?HEARTBEAT_OK\s*$", re.IGNORECASE)),
    (
        "dashboard-only-refresh",
        re.compile(r"\b(?:status page|status\.json|dashboard)\s+(?:updated|refreshed)\b", re.IGNORECASE),
    ),
    (
        "watson-sync-wrapper",
        re.compile(
            r"^\s*\[watson-(?:sync|midday|morning|afternoon|afternoon-wrap|night-watch)[^\]]*\].{0,200}"
            r"(?:business hours sync|delta since|sent mail reconciliation|briefing complete|midday pulse|afternoon wrap|night watch sync)",
            re.IGNORECASE | re.DOTALL,
        ),
    ),
    (
        "watson-sync-wrapper",
        re.compile(
            r"^\s*(?:\[[^\]]+\]\s*)?"
            r"(?:business hours sync|(?:mid-)?morning status sync|afternoon status sync|sent mail reconciliation|"
            r"midday pulse|briefing complete|watson midday pulse|watson afternoon wrap|sync\b)",
            re.IGNORECASE,
        ),
    ),
)
NONZERO_EMAIL_RE = re.compile(r"\b[1-9]\d*\s+(?:new\s+)?emails?\s+(?:captured|summarized|processed|found)\b", re.IGNORECASE)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def capture_text(capture: dict[str, Any]) -> str:
    return str(
        capture.get("raw_content")
        or capture.get("content")
        or capture.get("text")
        or capture.get("summary")
        or ""
    )


def derive_source_from_text(text: str) -> str:
    match = LEADING_TAG_RE.search(text or "")
    if not match:
        return "unknown"

    tag = " ".join(match.group(1).strip().split())
    tag_lower = tag.lower()
    first_token = tag.split()[0].strip().strip(":,;").lower()

    if tag_lower.startswith("email"):
        return "email-capture"
    if first_token.startswith("agent:"):
        return first_token.split(":", 1)[1] or "agent"
    if first_token.startswith("project:"):
        project = tag.split(":", 1)[1].strip().split()[0] if ":" in tag else ""
        return f"project:{project.lower()}" if project else "project-capture"
    if first_token.startswith("source="):
        return first_token.split("=", 1)[1] or "unknown"
    if first_token in {"sync", "insert", "email"}:
        return first_token
    if first_token:
        return first_token
    return "unknown"


def capture_source(capture: dict[str, Any]) -> str:
    for key in ("source", "capture_source", "origin", "created_by"):
        value = capture.get(key)
        if value not in (None, "", "unknown"):
            return str(value)
    return derive_source_from_text(capture_text(capture))


def capture_source_pointer(capture: dict[str, Any]) -> str | None:
    for key in ("source_pointer", "source_url", "url", "permalink", "thread_link"):
        value = capture.get(key)
        if value not in (None, ""):
            return str(value)
    text = capture_text(capture)
    match = re.search(r"https?://\S+", text)
    return match.group(0).rstrip(").,;") if match else None


def capture_quality_metadata(capture: dict[str, Any]) -> dict[str, Any]:
    derived = derive_source_from_text(capture_text(capture))
    structured = str(capture.get("source") or capture.get("capture_source") or "")
    return {
        "source_structured": structured or None,
        "source_derived": derived if derived != "unknown" else None,
    }


def capture_created_at(capture: dict[str, Any]) -> str:
    return str(capture.get("created_at") or capture.get("updated_at") or "")


def capture_id(capture: dict[str, Any]) -> str | None:
    for key in ("id", "capture_id", "@id"):
        value = capture.get(key)
        if value not in (None, ""):
            return str(value)
    return None


def capture_fingerprint(capture: dict[str, Any]) -> str:
    material = "\n".join((capture_source(capture), capture_created_at(capture), capture_text(capture)))
    return hashlib.sha256(material.encode("utf-8", errors="replace")).hexdigest()


def capture_key(capture: dict[str, Any]) -> str:
    cid = capture_id(capture)
    if cid:
        return f"id:{cid}"
    return f"sha256:{capture_fingerprint(capture)}"


def classify_capture(capture: dict[str, Any]) -> str | None:
    """Return a quarantine reason for obvious low-signal captures."""
    text = capture_text(capture).strip()
    if not text:
        return "empty-capture"

    compact_len = len(text)
    source = capture_source(capture).lower()
    for reason, pattern in JUNK_RULES:
        if not pattern.search(text):
            continue
        if reason == "idle-zero-tasks" and NONZERO_EMAIL_RE.search(text):
            # These rows may still be poorly formatted, but they often contain
            # the only summary/pointer for a real email. Standardize later;
            # do not quarantine as heartbeat junk.
            continue
        if reason == "dashboard-only-refresh":
            # Full briefings may legitimately mention that the dashboard was
            # updated. Only quarantine rows that are basically liveness notes.
            if compact_len > 700:
                continue
            if not any(marker in text.lower() for marker in ("no changes", "nothing", "heartbeat", "sync", "updated_at")):
                continue
        if reason in {"idle-all-quiet", "idle-zero-tasks", "idle-zero-email"}:
            if compact_len > 700 and "heartbeat" not in source and "check" not in source:
                continue
        return reason
    return None


def capture_tags(capture: dict[str, Any]) -> list[str]:
    return TAG_RE.findall(capture_text(capture))


class CaptureQualityStore:
    def __init__(self, db_path: Path):
        self.db_path = db_path
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
                CREATE TABLE IF NOT EXISTS capture_quality (
                    capture_key TEXT PRIMARY KEY,
                    capture_id TEXT,
                    fingerprint TEXT NOT NULL,
                    status TEXT NOT NULL,
                    reason TEXT,
                    source TEXT,
                    source_pointer TEXT,
                    raw_preview TEXT,
                    created_at TEXT,
                    updated_at TEXT NOT NULL,
                    metadata_json TEXT
                )
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_capture_quality_status ON capture_quality(status)")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_capture_quality_source ON capture_quality(source)")

    def get(self, key: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM capture_quality WHERE capture_key = ?", (key,)).fetchone()
        if not row:
            return None
        data = dict(row)
        try:
            data["metadata"] = json.loads(data.pop("metadata_json") or "{}")
        except json.JSONDecodeError:
            data["metadata"] = {}
        return data

    def status_for(self, capture: dict[str, Any]) -> str:
        record = self.get(capture_key(capture))
        return str(record.get("status")) if record else "active"

    def set_status(
        self,
        capture: dict[str, Any],
        status: str,
        reason: str | None = None,
        source_pointer: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in CAPTURE_STATUSES:
            raise ValueError(f"Invalid capture status {status!r}; expected one of {sorted(CAPTURE_STATUSES)}")

        text = capture_text(capture)
        key = capture_key(capture)
        cid = capture_id(capture)
        fingerprint = capture_fingerprint(capture)
        now = utc_now()
        merged_metadata = {**capture_quality_metadata(capture), **(metadata or {})}
        payload = {
            "capture_key": key,
            "capture_id": cid,
            "fingerprint": fingerprint,
            "status": status,
            "reason": reason,
            "source": capture_source(capture),
            "source_pointer": source_pointer or capture_source_pointer(capture),
            "raw_preview": text[:500],
            "created_at": capture_created_at(capture),
            "updated_at": now,
            "metadata_json": json.dumps(merged_metadata, sort_keys=True),
        }
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO capture_quality(
                    capture_key, capture_id, fingerprint, status, reason, source,
                    source_pointer, raw_preview, created_at, updated_at, metadata_json
                )
                VALUES(
                    :capture_key, :capture_id, :fingerprint, :status, :reason, :source,
                    :source_pointer, :raw_preview, :created_at, :updated_at, :metadata_json
                )
                ON CONFLICT(capture_key) DO UPDATE SET
                    capture_id=excluded.capture_id,
                    fingerprint=excluded.fingerprint,
                    status=excluded.status,
                    reason=excluded.reason,
                    source=excluded.source,
                    source_pointer=excluded.source_pointer,
                    raw_preview=excluded.raw_preview,
                    created_at=excluded.created_at,
                    updated_at=excluded.updated_at,
                    metadata_json=excluded.metadata_json
                """,
                payload,
            )
        payload["metadata"] = merged_metadata
        payload.pop("metadata_json", None)
        return payload

    def refresh_provenance(self, capture: dict[str, Any]) -> bool:
        key = capture_key(capture)
        record = self.get(key)
        if not record:
            return False
        source = capture_source(capture)
        pointer = capture_source_pointer(capture)
        existing_source = str(record.get("source") or "")
        existing_pointer = str(record.get("source_pointer") or "")
        needs_source = existing_source in {"", "unknown"} and source != "unknown"
        needs_pointer = not existing_pointer and bool(pointer)
        if not needs_source and not needs_pointer:
            return False
        metadata = {**record.get("metadata", {}), **capture_quality_metadata(capture)}
        with self._connect() as conn:
            conn.execute(
                """
                UPDATE capture_quality
                SET source = CASE WHEN source IS NULL OR source = '' OR source = 'unknown' THEN ? ELSE source END,
                    source_pointer = CASE WHEN source_pointer IS NULL OR source_pointer = '' THEN ? ELSE source_pointer END,
                    metadata_json = ?,
                    updated_at = ?
                WHERE capture_key = ?
                """,
                (source, pointer, json.dumps(metadata, sort_keys=True), utc_now(), key),
            )
        return True

    def filter_visible(self, captures: list[dict[str, Any]], include_quarantined: bool = False) -> list[dict[str, Any]]:
        if include_quarantined:
            return captures
        return [
            capture
            for capture in captures
            if self.status_for(capture) not in {"quarantined", "deleted", "superseded", "wrong_task_source"}
        ]

    def list_records(self, status: str | None = None, limit: int = 100) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit), 1000))
        sql = "SELECT * FROM capture_quality"
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE status = ?"
            params = (status,)
        sql += " ORDER BY updated_at DESC LIMIT ?"
        params = (*params, limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        records = []
        for row in rows:
            record = dict(row)
            try:
                record["metadata"] = json.loads(record.pop("metadata_json") or "{}")
            except json.JSONDecodeError:
                record["metadata"] = {}
            records.append(record)
        return records


def audit_captures(captures: list[dict[str, Any]]) -> dict[str, Any]:
    source_counts: Counter[str] = Counter()
    tag_counts: Counter[str] = Counter()
    reason_counts: Counter[str] = Counter()
    junk_matches: list[dict[str, Any]] = []
    total_chars = 0
    junk_chars = 0

    for capture in captures:
        text = capture_text(capture)
        total_chars += len(text)
        source_counts[capture_source(capture)] += 1
        tag_counts.update(capture_tags(capture))
        reason = classify_capture(capture)
        if reason:
            reason_counts[reason] += 1
            junk_chars += len(text)
            junk_matches.append({
                "capture_key": capture_key(capture),
                "capture_id": capture_id(capture),
                "source": capture_source(capture),
                "created_at": capture_created_at(capture),
                "reason": reason,
                "estimated_tokens": max(1, len(text) // 4),
                "preview": text[:220],
            })

    return {
        "captures_scanned": len(captures),
        "junk_matches": len(junk_matches),
        "estimated_total_tokens": max(1, total_chars // 4) if captures else 0,
        "estimated_junk_tokens": max(1, junk_chars // 4) if junk_matches else 0,
        "sources": source_counts.most_common(),
        "tags": tag_counts.most_common(50),
        "junk_reasons": reason_counts.most_common(),
        "matches": junk_matches,
    }
