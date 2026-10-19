"""
Watson API — ontology read + inbox write for LLM access.
Base: https://watson.backyardbrains.com
Auth: REST: ?secret= ; MCP: pass privateKey as tool param, or Authorization: Bearer <API_SECRET> / X-API-Key.

When USE_MEMORY_API=true, ontology and inbox come from memory.backyardbrains.com.
Links stay in JSON-LD (memory-api does not store links yet).
"""
import contextvars
import hashlib
import html
import json
import os
import re
import urllib.parse
import urllib.request
import subprocess
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.responses import FileResponse
from fastapi.security import APIKeyQuery
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

from background_queue import build_queue
from watson_ideas import WatsonIdeaStore

# OpenAI/ChatGPT require tool annotations to trust tools without prompting.
# Global read + non-destructive so ChatGPT can run without security confirmation dialogs.
READ_ONLY = ToolAnnotations(
    readOnlyHint=True,
    openWorldHint=True,
    destructiveHint=False,
)
WRITE_APPEND = ToolAnnotations(
    readOnlyHint=False,
    destructiveHint=False,
    openWorldHint=False,
)
# Legacy: x-openai-isConsequential equivalent for MCP (reduces permission prompts)
META_SAFE = {"openai/isConsequential": False}

# DATA_PATH: env DATA_PATH, or parent of openclaw repo when running locally
DATA_PATH = Path(os.environ.get("DATA_PATH", Path(__file__).resolve().parent.parent.parent))
ONTOLOGY = DATA_PATH / "workspaces" / "main" / "memory" / "ontology" / "entities"
MEMORY = DATA_PATH / "workspaces" / "main" / "memory"
WATSON_INBOX_WAKE_ACTION_LOG = Path(os.environ.get("WATSON_INBOX_WAKE_ACTION_LOG", DATA_PATH / "memory" / "watson-inbox-wake-actions.jsonl"))
EXPORT_DIR = Path("/data/config/exports").resolve()

EXPECTED_SECRET = lambda: os.environ.get("API_SECRET") or os.environ.get("BACKYARDBRAINS_API_KEY")
USE_MEMORY_API = os.environ.get("USE_MEMORY_API", "true").lower() in ("1", "true", "yes")
BACKGROUND_QUEUE = build_queue(DATA_PATH)
IDEA_STORE = WatsonIdeaStore(Path(os.environ.get("WATSON_IDEAS_DB", DATA_PATH / "memory" / "watson_ideas.sqlite3")))
VALID_BG_RESULT_STATUSES = {"completed", "failed", "needs_clarification", "expired"}
AGENT_MAX_RESULT_CHARS = int(os.environ.get("WATSON_AGENT_MAX_RESULT_CHARS", "24000"))
AGENT_MAX_CHECKINS_PER_MIN = int(os.environ.get("WATSON_AGENT_MAX_CHECKINS_PER_MIN", "30"))
_agent_checkin_events: dict[str, deque[float]] = defaultdict(deque)
_agent_checkin_lock = Lock()

# When mcporter sends Authorization: Bearer <API_SECRET>, middleware sets this so tools can skip privateKey
_mcp_header_auth_valid: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_header_auth_valid", default=False)


def _git_value(*args: str) -> str | None:
    data_repo = Path(os.environ.get("DATA_PATH", "") or "/data")
    repo = data_repo if (data_repo / ".git").exists() else Path(__file__).resolve().parent.parent.parent
    try:
        return subprocess.check_output(["git", "-C", str(repo), *args], text=True, stderr=subprocess.DEVNULL).strip()
    except Exception:
        git_dir = repo / ".git"
        head = git_dir / "HEAD"
        if not head.exists():
            return None
        try:
            head_value = head.read_text(encoding="utf-8").strip()
        except OSError:
            return None
        if args == ("rev-parse", "--abbrev-ref", "HEAD"):
            return head_value.removeprefix("ref: refs/heads/") if head_value.startswith("ref: refs/heads/") else "HEAD"
        if args == ("rev-parse", "HEAD"):
            if head_value.startswith("ref: "):
                ref_path = git_dir / head_value.removeprefix("ref: ").strip()
                try:
                    return ref_path.read_text(encoding="utf-8").strip() if ref_path.exists() else None
                except OSError:
                    return None
            return head_value
        return None


def service_metadata() -> dict[str, Any]:
    return {
        "service": "Watson API",
        "environment": os.getenv("APP_ENV", os.getenv("ENVIRONMENT", "production")),
        "git_sha": os.getenv("GIT_SHA") or _git_value("rev-parse", "HEAD"),
        "git_branch": os.getenv("GIT_BRANCH") or _git_value("rev-parse", "--abbrev-ref", "HEAD"),
        "dirty_worktree": bool(_git_value("status", "--short")),
        "deployed_at": os.getenv("DEPLOYED_AT"),
        "version": "2026.06.03-memory-stabilization",
        "canonical_memory": "BYB Shared Memory",
        "memory_api_url": os.getenv("MEMORY_API_URL", ""),
        "use_memory_api": USE_MEMORY_API,
        "hybrid_search_enabled": True,
        "sync_capture_enabled": True,
        "write_verify_enabled": True,
    }


def _verify_private_key(private_key: str | None = None) -> None:
    """Verify auth via privateKey param or via request header (set by middleware for /mcp requests)."""
    expected = EXPECTED_SECRET()
    if not expected:
        raise ValueError("Server auth not configured (API_SECRET or BACKYARDBRAINS_API_KEY)")
    if private_key and private_key == expected:
        return
    if _mcp_header_auth_valid.get():
        return
    raise ValueError("Invalid or missing privateKey; or missing Authorization: Bearer <API_SECRET> / X-API-Key")


def _verify_agent(agent_id: str, agent_key: str) -> None:
    if not (agent_id or "").strip():
        raise ValueError("agent_id is required")
    if not (agent_key or "").strip():
        raise ValueError("agent_key is required")
    if not BACKGROUND_QUEUE.verify_agent(agent_id.strip(), agent_key.strip()):
        raise ValueError("Invalid agent credentials")


def _enforce_agent_rate_limit(agent_id: str) -> None:
    now_ts = datetime.now().timestamp()
    with _agent_checkin_lock:
        q = _agent_checkin_events[agent_id]
        while q and (now_ts - q[0]) > 60:
            q.popleft()
        if len(q) >= AGENT_MAX_CHECKINS_PER_MIN:
            raise ValueError(f"Rate limit exceeded for agent_id={agent_id}")
        q.append(now_ts)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_watson_wake_action(
    *,
    content: str,
    source: str | None,
    project: str | None,
    reason: str | None,
    capture_id: Any = None,
) -> dict[str, Any]:
    normalized_reason = (reason or "inbox_followup").strip() or "inbox_followup"
    recorded_at = utc_now_iso()
    event_id = f"watson-inbox-wake-{uuid.uuid4()}"
    action_content = (
        "Watson inbox wake requested.\n"
        f"Reason: {normalized_reason}\n"
        f"Source: {source or 'watson-inbox'}\n"
        f"Project: {project or 'unspecified'}\n"
        f"Capture ID: {capture_id or 'unverified'}\n\n"
        "Inbox content:\n"
        f"{content}"
    )
    record = {
        "id": event_id,
        "recorded_at": recorded_at,
        "action": "tell-agent",
        "actor": source or "watson-inbox",
        "agentId": "watson",
        "content": action_content,
        "project": project or "",
        "wake_reason": normalized_reason,
        "source": source or "watson-inbox",
        "capture_id": capture_id,
    }
    WATSON_INBOX_WAKE_ACTION_LOG.parent.mkdir(parents=True, exist_ok=True)
    with WATSON_INBOX_WAKE_ACTION_LOG.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=True, sort_keys=True) + "\n")
    return {
        "requested": True,
        "event_id": event_id,
        "reason": normalized_reason,
        "target_agent": "watson",
        "relay": "cortex-action-relay.timer",
        "action_log": str(WATSON_INBOX_WAKE_ACTION_LOG),
    }


