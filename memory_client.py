"""
Memory API client. Watson uses this when USE_MEMORY_API=true.
"""

import os
from datetime import datetime
from typing import Any

import httpx

MEMORY_API_URL = os.environ.get("MEMORY_API_URL", "https://memory.backyardbrains.com")
MEMORY_API_KEY = os.environ.get("MEMORY_API_KEY", "sk_byb_greg_local")

HEADERS = {"X-API-Key": MEMORY_API_KEY, "Content-Type": "application/json"}


def _get(path: str, params: dict | None = None) -> Any:
    r = httpx.get(f"{MEMORY_API_URL}{path}", headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def _post(path: str, json: dict) -> Any:
    r = httpx.post(f"{MEMORY_API_URL}{path}", headers=HEADERS, json=json, timeout=30)
    r.raise_for_status()
    return r.json()


def get_projects() -> list[dict]:
    raw = _get("/v1/projects")
    return [
        {"@id": f"byb:project/{p['slug']}", "title": p["title"], "status": p["status"], "priority": p["priority"]}
        for p in raw
    ]


def get_tasks(project: str | None = None, status: str | None = None, include_complete: bool = True, limit: int = 50) -> list[dict]:
    params: dict = {"include_complete": include_complete, "limit": limit}
    if project:
        params["project_slug"] = project.replace("byb:project/", "")
    if status:
        params["status"] = status
    return _get("/v1/tasks", params)


def get_events(from_date: str | None = None, to_date: str | None = None, limit: int = 50) -> list[dict]:
    params: dict = {"limit": limit}
    if from_date:
        params["from_date"] = from_date
    if to_date:
        params["to_date"] = to_date
    return _get("/v1/events", params)


def get_summary() -> dict:
    projects = get_projects()
    tasks = get_tasks(include_complete=False, limit=15)
    today = datetime.now().strftime("%Y-%m-%d")
    events = get_events(from_date=today, limit=10)

    return {
        "projects": [{"id": p["@id"], "title": p["title"], "status": p["status"], "priority": p["priority"]} for p in projects],
        "open_tasks": [
            {"id": t.get("@id"), "description": t.get("description"), "status": t.get("status"), "dueDate": t.get("dueDate"), "partOf": t.get("partOf")}
            for t in tasks
        ],
        "upcoming_events": [{"id": e.get("@id"), "label": e.get("label"), "date": e.get("date"), "dateTime": e.get("dateTime")} for e in events],
    }


def post_capture(content: str, source: str = "watson", project: str | None = None) -> dict:
    raw = content
    if project:
        raw = f"[project: {project}] {raw}"
    return _post("/v1/captures", {"raw_content": raw, "source": source})


def get_links(project: str | None = None) -> dict:
    params = {}
    if project:
        params["project_slug"] = project.replace("byb:project/", "")
    return _get("/v1/links", params)


def set_task_status(task_id: int, status: str) -> dict:
    r = httpx.patch(
        f"{MEMORY_API_URL}/v1/tasks/{task_id}",
        headers=HEADERS,
        json={"status": status},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()
