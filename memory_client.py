"""
Memory API client. Watson uses this when USE_MEMORY_API=true.
"""

import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import httpx

from capture_quality import (
    CaptureQualityStore,
    DEFAULT_JUNK_QUERIES,
    audit_captures,
    capture_key,
    classify_capture,
)
from task_state import TaskDispositionStore, source_fingerprint, stable_task_key

MEMORY_API_URL = os.environ.get("MEMORY_API_URL", "https://memory.backyardbrains.com")
MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "")
DATA_PATH = Path(os.environ.get("DATA_PATH", Path(__file__).resolve().parent.parent.parent))
QUALITY_DB_PATH = Path(os.environ.get("WATSON_CAPTURE_QUALITY_DB", DATA_PATH / "memory" / "watson_capture_quality.sqlite3"))
OPENCLAW_CRON_JOBS_PATH = Path(os.environ.get("OPENCLAW_CRON_JOBS_PATH", DATA_PATH / "cron" / "jobs.json"))
OPENCLAW_CRON_RUNS_DIR = Path(os.environ.get("OPENCLAW_CRON_RUNS_DIR", DATA_PATH / "cron" / "runs"))

HEADERS = {"X-API-Key": MEMORY_API_KEY, "Content-Type": "application/json"}
QUALITY_STORE = CaptureQualityStore(QUALITY_DB_PATH)
TASK_DISPOSITION_STORE = TaskDispositionStore()
PROJECT_STATUS_RE = re.compile(
    r"\[project-status-current\]\s+project=(?P<slug>[\w-]+)\s+priority=(?P<priority>\S+)\s+status=\"(?P<status>[^\"]+)\""
)
WHAT_NOW_RE = re.compile(
    r"\[what-now-current\]\s+rank=(?P<rank>\d+)(?:\s+id=\"[^\"]+\")?\s+project=\"(?P<project>[^\"]+)\"\s+action=\"(?P<action>[^\"]+)\"\s+effort=\"(?P<effort>[^\"]+)\"\s+reason=\"(?P<reason>[^\"]+)\""
)
TASK_CURRENT_RE = re.compile(
    r"\[task-current\]\s+(?:id=\"(?P<id>[^\"]+)\"\s+)?project=\"(?P<project>[^\"]+)\"\s+owner=\"(?P<owner>[^\"]*)\"\s+status=\"(?P<status>[^\"]+)\"\s+priority=\"(?P<priority>[^\"]*)\"\s+due=\"(?P<due>[^\"]*)\"\s+source_pointer=\"(?P<source_pointer>[^\"]*)\""
)
TASK_BODY_RE = re.compile(r"^(?:Task|Action):\s*(?P<task>.+)$", re.MULTILINE)
TASK_KEY_RE = re.compile(r"^\[watson-task-key:\s*(?P<key>[^\]]+)\]\s*$", re.MULTILINE)
TASK_META_RE = re.compile(r"^(Owner|Priority|Source pointer|Source capture|Source fingerprint):\s*(.*)$", re.MULTILINE)
TASK_STATUS_ALIASES = {
    "open": "open",
    "to do": "open",
    "todo": "open",
    "active": "open",
    "in_progress": "in_progress",
    "in progress": "in_progress",
    "doing": "in_progress",
    "blocked": "blocked",
    "waiting": "blocked",
    "needs_clarification": "blocked",
    "done": "done",
    "complete": "done",
    "completed": "done",
    "closed": "done",
    "dropped": "dropped",
    "deferred": "open",
    "superseded": "dropped",
    "obsolete": "dropped",
    "stale": "dropped",
    "cancelled": "dropped",
    "canceled": "dropped",
}
CLOSED_TASK_STATUSES = {"done", "dropped"}
TODAY_SUPPRESSING_DISPOSITIONS = {"done", "wont_do", "wrong", "snoozed", "deferred", "not_today"}
PRIORITY_RANKS = {"high": 1, "red": 1, "critical": 1, "medium": 2, "orange": 2, "normal": 2, "low": 3, "yellow": 3}
GREG_OWNER_ALIASES = {"", "greg", "greg gage", "gagegreg", "you"}
OPEN_PROJECT_STATES = {"active", "open", "pending"}
CLOSED_PROJECT_STATES = {"closed", "archived", "retired", "merged", "duplicate", "inactive"}
PROJECT_CLOSED_RE = re.compile(
    r"\b(?:closed|archived|retired|merged|duplicate|superseded|obsolete|cancelled|canceled)\b",
    re.IGNORECASE,
)
ACTION_VERBS = {
    "approve",
    "ask",
    "call",
    "check",
    "choose",
    "confirm",
    "decide",
    "draft",
    "email",
    "finish",
    "fix",
    "make",
    "open",
    "pay",
    "prepare",
    "read",
    "reply",
    "review",
    "schedule",
    "send",
    "sign",
    "submit",
    "update",
    "write",
}
NON_ACTION_CONTEXT_RE = re.compile(
    r"\b(?:"
    r"has (?:a |an )?.{0,40}attitude|"
    r"someone else will fix it|"
    r"does not want to be (?:that )?someone else|"
    r"bellyach(?:ing)?|"
    r"vent(?:ing)?|"
    r"complain(?:ing|t)?"
    r")\b",
    re.IGNORECASE,
)
CLEAN_ACTION_PREFIX_RE = re.compile(
    r"^\s*(?:approve|ask|call|check|choose|confirm|decide|draft|email|finish|fix|make|open|pay|prepare|read|reply|review|schedule|send|sign|submit|update|write)\b",
    re.IGNORECASE,
)
MAX_TODAY_RECOMMENDATIONS = 5
MAX_TODAY_ACTION_CHARS = 150
MAX_DASHBOARD_PACKET_AGE_HOURS = 36
PROJECT_SLUG_OVERRIDES = {
    "BYB BizOps": "byb-ops",
    "BYB Business Operations": "byb-ops",
    "BYB Monthly Meeting": "byb-ops",
    "byb-ops": "byb-ops",
    "MIT Book": "mit-book",
    "MIT Press": "mit-book",
    "MIT Press Book": "mit-book",
    "mit-book": "mit-book",
    "mit-press-book": "mit-book",
    "Grass": "grass-foundation",
    "grass": "grass-foundation",
    "Grass Foundation": "grass-foundation",
    "Grass Foundation Program": "grass-foundation",
    "grass-foundation": "grass-foundation",
    "grass-foundation-program": "grass-foundation",
    "NSTA": "nsta-curriculum",
    "nsta": "nsta-curriculum",
    "NSTA Curriculum": "nsta-curriculum",
    "NSTA / Routledge Book": "nsta-curriculum",
    "nsta-routledge-book": "nsta-curriculum",
    "nsta-curriculum": "nsta-curriculum",
}
AGENT_VALID_COLORS = {"orange", "purple", "green", "blue", "yellow", "dark"}
AGENT_VALID_STATUSES = {"healthy", "needs_you", "error"}
AGENT_ID_ALIASES = {"editorial": "cajal"}


