#!/usr/bin/env python3
"""
rb: the only tool the right brain may run.

The right brain is a headless Claude session that reads Greg's mail, calendar
and Watson, and keeps Today and his task list true. It reads untrusted text
(email), so it gets no shell: its one permitted command is this program, and
this program can only do what is listed below. It cannot send email, run other
commands, read files, or change a project's lifecycle.

Reads:   today, tasks, task, projects, project, brief, search, capture,
         mail-search, mail-thread, mail-drafts, calendar
Writes:  task-new, task-update, answer, note, project-status, mail-draft,
         share, dashboard

Set RB_DRY_RUN=1 to make every write print what it would do and change nothing.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import stat
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import make_msgid
from pathlib import Path
from typing import Any

WATSON_URL = os.environ.get("RB_WATSON_URL", "http://127.0.0.1:8080")
MEMORY_URL = os.environ.get("RB_MEMORY_URL", "http://127.0.0.1:8002")
WATSON_ENV = Path(os.environ.get("RB_WATSON_ENV", "/var/www/watson.backyardbrains.com/.env"))
MEMORY_CLIENT_DIR = os.environ.get("RB_MEMORY_CLIENT_DIR", "/root/.local/share/byb-memory")
SHARE_ROOT = os.environ.get("RB_SHARE_ROOT", "/var/www/share.backyardbrains.com")
RUN_LOG = os.environ.get("RB_RUN_LOG", "")
DRY_RUN = os.environ.get("RB_DRY_RUN", "") not in ("", "0", "false", "no")
SOURCE = "server-claude"
ACTOR = "right-brain (server Claude)"
DASHBOARD_SOURCE_REF = "server-claude:right-brain"
GREG_ADDRESS = "gagegreg@backyardbrains.com"
MAX_OUTPUT_CHARS = 14000
MAX_RED_ITEMS = 3
TASK_STATUSES = {"open", "in_progress", "blocked", "done", "dropped"}
ANSWERS = {"done", "snoozed", "reopen"}

SIGNATURE_TEXT = """--
Gregory Gage, Ph.D.
Backyard Brains, Inc.
gagegreg@backyardbrains.com
mob: +1 (734) 968-7570
MIT Press book: How Your Brain Works (2022)
<https://mitpress.mit.edu/9780262544382/how-your-brain-works>
Human TED talk: https://www.youtube.com/watch?v=rSQNi5sAwuc
Plant TED talk: http://go.ted.com/greggage
Cockroach TED talk: https://www.ted.com/talks/the_cockroach_beatbox
Neuroscience for Everyone!"""

SIGNATURE_HTML = (
    '<span class="gmail_signature_prefix">-- </span><br><div dir="ltr" class="gmail_signature"><div dir="ltr">'
    "<div>Gregory Gage, Ph.D.</div><div>Backyard Brains, Inc.</div>"
    '<div><a href="mailto:gagegreg@backyardbrains.com" target="_blank">gagegreg@backyardbrains.com</a></div>'
    "<div>mob: +1 (734) 968-7570</div>"
    '<div><b>MIT Press </b>book:  <a href="https://mitpress.mit.edu/9780262544382/how-your-brain-works" target="_blank">How Your Brain Works (2022)</a></div>'
    '<div><div>Human <b style="color:rgb(255,0,0);font-size:12.8px">TED</b> talk: <a href="https://www.youtube.com/watch?v=rSQNi5sAwuc" target="_blank">https://www.youtube.com/watch?v=rSQNi5sAwuc</a></div>'
    '<div>Plant <b style="font-size:12.8px;color:rgb(255,0,0)">TED</b> talk: <a href="http://go.ted.com/greggage" target="_blank">http://go.ted.com/greggage</a> </div>'
    '<div>Cockroach <b style="font-size:12.8px;color:rgb(255,0,0)">TED</b> talk: <a href="https://www.ted.com/talks/the_cockroach_beatbox" target="_blank">https://www.ted.com/talks/the_cockroach_beatbox</a> </div></div>'
    '<div><span style="font-size:12.8px">Neuroscience for Everyone!</span><br></div>'
    '<div><img src="https://cdn.shopify.com/s/files/1/0613/9684/4785/files/email-cockroach.gif?v=1738206608"></div></div></div>'
)


class RbError(Exception):
    pass


# ----------------------------------------------------------------- plumbing

def _env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def _watson_secret() -> str:
    secret = os.environ.get("RB_WATSON_SECRET") or _env_file(WATSON_ENV).get("API_SECRET")
    if not secret:
        raise RbError("Watson secret is not configured")
    return secret


def _memory_key() -> str:
    if os.environ.get("RB_MEMORY_KEY"):
        return os.environ["RB_MEMORY_KEY"]
    sys.path.insert(0, MEMORY_CLIENT_DIR)
    try:
        import watson_memory  # type: ignore
    except ImportError as exc:
        raise RbError("Memory credential is not configured") from exc
    key = watson_memory.credentials()
    if not key:
        raise RbError("Memory credential is not configured")
    return key


def _http(method: str, url: str, headers: dict[str, str], body: Any = None, timeout: int = 90) -> Any:
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", **headers})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")[:400]
        raise RbError(f"{method} {urllib.parse.urlsplit(url).path} failed with HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RbError(f"{method} {urllib.parse.urlsplit(url).path} could not be reached: {exc.reason}") from exc


def watson(path: str, method: str = "GET", body: Any = None, **query: Any) -> Any:
    params = {key: value for key, value in query.items() if value is not None}
    params["secret"] = _watson_secret()
    return _http(method, f"{WATSON_URL}{path}?{urllib.parse.urlencode(params)}", {}, body)


def memory(path: str, method: str = "GET", body: Any = None, **query: Any) -> Any:
    params = {key: value for key, value in query.items() if value is not None}
    url = f"{MEMORY_URL}{path}" + (f"?{urllib.parse.urlencode(params)}" if params else "")
    return _http(method, url, {"X-API-Key": _memory_key()}, body)


def gws(*args: str, params: dict | None = None, body: dict | None = None) -> Any:
    command = ["gws", *args]
    if params is not None:
        command += ["--params", json.dumps(params)]
    if body is not None:
        command += ["--json", json.dumps(body)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=120, env={**os.environ, "HOME": "/root"})
    output = result.stdout
    start = min([i for i in (output.find("{"), output.find("[")) if i >= 0], default=-1)
    if result.returncode != 0 or start < 0:
        raise RbError(f"gws {' '.join(args[:4])} failed: {(result.stderr or output).strip()[:300]}")
    return json.loads(output[start:])


def log(verb: str, detail: dict[str, Any]) -> None:
    if not RUN_LOG:
        return
    with open(RUN_LOG, "a", encoding="utf-8") as handle:
        handle.write(json.dumps({"at": datetime.now(timezone.utc).isoformat(timespec="seconds"), "verb": verb, "dry_run": DRY_RUN, **detail}, default=str) + "\n")


def emit(value: Any) -> None:
    text = value if isinstance(value, str) else json.dumps(value, indent=1, default=str, ensure_ascii=False)
    if len(text) > MAX_OUTPUT_CHARS:
        text = text[:MAX_OUTPUT_CHARS] + f"\n… cut at {MAX_OUTPUT_CHARS} characters; narrow the request."
    print(text)


def stdin_text() -> str:
    if sys.stdin.isatty():
        return ""
    try:
        info = os.fstat(sys.stdin.fileno())
    except (OSError, ValueError):
        info = None
    if info and not stat.S_ISFIFO(info.st_mode) and not (stat.S_ISREG(info.st_mode) and info.st_nlink == 0):
        # `rb note < /some/file` would copy a file from this server into a note
        # or a draft. Text comes from a heredoc or a pipe, never from a file.
        raise RbError("text must come from a heredoc, not from a file")
    return sys.stdin.read().strip()


def first_line(text: str, limit: int = 150) -> str:
    line = next((part.strip() for part in str(text or "").splitlines() if part.strip()), "")
    return line if len(line) <= limit else line[: limit - 1] + "…"


def valid_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


# -------------------------------------------------------------------- reads

def cmd_today(args: argparse.Namespace) -> None:
    data = watson("/current-priorities")
    emit({
        "headline": (data.get("headline") or {}).get("title"),
        "packet": data.get("today_packet"),
        "red_now": [{k: row.get(k) for k in ("action", "project", "task_id", "task_key", "description", "evidence", "reason")} for row in data.get("recommended_now", [])],
        "hidden_now": data.get("hidden_now", []),
    })


def _task_line(task: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": task.get("id"),
        "status": task.get("status"),
        "due": str(task.get("dueDate") or task.get("due_date") or "")[:10] or None,
        "owner": task.get("owner") or "greg",
        "project": str(task.get("partOf") or "").replace("byb:project/", "") or None,
        "task": first_line(task.get("description")),
    }


def cmd_tasks(args: argparse.Namespace) -> None:
    tasks = watson("/tasks", include_complete="false", limit=200, project=args.project)["tasks"]
    parked = [task for task in tasks if str(task.get("status")) in {"deferred", "snoozed"}]
    if not args.all:
        tasks = [task for task in tasks if task not in parked]
    if args.owner:
        tasks = [task for task in tasks if (task.get("owner") or "greg").lower() == args.owner]
    if args.due_within is not None:
        horizon = (date.today() + timedelta(days=args.due_within)).isoformat()
        tasks = [task for task in tasks if str(task.get("dueDate") or "")[:10] and str(task.get("dueDate"))[:10] <= horizon]
    lines = [f"{len(tasks)} open tasks" + ("" if args.all else f" ({len(parked)} deferred or snoozed ones hidden; --all shows them)"),
             "id | status | due | owner | project | task"]
    for row in (_task_line(task) for task in tasks):
        lines.append(f"{row['id']} | {row['status']} | {row['due'] or '-'} | {row['owner']} | {row['project'] or '-'} | {row['task'][:110]}")
    emit("\n".join(lines))


def cmd_task(args: argparse.Namespace) -> None:
    emit(memory(f"/v1/tasks/{int(args.id)}"))


def cmd_projects(args: argparse.Namespace) -> None:
    rows = memory("/v1/projects")
    emit([{k: (str(row.get(k))[:260] if k == "status" else row.get(k)) for k in ("slug", "title", "priority", "waiting_on", "last_activity_at", "status")} for row in rows])


def cmd_project(args: argparse.Namespace) -> None:
    emit(memory(f"/v1/projects/{args.slug}"))


def cmd_brief(args: argparse.Namespace) -> None:
    emit(memory("/v1/brief", q=args.question, project_slug=args.project, client="right-brain"))


def cmd_search(args: argparse.Namespace) -> None:
    rows = memory("/v1/search", q=args.query, limit=min(args.limit, 25), project_slug=args.project)
    emit([{"capture_id": row.get("capture_id"), "created_at": str(row.get("created_at"))[:16], "source": row.get("source"),
           "text": (row.get("content") or row.get("raw_content") or "")[:args.chars]} for row in rows])


def cmd_capture(args: argparse.Namespace) -> None:
    row = memory(f"/v1/captures/{int(args.id)}")
    emit({k: row.get(k) for k in ("capture_id", "created_at", "source", "project_slug", "memory_status", "raw_content")})


def _headers(message: dict[str, Any]) -> dict[str, str]:
    return {h["name"].lower(): h["value"] for h in (message.get("payload") or {}).get("headers", [])}


def _plain_body(payload: dict[str, Any]) -> str:
    if payload.get("mimeType") == "text/plain" and (payload.get("body") or {}).get("data"):
        return base64.urlsafe_b64decode(payload["body"]["data"] + "==").decode("utf-8", "replace")
    for part in payload.get("parts") or []:
        text = _plain_body(part)
        if text:
            return text
    return ""


def cmd_mail_search(args: argparse.Namespace) -> None:
    listing = gws("gmail", "users", "threads", "list", params={"userId": "me", "q": args.query, "maxResults": min(args.max, 20)})
    threads = []
    for item in listing.get("threads", []):
        thread = gws("gmail", "users", "threads", "get", params={"userId": "me", "id": item["id"], "format": "metadata", "metadataHeaders": ["From", "To", "Subject", "Date"]})
        messages = thread.get("messages", [])
        last = _headers(messages[-1]) if messages else {}
        threads.append({"thread_id": item["id"], "messages": len(messages), "subject": last.get("subject"), "last_from": last.get("from"),
                        "last_date": last.get("date"), "last_is_sent_by_greg": "SENT" in (messages[-1].get("labelIds") or []) if messages else None,
                        "snippet": (item.get("snippet") or "")[:160]})
    emit({"query": args.query, "threads": threads})


def cmd_mail_thread(args: argparse.Namespace) -> None:
    thread = gws("gmail", "users", "threads", "get", params={"userId": "me", "id": args.id, "format": "full"})
    messages = []
    for message in thread.get("messages", []):
        headers = _headers(message)
        body = re.sub(r"\n(?:>.*\n?)+", "\n[quoted text cut]\n", _plain_body(message.get("payload") or {}))
        messages.append({"message_id": message.get("id"), "from": headers.get("from"), "to": headers.get("to"), "date": headers.get("date"),
                         "subject": headers.get("subject"), "sent_by_greg": "SENT" in (message.get("labelIds") or []),
                         "draft": "DRAFT" in (message.get("labelIds") or []), "body": body.strip()[:args.chars]})
    emit({"thread_id": args.id, "messages": messages})


def cmd_mail_drafts(args: argparse.Namespace) -> None:
    listing = gws("gmail", "users", "drafts", "list", params={"userId": "me", "maxResults": min(args.max, 25)})
    drafts = []
    for item in listing.get("drafts", []):
        draft = gws("gmail", "users", "drafts", "get", params={"userId": "me", "id": item["id"], "format": "metadata"})
        headers = _headers(draft.get("message") or {})
        internal = (draft.get("message") or {}).get("internalDate")
        drafts.append({"draft_id": item["id"], "thread_id": (draft.get("message") or {}).get("threadId"), "to": headers.get("to"), "subject": headers.get("subject"),
                       "saved": datetime.fromtimestamp(int(internal) / 1000, timezone.utc).isoformat(timespec="minutes") if internal else None})
    emit({"drafts": drafts})


def cmd_calendar(args: argparse.Namespace) -> None:
    start = datetime.now(timezone.utc)
    events = gws("calendar", "events", "list", params={"calendarId": "primary", "timeMin": start.isoformat(timespec="seconds"),
                 "timeMax": (start + timedelta(days=min(args.days, 31))).isoformat(timespec="seconds"), "singleEvents": True, "orderBy": "startTime", "maxResults": 80})
    emit([{"when": (event.get("start") or {}).get("dateTime") or (event.get("start") or {}).get("date"), "what": event.get("summary"),
           "where": event.get("location"), "with": [a.get("email") for a in event.get("attendees") or [] if not a.get("self")][:6],
           "notes": (event.get("description") or "")[:200]} for event in events.get("items", [])])


# ------------------------------------------------------------------- writes

def _project_id(slug: str | None) -> int | None:
    if not slug or slug == "none":
        return None
    return memory(f"/v1/projects/{slug}")["id"]


def cmd_task_new(args: argparse.Namespace) -> None:
    text = stdin_text()
    if not text:
        raise RbError("put the task text on stdin")
    description = text if args.owner == "greg" else f"{text}\n\nOwner: {args.owner}"
    body = {"description": description, "status": "To Do", "project_id": _project_id(args.project), "due_date": valid_date(args.due) if args.due else None}
    log("task-new", {"project": args.project, "due": args.due, "owner": args.owner, "task": first_line(text), "why": args.why})
    if DRY_RUN:
        return emit({"dry_run": True, "would_create": body})
    created = memory("/v1/tasks", method="POST", body=body)
    emit({"created_task": created.get("id"), "task": first_line(text)})


def cmd_task_update(args: argparse.Namespace) -> None:
    patch: dict[str, Any] = {}
    if args.status:
        patch["status"] = args.status
    if args.due:
        patch["due_date"] = None if args.due == "none" else valid_date(args.due)
    if args.project:
        patch["project_slug"] = None if args.project == "none" else args.project
    if args.snooze:
        patch.update(attention_state="snoozed", snooze_until=valid_date(args.snooze), attention_reason=args.why)
    if args.defer:
        patch.update(attention_state="deferred", attention_reason=args.why)
    if args.text:
        text = stdin_text()
        if not text:
            raise RbError("--text needs the new task text on stdin")
        patch["description"] = text
    if not patch:
        raise RbError("nothing to change")
    log("task-update", {"task": args.id, "patch": {k: (first_line(v) if k == "description" else v) for k, v in patch.items()}, "why": args.why})
    if DRY_RUN:
        return emit({"dry_run": True, "task": args.id, "would_patch": patch})
    updated = memory(f"/v1/tasks/{int(args.id)}", method="PATCH", body=patch)
    emit({"task": updated.get("id"), "status": updated.get("status"), "due": str(updated.get("due_date"))[:10], "attention": updated.get("attention_state")})


def cmd_answer(args: argparse.Namespace) -> None:
    body = {"id": args.item, "disposition": args.disposition, "note": args.why, "reason": args.why, "snooze_until": valid_date(args.until) if args.until else None,
            "actor": ACTOR, "source": "right-brain", "todo": {"title": args.title, "project": args.project}}
    log("answer", {"item": args.item, "disposition": args.disposition, "why": args.why})
    if DRY_RUN:
        return emit({"dry_run": True, "would_answer": body})
    result = watson("/playground/task-feedback", method="POST", body=body)
    emit({"ok": result.get("ok"), "task_key": result.get("task_key"), "memory_task": result.get("memory_task")})


def cmd_note(args: argparse.Namespace) -> None:
    text = stdin_text()
    if not text:
        raise RbError("put the note on stdin")
    if re.search(r"coach:watson|\[(standing-rules|memory-protocol|greg-vocabulary)\]", text, re.I):
        # These tags mark Greg's standing rules for every assistant. Only his
        # own corrections belong there; a routine never writes one.
        raise RbError("a routine note must not carry a standing-rule tag")
    if args.project and not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", args.project):
        raise RbError("--project takes a project slug")
    # Saved as plain knowledge whatever the text says: Memory would otherwise
    # read tags in the text and could file the note as a rule.
    body = {"raw_content": text, "source": SOURCE, "memory_visibility": "knowledge"}
    if args.project:
        body["project_slug"] = args.project
    log("note", {"project": args.project, "note": first_line(text)})
    if DRY_RUN:
        return emit({"dry_run": True, "would_capture": {**body, "raw_content": body["raw_content"][:400]}})
    saved = memory("/v1/captures", method="POST", body=body)
    capture_id = saved.get("capture_id") or saved.get("id")
    check = memory(f"/v1/captures/{capture_id}")
    emit({"capture_id": capture_id, "verified": check.get("raw_content") == body["raw_content"]})


def cmd_project_status(args: argparse.Namespace) -> None:
    text = stdin_text()
    if not text:
        raise RbError("put the new status text on stdin")
    patch: dict[str, Any] = {"status": text, "last_activity_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                             "revision_reason": args.why, "revision_actor": "server-claude:right-brain"}
    if args.waiting_on is not None:
        patch["waiting_on"] = args.waiting_on or None
    log("project-status", {"project": args.slug, "status": first_line(text), "why": args.why})
    if DRY_RUN:
        return emit({"dry_run": True, "project": args.slug, "would_patch": patch})
    updated = memory(f"/v1/projects/{args.slug}", method="PATCH", body=patch)
    emit({"project": updated.get("slug"), "lifecycle_state": updated.get("lifecycle_state"), "warnings": updated.get("warnings")})


def cmd_mail_draft(args: argparse.Namespace) -> None:
    text = stdin_text()
    if not text:
        raise RbError("put the email body on stdin")
    message = EmailMessage()
    message["From"] = GREG_ADDRESS
    message["To"] = ", ".join(args.to)
    if args.cc:
        message["Cc"] = ", ".join(args.cc)
    thread_id = None
    subject = args.subject or ""
    if args.reply_to:
        original = gws("gmail", "users", "messages", "get", params={"userId": "me", "id": args.reply_to, "format": "metadata", "metadataHeaders": ["Subject", "Message-ID", "References"]})
        headers = _headers(original)
        thread_id = original.get("threadId")
        original_subject = headers.get("subject") or ""
        subject = original_subject if original_subject.lower().startswith("re:") else f"Re: {original_subject}"
        if headers.get("message-id"):
            message["In-Reply-To"] = headers["message-id"]
            message["References"] = " ".join(part for part in (headers.get("references"), headers["message-id"]) if part)
    if not subject:
        raise RbError("a new email needs --subject")
    message["Subject"] = subject
    message["Message-ID"] = make_msgid(domain="backyardbrains.com")
    message.set_content(f"{text}\n\n{SIGNATURE_TEXT}\n")
    paragraphs = "".join(f"<div>{line or '<br>'}</div>" for line in (escape_html(line) for line in text.splitlines()))
    message.add_alternative(f"{paragraphs}<div><br></div>{SIGNATURE_HTML}", subtype="html")
    raw = base64.urlsafe_b64encode(message.as_bytes()).decode()
    log("mail-draft", {"to": args.to, "subject": subject, "reply_to": args.reply_to, "why": args.why})
    if DRY_RUN:
        return emit({"dry_run": True, "would_draft": {"to": args.to, "subject": subject, "thread_id": thread_id, "body": text}})
    draft = gws("gmail", "users", "drafts", "create", params={"userId": "me"}, body={"message": {"raw": raw, **({"threadId": thread_id} if thread_id else {})}})
    created_thread = (draft.get("message") or {}).get("threadId")
    if thread_id and created_thread != thread_id:
        raise RbError(f"draft {draft.get('id')} landed outside the thread ({created_thread}); delete it and tell Greg")
    emit({"draft_id": draft.get("id"), "thread_id": created_thread, "in_thread": bool(thread_id), "note": "saved in Gmail drafts; not sent"})


def escape_html(text: str) -> str:
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def cmd_share(args: argparse.Namespace) -> None:
    text = stdin_text()
    if not text:
        raise RbError("put the document (Markdown) on stdin")
    log("share", {"title": args.title, "page_id": args.page_id})
    if DRY_RUN:
        return emit({"dry_run": True, "would_publish": {"title": args.title, "characters": len(text)}})
    os.environ["WATSON_SHARE_ROOT"] = SHARE_ROOT
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    import share_publish  # type: ignore
    result = share_publish.publish_page(args.title, text, page_id=args.page_id, actor="right-brain")
    folder = Path(SHARE_ROOT) / share_publish.OWNER_FOLDER / result["page_id"]
    owner = folder.parent.stat()
    for path in [folder, *folder.iterdir()]:  # keep the page editable by the Watson service account
        if not path.is_symlink():
            os.chown(path, owner.st_uid, owner.st_gid, follow_symlinks=False)
    emit(result)


def _https_link(url: str) -> str:
    if not re.fullmatch(r"https://[^\s<>\"']+", url):
        raise RbError(f"a link must be an https:// address: {url[:80]!r}")
    return url


def _action_items(rows: list[dict[str, Any]], priority: str) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        label = " ".join(str(row.get("label") or "").split())
        if not label:
            raise RbError("every item needs a label")
        item: dict[str, Any] = {"label": label, "detail": str(row.get("detail") or ""), "priority": priority, "kind": "act",
                                "project": str(row.get("project") or "general"), "evidence": str(row.get("evidence") or "")}
        if item["evidence"] and not re.fullmatch(r"\d{1,9}(\s*,\s*\d{1,9})*", item["evidence"]):
            raise RbError(f"evidence for {label!r} must be capture ids separated by commas")
        if row.get("task_id"):
            task_id = str(row["task_id"]).lstrip("#Tt ")
            if not task_id.isdigit() or int(task_id) < 1:
                raise RbError(f"task for {label!r} must be a task id")
            item["task_id"] = int(task_id)
        if row.get("action_url"):
            item["action_url"] = _https_link(str(row["action_url"]))
        items.append(item)
    return items


DASHBOARD_SECTIONS = {"RED": "red", "WAITING": "waiting", "PREPARED": "prepared"}
DASHBOARD_FIELDS = {"task": "task_id", "project": "project", "why": "detail", "evidence": "evidence", "link": "action_url"}


def parse_dashboard(text: str) -> dict[str, Any]:
    """Read the plain-line dashboard format.

        CHANGED: what changed, two or three sentences
        RED: Imperative action for Greg
          task: 638
          project: mit-book
          why: one line on why today
          evidence: 113664, 113518
          link: https://...
        WAITING: Who owes what          (same fields)
        PREPARED: What it is            (link and why)

    Plain lines rather than JSON: the session's sandbox refuses a heredoc that
    contains braces.
    """
    plan: dict[str, Any] = {"what_changed": "", "red": [], "waiting": [], "prepared": []}
    current: dict[str, Any] | None = None
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        head, _, rest = line.partition(":")
        key = head.strip()
        value = rest.strip()
        if key.upper() == "CHANGED":
            plan["what_changed"] = value
            current = None
        elif key.upper() in DASHBOARD_SECTIONS:
            current = {"label": value}
            plan[DASHBOARD_SECTIONS[key.upper()]].append(current)
        elif current is not None and key.lower() in DASHBOARD_FIELDS:
            current[DASHBOARD_FIELDS[key.lower()]] = value
        elif current is None and plan["what_changed"] and not plan["red"]:
            plan["what_changed"] += " " + line  # a CHANGED sentence that wrapped
        else:
            raise RbError(f"dashboard line not understood: {line[:80]!r}")
    for item in plan["prepared"]:
        item["title"], item["url"], item["description"] = item.pop("label"), item.pop("action_url", ""), item.pop("detail", "")
        if item["url"]:
            _https_link(item["url"])
    return plan


def cmd_dashboard(args: argparse.Namespace) -> None:
    plan = parse_dashboard(stdin_text())
    red = plan.get("red") or []
    if len(red) > MAX_RED_ITEMS:
        raise RbError(f"at most {MAX_RED_ITEMS} Red items; put the rest under waiting")
    for row in red:
        if not str(row.get("evidence") or "").strip() and not row.get("task_id"):
            raise RbError(f"Red item {row.get('label')!r} needs evidence capture ids or a task_id")
    now = datetime.now(timezone.utc).replace(microsecond=0)
    blocks: list[dict[str, Any]] = [{"type": "text", "placement": "summary", "title": "Today", "body": str(plan.get("what_changed") or "")[:1500]}]
    blocks.append({"type": "actions", "placement": "detail", "title": "Red", "items": _action_items(red, "red")})
    if plan.get("waiting"):
        blocks.append({"type": "actions", "placement": "detail", "title": "Waiting and backlog", "items": _action_items(plan["waiting"], "orange")})
    if plan.get("prepared"):
        blocks.append({"type": "document", "placement": "detail", "title": "Prepared work",
                       "items": [{"title": str(d.get("title") or ""), "url": str(d.get("url") or ""), "description": str(d.get("description") or "")} for d in plan["prepared"]]})
    snapshot = {"agent_id": "watson", "dashboard_version": f"watson:{now.isoformat().replace('+00:00', 'Z')}", "generated_at": now.isoformat().replace("+00:00", "Z"),
                "expires_at": (now + timedelta(hours=36)).isoformat().replace("+00:00", "Z"), "status": "needs_you" if red else "healthy",
                "needs_you_count": len(red), "source_ref": DASHBOARD_SOURCE_REF, "blocks": blocks}
    log("dashboard", {"red": [row.get("label") for row in red], "waiting": len(plan.get("waiting") or []), "prepared": len(plan.get("prepared") or [])})
    if DRY_RUN:
        return emit({"dry_run": True, "would_publish": snapshot})
    memory("/v1/agents/status", method="POST", body=snapshot)
    readback = watson("/current-priorities")
    emit({"published": snapshot["dashboard_version"], "headline_now": (readback.get("headline") or {}).get("title"),
          "red_now": [row.get("action") for row in readback.get("recommended_now", [])], "hidden_now": readback.get("hidden_now", [])})


# --------------------------------------------------------------------- main

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="rb", description="Right-brain tool: read Watson, mail and calendar; write tasks, answers, notes, drafts and Today.")
    sub = parser.add_subparsers(dest="verb", required=True)

    sub.add_parser("today", help="Red items on Today now, and what is hidden and why").set_defaults(run=cmd_today)
    p = sub.add_parser("tasks", help="open tasks"); p.add_argument("--project"); p.add_argument("--owner", choices=["greg", "watson"]); p.add_argument("--due-within", type=int); p.add_argument("--all", action="store_true"); p.set_defaults(run=cmd_tasks)
    p = sub.add_parser("task", help="one task in full"); p.add_argument("id"); p.set_defaults(run=cmd_task)
    sub.add_parser("projects", help="active projects").set_defaults(run=cmd_projects)
    p = sub.add_parser("project", help="one project row"); p.add_argument("slug"); p.set_defaults(run=cmd_project)
    p = sub.add_parser("brief", help="standing rules, vocabulary and records for a question"); p.add_argument("question"); p.add_argument("--project"); p.set_defaults(run=cmd_brief)
    p = sub.add_parser("search", help="search Watson memory"); p.add_argument("query"); p.add_argument("--limit", type=int, default=8); p.add_argument("--project"); p.add_argument("--chars", type=int, default=500); p.set_defaults(run=cmd_search)
    p = sub.add_parser("capture", help="one memory capture in full"); p.add_argument("id"); p.set_defaults(run=cmd_capture)
    p = sub.add_parser("mail-search", help="Gmail search (Gmail query syntax)"); p.add_argument("query"); p.add_argument("--max", type=int, default=10); p.set_defaults(run=cmd_mail_search)
    p = sub.add_parser("mail-thread", help="every message in one Gmail thread"); p.add_argument("id"); p.add_argument("--chars", type=int, default=2500); p.set_defaults(run=cmd_mail_thread)
    p = sub.add_parser("mail-drafts", help="drafts waiting in Gmail"); p.add_argument("--max", type=int, default=15); p.set_defaults(run=cmd_mail_drafts)
    p = sub.add_parser("calendar", help="events in the next N days"); p.add_argument("--days", type=int, default=14); p.set_defaults(run=cmd_calendar)

    p = sub.add_parser("task-new", help="create a task (text on stdin)"); p.add_argument("--project", required=True); p.add_argument("--due"); p.add_argument("--owner", choices=["greg", "watson"], default="greg"); p.add_argument("--why", required=True); p.set_defaults(run=cmd_task_new)
    p = sub.add_parser("task-update", help="change a task"); p.add_argument("id"); p.add_argument("--status", choices=sorted(TASK_STATUSES)); p.add_argument("--due"); p.add_argument("--project"); p.add_argument("--snooze"); p.add_argument("--defer", action="store_true"); p.add_argument("--text", action="store_true"); p.add_argument("--why", required=True); p.set_defaults(run=cmd_task_update)
    p = sub.add_parser("answer", help="mark a Today item done, snoozed or reopened, with evidence"); p.add_argument("item"); p.add_argument("--disposition", required=True, choices=sorted(ANSWERS)); p.add_argument("--title", required=True); p.add_argument("--project", required=True); p.add_argument("--until"); p.add_argument("--why", required=True); p.set_defaults(run=cmd_answer)
    p = sub.add_parser("note", help="save a note to Watson memory (text on stdin)"); p.add_argument("--project"); p.set_defaults(run=cmd_note)
    p = sub.add_parser("project-status", help="replace a project's status text (text on stdin)"); p.add_argument("slug"); p.add_argument("--waiting-on"); p.add_argument("--why", required=True); p.set_defaults(run=cmd_project_status)
    p = sub.add_parser("mail-draft", help="save a draft in Greg's Gmail (body on stdin); never sends"); p.add_argument("--to", nargs="+", required=True); p.add_argument("--cc", nargs="*"); p.add_argument("--subject"); p.add_argument("--reply-to"); p.add_argument("--why", required=True); p.set_defaults(run=cmd_mail_draft)
    p = sub.add_parser("share", help="publish a document to Greg's share site (Markdown on stdin)"); p.add_argument("--title", required=True); p.add_argument("--page-id"); p.set_defaults(run=cmd_share)
    sub.add_parser("dashboard", help="publish Today (plain lines on stdin: CHANGED / RED / WAITING / PREPARED) and read it back").set_defaults(run=cmd_dashboard)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        args.run(args)
    except RbError as exc:
        log(args.verb, {"error": str(exc)})
        print(f"rb {args.verb}: {exc}", file=sys.stderr)
        return 1
    except (ValueError, KeyError) as exc:
        log(args.verb, {"error": f"{type(exc).__name__}: {exc}"})
        print(f"rb {args.verb}: bad input: {exc}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