def verify_secret(secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    if _mcp_header_auth_valid.get():
        return secret or ""
    expected = EXPECTED_SECRET()
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing secret")
    return secret


def get_file(
    name: str,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    path = (EXPORT_DIR / name).resolve()
    if not path.is_relative_to(EXPORT_DIR) or not path.is_file():
        raise HTTPException(status_code=404, detail="File not found")

    digest = hashlib.sha256()
    with path.open("rb") as export_file:
        for chunk in iter(lambda: export_file.read(1024 * 1024), b""):
            digest.update(chunk)

    return FileResponse(
        path,
        filename=name,
        headers={"X-SHA256": digest.hexdigest()},
    )


def load_jsonld(path: Path) -> list:
    if not path.exists():
        return []
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    return data.get("@graph", data) if isinstance(data, dict) else data


def normalize_project(s: str) -> str:
    """Accept 'nimh-grant' or 'byb:project/nimh-grant'."""
    if not s:
        return ""
    s = s.strip()
    if s.startswith("byb:project/"):
        return s
    return f"byb:project/{s}"


def task_matches(t: dict, project: str | None, status: str | None, include_complete: bool) -> bool:
    part_of = t.get("partOf", "")
    if project:
        proj_norm = normalize_project(project)
        if proj_norm and part_of != proj_norm:
            return False
    if status:
        s = (t.get("status") or "").lower()
        status_lower = status.lower()
        if status_lower not in s and s != status_lower:
            return False
    if not include_complete:
        st = (t.get("status") or "").lower()
        if "complete" in st or "✅" in st or "dropped" in st:
            return False
    return True


def event_in_range(e: dict, from_date: str | None, to_date: str | None) -> bool:
    d = e.get("date") or e.get("dateTime", "")[:10]
    if not d:
        return True
    if from_date and d < from_date:
        return False
    if to_date and d > to_date:
        return False
    return True


def link_for_project(link: dict, project: str) -> bool:
    related = link.get("relatedTo", [])
    proj_norm = normalize_project(project)
    if not proj_norm:
        return True
    return proj_norm in related or f"byb:project/{project}" in related


def root():
    return {
        **service_metadata(),
        "docs": "https://watson.backyardbrains.com/docs",
        "endpoints": [
            "/summary",
            "/projects",
            "/tasks",
            "/events",
            "/links",
            "/inbox",
            "/memory/search",
            "/memory/audit",
            "/memory/quality",
            "/playground/watson-ideas",
            "/worker/steward",
            "/mcp",
        ],
    }


def summary():
    """Condensed digest: projects, key tasks, upcoming events. For 'What do I need to do?'"""
    if USE_MEMORY_API:
        from memory_client import get_summary as memory_get_summary
        return memory_get_summary()
    projects = load_jsonld(ONTOLOGY / "projects.jsonld")
    tasks = load_jsonld(ONTOLOGY / "tasks.jsonld")
    events = load_jsonld(ONTOLOGY / "events.jsonld")
    today = datetime.now().strftime("%Y-%m-%d")
    open_tasks = [t for t in tasks if task_matches(t, None, None, False)]
    upcoming = sorted(
        [e for e in events if event_in_range(e, today, None)],
        key=lambda x: x.get("date") or x.get("dateTime") or "9999",
    )[:10]
    return {
        "projects": [{"id": p.get("@id"), "title": p.get("title"), "status": p.get("status"), "priority": p.get("priority")} for p in projects],
        "open_tasks": [{"id": t.get("@id"), "description": t.get("description"), "status": t.get("status"), "dueDate": t.get("dueDate"), "partOf": t.get("partOf")} for t in open_tasks[:15]],
        "upcoming_events": [{"id": e.get("@id"), "label": e.get("label"), "date": e.get("date"), "dateTime": e.get("dateTime")} for e in upcoming],
    }


def current_priorities():
    """Current recommended actions from Watson's latest evidence-first packet, with canonical Task fallback."""
    data = summary()
    return {
        "source_of_truth": data.get("source_of_truth"),
        "freshness_contract": data.get("freshness_contract"),
        "reconciliation_cadence": data.get("reconciliation_cadence"),
        "headline": data.get("headline"),
        "summary": data.get("summary"),
        "today_packet": data.get("today_packet"),
        "recommended_now": data.get("recommended_now", []),
    }


def memory_search(
    q: str = Query(""),
    limit: int = Query(10, ge=1, le=200),
    include_quarantined: bool = Query(False),
):
    """Search Memory captures through Watson's quarantine overlay."""
    if not USE_MEMORY_API:
        raise HTTPException(status_code=422, detail="memory_search requires USE_MEMORY_API=true")
    from memory_client import search_captures
    return {
        "captures": search_captures(q=q, limit=limit, include_quarantined=include_quarantined),
        "include_quarantined": include_quarantined,
    }


def memory_context(q: str = Query('',max_length=2000),project_slug: str | None = None):
    if not USE_MEMORY_API:raise HTTPException(422,'Memory API is disabled')
    from memory_client import get_memory_context
    return get_memory_context(q,project_slug,'watson-rest')


class MemoryAuditPayload(BaseModel):
    limit: int = 200
    apply_quarantine: bool = False
    queries: list[str] | None = None


def memory_audit(payload: MemoryAuditPayload):
    """Audit captures for obvious junk; optionally apply reversible quarantine flags."""
    if not USE_MEMORY_API:
        raise HTTPException(status_code=422, detail="memory_audit requires USE_MEMORY_API=true")
    from memory_client import audit_memory_captures
    return audit_memory_captures(
        limit=max(1, min(int(payload.limit), 200)),
        apply_quarantine=payload.apply_quarantine,
        queries=payload.queries,
    )


def memory_quality(status: str | None = Query(None), limit: int = Query(100, ge=1, le=1000)):
    """Review Watson's local capture-quality overlay."""
    if not USE_MEMORY_API:
        raise HTTPException(status_code=422, detail="memory_quality requires USE_MEMORY_API=true")
    from memory_client import list_capture_quality
    return {"records": list_capture_quality(status=status, limit=limit)}


def projects(
    state: str | None = Query(None),
    include_closed: bool = Query(False),
):
    if USE_MEMORY_API:
        from memory_client import get_projects
        return {"projects": get_projects(state=state, include_closed=include_closed)}
    from memory_client import filter_projects
    data = load_jsonld(ONTOLOGY / "projects.jsonld")
    return {"projects": filter_projects(data, state=state, include_closed=include_closed)}


def tasks(
    project: str | None = Query(None),
    status: str | None = Query(None),
    include_complete: bool = Query(True),
    limit: int = Query(50, le=200),
):
    if USE_MEMORY_API:
        from memory_client import get_tasks
        data = get_tasks(project=project, status=status, include_complete=include_complete, limit=limit)
        return {"tasks": data}
    data = load_jsonld(ONTOLOGY / "tasks.jsonld")
    filtered = [t for t in data if task_matches(t, project, status, include_complete)]
    return {"tasks": filtered[:limit]}


def events(
    from_date: str | None = Query(None, alias="from"),
    to_date: str | None = Query(None, alias="to"),
    limit: int = Query(50, le=200),
):
    if USE_MEMORY_API:
        from memory_client import get_events
        data = get_events(from_date=from_date, to_date=to_date, limit=limit)
        return {"events": data}
    data = load_jsonld(ONTOLOGY / "events.jsonld")
    filtered = [e for e in data if event_in_range(e, from_date, to_date)]
    return {"events": sorted(filtered, key=lambda x: x.get("date") or x.get("dateTime") or "9999")[:limit]}


def links(project: str | None = Query(None)):
    """Links/documents for a project. description = URL."""
    if USE_MEMORY_API:
        from memory_client import get_links
        return get_links(project=project)
    data = load_jsonld(ONTOLOGY / "links.jsonld")
    if project:
        filtered = [l for l in data if link_for_project(l, project)]
    else:
        filtered = data
    return {"docs": [{"id": d.get("@id"), "label": d.get("label"), "url": d.get("description")} for d in filtered]}


class InboxPayload(BaseModel):
    content: str
    project: str | None = None
    source: str | None = None
    wake: bool = False
    reason: str | None = None


def inbox_response(
    *,
    ok: bool,
    message: str,
    content: str,
    source: str | None,
    project: str | None,
    wake: bool,
    reason: str | None,
    verified: bool | None = None,
    capture_id: Any = None,
) -> dict[str, Any]:
    response: dict[str, Any] = {"ok": ok, "message": message}
    if verified is not None:
        response["verified"] = verified
    if capture_id is not None:
        response["capture_id"] = capture_id
    if wake:
        try:
            wake_result = append_watson_wake_action(
                content=content,
                source=source,
                project=project,
                reason=reason,
                capture_id=capture_id,
            )
            response["message"] = f"{message} Watson wake queued."
        except OSError as exc:
            wake_result = {
                "requested": False,
                "error": str(exc),
                "target_agent": "watson",
                "relay": "cortex-action-relay.timer",
            }
            response["message"] = f"{message} Watson wake request failed."
        response["wake"] = wake_result
    else:
        response["wake"] = {"requested": False}
    return response


def inbox(payload: InboxPayload):
    if USE_MEMORY_API:
        from memory_client import post_capture
        result = post_capture(payload.content, source=payload.source or "watson", project=payload.project, sync=True)
        verified = bool(result.get("verified"))
        capture_id = result.get("capture_id") or result.get("id")
        return inbox_response(
            ok=verified,
            verified=verified,
            capture_id=capture_id,
            message="Captured and verified in BYB Shared Memory." if verified else "Capture stored, but BYB Shared Memory read-back verification failed.",
            content=payload.content,
            source=payload.source or "watson",
            project=payload.project,
            wake=payload.wake,
            reason=payload.reason,
        )
    inbox_path = MEMORY / "inbox-from-external.md"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"\n---\n**{datetime.now().isoformat()}**"
    if payload.source:
        line += f" (from {payload.source})"
    if payload.project:
        line += f" [project: {payload.project}]"
    line += f"\n{payload.content}\n"
    with open(inbox_path, "a", encoding="utf-8") as f:
        f.write(line)
    return inbox_response(
        ok=True,
        message="Appended to inbox. Watson will reconcile on next run.",
        content=payload.content,
        source=payload.source,
        project=payload.project,
        wake=payload.wake,
        reason=payload.reason,
    )


# --- MCP Server (auth via privateKey param, no Bearer) ---
mcp = FastMCP("Watson",instructions='Before BYB work, call get_memory_context with the task question and project. Apply all active rules, cite source evidence, and state what is unknown. Retrieval alone does not prove compliance. Record feedback only after actual use or an observed miss.')


def _get_summary_data():
    if USE_MEMORY_API:
        from memory_client import get_summary
        return get_summary()
    projects = load_jsonld(ONTOLOGY / "projects.jsonld")
    tasks = load_jsonld(ONTOLOGY / "tasks.jsonld")
    events = load_jsonld(ONTOLOGY / "events.jsonld")
    today = datetime.now().strftime("%Y-%m-%d")
    open_tasks = [t for t in tasks if task_matches(t, None, None, False)]
    upcoming = sorted(
        [e for e in events if event_in_range(e, today, None)],
        key=lambda x: x.get("date") or x.get("dateTime") or "9999",
    )[:10]
    return {
        "projects": [{"id": p.get("@id"), "title": p.get("title"), "status": p.get("status"), "priority": p.get("priority")} for p in projects],
        "open_tasks": [{"id": t.get("@id"), "description": t.get("description"), "status": t.get("status"), "dueDate": t.get("dueDate"), "partOf": t.get("partOf")} for t in open_tasks[:15]],
        "upcoming_events": [{"id": e.get("@id"), "label": e.get("label"), "date": e.get("date"), "dateTime": e.get("dateTime")} for e in upcoming],
    }


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_summary(privateKey: str | None = None) -> dict:
    """What should Greg work on right now? Use recommended_now first. Uses Watson's latest evidence-first dashboard packet, then canonical Memory tasks, projects, and events as fallback."""
    _verify_private_key(privateKey)
    return _get_summary_data()


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_current_priorities(privateKey: str | None = None) -> dict:
    """Best tool for 'what should I work on?' Returns Watson-ranked current actions plus Today narrative."""
    _verify_private_key(privateKey)
    data = _get_summary_data()
    return {
        "source_of_truth": data.get("source_of_truth"),
        "freshness_contract": data.get("freshness_contract"),
        "reconciliation_cadence": data.get("reconciliation_cadence"),
        "headline": data.get("headline"),
        "summary": data.get("summary"),
        "today_packet": data.get("today_packet"),
        "recommended_now": data.get("recommended_now", []),
    }


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def search_memory(
    privateKey: str | None = None,
    q: str = "",
    limit: int = 10,
    include_quarantined: bool = False,
) -> dict:
    """Search Memory captures through Watson's reversible quarantine overlay."""
    _verify_private_key(privateKey)
    if not USE_MEMORY_API:
        raise ValueError("search_memory requires USE_MEMORY_API=true")
    from memory_client import search_captures
    return {
        "captures": search_captures(q=q, limit=max(1, min(int(limit), 200)), include_quarantined=include_quarantined),
        "include_quarantined": include_quarantined,
    }


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_memory_context(privateKey: str | None = None,q: str = '',project_slug: str | None = None) -> dict:
    """Start a task with current rules and relevant source excerpts; fetching does not prove compliance."""
    _verify_private_key(privateKey)
    if not USE_MEMORY_API:raise ValueError('Memory API is disabled')
    from memory_client import get_memory_context as get_context
    return get_context(q,project_slug,'watson-mcp')


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def record_memory_feedback(context_id: str,outcome: str,capture_ids: list[int],privateKey: str | None = None) -> dict:
    """Report actual use or an observed miss; private telemetry, not a durable capture."""
    _verify_private_key(privateKey)
    if not USE_MEMORY_API:raise ValueError('Memory API is disabled')
    from memory_client import record_memory_feedback as record
    return record(context_id,outcome,capture_ids)


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def audit_memory_captures(
    privateKey: str | None = None,
    limit: int = 200,
    apply_quarantine: bool = False,
    queries: list[str] | None = None,
) -> dict:
    """Audit captures for junk; set apply_quarantine=true to hide matched rows from Watson by default."""
    _verify_private_key(privateKey)
    if not USE_MEMORY_API:
        raise ValueError("audit_memory_captures requires USE_MEMORY_API=true")
    from memory_client import audit_memory_captures as memory_audit_captures
    return memory_audit_captures(
        limit=max(1, min(int(limit), 200)),
        apply_quarantine=apply_quarantine,
        queries=queries,
    )


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def list_memory_quality(
    privateKey: str | None = None,
    status: str | None = None,
    limit: int = 100,
) -> dict:
    """Review Watson's capture quality overlay: active/quarantined/superseded/deleted."""
    _verify_private_key(privateKey)
    if not USE_MEMORY_API:
        raise ValueError("list_memory_quality requires USE_MEMORY_API=true")
    from memory_client import list_capture_quality
    return {"records": list_capture_quality(status=status, limit=max(1, min(int(limit), 1000)))}


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_projects(
    privateKey: str | None = None,
    state: str | None = None,
    include_closed: bool = False,
) -> dict:
    """List projects. Defaults to open projects; use include_closed=true or state=all/closed for lifecycle audits."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import get_projects
        return {"projects": get_projects(state=state, include_closed=include_closed)}
    from memory_client import filter_projects
    return {"projects": filter_projects(load_jsonld(ONTOLOGY / "projects.jsonld"), state=state, include_closed=include_closed)}


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_tasks(
    privateKey: str | None = None,
    project: str | None = None,
    status: str | None = None,
    include_complete: bool = True,
    limit: int = 50,
) -> dict:
    """List tasks. Filter by project (e.g. nimh-grant) or status."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import get_tasks
        return {"tasks": get_tasks(project=project, status=status, include_complete=include_complete, limit=limit)}
    data = load_jsonld(ONTOLOGY / "tasks.jsonld")
    filtered = [t for t in data if task_matches(t, project, status, include_complete)]
    return {"tasks": filtered[:limit]}


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_events(
    privateKey: str | None = None,
    from_date: str | None = None,
    to_date: str | None = None,
    limit: int = 50,
) -> dict:
    """List events in date range. Dates as YYYY-MM-DD."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import get_events
        return {"events": get_events(from_date=from_date, to_date=to_date, limit=limit)}
    data = load_jsonld(ONTOLOGY / "events.jsonld")
    filtered = [e for e in data if event_in_range(e, from_date, to_date)]
    return {"events": sorted(filtered, key=lambda x: x.get("date") or x.get("dateTime") or "9999")[:limit]}


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_links(privateKey: str | None = None, project: str | None = None) -> dict:
    """Links/documents for a project. Returns label and URL."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import get_links
        return get_links(project=project)
    data = load_jsonld(ONTOLOGY / "links.jsonld")
    if project:
        filtered = [l for l in data if link_for_project(l, project)]
    else:
        filtered = data
    return {"docs": [{"id": d.get("@id"), "label": d.get("label"), "url": d.get("description")} for d in filtered]}


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def set_task_status(
    task_id: int,
    status: str,
    privateKey: str | None = None,
) -> dict:
    """Update a task's status. task_id from get_tasks (e.g. 69). status: open, in_progress, blocked, done, dropped."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import set_task_status as memory_set_task_status
        memory_set_task_status(task_id, status)
        return {"ok": True, "message": f"Task {task_id} status set to {status}"}
    raise ValueError("set_task_status requires USE_MEMORY_API=true (ontology is now in memory-api)")


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def set_task_disposition(
    task_id: int,
    disposition: str,
    reason: str = "",
    privateKey: str | None = None,
) -> dict:
    """Durably mark a canonical task done, wont_do, or wrong. Wrong also marks the source when available."""
    _verify_private_key(privateKey)
    if not USE_MEMORY_API:
        raise ValueError("set_task_disposition requires USE_MEMORY_API=true")
    from memory_client import get_tasks, record_task_disposition
    tasks = get_tasks(include_complete=True, limit=1000)
    task = next((item for item in tasks if str(item.get("id")) == str(task_id)), None)
    if not task:
        raise ValueError(f"Task {task_id} not found")
    record = record_task_disposition(task=task, disposition=disposition, reason=reason or None)
    return {"ok": True, "disposition": record}


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def post_inbox(
    privateKey: str | None = None,
    content: str = "",
    project: str | None = None,
    source: str | None = None,
    wake: bool = False,
    reason: str | None = None,
) -> dict:
    """Record a note from external LM. Set wake=true to queue an immediate Watson reconciliation wake."""
    _verify_private_key(privateKey)
    if not (content or "").strip():
        raise ValueError("content is required and must be non-empty")
    if USE_MEMORY_API:
        from memory_client import post_capture
        result = post_capture(content, source=source or "watson", project=project, sync=True)
        verified = bool(result.get("verified"))
        capture_id = result.get("capture_id") or result.get("id")
        return inbox_response(
            ok=verified,
            verified=verified,
            capture_id=capture_id,
            message="Captured and verified in BYB Shared Memory." if verified else "Capture stored, but BYB Shared Memory read-back verification failed.",
            content=content,
            source=source or "watson",
            project=project,
            wake=wake,
            reason=reason,
        )
    inbox_path = MEMORY / "inbox-from-external.md"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    line = f"\n---\n**{datetime.now().isoformat()}**"
    if source:
        line += f" (from {source})"
    if project:
        line += f" [project: {project}]"
    line += f"\n{content}\n"
    with open(inbox_path, "a", encoding="utf-8") as f:
        f.write(line)
    return inbox_response(
        ok=True,
        message="Appended to inbox. Watson will reconcile on next run.",
        content=content,
        source=source,
        project=project,
        wake=wake,
        reason=reason,
    )


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def register_background_agent(
    agent_id: str,
    agent_key: str,
    privateKey: str | None = None,
    capabilities: list[str] | None = None,
    enabled: bool = True,
) -> dict:
    """Create or rotate a remote worker agent credential."""
    _verify_private_key(privateKey)
    if not (agent_id or "").strip():
        raise ValueError("agent_id is required")
    if not (agent_key or "").strip():
        raise ValueError("agent_key is required")
    BACKGROUND_QUEUE.register_agent(
        agent_id=agent_id.strip(),
        agent_key=agent_key.strip(),
        capabilities=capabilities or [],
        enabled=enabled,
    )
    return {"ok": True, "agent_id": agent_id.strip(), "enabled": enabled, "capabilities": capabilities or []}


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def enqueue_background_task(
    target_agent_id: str,
    instructions: str,
    privateKey: str | None = None,
    context: dict[str, Any] | None = None,
    priority: int = 5,
    created_by: str = "spike",
    linked_task_id: int | None = None,
    max_attempts: int | None = None,
) -> dict:
    """Queue background work for a specific remote agent."""
    _verify_private_key(privateKey)
    if not (target_agent_id or "").strip():
        raise ValueError("target_agent_id is required")
    if not (instructions or "").strip():
        raise ValueError("instructions is required")
    task = BACKGROUND_QUEUE.enqueue_task(
        target_agent_id=target_agent_id.strip(),
        instructions=instructions.strip(),
        context=context or {},
        priority=max(1, min(10, int(priority))),
        created_by=created_by,
        linked_task_id=linked_task_id,
        max_attempts=max_attempts,
    )
    return {"ok": True, "task": task}


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def agent_check_in(
    agent_id: str,
    agent_key: str,
    max_tasks: int = 1,
) -> dict:
    """Worker check-in endpoint: claims next targeted task if available."""
    _verify_agent(agent_id, agent_key)
    _enforce_agent_rate_limit(agent_id)
    max_tasks = 1 if max_tasks < 1 else min(max_tasks, 3)
    claimed: list[dict] = []
    for _ in range(max_tasks):
        task = BACKGROUND_QUEUE.claim_next_task(agent_id=agent_id.strip())
        if not task:
            break
        claimed.append(task)
    return {"ok": True, "agent_id": agent_id.strip(), "tasks": claimed}


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def submit_background_result(
    task_id: int,
    agent_id: str,
    agent_key: str,
    status: str,
    result_text: str,
    metadata: dict[str, Any] | None = None,
) -> dict:
    """Submit result for a claimed task and ingest it into Watson memory."""
    _verify_agent(agent_id, agent_key)
    if not (result_text or "").strip():
        raise ValueError("result_text is required")
    if len(result_text) > AGENT_MAX_RESULT_CHARS:
        raise ValueError(f"result_text too large ({len(result_text)} chars > {AGENT_MAX_RESULT_CHARS})")
    status_norm = (status or "").strip().lower()
    if status_norm not in VALID_BG_RESULT_STATUSES:
        raise ValueError(f"Invalid status '{status}'. Expected one of: {sorted(VALID_BG_RESULT_STATUSES)}")

    updated_task = BACKGROUND_QUEUE.submit_result(
        task_id=task_id,
        agent_id=agent_id.strip(),
        status=status_norm,
        result_text=result_text.strip(),
        metadata=metadata or {},
    )
    tags = f"[bg-task:{task_id}] [agent:{agent_id.strip()}] [status:{status_norm}]"
    capture_text = f"{tags}\n{result_text.strip()}"
    capture_source = f"remote-agent:{agent_id.strip()}"
    if USE_MEMORY_API:
        from memory_client import post_capture
        post_capture(capture_text, source=capture_source)

        linked_task_id = updated_task.get("linked_task_id")
        if linked_task_id and status_norm == "completed":
            try:
                from memory_client import set_task_status as memory_set_task_status
                memory_set_task_status(int(linked_task_id), "done")
            except Exception:
                # Keep capture even if optional status sync fails.
                pass
    else:
        inbox_path = MEMORY / "inbox-from-external.md"
        inbox_path.parent.mkdir(parents=True, exist_ok=True)
        with open(inbox_path, "a", encoding="utf-8") as f:
            f.write(f"\n---\n**{datetime.now().isoformat()}** (from {capture_source})\n{capture_text}\n")

    if status_norm == "completed":
        try:
            rendered = render_background_task_report(task_id)
            if rendered.get("ok"):
                BACKGROUND_QUEUE.update_latest_result_metadata(
                    task_id,
                    {
                        "report_url": rendered.get("report_url"),
                        "report_path": rendered.get("report_path"),
                        "report_rendered_at": rendered.get("rendered_at"),
                        "watson_idea_id": rendered.get("watson_idea_id"),
                    },
                )
        except Exception:
            # Report generation is best-effort; keep the worker submission durable.
            pass

    return {"ok": True, "task": updated_task}


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_background_task_status(
    task_id: int,
    privateKey: str | None = None,
) -> dict:
    """Inspect queue task state and all submissions."""
    _verify_private_key(privateKey)
    return BACKGROUND_QUEUE.get_task_status(task_id)


def _completed_linked_task_ids_from_memory() -> list[int]:
    if not USE_MEMORY_API:
        return []
    try:
        from memory_client import get_tasks
        tasks = get_tasks(include_complete=True, limit=200)
    except Exception:
        return []

    completed_ids: list[int] = []
    for task in tasks:
        status = str(task.get("status") or "").lower()
        if "complete" not in status:
            continue
        raw_id = task.get("id") or task.get("@id") or ""
        raw_id = str(raw_id).rstrip("/")
        numeric = raw_id.rsplit("/", 1)[-1].rsplit(":", 1)[-1]
        if numeric.isdigit():
            completed_ids.append(int(numeric))
    return completed_ids


def _record_steward_run(result: dict[str, Any]) -> None:
    summary = result.get("summary", {})
    lines = [
        "[queue-steward]",
        f"stale_tasks={summary.get('stale_tasks', 0)}",
        f"actions_taken={summary.get('actions_taken', 0)}",
        f"escalations={summary.get('escalations', 0)}",
        f"dry_run={result.get('dry_run')}",
    ]
    for action in result.get("actions_taken", []):
        task = action.get("task") or {}
        lines.append(f"action task={task.get('id')} type={action.get('action')} reason={action.get('reason')}")
    for escalation in result.get("escalations", []):
        lines.append(f"escalation task={escalation.get('task_id')} reason={escalation.get('reason')}")
    capture_text = "\n".join(lines)
    if USE_MEMORY_API:
        try:
            from memory_client import post_capture
            post_capture(capture_text, source="watson-queue-steward")
            return
        except Exception:
            pass
    inbox_path = MEMORY / "inbox-from-external.md"
    inbox_path.parent.mkdir(parents=True, exist_ok=True)
    with open(inbox_path, "a", encoding="utf-8") as f:
        f.write(f"\n---\n**{datetime.now().isoformat()}** (from watson-queue-steward)\n{capture_text}\n")


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def run_background_queue_steward(
    privateKey: str | None = None,
    stale_after_hours: int = 24,
    inactive_after_hours: int = 24,
    fallback_agent_id: str | None = None,
    dry_run: bool = False,
    completed_linked_task_ids: list[int] | None = None,
    check_memory_for_completed_links: bool = True,
) -> dict:
    """Audit stale background work, safely remediate mechanical cases, and escalate the rest."""
    _verify_private_key(privateKey)
    completed_ids = list(completed_linked_task_ids or [])
    if check_memory_for_completed_links:
        completed_ids.extend(_completed_linked_task_ids_from_memory())
    result = BACKGROUND_QUEUE.run_steward(
        stale_after_hours=max(1, int(stale_after_hours)),
        inactive_after_hours=max(1, int(inactive_after_hours)),
        fallback_agent_id=fallback_agent_id,
        dry_run=dry_run,
        completed_linked_task_ids=sorted(set(completed_ids)),
    )
    _record_steward_run(result)
    return result


mcp_app = mcp.http_app(path="/")


class HeaderAuthMiddleware(BaseHTTPMiddleware):
    """Accept Authorization: Bearer, X-API-Key, or ?key= on /mcp and /playground/ paths.
    For /playground/: nginx injects X-API-Key after OAuth2 validation, so the browser never needs the secret.
    For /mcp: ChatGPT connector can't send headers; use URL with key."""

    _AUTH_PATHS = ("/mcp", "/playground/")

    async def dispatch(self, request: Request, call_next):
        if any(request.url.path.startswith(p) for p in self._AUTH_PATHS):
            expected = EXPECTED_SECRET()
            if expected:
                auth = request.headers.get("authorization") or request.headers.get("Authorization")
                api_key = request.headers.get("x-api-key") or request.headers.get("X-API-Key")
                query_key = request.query_params.get("key")
                token = None
                if auth and auth.startswith("Bearer "):
                    token = auth[7:].strip()
                elif api_key:
                    token = api_key.strip()
                elif query_key:
                    token = query_key.strip()
                if token and token == expected:
                    tok = _mcp_header_auth_valid.set(True)
                    try:
                        return await call_next(request)
                    finally:
                        _mcp_header_auth_valid.reset(tok)
        return await call_next(request)


app = FastAPI(
    title="Watson API",
    description="Ontology and inbox access for OpenClaw. Used by Claude and other LLMs.",
    lifespan=mcp_app.lifespan,
)
app.add_middleware(HeaderAuthMiddleware)
# Register REST routes on main app
app.get("/")(root)
app.get("/summary", dependencies=[Depends(verify_secret)])(summary)
app.get("/current-priorities", dependencies=[Depends(verify_secret)])(current_priorities)
app.get("/projects", dependencies=[Depends(verify_secret)])(projects)
app.get("/tasks", dependencies=[Depends(verify_secret)])(tasks)
app.get("/events", dependencies=[Depends(verify_secret)])(events)
app.get("/links", dependencies=[Depends(verify_secret)])(links)
app.post("/inbox", dependencies=[Depends(verify_secret)])(inbox)
app.get("/memory/search", dependencies=[Depends(verify_secret)])(memory_search)
app.get("/memory/context", dependencies=[Depends(verify_secret)])(memory_context)
app.post("/memory/audit", dependencies=[Depends(verify_secret)])(memory_audit)
app.get("/memory/quality", dependencies=[Depends(verify_secret)])(memory_quality)
app.get("/files/{name}")(get_file)


# --- Worker REST routes (mirrors MCP tools for curl-based clients) ---

class WorkerCheckInPayload(BaseModel):
    agent_id: str
    agent_key: str
    max_tasks: int = 1


class WorkerSubmitPayload(BaseModel):
    task_id: int
    agent_id: str
    agent_key: str
    status: str
    result_text: str
    metadata: dict[str, Any] | None = None


def worker_check_in_rest(payload: WorkerCheckInPayload):
    try:
        return agent_check_in(
            agent_id=payload.agent_id,
            agent_key=payload.agent_key,
            max_tasks=payload.max_tasks,
        )
    except ValueError as e:
        raise HTTPException(status_code=401, detail=str(e))


def worker_submit_rest(payload: WorkerSubmitPayload):
    try:
        return submit_background_result(
            task_id=payload.task_id,
            agent_id=payload.agent_id,
            agent_key=payload.agent_key,
            status=payload.status,
            result_text=payload.result_text,
            metadata=payload.metadata,
        )
    except ValueError as e:
        status = 401 if "credentials" in str(e).lower() else 422
        raise HTTPException(status_code=status, detail=str(e))


class WorkerEnqueuePayload(BaseModel):
    target_agent_id: str
    instructions: str
    context: dict[str, Any] | None = None
    priority: int = 5
    created_by: str = "spike"
    linked_task_id: int | None = None
    max_attempts: int | None = None


class WorkerStewardPayload(BaseModel):
    stale_after_hours: int = 24
    inactive_after_hours: int = 24
    fallback_agent_id: str | None = None
    dry_run: bool = False
    completed_linked_task_ids: list[int] | None = None
    check_memory_for_completed_links: bool = True


def worker_enqueue_rest(payload: WorkerEnqueuePayload, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    try:
        return enqueue_background_task(
            target_agent_id=payload.target_agent_id,
            instructions=payload.instructions,
            privateKey=EXPECTED_SECRET(),
            context=payload.context,
            priority=payload.priority,
            created_by=payload.created_by,
            linked_task_id=payload.linked_task_id,
            max_attempts=payload.max_attempts,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


def worker_status_rest(task_id: int, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    try:
        return get_background_task_status(task_id=task_id, privateKey=EXPECTED_SECRET())
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))


def worker_steward_rest(payload: WorkerStewardPayload, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    try:
        return run_background_queue_steward(
            privateKey=EXPECTED_SECRET(),
            stale_after_hours=payload.stale_after_hours,
            inactive_after_hours=payload.inactive_after_hours,
            fallback_agent_id=payload.fallback_agent_id,
            dry_run=payload.dry_run,
            completed_linked_task_ids=payload.completed_linked_task_ids,
            check_memory_for_completed_links=payload.check_memory_for_completed_links,
        )
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))


app.post("/worker/check-in")(worker_check_in_rest)
app.post("/worker/submit")(worker_submit_rest)
app.post("/worker/enqueue", dependencies=[])(worker_enqueue_rest)
app.get("/worker/status/{task_id}", dependencies=[])(worker_status_rest)
app.post("/worker/steward", dependencies=[])(worker_steward_rest)

app.mount("/mcp", mcp_app)


# --- Cortex user feedback state (legacy /playground API compatibility) ---

PLAYGROUND_STATE_PATH = DATA_PATH / "workspaces" / "main" / "cortex-user-state.json"
PLAYGROUND_ADDY_PATH = DATA_PATH / "workspaces" / "addy" / "cortex-blocks.json"
BYB_API_BASE = os.environ.get("BYB_API_BASE", "https://api.backyardbrains.com").rstrip("/")


def playground_state_get(secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    if not PLAYGROUND_STATE_PATH.exists():
        return {}
    try:
        with open(PLAYGROUND_STATE_PATH, encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _read_json_file(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        with open(path, encoding="utf-8") as f:
            parsed = json.load(f)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, OSError):
        return {}


def _pick_metric(totals: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        value = totals.get(key)
        if value is not None and value != "":
            return value
    return None


def _build_live_audit_rows(report_rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for campaign in report_rows:
        campaign_name = str(campaign.get("name") or "Unknown Campaign")
        ad_groups = campaign.get("ad_groups")
        if not isinstance(ad_groups, dict):
            continue
        for ad_group_name, ad_group_data in ad_groups.items():
            totals = ad_group_data.get("totals") if isinstance(ad_group_data, dict) else {}
            totals = totals if isinstance(totals, dict) else {}
            rows.append({
                "date": datetime.utcnow().strftime("%Y-%m-%d"),
                "campaign": campaign_name,
                "ad_group": str(ad_group_name),
                "adset_name": str(ad_group_name),
                "spend": _pick_metric(totals, ("spend", "ad_spend", "cost")),
                "impressions": _pick_metric(totals, ("impressions",)),
                "link_clicks": _pick_metric(totals, ("clicks", "link_clicks")),
                "link_ctr_pct": _pick_metric(totals, ("ctr", "link_ctr_pct")),
                "cpc": _pick_metric(totals, ("cpc",)),
                "landing_page_views": _pick_metric(totals, ("landing_page_views", "lp_views")),
                "lp_conv_rate_pct": _pick_metric(totals, ("lp_conversion_rate", "lp_conv_rate_pct")),
                "adds_to_cart": _pick_metric(totals, ("atcs", "adds_to_cart")),
                "atc_rate_pct": _pick_metric(totals, ("atc_rate", "atc_rate_pct")),
                "cost_per_atc": _pick_metric(totals, ("cost_per_atc",)),
                "purchases": _pick_metric(totals, ("direct_purchases", "purchases")),
                "purchase_rev": _pick_metric(totals, ("actual_revenue", "purchase_rev")),
                "cart_to_purchase_pct": _pick_metric(totals, ("cart_to_purchase_pct",)),
                "cpa": _pick_metric(totals, ("cpa",)),
                "roas": _pick_metric(totals, ("potential_roas", "roas")),
            })
    return rows


def playground_addy_live_get(secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    snapshot = _read_json_file(PLAYGROUND_ADDY_PATH)
    expected_secret = EXPECTED_SECRET()
    byb_key = os.environ.get("BACKYARDBRAINS_API_KEY") or expected_secret
    if not byb_key:
        return {
            "ok": False,
            "reason": "missing-byb-api-key",
            "error": "BACKYARDBRAINS_API_KEY is not configured on server",
            "snapshot": snapshot,
        }

    refresh_error = None
    try:
        refresh_query = urllib.parse.urlencode({"secret": byb_key})
        refresh_url = f"{BYB_API_BASE}/api/meta/refresh?{refresh_query}"
        refresh_request = urllib.request.Request(refresh_url, method="POST")
        with urllib.request.urlopen(refresh_request, timeout=20):
            pass
    except Exception as exc:
        refresh_error = str(exc)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    report_query = urllib.parse.urlencode({
        "start_date": today,
        "end_date": today,
        "aggregation": "day",
        "product": "All",
        "secret": byb_key,
    })
    report_url = f"{BYB_API_BASE}/api/meta/report?{report_query}"
    try:
        with urllib.request.urlopen(report_url, timeout=30) as response:
            report_payload = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        return {
            "ok": False,
            "reason": "report-fetch-failed",
            "error": str(exc),
            "refresh_error": refresh_error,
            "snapshot": snapshot,
        }

    report_rows = report_payload.get("data", {}).get("report", [])
    report_rows = report_rows if isinstance(report_rows, list) else []
    live_audit_rows = _build_live_audit_rows(report_rows)
    if not live_audit_rows:
        return {
            "ok": False,
            "reason": "empty-live-report",
            "error": "Live API returned no ad group rows for current day",
            "refresh_error": refresh_error,
            "snapshot": snapshot,
            "report_meta": report_payload.get("data", {}),
        }

    live_data = dict(snapshot)
    live_data["live_mode"] = True
    live_data["updated_at"] = datetime.utcnow().isoformat() + "Z"
    live_data["summary"] = "Latest available campaign snapshot pulled now (all products/campaigns in BYB API cache). Addy daily snapshot remains the strategic baseline."
    live_data["audit_rows"] = live_audit_rows
    live_data["date_ranges"] = {
        "trend_window": {"start": today, "end": today, "includes_partial_today": True},
        "audit_table_window": {"start": today, "end": today, "includes_partial_today": True},
        "timezone": "America/Detroit",
    }
    live_data["data_gap_note"] = (
        "Live mode pulls the latest available BYB API snapshot and may lag while refresh runs. "
        "This source can be completed-day biased depending on upstream sync behavior. "
        "Conversion fields remain source-dependent and can be null for KS tracking."
    )

    report_meta = report_payload.get("data", {}) if isinstance(report_payload, dict) else {}
    return {
        "ok": True,
        "data": live_data,
        "live_meta": {
            "today": today,
            "last_refresh": report_meta.get("last_refresh"),
            "last_sync": report_meta.get("last_sync"),
            "refresh_error": refresh_error,
        },
    }


async def playground_state_post(request: Request, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")
    if not isinstance(body, dict):
        raise HTTPException(status_code=422, detail="Body must be a JSON object")
    body["updated_at"] = datetime.now().isoformat() + "Z"
    PLAYGROUND_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYGROUND_STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(body, f, indent=2, ensure_ascii=False)
    tmp.rename(PLAYGROUND_STATE_PATH)
    return {"ok": True, "updated_at": body["updated_at"]}


class PlaygroundTaskFeedbackPayload(BaseModel):
    id: str
    disposition: str
    reason: str | None = None
    snooze_until: str | None = None
    todo: dict[str, Any] | None = None


def _normalize_dashboard_disposition(disposition: str) -> str:
    raw = (disposition or "").strip().lower().replace("-", "_")
    if raw in {"done", "complete", "completed"}:
        return "done"
    if raw in {"wrong", "incorrect_fact", "changed_direction"}:
        return "wrong"
    if raw in {"wont_do", "wontdo", "won_t_do", "won't_do", "rejected", "declined"}:
        return "wont_do"
    if raw in {"snooze", "snoozed"}:
        return "snoozed"
    if raw in {"defer", "deferred", "delay", "delayed", "later"}:
        return "deferred"
    if raw in {"not_today", "not-today", "not today", "not_now", "not-now"}:
        return "not_today"
    raise HTTPException(status_code=422, detail="disposition must be done, wont_do, wrong, snoozed, deferred, or not_today")


def _dashboard_task_key(todo_id: str, todo: dict[str, Any] | None) -> str:
    from task_state import stable_task_key
    project = (todo or {}).get("project") or (todo or {}).get("partOf") or "general"
    return stable_task_key(str(project), str(todo_id))


def _find_capture_for_feedback(todo_id: str, todo: dict[str, Any] | None) -> dict[str, Any] | None:
    from memory_client import search_captures
    from capture_quality import capture_key
    seen: set[str] = set()
    project = str((todo or {}).get("project") or "")
    text = str((todo or {}).get("text") or (todo or {}).get("title") or "")
    queries = ["task-current", todo_id, project, text[:80]]
    for query in [q for q in queries if q]:
        try:
            rows = search_captures(query, limit=100, include_quarantined=True)
        except Exception:
            continue
        for row in rows:
            key = capture_key(row)
            if key in seen:
                continue
            seen.add(key)
            raw = str(row.get("raw_content") or "")
            if "[task-current]" not in raw:
                continue
            if f'id="{todo_id}"' in raw:
                return row
            if project and f'project="{project}"' in raw and text and text[:60] in raw:
                return row
    return None


def _find_canonical_task_for_feedback(todo_id: str, todo: dict[str, Any] | None) -> dict[str, Any] | None:
    from memory_client import get_tasks
    task_key = _dashboard_task_key(todo_id, todo)
    try:
        tasks = get_tasks(include_complete=True, limit=1000)
    except Exception:
        return None
    for task in tasks:
        if str(task.get("task_key") or "") == task_key:
            return task
        if str(task.get("id") or "") == str(todo_id) or str(task.get("@id") or "") == str(todo_id):
            return task
    return None


def playground_task_feedback_post(
    payload: PlaygroundTaskFeedbackPayload,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    disposition = _normalize_dashboard_disposition(payload.disposition)
    if disposition == "snoozed" and not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(payload.snooze_until or "")):
        raise HTTPException(status_code=422, detail="snoozed feedback requires snooze_until as YYYY-MM-DD")
    todo = payload.todo or {}
    task = _find_canonical_task_for_feedback(payload.id, todo)
    capture = _find_capture_for_feedback(payload.id, todo)
    source_pointer = (
        todo.get("source_pointer")
        or todo.get("action_url")
        or (capture or {}).get("source_pointer")
        or None
    )
    description = todo.get("text") or todo.get("title") or todo.get("description") or payload.id

    from task_state import TaskDispositionStore, source_fingerprint
    store = TaskDispositionStore()
    task_key = str((task or {}).get("task_key") or _dashboard_task_key(payload.id, todo))
    source_fp = (task or {}).get("source_fingerprint") or source_fingerprint(str(source_pointer or ""), str(description or ""))
    status_result = None
    if USE_MEMORY_API and task and task.get("id") is not None:
        if disposition in {"done", "wrong", "wont_do"}:
            from memory_client import set_task_status
            target_status = "done" if disposition == "done" else "dropped"
            status_result = set_task_status(int(task["id"]), target_status)
        elif disposition in {"snoozed", "deferred", "not_today"}:
            from memory_client import set_task_attention
            attention_state = "snoozed" if disposition in {"snoozed", "not_today"} else "deferred"
            status_result = set_task_attention(
                int(task["id"]),
                attention_state,
                reason=payload.reason,
                snooze_until=payload.snooze_until if attention_state == "snoozed" else None,
            )
    record = store.record(
        task_key=task_key,
        task_id=(task or {}).get("id"),
        disposition=disposition,
        reason=payload.reason,
        actor="Greg",
        source="dashboard",
        project=todo.get("project") or (task or {}).get("partOf"),
        description=str(description or ""),
        source_pointer=str(source_pointer or "") or None,
        source_fingerprint_value=str(source_fp or "") or None,
        metadata={
            "dashboard_todo_id": payload.id,
            "memory_task": (task or {}).get("@id"),
            "snooze_until_date": payload.snooze_until,
        },
    )
    capture_flag = None
    if disposition == "wrong" and capture:
        from capture_quality import CaptureQualityStore
        quality_db = Path(os.environ.get("WATSON_CAPTURE_QUALITY_DB", DATA_PATH / "memory" / "watson_capture_quality.sqlite3"))
        capture_flag = CaptureQualityStore(quality_db).set_status(
            capture,
            "wrong_task_source",
            reason=payload.reason or "Task marked wrong in dashboard",
            metadata={"task_key": task_key, "dashboard_todo_id": payload.id},
        )
    return {
        "ok": True,
        "task_key": task_key,
        "memory_task": (task or {}).get("@id"),
        "status_result": status_result,
        "disposition": record,
        "capture_flag": capture_flag,
    }


# --- Legacy Playground dashboard status API (retired) ---

PLAYGROUND_EXTENSIONS_DIR = DATA_PATH / "workspaces" / "main" / "cortex-extensions-retired"
PLAYGROUND_REPORTS_DIR = DATA_PATH / "workspaces" / "watson" / "reports"

async def playground_status_get(secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    raise HTTPException(
        status_code=410,
        detail="Legacy Playground dashboard status is retired. Use Memory-backed Cortex agent snapshots.",
    )


async def playground_status_post(request: Request, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    raise HTTPException(
        status_code=410,
        detail="Legacy Playground dashboard writes are retired. Publish Watson snapshots through Memory.",
    )


async def playground_todos_post(request: Request, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    raise HTTPException(
        status_code=410,
        detail="Legacy Playground todo writes are retired. Update Memory tasks instead.",
    )


async def playground_email_help_post(request: Request, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    """User requests Watson's help with an email."""
    verify_secret(secret)
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(status_code=422, detail="Invalid JSON body")
    if not isinstance(body, dict) or not body.get("email_id"):
        raise HTTPException(status_code=422, detail="email_id is required")

    state = {}
    if PLAYGROUND_STATE_PATH.exists():
        try:
            with open(PLAYGROUND_STATE_PATH, encoding="utf-8") as f:
                state = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    requests_list = state.get("email_help_requests", [])
    if not any(r.get("email_id") == body["email_id"] for r in requests_list):
        requests_list.append({
            "email_id": body["email_id"],
            "request": body.get("request", "Please help me draft a reply"),
            "created_at": datetime.now().isoformat() + "Z",
        })
    state["email_help_requests"] = requests_list
    state["updated_at"] = datetime.now().isoformat() + "Z"

    PLAYGROUND_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = PLAYGROUND_STATE_PATH.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, indent=2, ensure_ascii=False)
    tmp.rename(PLAYGROUND_STATE_PATH)
    return {"ok": True, "email_id": body["email_id"]}


def playground_extensions_get(secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    """Legacy extension widgets are retired; Cortex reads Memory-backed cards."""
    verify_secret(secret)
    return {"extensions": [], "retired": True, "message": "Legacy Playground extensions are retired. Use Cortex agent snapshots."}


class WatsonIdeaPayload(BaseModel):
    id: str | None = None
    kind: str = "idea"
    title: str
    summary: str = ""
    project: str | None = None
    status: str = "pending"
    confidence: float | None = None
    artifact_url: str | None = None
    artifact_path: str | None = None
    source_type: str = "watson"
    source_id: str | None = None
    source_url: str | None = None
    created_by: str = "watson"
    evidence: list[Any] | None = None
    metadata: dict[str, Any] | None = None
    expires_at: str | None = None


class WatsonIdeaFeedbackPayload(BaseModel):
    disposition: str
    reason: str | None = None
    actor: str = "Greg"
    metadata: dict[str, Any] | None = None


def playground_watson_ideas_get(
    status: str | None = Query("pending"),
    project: str | None = None,
    limit: int = Query(50, ge=1, le=200),
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    return {
        "ok": True,
        "ideas": IDEA_STORE.list(status=status or "pending", project=project, limit=limit),
        "contract": {
            "badge": "ai_star",
            "feedback": ["accepted", "dismissed", "wrong", "archived"],
            "source_required": True,
        },
    }


def playground_watson_idea_post(
    payload: WatsonIdeaPayload,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    if not (payload.title or "").strip():
        raise HTTPException(status_code=422, detail="title is required")
    idea = IDEA_STORE.upsert(payload.dict(exclude_none=True))
    return {"ok": True, "idea": idea}


def playground_watson_idea_feedback_post(
    idea_id: str,
    payload: WatsonIdeaFeedbackPayload,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    try:
        idea = IDEA_STORE.record_feedback(
            idea_id=idea_id,
            disposition=payload.disposition,
            reason=payload.reason,
            actor=payload.actor or "Greg",
            metadata=payload.metadata,
        )
    except ValueError as e:
        status_code = 404 if "not found" in str(e).lower() else 422
        raise HTTPException(status_code=status_code, detail=str(e))
    return {"ok": True, "idea": idea}


class PlaygroundBackgroundTaskPayload(BaseModel):
    title: str
    question: str
    project: str | None = None
    output_type: str = "research_report"
    priority: int = 3
    include_email_drafts: bool = True
    target_agent_id: str = "claude-desktop-1"


def _safe_slug(value: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (value or "").lower()).strip("-")
    return slug[:80] or "report"


def _extract_report_spec(result_text: str) -> dict[str, Any] | None:
    text = (result_text or "").strip()
    candidates: list[str] = []
    for match in re.finditer(r"```(?:json)?\s*([\s\S]*?)\s*```", text, re.IGNORECASE):
        candidates.append(match.group(1).strip())
    candidates.append(text)

    decoder = json.JSONDecoder()
    for candidate in candidates:
        starts = [idx for idx, ch in enumerate(candidate) if ch == "{"]
        for start in starts:
            snippet = candidate[start:]
            if "report_spec" not in snippet[:2000]:
                continue
            try:
                data, _ = decoder.raw_decode(snippet)
            except json.JSONDecodeError:
                continue
            spec = data.get("report_spec", data) if isinstance(data, dict) else None
            if isinstance(spec, dict):
                return spec
    return None


def _report_spec_for_task_status(task_status: dict[str, Any]) -> dict[str, Any]:
    results = task_status.get("results", [])
    latest = results[-1] if results else {}
    return _extract_report_spec(str(latest.get("result_text") or "")) or {}


def _as_list(value: Any) -> list[Any]:
    return value if isinstance(value, list) else []


def _as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        try:
            return json.dumps(value, ensure_ascii=False)
        except TypeError:
            return str(value)
    return str(value)


def _report_text_block(text: str) -> str:
    escaped = html.escape(text or "")
    escaped = re.sub(r"\n#{1,3}\s+(.+)", r"\n\n<strong>\1</strong>", escaped)
    escaped = escaped.replace("\n", "<br>")
    return f'<div class="raw-report">{escaped}</div>'


def _first_markdown_heading(text: str) -> str | None:
    for line in (text or "").splitlines():
        match = re.match(r"^\s*#{1,3}\s+(.+?)\s*$", line)
        if match:
            return match.group(1).strip()
    return None


def _legacy_report_title(text: str) -> str | None:
    tag = re.search(r"\[bg-task:([^\]]+)\]", text or "", re.IGNORECASE)
    if tag:
        return tag.group(1).replace("-", " ").replace("_", " ").strip().title()
    return _first_markdown_heading(text)


def _render_report_html(task_status: dict[str, Any]) -> str:
    task = task_status["task"]
    results = task_status.get("results", [])
    latest = results[-1] if results else {}
    result_text = latest.get("result_text", "")
    spec = _extract_report_spec(result_text) or {}
    title = spec.get("title") or task.get("context", {}).get("title") or _legacy_report_title(result_text) or f"Background Task #{task['id']}"
    summary = spec.get("executive_summary") or spec.get("summary") or ""
    report_id = f"bg-task-{task['id']}"

    def esc(value: Any) -> str:
        return html.escape(str(value or ""))

    def render_action_items(items: Any, empty: str = "") -> str:
        if not isinstance(items, list) or not items:
            return f'<p class="muted">{esc(empty)}</p>' if empty else ""
        out = ['<div class="action-list">']
        for idx, item in enumerate(items):
            if isinstance(item, dict):
                label = item.get("title") or item.get("label") or item.get("text") or item.get("finding") or f"Item {idx+1}"
                detail = item.get("detail") or item.get("description") or item.get("why") or item.get("evidence") or ""
                owner = item.get("owner") or ""
            else:
                label, detail, owner = str(item), "", ""
            out.append(
                f'<label class="action-row"><input type="checkbox" data-report-check="{report_id}-action-{idx}">'
                f'<span><strong>{esc(label)}</strong>{f"<small>{esc(owner)}</small>" if owner else ""}{f"<em>{esc(detail)}</em>" if detail else ""}</span></label>'
            )
        out.append("</div>")
        return "".join(out)

    def render_findings(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return '<p class="muted">No structured findings were provided.</p>'
        out = ['<div class="finding-grid">']
        for item in items:
            if isinstance(item, dict):
                title = item.get("title") or item.get("finding") or "Finding"
                detail = item.get("detail") or item.get("description") or ""
                evidence = item.get("evidence") or item.get("source") or ""
            else:
                title, detail, evidence = str(item), "", ""
            out.append(
                '<article class="finding-card">'
                f'<h3>{esc(title)}</h3>'
                f'{f"<p>{esc(detail)}</p>" if detail else ""}'
                f'{f"<blockquote>{esc(evidence)}</blockquote>" if evidence else ""}'
                '</article>'
            )
        out.append("</div>")
        return "".join(out)

    def render_email_drafts(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        out = ['<section id="drafts"><h2>Email Drafts</h2>']
        for idx, draft in enumerate(items):
            if not isinstance(draft, dict):
                continue
            subject = draft.get("subject", "")
            to = draft.get("to", "")
            body = draft.get("body", draft.get("text", ""))
            out.append(
                '<div class="email-draft">'
                f'<div class="draft-meta"><span>To: {esc(to)}</span><span>Subject: {esc(subject)}</span></div>'
                f'<textarea id="draft-{idx}">{esc(body)}</textarea>'
                f'<button data-copy-target="draft-{idx}">Copy draft</button>'
                '</div>'
            )
        out.append("</section>")
        return "".join(out)

    def render_tables(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        out = ['<section id="tables"><h2>Tables</h2>']
        for table in items:
            if not isinstance(table, dict):
                continue
            rows = table.get("rows") or []
            columns = table.get("columns") or (list(rows[0].keys()) if rows and isinstance(rows[0], dict) else [])
            out.append(f'<h3>{esc(table.get("title", ""))}</h3><div class="table-wrap"><table><thead><tr>')
            for col in columns:
                out.append(f"<th>{esc(col)}</th>")
            out.append("</tr></thead><tbody>")
            for row in rows:
                out.append("<tr>")
                for idx, col in enumerate(columns):
                    if isinstance(row, dict):
                        val = row.get(col, "")
                    elif isinstance(row, list):
                        val = row[idx] if idx < len(row) else ""
                    else:
                        val = ""
                    out.append(f"<td>{esc(val)}</td>")
                out.append("</tr>")
            out.append("</tbody></table></div>")
        out.append("</section>")
        return "".join(out)

    def render_charts(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        out = ['<section id="charts"><h2>Charts</h2><div class="charts">']
        for chart in items:
            if not isinstance(chart, dict):
                continue
            data = chart.get("data") or []
            max_val = max([float(d.get("value", 0) or 0) for d in data if isinstance(d, dict)] or [1])
            out.append(f'<div class="chart-card"><h3>{esc(chart.get("title", ""))}</h3>')
            for datum in data:
                if not isinstance(datum, dict):
                    continue
                value = float(datum.get("value", 0) or 0)
                width = max(3, min(100, int((value / max_val) * 100))) if max_val else 3
                out.append(
                    f'<div class="bar-row"><span>{esc(datum.get("label", ""))}</span>'
                    f'<div class="bar-track"><div style="width:{width}%"></div></div>'
                    f'<b>{esc(datum.get("value", ""))}</b></div>'
                )
            out.append("</div>")
        out.append("</div></section>")
        return "".join(out)

    def render_timeline(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        out = ['<section id="timeline"><h2>Timeline</h2><div class="timeline">']
        for item in items:
            if isinstance(item, dict):
                out.append(f'<div><time>{esc(item.get("date") or item.get("time"))}</time><p>{esc(item.get("event") or item.get("text"))}</p></div>')
        out.append("</div></section>")
        return "".join(out)

    def render_sources(items: Any) -> str:
        if not isinstance(items, list) or not items:
            return ""
        out = ['<section id="sources"><h2>Sources</h2><div class="sources">']
        for source in items:
            if isinstance(source, dict):
                label = esc(source.get("label") or source.get("title") or source.get("url"))
                url = source.get("url")
                out.append(f'<details><summary>{label}</summary><p>{esc(source.get("note") or source.get("description"))}</p>{f"<a href=\"{esc(url)}\" target=\"_blank\">Open source</a>" if url else ""}</details>')
            else:
                out.append(f"<details><summary>{esc(source)}</summary></details>")
        out.append("</div></section>")
        return "".join(out)

    body = [
        "<!DOCTYPE html><html><head><meta charset=\"utf-8\"><meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">",
        f"<title>{esc(title)}</title>",
        "<style>",
        "body{margin:0;background:#f5f0e7;color:#171615;font-family:Georgia,'Times New Roman',serif}main{max-width:1160px;margin:0 auto;padding:42px 20px 80px}header{display:grid;grid-template-columns:1fr auto;gap:20px;border-bottom:1px solid #d8cdbb;margin-bottom:28px;padding-bottom:24px}h1{font-size:46px;line-height:1.02;margin:0 0 12px;letter-spacing:-1.7px;max-width:820px}h2{font:900 12px/1.2 system-ui,sans-serif;text-transform:uppercase;letter-spacing:1.5px;margin:36px 0 14px;color:#ad4b23}h3{font:800 16px/1.25 system-ui,sans-serif;margin:0 0 9px}.meta{font:12px ui-monospace,monospace;color:#756f66}.report-nav{display:flex;gap:8px;flex-wrap:wrap;align-content:start;justify-content:flex-end}.report-nav a{font:800 11px system-ui,sans-serif;text-transform:uppercase;letter-spacing:.8px;color:#171615;text-decoration:none;background:#fff;border:1px solid #ded3c2;border-radius:999px;padding:8px 10px}.summary{font-size:20px;line-height:1.6;background:#fffdf8;border:1px solid #ded3c2;border-radius:22px;padding:24px;box-shadow:0 14px 40px rgba(50,35,12,.08)}.raw-report{font:15px/1.65 system-ui,sans-serif;background:#fff;border:1px solid #ded3c2;border-radius:18px;padding:22px}.action-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(280px,1fr));gap:10px}.action-row{display:flex;gap:12px;background:#fff;border:1px solid #ded3c2;border-radius:16px;padding:15px;margin:0;font:14px/1.45 system-ui,sans-serif}.action-row input{margin-top:3px}.action-row small{display:inline-block;margin-left:8px;color:#ad4b23;background:#f5e3d8;border-radius:999px;padding:2px 7px;font:800 10px system-ui,sans-serif;text-transform:uppercase}.action-row em{display:block;color:#625b52;font-style:normal;margin-top:5px}.finding-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.finding-card,.email-draft,.chart-card{background:#fff;border:1px solid #ded3c2;border-radius:18px;padding:18px;box-shadow:0 7px 24px rgba(50,35,12,.045)}.finding-card p{font:14px/1.55 system-ui,sans-serif;color:#38342f;margin:0}.finding-card blockquote{font:12px/1.45 ui-monospace,monospace;color:#756f66;margin:12px 0 0;padding-left:12px;border-left:3px solid #d4552a}.draft-meta{display:flex;gap:14px;flex-wrap:wrap;font:12px ui-monospace,monospace;color:#756f66;margin-bottom:10px}textarea{width:100%;min-height:180px;border:1px solid #d8d0c0;border-radius:12px;padding:12px;font:14px/1.55 system-ui,sans-serif;background:#fffdf8;color:#171615}button{margin-top:10px;border:0;border-radius:999px;background:#171615;color:#fff;padding:9px 14px;font-weight:800;cursor:pointer}.table-wrap{overflow:auto;border:1px solid #ded3c2;border-radius:14px;background:#fff}table{border-collapse:collapse;width:100%;font:14px system-ui,sans-serif}th,td{padding:11px 12px;border-bottom:1px solid #eee6d8;text-align:left;vertical-align:top}th{background:#eee3d2;font-size:11px;text-transform:uppercase;letter-spacing:.7px}.charts{display:grid;grid-template-columns:repeat(auto-fit,minmax(300px,1fr));gap:12px}.bar-row{display:grid;grid-template-columns:150px 1fr 70px;gap:10px;align-items:center;font:13px system-ui,sans-serif;margin:9px 0}.bar-track{height:11px;background:#eee6d8;border-radius:10px;overflow:hidden}.bar-track div{height:100%;background:linear-gradient(90deg,#d4552a,#3d7abf)}.timeline{border-left:2px solid #d4552a;padding-left:18px}.timeline div{margin:0 0 16px}.timeline time{font:12px ui-monospace,monospace;color:#756f66}.timeline p{margin:4px 0 0;font:14px/1.45 system-ui,sans-serif}.sources{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:8px}.sources details{background:#fff;border:1px solid #ded3c2;border-radius:12px;padding:12px;font:14px system-ui,sans-serif}.sources summary{font-weight:800;cursor:pointer}.sources p{color:#625b52;line-height:1.45}.muted{color:#756f66;font:14px system-ui,sans-serif}@media(max-width:760px){header{display:block}h1{font-size:34px}.report-nav{justify-content:flex-start}.bar-row{grid-template-columns:1fr}.action-list,.finding-grid,.charts,.sources{grid-template-columns:1fr}}",
        "</style></head><body><main>",
        f"<header><div><h1>{esc(title)}</h1><div class=\"meta\">Task #{task['id']} · {esc(task.get('status'))} · {esc(task.get('completed_at') or task.get('created_at'))}</div></div><nav class=\"report-nav\"><a href=\"#actions\">Actions</a><a href=\"#findings\">Findings</a><a href=\"#drafts\">Drafts</a><a href=\"#tables\">Tables</a><a href=\"#timeline\">Timeline</a><a href=\"#sources\">Sources</a></nav></header>",
    ]
    if summary:
        body.append(f'<section><h2>Executive Summary</h2><div class="summary">{esc(summary)}</div></section>')
    body.append('<section id="actions"><h2>Action Items</h2>')
    body.append(render_action_items(spec.get("action_items"), "No structured action items were provided."))
    body.append("</section>")
    if spec:
        body.extend([
            '<section id="findings"><h2>Findings</h2>',
            render_findings(spec.get("findings")),
            "</section>",
            render_email_drafts(spec.get("email_drafts")),
            render_tables(spec.get("tables")),
            render_charts(spec.get("charts")),
            render_timeline(spec.get("timeline")),
            render_sources(spec.get("sources")),
        ])
    else:
        body.append("<section><h2>Full Result</h2>")
        body.append(_report_text_block(result_text))
        body.append("</section>")
    body.extend([
        "<script>document.querySelectorAll('[data-copy-target]').forEach(function(b){b.addEventListener('click',function(){var el=document.getElementById(b.getAttribute('data-copy-target'));navigator.clipboard.writeText(el.value);b.textContent='Copied';setTimeout(function(){b.textContent='Copy draft'},1200);});});document.querySelectorAll('[data-report-check]').forEach(function(c){var k='report:'+c.getAttribute('data-report-check');c.checked=localStorage.getItem(k)==='1';c.addEventListener('change',function(){localStorage.setItem(k,c.checked?'1':'0')});});</script>",
        "</main></body></html>",
    ])
    return "".join(body)


def render_background_task_report(task_id: int) -> dict[str, Any]:
    raise ValueError("Legacy Playground report rendering is retired. Store artifacts in Memory/Cortex instead.")


def _record_background_report_idea(
    task_status: dict[str, Any],
    report_url: str,
    report_path: str,
    rendered_at: str,
) -> dict[str, Any]:
    task = task_status["task"]
    results = task_status.get("results", [])
    latest = results[-1] if results else {}
    result_text = str(latest.get("result_text") or "")
    spec = _report_spec_for_task_status(task_status)
    context = task.get("context") or {}
    title = spec.get("title") or context.get("title") or _legacy_report_title(result_text) or f"Background Task #{task['id']}"
    summary = spec.get("executive_summary") or spec.get("summary") or task.get("latest_result_preview") or result_text[:1000]
    evidence = spec.get("sources") or []
    if not evidence:
        evidence = [
            {
                "label": "Background task",
                "note": f"Task #{task['id']} completed by {task.get('target_agent_id') or 'worker'}",
            }
        ]
    metadata = {
        "background_task_id": task.get("id"),
        "target_agent_id": task.get("target_agent_id"),
        "latest_result_status": latest.get("status"),
        "latest_result_at": latest.get("submitted_at"),
        "rendered_at": rendered_at,
        "output_type": context.get("output_type"),
    }
    return IDEA_STORE.upsert(
        {
            "kind": context.get("output_type") or "research_report",
            "title": title,
            "summary": summary,
            "project": context.get("project") or "",
            "artifact_url": report_url,
            "artifact_path": report_path,
            "source_type": "background_task",
            "source_id": str(task.get("id")),
            "created_by": task.get("created_by") or "watson",
            "confidence": (latest.get("metadata") or {}).get("confidence", 0.72),
            "evidence": evidence,
            "metadata": metadata,
        }
    )


def _build_deep_research_prompt(payload: PlaygroundBackgroundTaskPayload) -> str:
    email_line = "Include editable email drafts where useful." if payload.include_email_drafts else "Do not draft emails unless essential."
    return f"""DEEP RESEARCH REQUEST FROM GREG'S PLAYGROUND DASHBOARD

Title: {payload.title}
Project/context: {payload.project or 'general'}
Desired output: {payload.output_type}

Research question:
{payload.question}

Requirements:
1. Do deep research, not a shallow summary. Gather evidence, reconcile conflicting signals, and be explicit about uncertainty.
2. Return a practical report with decisions, evidence, action items, and sources.
3. {email_line}
4. When data is numeric, procedural, or relational, include a table, chart, or timeline.
5. Make the result useful on a portal: concise executive summary, clear next actions, source notes, and any draft communications.
6. Include this exact JSON block near the end so the portal can render widgets:

```json
{{
  "report_spec": {{
    "title": "{payload.title}",
    "executive_summary": "",
    "findings": [{{"title": "", "detail": "", "evidence": ""}}],
    "action_items": [{{"title": "", "detail": "", "owner": "Greg"}}],
    "email_drafts": [{{"to": "", "subject": "", "body": ""}}],
    "tables": [{{"title": "", "columns": [], "rows": []}}],
    "charts": [{{"title": "", "data": [{{"label": "", "value": 0}}]}}],
    "timeline": [{{"date": "", "event": ""}}],
    "sources": [{{"label": "", "url": "", "note": ""}}]
  }}
}}
```
"""


def _task_summary(task: dict[str, Any]) -> dict[str, Any]:
    metadata = task.get("latest_result_metadata") or {}
    report_url = metadata.get("report_url")
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "target_agent_id": task.get("target_agent_id"),
        "created_by": task.get("created_by"),
        "created_at": task.get("created_at"),
        "claimed_at": task.get("claimed_at"),
        "completed_at": task.get("completed_at"),
        "attempts": task.get("attempts"),
        "max_attempts": task.get("max_attempts"),
        "context": task.get("context") or {},
        "title": (task.get("context") or {}).get("title") or f"Task #{task.get('id')}",
        "project": (task.get("context") or {}).get("project"),
        "instructions_preview": str(task.get("instructions") or "")[:600],
        "result_count": task.get("result_count", 0),
        "latest_result_status": task.get("latest_result_status"),
        "latest_result_at": task.get("latest_result_at"),
        "latest_result_preview": task.get("latest_result_preview"),
        "report_url": report_url,
        "target_agent": task.get("target_agent"),
    }


def playground_background_tasks_get(
    limit: int = Query(50, ge=1, le=200),
    status: str | None = None,
    project: str | None = None,
    search: str | None = None,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    tasks = BACKGROUND_QUEUE.list_tasks(limit=limit, status=status, project=project, search=search)
    return {"ok": True, "tasks": [_task_summary(t) for t in tasks]}


def playground_background_task_get(task_id: int, secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    verify_secret(secret)
    try:
        status = BACKGROUND_QUEUE.get_task_status(task_id)
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    task = _task_summary(status["task"])
    results = []
    for result in status.get("results", []):
        metadata = result.get("metadata") or {}
        results.append({
            "status": result.get("status"),
            "agent_id": result.get("agent_id"),
            "submitted_at": result.get("submitted_at"),
            "result_text": result.get("result_text"),
            "metadata": metadata,
            "report_url": metadata.get("report_url") or task.get("report_url"),
        })
    return {"ok": True, "task": task, "results": results}


def playground_background_task_post(
    payload: PlaygroundBackgroundTaskPayload,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    title = (payload.title or "").strip()
    question = (payload.question or "").strip()
    if not title or not question:
        raise HTTPException(status_code=422, detail="title and question are required")
    instructions = _build_deep_research_prompt(payload)
    try:
        task = enqueue_background_task(
            target_agent_id=payload.target_agent_id,
            instructions=instructions,
            privateKey=EXPECTED_SECRET(),
            context={
                "origin": "playground-research",
                "title": title,
                "project": payload.project or "",
                "output_type": payload.output_type,
                "include_email_drafts": payload.include_email_drafts,
            },
            priority=max(1, min(10, int(payload.priority))),
            created_by="playground",
        )["task"]
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))
    return {"ok": True, "task": _task_summary(task)}


def playground_background_task_render_report_post(
    task_id: int,
    secret: str = Depends(APIKeyQuery(name="secret", auto_error=False)),
):
    verify_secret(secret)
    raise HTTPException(
        status_code=410,
        detail="Legacy Playground report rendering is retired. Store artifacts in Memory/Cortex instead.",
    )


app.get("/playground/state")(playground_state_get)
app.get("/playground/addy-live")(playground_addy_live_get)
app.post("/playground/state")(playground_state_post)
app.post("/playground/task-feedback")(playground_task_feedback_post)
app.get("/playground/status")(playground_status_get)
app.post("/playground/status")(playground_status_post)
app.post("/playground/todos")(playground_todos_post)
app.post("/playground/email-help")(playground_email_help_post)
app.get("/playground/extensions")(playground_extensions_get)
app.get("/playground/watson-ideas")(playground_watson_ideas_get)
app.post("/playground/watson-ideas")(playground_watson_idea_post)
app.post("/playground/watson-ideas/{idea_id}/feedback")(playground_watson_idea_feedback_post)
app.get("/playground/background-tasks")(playground_background_tasks_get)
app.post("/playground/background-tasks")(playground_background_task_post)
app.get("/playground/background-tasks/{task_id}")(playground_background_task_get)
app.post("/playground/background-tasks/{task_id}/render-report")(playground_background_task_render_report_post)