def canonical_project_slug(project: str | None) -> str:
    value = str(project or "general").strip()
    return PROJECT_SLUG_OVERRIDES.get(value, re.sub(r"[^a-z0-9]+", "-", value.lower()).strip("-") or "general")


def normalize_project_lifecycle(project: dict[str, Any]) -> str:
    for key in ("lifecycle_state", "lifecycleState", "project_state", "projectState", "state"):
        value = str(project.get(key) or "").strip().lower().replace("-", "_")
        if not value:
            continue
        if value in {"open", "active"}:
            return "open"
        if value in {"closed", "retired", "merged", "duplicate", "inactive"}:
            return "closed"
        if value == "archived":
            return "archived"

    # Compatibility fallback until Memory exposes a first-class lifecycle field.
    priority = str(project.get("priority") or "").strip().lower()
    status = str(project.get("status") or "").strip()
    if priority == "closed" or PROJECT_CLOSED_RE.search(status):
        return "closed"
    return "open"


def filter_projects(
    projects: list[dict[str, Any]],
    *,
    state: str | None = None,
    include_closed: bool = False,
) -> list[dict[str, Any]]:
    requested_state = str(state or "").strip().lower().replace("-", "_")
    if requested_state in {"", "default"}:
        requested_state = "all" if include_closed else "open"
    elif requested_state == "active":
        requested_state = "open"

    filtered: list[dict[str, Any]] = []
    for project in projects:
        lifecycle_state = normalize_project_lifecycle(project)
        row = {**project, "lifecycle_state": lifecycle_state}
        if requested_state in {"all", "any"}:
            filtered.append(row)
        elif requested_state == "open" and lifecycle_state == "open":
            filtered.append(row)
        elif requested_state == "closed" and lifecycle_state in {"closed", "archived"}:
            filtered.append(row)
        elif requested_state == lifecycle_state:
            filtered.append(row)
    return filtered


