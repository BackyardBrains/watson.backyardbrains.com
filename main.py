"""
Watson API — ontology read + inbox write for LLM access.
Base: https://watson.backyardbrains.com
Auth: REST: ?secret= ; MCP: pass privateKey as tool param, or Authorization: Bearer <API_SECRET> / X-API-Key.

When USE_MEMORY_API=true, ontology and inbox come from memory.backyardbrains.com.
Links stay in JSON-LD (memory-api does not store links yet).
"""
import contextvars
import json
import os
from datetime import datetime
from pathlib import Path

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from fastapi import FastAPI, Depends, HTTPException, Query
from fastapi.security import APIKeyQuery
from fastmcp import FastMCP
from mcp.types import ToolAnnotations
from pydantic import BaseModel

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

EXPECTED_SECRET = lambda: os.environ.get("API_SECRET") or os.environ.get("BACKYARDBRAINS_API_KEY")
USE_MEMORY_API = os.environ.get("USE_MEMORY_API", "true").lower() in ("1", "true", "yes")

# When mcporter sends Authorization: Bearer <API_SECRET>, middleware sets this so tools can skip privateKey
_mcp_header_auth_valid: contextvars.ContextVar[bool] = contextvars.ContextVar("mcp_header_auth_valid", default=False)


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


def verify_secret(secret: str = Depends(APIKeyQuery(name="secret", auto_error=False))):
    expected = EXPECTED_SECRET()
    if not expected or secret != expected:
        raise HTTPException(status_code=401, detail="Invalid or missing secret")
    return secret


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
        if "complete" in st or "✅" in st or "deferred" in st:
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
        "service": "Watson API",
        "docs": "https://watson.backyardbrains.com/docs",
        "endpoints": ["/summary", "/projects", "/tasks", "/events", "/links", "/inbox", "/mcp"],
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


def projects():
    if USE_MEMORY_API:
        from memory_client import get_projects
        return {"projects": get_projects()}
    data = load_jsonld(ONTOLOGY / "projects.jsonld")
    return {"projects": data}


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


def inbox(payload: InboxPayload):
    if USE_MEMORY_API:
        from memory_client import post_capture
        post_capture(payload.content, source=payload.source or "watson", project=payload.project)
        return {"ok": True, "message": "Captured to memory-api. Watson will reconcile on next run."}
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
    return {"ok": True, "message": "Appended to inbox. Watson will reconcile on next run."}


# --- MCP Server (auth via privateKey param, no Bearer) ---
mcp = FastMCP("Watson")


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
    """What do I need to do right now? Condensed digest of projects, tasks, events."""
    _verify_private_key(privateKey)
    return _get_summary_data()


@mcp.tool(annotations=READ_ONLY, meta=META_SAFE)
def get_projects(privateKey: str | None = None) -> dict:
    """List all projects with status and priorities."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import get_projects
        return {"projects": get_projects()}
    return {"projects": load_jsonld(ONTOLOGY / "projects.jsonld")}


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
    """Update a task's status. task_id from get_tasks (e.g. 69). status: To Do, In Progress, Complete, Deferred."""
    _verify_private_key(privateKey)
    if USE_MEMORY_API:
        from memory_client import set_task_status as memory_set_task_status
        memory_set_task_status(task_id, status)
        return {"ok": True, "message": f"Task {task_id} status set to {status}"}
    raise ValueError("set_task_status requires USE_MEMORY_API=true (ontology is now in memory-api)")


@mcp.tool(annotations=WRITE_APPEND, meta=META_SAFE)
def post_inbox(
    privateKey: str | None = None,
    content: str = "",
    project: str | None = None,
    source: str | None = None,
) -> dict:
    """Record a note from external LM. Watson reconciles on next run."""
    _verify_private_key(privateKey)
    if not (content or "").strip():
        raise ValueError("content is required and must be non-empty")
    if USE_MEMORY_API:
        from memory_client import post_capture
        post_capture(content, source=source or "watson", project=project)
        return {"ok": True, "message": "Captured to memory-api. Watson will reconcile on next run."}
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
    return {"ok": True, "message": "Appended to inbox. Watson will reconcile on next run."}


mcp_app = mcp.http_app(path="/")


class MCPHeaderAuthMiddleware(BaseHTTPMiddleware):
    """For /mcp requests: accept Authorization: Bearer, X-API-Key, or ?key= query param. ChatGPT connector can't pass headers; use URL with key."""

    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
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
app.add_middleware(MCPHeaderAuthMiddleware)
# Register REST routes on main app
app.get("/")(root)
app.get("/summary", dependencies=[Depends(verify_secret)])(summary)
app.get("/projects", dependencies=[Depends(verify_secret)])(projects)
app.get("/tasks", dependencies=[Depends(verify_secret)])(tasks)
app.get("/events", dependencies=[Depends(verify_secret)])(events)
app.get("/links", dependencies=[Depends(verify_secret)])(links)
app.post("/inbox", dependencies=[Depends(verify_secret)])(inbox)
app.mount("/mcp", mcp_app)