def _get(path: str, params: dict | None = None) -> Any:
    r = httpx.get(f"{MEMORY_API_URL}{path}", headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, json: dict, params: dict | None = None, timeout: int = 30) -> Any:
    r = httpx.post(f"{MEMORY_API_URL}{path}", headers=HEADERS, json=json, params=params or {}, timeout=timeout)
    r.raise_for_status()
    return r.json()


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _parse_iso(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None


def _format_iso(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def _ms_to_iso(value: Any) -> str | None:
    try:
        timestamp_ms = int(value)
    except (TypeError, ValueError):
        return None
    return _format_iso(datetime.fromtimestamp(timestamp_ms / 1000, timezone.utc))


def _latest_cron_run(job_id: Any) -> dict[str, Any]:
    job_id_text = str(job_id or "").strip()
    if not job_id_text:
        return {}
    path = OPENCLAW_CRON_RUNS_DIR / f"{job_id_text}.jsonl"
    try:
        lines = [line.strip() for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
    except OSError:
        return {}
    if not lines:
        return {}
    try:
        row = json.loads(lines[-1])
    except json.JSONDecodeError:
        return {}
    return {
        "last_finished_at": _ms_to_iso(row.get("ts")),
        "last_run_at": _ms_to_iso(row.get("runAtMs")),
        "next_run_at": _ms_to_iso(row.get("nextRunAtMs")),
        "last_status": row.get("status"),
    }


def get_reconciliation_cadence(agent_id: str = "watson") -> dict[str, Any]:
    try:
        data = json.loads(OPENCLAW_CRON_JOBS_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {
            "source": "OpenClaw cron/jobs.json",
            "agent_id": agent_id,
            "status": "unavailable",
            "error": str(exc),
            "meaning": "Schedule metadata could not be read; freshness_contract is still a conflict-resolution rule, not a cadence.",
        }

    jobs: list[dict[str, Any]] = []
    for job in data.get("jobs", []):
        if not isinstance(job, dict):
            continue
        if str(job.get("agentId") or "").strip() != agent_id or not bool(job.get("enabled")):
            continue
        schedule = job.get("schedule") if isinstance(job.get("schedule"), dict) else {}
        run_state = _latest_cron_run(job.get("id"))
        jobs.append({
            "id": job.get("id"),
            "name": job.get("name"),
            "schedule": schedule,
            "wake_mode": job.get("wakeMode"),
            "delivery_mode": (job.get("delivery") or {}).get("mode") if isinstance(job.get("delivery"), dict) else None,
            **{key: value for key, value in run_state.items() if value is not None},
        })

    return {
        "source": "OpenClaw cron/jobs.json",
        "agent_id": agent_id,
        "status": "configured" if jobs else "no_enabled_jobs",
        "meaning": "Watson scheduled reconciliation cadence. Cortex and Share feedback can still wake agents immediately through OpenClaw; MCP/REST inbox captures are reconciled by these scheduled Watson runs unless separately routed through a direct wakeup.",
        "jobs": jobs,
    }


def normalize_agent_id(agent_id: str | None) -> str:
    raw = str(agent_id or "").strip().lower()
    return AGENT_ID_ALIASES.get(raw, raw)


def normalize_agent_color(color: str | None, fallback: str = "orange") -> str:
    normalized = str(color or fallback).strip().lower()
    if normalized not in AGENT_VALID_COLORS:
        raise ValueError(f"invalid agent color {normalized!r}; expected one of {sorted(AGENT_VALID_COLORS)}")
    return normalized


def publish_agent_profile(
    *,
    agent_id: str,
    display_name: str,
    role: str,
    color: str,
    lifecycle: str = "active",
    updated_at: str | None = None,
    retired_at: str | None = None,
    retired_reason: str | None = None,
    successor_agent_id: str | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "agent_id": normalize_agent_id(agent_id),
        "display_name": display_name,
        "role": role,
        "color": normalize_agent_color(color),
        "lifecycle": lifecycle,
        "updated_at": updated_at or _utc_now_iso(),
    }
    if retired_at:
        payload["retired_at"] = retired_at
    if retired_reason:
        payload["retired_reason"] = retired_reason
    if successor_agent_id:
        payload["successor_agent_id"] = normalize_agent_id(successor_agent_id)
    return _post("/v1/agents", payload)


def publish_agent_dashboard_status(
    *,
    agent_id: str,
    report: dict[str, Any],
    lease_hours: int = 24,
    expires_hours: int = 48,
    source_ref: str | None = None,
    dashboard_version: str | None = None,
) -> dict[str, Any]:
    normalized_agent_id = normalize_agent_id(agent_id)
    status = str(report.get("status") or "healthy").strip().lower()
    if status not in AGENT_VALID_STATUSES:
        status = "error"
    blocks = report.get("blocks")
    if blocks is None:
        blocks = report.get("cards")
    if not isinstance(blocks, list):
        raise ValueError(f"agent dashboard report for {normalized_agent_id} must contain blocks[] or cards[]")
    generated_dt = _parse_iso(report.get("generated_at"))
    generated_at = _format_iso(generated_dt) if generated_dt else _utc_now_iso()
    base_dt = _parse_iso(generated_at) or datetime.now(timezone.utc)
    payload: dict[str, Any] = {
        "agent_id": normalized_agent_id,
        "dashboard_version": dashboard_version or str(report.get("dashboard_version") or f"{normalized_agent_id}:{generated_at}"),
        "generated_at": generated_at,
        "next_expected_at": _format_iso(base_dt + timedelta(hours=lease_hours)),
        "expires_at": _format_iso(base_dt + timedelta(hours=expires_hours)),
        "status": status,
        "needs_you_count": int(report.get("needs_you_count") or 0),
        "blocks": blocks,
    }
    change_note = report.get("change_note")
    if change_note:
        payload["change_note"] = str(change_note)
    if source_ref:
        payload["source_ref"] = source_ref
    return _post("/v1/agents/status", payload)


def normalize_task_status(status: str | None) -> str:
    raw = str(status or "").strip().lower()
    raw = raw.replace("✅", "").replace("—", " ").replace("-", " ")
    raw = " ".join(raw.split())
    if raw in TASK_STATUS_ALIASES:
        return TASK_STATUS_ALIASES[raw]
    if raw.startswith("in progress"):
        return "in_progress"
    if re.search(r"\b(done|complete|completed|closed)\b", raw):
        return "done"
    if re.search(r"\b(superseded|obsolete|stale|dropped|cancelled|canceled)\b", raw):
        return "dropped"
    if re.search(r"\b(deferred|snoozed|not today)\b", raw):
        return "open"
    if re.search(r"\b(blocked|waiting|needs clarification|pending greg input|on hold)\b", raw):
        return "blocked"
    if re.search(r"\b(in progress|doing|reported|review|pending|scheduled|awaiting|overdue|partially addressed)\b", raw):
        return "in_progress"
    if re.search(r"\b(open|to do|todo|active)\b", raw):
        return "open"
    return "open" if not raw else raw.replace(" ", "_")


def normalize_task(task: dict) -> dict:
    normalized = dict(task)
    raw_status = task.get("status")
    status = normalize_task_status(raw_status)
    if raw_status != status:
        normalized["status_raw"] = raw_status
    normalized["status"] = status
    description = str(task.get("description") or "")
    metadata = parse_canonical_task_metadata(description)
    if metadata:
        normalized.update({key: value for key, value in metadata.items() if value not in (None, "")})
        normalized["description"] = strip_canonical_task_metadata(description)
    if not normalized.get("task_key"):
        normalized["task_key"] = stable_task_key(str(normalized.get("partOf") or ""), normalized.get("description"))
    if not normalized.get("project") and normalized.get("partOf"):
        normalized["project"] = str(normalized["partOf"]).replace("byb:project/", "")
    return normalized


def _is_open_task(task: dict) -> bool:
    return normalize_task_status(task.get("status")) not in CLOSED_TASK_STATUSES


def _local_today() -> date:
    return datetime.now(ZoneInfo("America/Detroit")).date()


def _parse_date(value: Any) -> date | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _has_memory_attention_fields(task: dict[str, Any]) -> bool:
    return "attention_state" in task or "attentionState" in task


def memory_attention_suppression(task: dict[str, Any]) -> str | None:
    attention_state = str(task.get("attention_state") or task.get("attentionState") or "").strip().lower()
    if attention_state == "deferred":
        return str(task.get("attention_reason") or task.get("attentionReason") or task.get("blocker_label") or task.get("blockerLabel") or "deferred")
    if attention_state == "snoozed":
        snooze_until = task.get("snooze_until") or task.get("snoozeUntil")
        snooze_date = _parse_date(snooze_until)
        if snooze_date and snooze_date <= _local_today():
            return None
        return f"snoozed until {snooze_until}" if snooze_until else "snoozed"
    if not _has_memory_attention_fields(task) and re.search(r"\bdeferred\b", str(task.get("status_raw") or task.get("status") or ""), re.I):
        return "deferred"
    return None


def _task_disposition(task: dict[str, Any], description: str | None = None) -> dict[str, Any] | None:
    task_key = str(task.get("task_key") or "")
    source_pointer = task.get("source_pointer") or task.get("action_url")
    fingerprint = task.get("source_fingerprint") or source_fingerprint(
        str(source_pointer or ""),
        str(description or task.get("description") or ""),
    )
    return TASK_DISPOSITION_STORE.disposition_for(
        task_key=task_key or None,
        task_id=task.get("id") or task.get("@id"),
        source_fingerprint_value=str(fingerprint or "") or None,
    )


def task_today_suppression(task: dict[str, Any], description: str | None = None) -> str | None:
    if _has_memory_attention_fields(task):
        return memory_attention_suppression(task)
    memory_suppression = memory_attention_suppression(task)
    if memory_suppression:
        return memory_suppression
    disposition = _task_disposition(task, description)
    if disposition and disposition.get("disposition") in TODAY_SUPPRESSING_DISPOSITIONS:
        label = str(disposition.get("disposition") or "").replace("_", " ")
        metadata = disposition.get("metadata") if isinstance(disposition.get("metadata"), dict) else {}
        snooze_until = metadata.get("snooze_until_date")
        if disposition.get("disposition") == "snoozed" and snooze_until:
            try:
                until_date = datetime.fromisoformat(str(snooze_until)[:10]).date()
                today_et = datetime.now(ZoneInfo("America/Detroit")).date()
                if until_date <= today_et:
                    return None
            except ValueError:
                pass
            return f"snoozed until {snooze_until}"
        return label
    return None


def _apply_task_disposition_overlay(task: dict[str, Any]) -> dict[str, Any]:
    if _has_memory_attention_fields(task):
        suppression = memory_attention_suppression(task)
        if not suppression:
            return task
        updated = dict(task)
        updated["status_raw"] = updated.get("status_raw", updated.get("status"))
        updated["status"] = str(updated.get("attention_state") or updated.get("attentionState"))
        updated["disposition_reason"] = updated.get("attention_reason") or updated.get("attentionReason")
        return updated
    legacy_suppression = memory_attention_suppression(task)
    if legacy_suppression:
        updated = dict(task)
        updated["status_raw"] = updated.get("status_raw", updated.get("status"))
        updated["status"] = "deferred"
        updated["disposition_reason"] = legacy_suppression
        return updated
    description = strip_canonical_task_metadata(str(task.get("description") or ""))
    disposition = _task_disposition(task, description)
    if not disposition or disposition.get("disposition") not in {"snoozed", "deferred", "not_today"}:
        return task
    suppression = task_today_suppression(task, description)
    if not suppression:
        return task
    metadata = disposition.get("metadata") if isinstance(disposition.get("metadata"), dict) else {}
    updated = dict(task)
    updated["status_raw"] = updated.get("status_raw", updated.get("status"))
    updated["status"] = str(disposition.get("disposition"))
    updated["disposition_reason"] = disposition.get("reason")
    if metadata.get("snooze_until_date"):
        updated["snooze_until"] = metadata.get("snooze_until_date")
    return updated


def strip_canonical_task_metadata(description: str) -> str:
    lines = []
    for line in str(description or "").splitlines():
        if TASK_KEY_RE.match(line) or TASK_META_RE.match(line):
            continue
        lines.append(line)
    return "\n".join(lines).strip()


def parse_canonical_task_metadata(description: str) -> dict[str, Any]:
    text = str(description or "")
    metadata: dict[str, Any] = {}
    key_match = TASK_KEY_RE.search(text)
    if key_match:
        metadata["task_key"] = key_match.group("key").strip()
    for match in TASK_META_RE.finditer(text):
        label = match.group(1).lower().replace(" ", "_")
        value = match.group(2).strip()
        if label == "source_pointer":
            metadata["source_pointer"] = value
        elif label == "source_capture":
            metadata["source_capture_id"] = value
        elif label == "source_fingerprint":
            metadata["source_fingerprint"] = value
        else:
            metadata[label] = value
    return metadata


def canonical_task_description(
    *,
    description: str,
    task_key: str,
    owner: str | None = None,
    priority: str | None = None,
    source_pointer: str | None = None,
    source_capture_id: Any = None,
    source_fingerprint_value: str | None = None,
) -> str:
    lines = [strip_canonical_task_metadata(description)]
    lines.append("")
    lines.append(f"[watson-task-key: {task_key}]")
    if owner:
        lines.append(f"Owner: {owner}")
    if priority:
        lines.append(f"Priority: {priority}")
    if source_pointer:
        lines.append(f"Source pointer: {source_pointer}")
    if source_capture_id not in (None, ""):
        lines.append(f"Source capture: {source_capture_id}")
    if source_fingerprint_value:
        lines.append(f"Source fingerprint: {source_fingerprint_value}")
    return "\n".join(lines).strip()


def task_sort_key(task: dict[str, Any]) -> tuple[int, str, str]:
    priority = str(task.get("priority") or "").lower()
    rank = PRIORITY_RANKS.get(priority, 9)
    return (rank, str(task.get("created_at") or ""), str(task.get("description") or ""))


def owner_is_greg_action(owner: Any) -> bool:
    return str(owner or "").strip().lower() in GREG_OWNER_ALIASES


def task_action_text(description: str) -> str:
    clean_description = strip_canonical_task_metadata(description)
    greg_action = re.search(r"\bGreg\s+(?:should|needs to|must)\s+[^.!?]+[.!?]?", clean_description, re.I)
    if greg_action:
        action = greg_action.group(0).strip()
        return action[: MAX_TODAY_ACTION_CHARS - 1].rstrip(" ,;:-") + "…" if len(action) > MAX_TODAY_ACTION_CHARS else action
    for line in clean_description.splitlines():
        action = line.strip()
        if action:
            break
    else:
        return ""
    action = re.sub(r"^(?:Task|Action):\s*", "", action, flags=re.I).strip()
    if len(action) <= MAX_TODAY_ACTION_CHARS:
        return action
    sentence = re.split(r"(?<=[.!?])\s+", action, maxsplit=1)[0].strip()
    if 20 <= len(sentence) <= MAX_TODAY_ACTION_CHARS:
        return sentence
    return action[: MAX_TODAY_ACTION_CHARS - 1].rstrip(" ,;:-") + "…"


def task_quality_issues(action: str) -> list[str]:
    text = " ".join(str(action or "").split())
    lowered = text.lower()
    issues: list[str] = []
    if not text:
        issues.append("missing action")
        return issues
    if len(text) > MAX_TODAY_ACTION_CHARS:
        issues.append("too long for Today")
    sentence_count = len([part for part in re.split(r"(?<=[.!?])\s+", text) if part.strip()])
    if sentence_count > 1:
        issues.append("multiple sentences")
    connector_count = len(re.findall(r"\b(?:and|also|then|plus|or)\b", lowered))
    if connector_count >= 3 or text.count(";") >= 2:
        issues.append("likely composite task")
    if NON_ACTION_CONTEXT_RE.search(text):
        issues.append("context/vent, not action")
    if not any(re.search(rf"\b{re.escape(verb)}\b", lowered) for verb in ACTION_VERBS):
        issues.append("no clear action verb")
    return issues


def task_is_today_candidate(task: dict[str, Any], action: str) -> bool:
    if task_today_suppression(task):
        return False
    if not owner_is_greg_action(task.get("owner")):
        return False
    description = strip_canonical_task_metadata(str(task.get("description") or ""))
    first_sentence = re.split(r"(?<=[.!?])\s+", description, maxsplit=1)[0]
    if NON_ACTION_CONTEXT_RE.search(first_sentence) and not CLEAN_ACTION_PREFIX_RE.search(action):
        return False
    issues = task_quality_issues(action)
    severe = {"missing action", "too long for Today", "multiple sentences", "likely composite task", "context/vent, not action"}
    return not (severe & set(issues))


def _due_rank(task: dict[str, Any]) -> int:
    due = task.get("dueDate") or task.get("due_date") or task.get("due")
    if not due:
        return 20
    text = str(due).strip().lower()
    if text in {"today", "today_or_next_sync", "now"}:
        return -35
    if text == "tomorrow":
        return -15
    try:
        due_date = datetime.fromisoformat(text[:10]).date()
    except ValueError:
        return 10
    today = datetime.now(timezone.utc).date()
    days = (due_date - today).days
    if days <= 0:
        return -35
    if days == 1:
        return -15
    return min(60, days * 8)


def _surface_rank(task: dict[str, Any]) -> int:
    state = str(task.get("state") or "").strip().lower()
    if state in {"ready", "decision", "document"}:
        return -18
    if task.get("draft_text") or task.get("draftText") or task.get("decision_question") or task.get("document_url"):
        return -16
    if task.get("source_pointer") or task.get("action_url"):
        return -8
    return 0


def today_priority_score(task: dict[str, Any], action: str) -> tuple[int, str, str]:
    priority = str(task.get("priority") or "").strip().lower()
    rank = PRIORITY_RANKS.get(priority, 4)
    score = rank * 100 + _due_rank(task) + _surface_rank(task)
    if task_quality_issues(action):
        score += 12
    return (score, str(task.get("dueDate") or task.get("due_date") or ""), str(task.get("created_at") or ""))


def today_rank_reason(task: dict[str, Any], action: str) -> str:
    parts = [f"priority={task.get('priority') or 'unknown'}"]
    if task.get("dueDate") or task.get("due_date") or task.get("due"):
        parts.append(f"due={task.get('dueDate') or task.get('due_date') or task.get('due')}")
    if _surface_rank(task) < 0:
        parts.append("has prepared surface/source")
    issues = task_quality_issues(action)
    if issues:
        parts.append(f"quality caveat: {', '.join(issues)}")
    return "; ".join(parts)


def search_captures(q: str = "", limit: int = 10, include_quarantined: bool = False) -> list[dict]:
    requested = max(1, min(int(limit), 200))
    overfetch = min(200, max(requested, requested * 4))
    captures = _get("/v1/search", {"q": q, "limit": overfetch})
    if not isinstance(captures, list):
        return []
    visible = QUALITY_STORE.filter_visible(captures, include_quarantined=include_quarantined)
    return visible[:requested]


def get_memory_context(q: str = '', project_slug: str | None = None, client: str = 'watson') -> dict:
    params={'q':q,'client':client}
    if project_slug:params['project_slug']=project_slug
    return _get('/v1/context',params)


def record_memory_feedback(context_id: str,outcome: str,capture_ids: list[int]) -> dict:
    return _post('/v1/feedback',dict(context_id=context_id,outcome=outcome,capture_ids=capture_ids))


def _collect_audit_captures(limit: int = 200, queries: list[str] | None = None) -> list[dict]:
    seen: set[str] = set()
    captures: list[dict] = []
    for q in ["", *(queries or list(DEFAULT_JUNK_QUERIES))]:
        try:
            rows = _get("/v1/search", {"q": q, "limit": max(1, min(int(limit), 200))})
        except Exception:
            continue
        if not isinstance(rows, list):
            continue
        for row in rows:
            key = capture_key(row)
            if key in seen:
                continue
            seen.add(key)
            captures.append(row)
    return captures


def audit_memory_captures(limit: int = 200, apply_quarantine: bool = False, queries: list[str] | None = None) -> dict:
    captures = _collect_audit_captures(limit=limit, queries=queries)
    report = audit_captures(captures)
    quarantined: list[dict[str, Any]] = []
    new_matches: list[dict[str, Any]] = []
    already_hidden: list[dict[str, Any]] = []
    by_key = {capture_key(capture): capture for capture in captures}
    provenance_refreshed = 0
    for match in report["matches"]:
        capture = by_key.get(match["capture_key"])
        if not capture:
            continue
        status = QUALITY_STORE.status_for(capture)
        if QUALITY_STORE.refresh_provenance(capture):
            provenance_refreshed += 1
        match_with_status = {**match, "quality_status": status}
        if status in {"quarantined", "deleted", "superseded"}:
            already_hidden.append(match_with_status)
        else:
            new_matches.append(match_with_status)
    if apply_quarantine:
        for match in new_matches:
            capture = by_key.get(match["capture_key"])
            if not capture:
                continue
            reason = classify_capture(capture)
            if not reason:
                continue
            quarantined.append(
                QUALITY_STORE.set_status(
                    capture,
                    "quarantined",
                    reason=reason,
                    metadata={"audit": "watson-retrofit", "dry_run": False},
                )
            )
    report["apply_quarantine"] = apply_quarantine
    report["new_quarantine_candidates"] = len(new_matches)
    report["already_hidden_matches"] = len(already_hidden)
    report["new_matches"] = new_matches
    report["quarantined"] = quarantined
    report["quality_db"] = str(QUALITY_DB_PATH)
    report["provenance_refreshed"] = provenance_refreshed
    return report


def list_capture_quality(status: str | None = None, limit: int = 100) -> list[dict]:
    return QUALITY_STORE.list_records(status=status, limit=limit)


def _project_status_overrides() -> dict[str, dict[str, str]]:
    """Latest Memory captures can correct stale project rows until project PATCH exists."""
    try:
        captures = search_captures("project-status-current", limit=50)
    except Exception:
        return {}

    overrides: dict[str, dict[str, str]] = {}
    for capture in captures:
        raw = str(capture.get("raw_content") or "")
        match = PROJECT_STATUS_RE.search(raw)
        if not match:
            continue
        slug = match.group("slug")
        created_at = str(capture.get("created_at") or "")
        existing = overrides.get(slug)
        if existing and existing.get("created_at", "") > created_at:
            continue
        overrides[slug] = {
            "status": match.group("status"),
            "priority": match.group("priority"),
            "created_at": created_at,
        }
    return overrides


def _current_recommendations() -> list[dict[str, Any]]:
    try:
        # Empty q returns recent captures; filter exact tags locally so semantic
        # ranking cannot drop one of the current priority records.
        captures = search_captures("", limit=75)
    except Exception:
        return []

    recommendations: dict[int, dict[str, Any]] = {}
    for capture in captures:
        raw = str(capture.get("raw_content") or "")
        match = WHAT_NOW_RE.search(raw)
        if not match:
            continue
        rank = int(match.group("rank"))
        created_at = str(capture.get("created_at") or "")
        existing = recommendations.get(rank)
        if existing and existing.get("created_at", "") > created_at:
            continue
        action = match.group("action")
        project = match.group("project")
        task_id = match.groupdict().get("id")
        task_key = stable_task_key(project, task_id or action)
        if TASK_DISPOSITION_STORE.is_suppressed(task_key=task_key, task_id=task_id):
            continue
        recommendations[rank] = {
            "rank": rank,
            "project": project,
            "action": action,
            "effort": match.group("effort"),
            "reason": match.group("reason"),
            "created_at": created_at,
        }
    return [recommendations[rank] for rank in sorted(recommendations)]


def _current_tasks(limit: int = 50) -> list[dict[str, Any]]:
    try:
        seen: set[str] = set()
        captures = []
        for query in ("task-current", ""):
            for capture in search_captures(query, limit=200):
                key = capture_key(capture)
                if key in seen:
                    continue
                seen.add(key)
                captures.append(capture)
    except Exception:
        return []

    tasks: dict[str, dict[str, Any]] = {}
    for capture in captures:
        raw = str(capture.get("raw_content") or "")
        match = TASK_CURRENT_RE.search(raw)
        if not match:
            continue
        status = normalize_task_status(match.group("status"))
        if status in CLOSED_TASK_STATUSES:
            continue
        body_match = TASK_BODY_RE.search(raw)
        description = body_match.group("task").strip() if body_match else match.group("project")
        project = match.group("project")
        project_slug = canonical_project_slug(project)
        task_id = match.group("id") or stable_task_key(project_slug, description)
        source_pointer_value = match.group("source_pointer") or None
        key = stable_task_key(project_slug, task_id)
        fingerprint = source_fingerprint(source_pointer_value, description)
        if TASK_DISPOSITION_STORE.is_suppressed(task_key=key, source_fingerprint_value=fingerprint):
            continue
        created_at = str(capture.get("created_at") or "")
        existing = tasks.get(task_id)
        if existing and existing.get("created_at", "") > created_at:
            continue
        tasks[task_id] = {
            "@id": f"watson:task-current/{task_id}",
            "description": description,
            "status": status,
            "status_raw": match.group("status") if match.group("status") != status else None,
            "dueDate": match.group("due") or None,
            "partOf": f"byb:project/{project_slug}",
            "project": project,
            "owner": match.group("owner") or None,
            "priority": match.group("priority") or None,
            "source_pointer": match.group("source_pointer") or None,
            "source_capture_id": capture.get("id") or capture.get("capture_id"),
            "source_fingerprint": fingerprint,
            "task_key": key,
            "created_at": created_at,
            "source": "task-current capture",
        }
    return sorted(tasks.values(), key=task_sort_key)[:limit]


def get_projects(state: str | None = None, include_closed: bool = False) -> list[dict]:
    params: dict[str, Any] = {}
    if state:
        params["state"] = state
    if include_closed:
        params["include_closed"] = "true"
    raw = _get("/v1/projects", params)
    overrides = _project_status_overrides()
    projects = []
    for p in raw:
        if not isinstance(p, dict) or not p.get("slug"):
            continue
        override = overrides.get(str(p["slug"]), {})
        projects.append({
            "@id": f"byb:project/{p['slug']}",
            "id": p.get("id"),
            "slug": p["slug"],
            "title": p.get("title") or p["slug"],
            "status": override.get("status", p.get("status") or ""),
            "priority": override.get("priority", p.get("priority") or ""),
            "status_source": "project-status-current capture" if override else "project row",
        })
    return filter_projects(projects, state=state, include_closed=include_closed)


def _task_limit(limit: int, include_complete: bool) -> int:
    # The API can return closed/superseded tasks even when include_complete=false.
    # Over-fetch so local filtering still returns enough current tasks.
    return min(200, max(limit, limit * 4)) if not include_complete else limit


def _memory_tasks(params: dict | None = None) -> list[dict[str, Any]]:
    rows = _get("/v1/tasks", params or {})
    if not isinstance(rows, list):
        return []
    return [normalize_task(task) for task in rows if isinstance(task, dict)]


def _task_table_empty() -> bool:
    return len(_memory_tasks({"include_complete": True, "limit": 1})) == 0


def _apply_task_filters(
    tasks: list[dict[str, Any]],
    project: str | None = None,
    status: str | None = None,
    include_complete: bool = True,
) -> list[dict[str, Any]]:
    filtered = tasks
    if project:
        slug = project.replace("byb:project/", "")
        filtered = [task for task in filtered if str(task.get("partOf", "")).endswith(slug)]
    if not include_complete:
        filtered = [task for task in filtered if _is_open_task(task)]
    if status:
        status_norm = normalize_task_status(status)
        filtered = [task for task in filtered if normalize_task_status(task.get("status")) == status_norm]
    return filtered


def get_tasks(project: str | None = None, status: str | None = None, include_complete: bool = True, limit: int = 50) -> list[dict]:
    table_probe = _memory_tasks({"include_complete": True, "limit": 1})
    table_has_rows = bool(table_probe)
    params: dict = {"include_complete": include_complete, "limit": _task_limit(limit, include_complete)}
    if project:
        params["project_slug"] = project.replace("byb:project/", "")
    if status:
        params["status"] = normalize_task_status(status)
    elif not include_complete:
        params["status"] = "open"
    if table_has_rows:
        filtered = _apply_task_filters(_memory_tasks(params), project=project, status=status, include_complete=include_complete)
        return [_apply_task_disposition_overlay(task) for task in filtered[:limit]]

    current = _current_tasks(limit=limit)
    filtered = _apply_task_filters(current, project=project, status=status, include_complete=include_complete)
    return [_apply_task_disposition_overlay(task) for task in filtered[:limit]]


def get_events(from_date: str | None = None, to_date: str | None = None, limit: int = 50) -> list[dict]:
    params: dict = {"limit": limit}
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    return _get("/v1/events", params)


def _agent_status_rows(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        for key in ("statuses", "snapshots", "agents"):
            rows = payload.get(key)
            if isinstance(rows, list):
                return [row for row in rows if isinstance(row, dict)]
        return [payload]
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def latest_watson_dashboard_snapshot() -> dict[str, Any] | None:
    try:
        payload = _get("/v1/agents/status/latest")
    except Exception:
        return None
    snapshots = [
        row
        for row in _agent_status_rows(payload)
        if str(row.get("agent_id") or row.get("agentId") or row.get("id") or "").strip().lower() == "watson"
    ]
    if not snapshots:
        return None
    return max(snapshots, key=lambda row: str(row.get("generated_at") or row.get("generatedAt") or row.get("updated_at") or ""))


def dashboard_snapshot_is_fresh(snapshot: dict[str, Any] | None) -> bool:
    if not snapshot:
        return False
    now = datetime.now(timezone.utc)
    expires_at = _parse_iso(snapshot.get("expires_at") or snapshot.get("expiresAt"))
    if expires_at and expires_at >= now:
        return True
    generated_at = _parse_iso(snapshot.get("generated_at") or snapshot.get("generatedAt") or snapshot.get("updated_at"))
    if not generated_at:
        return False
    return now - generated_at <= timedelta(hours=MAX_DASHBOARD_PACKET_AGE_HOURS)


def dashboard_packet_identity(snapshot: dict[str, Any] | None) -> dict[str, Any] | None:
    if not snapshot:
        return None
    return {
        "agent_id": snapshot.get("agent_id") or snapshot.get("agentId") or snapshot.get("id"),
        "dashboard_version": snapshot.get("dashboard_version") or snapshot.get("dashboardVersion"),
        "generated_at": snapshot.get("generated_at") or snapshot.get("generatedAt") or snapshot.get("updated_at"),
        "expires_at": snapshot.get("expires_at") or snapshot.get("expiresAt"),
        "source_ref": snapshot.get("source_ref") or snapshot.get("sourceRef"),
    }


def _string_field(row: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = row.get(key)
        if value not in (None, ""):
            return str(value)
    return ""


def _dashboard_blocks(snapshot: dict[str, Any] | None) -> list[dict[str, Any]]:
    blocks = (snapshot or {}).get("blocks") or (snapshot or {}).get("cards")
    return [block for block in blocks if isinstance(block, dict)] if isinstance(blocks, list) else []


NO_TODAY_ACTION_RE = re.compile(
    r"\b(?:no\s+(?:greg\s+)?action|no\s+action|fyi|for\s+context|monitor\s+only|wait\s+for|waiting\s+on)\b",
    re.IGNORECASE,
)


def _dashboard_action_text(item: dict[str, Any]) -> str:
    text = _string_field(item, "label", "action", "title", "text")
    return re.sub(r"\s+\(Task\s+\d+\)\s*$", "", text, flags=re.IGNORECASE).strip()


def _dashboard_action_is_today_item(item: dict[str, Any], block_title: str) -> bool:
    label = _dashboard_action_text(item)
    detail = _string_field(item, "detail", "description", "evidence")
    kind = str(item.get("kind") or "act").strip().lower()
    priority = str(item.get("priority") or "").strip().lower()
    title = block_title.lower()
    if not label:
        return False
    if NO_TODAY_ACTION_RE.search(label) or (NO_TODAY_ACTION_RE.search(detail) and not re.search(r"\b(?:send|check|reply|decide|choose|approve)\b", label, re.I)):
        return False
    if kind not in {"act", "action", "decision"}:
        return False
    return priority in {"red", "high", "critical"} or "red" in title or "top 3" in title


def _task_id_key(value: Any) -> str:
    return re.sub(r"\D+", "", str(value or "")) or str(value or "")


def _tasks_by_id(tasks: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    by_id: dict[str, dict[str, Any]] = {}
    for task in tasks:
        for key in (task.get("id"), task.get("@id"), task.get("task_id")):
            normalized = _task_id_key(key)
            if normalized:
                by_id[normalized] = task
    return by_id


def _dashboard_item_suppression(item: dict[str, Any], tasks_by_id: dict[str, dict[str, Any]]) -> str | None:
    task_id = item.get("task_id") or item.get("taskId")
    task = tasks_by_id.get(_task_id_key(task_id))
    if task:
        suppression = task_today_suppression(task)
        if suppression:
            return suppression
    label = _dashboard_action_text(item)
    project = canonical_project_slug(_string_field(item, "project") or "general")
    task_key = stable_task_key(project, str(task_id or label))
    fingerprint = source_fingerprint(_string_field(item, "action_url", "source_pointer", "url"), label)
    if TASK_DISPOSITION_STORE.is_suppressed(
        task_key=task_key,
        task_id=task_id,
        source_fingerprint_value=fingerprint,
    ):
        return "task disposition"
    return None


def dashboard_action_recommendations(
    snapshot: dict[str, Any] | None,
    tasks: list[dict[str, Any]],
    limit: int = MAX_TODAY_RECOMMENDATIONS,
) -> list[dict[str, Any]]:
    if not dashboard_snapshot_is_fresh(snapshot):
        return []
    task_lookup = _tasks_by_id(tasks)
    rows: list[dict[str, Any]] = []
    blocks = sorted(
        [block for block in _dashboard_blocks(snapshot) if block.get("type") == "actions"],
        key=lambda block: (0 if re.search(r"\b(?:top\s*3|red)\b", str(block.get("title") or ""), re.I) else 1),
    )
    for block in blocks:
        title = str(block.get("title") or "")
        items = block.get("items")
        if not isinstance(items, list):
            continue
        for item in [candidate for candidate in items if isinstance(candidate, dict)]:
            if not _dashboard_action_is_today_item(item, title):
                continue
            suppression = _dashboard_item_suppression(item, task_lookup)
            if suppression:
                continue
            action = _dashboard_action_text(item)
            task_id = item.get("task_id") or item.get("taskId")
            project = canonical_project_slug(_string_field(item, "project") or "general")
            source_pointer = _string_field(item, "action_url", "source_pointer", "url") or None
            rows.append({
                "rank": len(rows) + 1,
                "id": f"watson-dashboard:{snapshot.get('dashboard_version') or snapshot.get('generated_at') or 'latest'}:{task_id or len(rows) + 1}",
                "task_id": task_id,
                "task_key": stable_task_key(project, str(task_id or action)),
                "project": project,
                "action": action,
                "description": _string_field(item, "detail", "description", "evidence") or action,
                "priority": item.get("priority") or "red",
                "owner": "greg",
                "source_pointer": source_pointer,
                "action_url": source_pointer,
                "rank_reason": _string_field(item, "evidence", "detail"),
                "reason": "Watson evidence-first dashboard packet",
                "effort": _string_field(item, "effort") or None,
                "evidence": _string_field(item, "evidence") or None,
                "dashboard_block": title,
            })
            if len(rows) >= limit:
                return rows
    return rows


def _compact_list(items: list[str]) -> str:
    clean = [item for item in items if item]
    if not clean:
        return ""
    if len(clean) == 1:
        return clean[0]
    if len(clean) == 2:
        return f"{clean[0]} and {clean[1]}"
    return f"{', '.join(clean[:-1])}, and {clean[-1]}"


def _short_action_name(action: str) -> str:
    text = re.sub(r"\s+\(Task\s+\d+\)\s*$", "", action).strip()
    text = re.split(r"\s+[—-]\s+", text, maxsplit=1)[0].strip()
    return text[:80].rstrip(" ,;:")


def dashboard_packet_summary(snapshot: dict[str, Any] | None, recommendations: list[dict[str, Any]]) -> str:
    names = [_short_action_name(str(row.get("action") or "")) for row in recommendations[:3]]
    if names:
        return f"{_compact_list(names)} are the current Red actions; orange and waiting items stay in project context."
    for block in _dashboard_blocks(snapshot):
        if block.get("type") != "text":
            continue
        for key in ("body", "note", "summary", "text"):
            text = str(block.get(key) or "").strip()
            if text:
                return text
    return "Watson did not publish a current Today narrative."


def dashboard_packet_headline(snapshot: dict[str, Any] | None, recommendations: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not snapshot and not recommendations:
        return None
    count = len(recommendations)
    if count:
        noun = "action" if count == 1 else "actions"
        verb = "needs" if count == 1 else "need"
        title = f"{count} Red {noun} {verb} Greg today"
        subline = dashboard_packet_summary(snapshot, recommendations)
    else:
        title = "No Red Greg action in the current Watson packet"
        subline = dashboard_packet_summary(snapshot, recommendations)
    return {
        "title": title,
        "subline": subline,
        "summary": subline,
        "source": "Watson evidence-first dashboard packet",
        **(dashboard_packet_identity(snapshot) or {}),
    }


def get_summary() -> dict:
    projects = get_projects()
    tasks = get_tasks(include_complete=False, limit=15)
    today = datetime.now().strftime("%Y-%m-%d")
    events = get_events(from_date=today, limit=10)
    dashboard_snapshot = latest_watson_dashboard_snapshot()
    dashboard_recommendations = dashboard_action_recommendations(dashboard_snapshot, tasks)
    recommended_now = dashboard_recommendations or task_recommendations(tasks) or (_current_recommendations() if _task_table_empty() else [])
    headline = dashboard_packet_headline(dashboard_snapshot, dashboard_recommendations)

    return {
        "source_of_truth": "Fresh Watson evidence-first dashboard packet first; canonical Memory Task rows are fallback; task-current captures are fallback only when the Task table is empty. All paths honor Memory attention state and Watson task dispositions.",
        "freshness_contract": "Conflict-resolution rule, not a schedule: if this summary conflicts with live sent mail, inbox, calendar, or direct user feedback, live reconciliation wins.",
        "reconciliation_cadence": get_reconciliation_cadence(),
        "headline": headline,
        "summary": headline.get("summary") if headline else None,
        "today_packet": dashboard_packet_identity(dashboard_snapshot),
        "recommended_now": recommended_now,
        "projects": [{"id": p["@id"], "title": p["title"], "status": p["status"], "priority": p["priority"]} for p in projects],
        "open_tasks": [
            {"id": t.get("@id"), "description": t.get("description"), "status": t.get("status"), "dueDate": t.get("dueDate"), "partOf": t.get("partOf")}
            for t in tasks
        ],
        "upcoming_events": [{"id": e.get("@id"), "label": e.get("label"), "date": e.get("date"), "dateTime": e.get("dateTime")} for e in events],
    }


def task_recommendations(tasks: list[dict[str, Any]], limit: int = MAX_TODAY_RECOMMENDATIONS) -> list[dict[str, Any]]:
    candidates: list[tuple[tuple[int, str, str], dict[str, Any], str, str]] = []
    for task in tasks:
        description = strip_canonical_task_metadata(str(task.get("description") or ""))
        action = task_action_text(description)
        if not task_is_today_candidate(task, action):
            continue
        candidates.append((today_priority_score(task, action), task, action, description))

    recommendations = []
    for idx, (_score, task, action, description) in enumerate(sorted(candidates, key=lambda item: item[0])[:limit], start=1):
        recommendations.append({
            "rank": idx,
            "id": task.get("@id") or task.get("id"),
            "task_id": task.get("id"),
            "task_key": task.get("task_key"),
            "project": task.get("project") or str(task.get("partOf") or "").replace("byb:project/", ""),
            "action": action,
            "description": description,
            "priority": task.get("priority"),
            "owner": task.get("owner"),
            "source_pointer": task.get("source_pointer"),
            "rank_reason": today_rank_reason(task, action),
            "reason": "canonical Task row",
        })
    return recommendations


def post_capture(content: str, source: str = "watson", project: str | None = None, sync: bool = True) -> dict:
    raw = content
    if project:
        raw = f"[project: {project}] {raw}"
    junk_reason = classify_capture({"raw_content": raw, "source": source})
    if junk_reason == "watson-sync-wrapper":
        return {
            "ok": False,
            "skipped": True,
            "reason": junk_reason,
            "message": "Skipped Watson sync wrapper. Memory captures must be concrete who/what/why facts, not run summaries.",
        }
    return _post(
        "/v1/captures",
        {"raw_content": raw, "source": source},
        params={"sync": "true"} if sync else None,
        timeout=60 if sync else 30,
    )


def get_links(project: str | None = None) -> dict:
    params = {}
    if project:
        params["project_slug"] = project.replace("byb:project/", "")
    return _get("/v1/links", params)


def set_task_status(task_id: int, status: str) -> dict:
    status_norm = normalize_task_status(status)
    if status_norm not in {"open", "in_progress", "blocked", "done", "dropped"}:
        raise ValueError("status must normalize to one of: open, in_progress, blocked, done, dropped")
    r = httpx.patch(
        f"{MEMORY_API_URL}/v1/tasks/{task_id}",
        headers=HEADERS,
        json={"status": status_norm},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def set_task_attention(
    task_id: int,
    attention_state: str,
    *,
    reason: str | None = None,
    snooze_until: str | None = None,
    blocker_type: str | None = None,
    blocker_label: str | None = None,
    blocker_capture_id: int | None = None,
) -> dict:
    state = str(attention_state or "").strip().lower().replace("-", "_")
    if state in {"delay", "delayed", "later"}:
        state = "snoozed" if snooze_until else "deferred"
    if state == "not_today":
        state = "snoozed"
    if state not in {"active", "snoozed", "deferred"}:
        raise ValueError("attention_state must be active, snoozed, or deferred")
    payload: dict[str, Any] = {"attention_state": state}
    if reason is not None:
        payload["attention_reason"] = reason
    if snooze_until is not None:
        payload["snooze_until"] = snooze_until
    if blocker_type is not None:
        payload["blocker_type"] = blocker_type
    if blocker_label is not None:
        payload["blocker_label"] = blocker_label
    if blocker_capture_id is not None:
        payload["blocker_capture_id"] = blocker_capture_id
    r = httpx.patch(
        f"{MEMORY_API_URL}/v1/tasks/{task_id}",
        headers=HEADERS,
        json=payload,
        timeout=30,
    )
    r.raise_for_status()
    return r.json()


def create_task(description: str, status: str = "open", project_id: int | None = None, due_date: str | None = None) -> dict:
    body: dict[str, Any] = {
        "description": description,
        "status": normalize_task_status(status),
        "project_id": project_id,
        "due_date": due_date,
    }
    return _post("/v1/tasks", body)


def record_task_disposition(
    *,
    task: dict[str, Any],
    disposition: str,
    reason: str | None = None,
    actor: str = "Greg",
    source: str = "dashboard",
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    normalized = normalize_task(task)
    disposition_norm = str(disposition or "").strip().lower().replace("-", "_")
    task_id = normalized.get("id")
    if task_id is not None:
        if disposition_norm in {"done", "wont_do", "wrong"}:
            status = "done" if disposition_norm == "done" else "dropped"
            set_task_status(int(task_id), status)
        elif disposition_norm in {"snoozed", "deferred", "not_today"}:
            disposition_metadata = metadata or {}
            snooze_until = disposition_metadata.get("snooze_until_date") or disposition_metadata.get("snooze_until")
            set_task_attention(
                int(task_id),
                "snoozed" if disposition_norm == "not_today" else disposition_norm,
                reason=reason,
                snooze_until=str(snooze_until) if snooze_until else None,
                blocker_type=disposition_metadata.get("blocker_type"),
                blocker_label=disposition_metadata.get("blocker_label") or disposition_metadata.get("waiting_on"),
                blocker_capture_id=disposition_metadata.get("blocker_capture_id")
                or disposition_metadata.get("evidence_capture_id"),
            )
    task_key = str(normalized.get("task_key") or stable_task_key(normalized.get("partOf"), normalized.get("description")))
    return TASK_DISPOSITION_STORE.record(
        task_key=task_key,
        task_id=task_id,
        disposition=disposition,
        reason=reason,
        actor=actor,
        source=source,
        project=normalized.get("partOf") or normalized.get("project"),
        description=normalized.get("description"),
        source_pointer=normalized.get("source_pointer"),
        source_fingerprint_value=normalized.get("source_fingerprint"),
        metadata={"memory_task": normalized.get("@id"), **(metadata or {})},
    )
