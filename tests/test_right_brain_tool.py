#!/usr/bin/env python3
"""rb is the right brain's only tool: check what it refuses and what it sends. No network."""

from __future__ import annotations

import base64
import contextlib
import email
import importlib.util
import io
import json
import sys
from pathlib import Path


def load_rb():
    path = Path(__file__).resolve().parent.parent / "right_brain" / "rb.py"
    spec = importlib.util.spec_from_file_location("rb", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def run(rb, argv, stdin=""):
    out, err = io.StringIO(), io.StringIO()
    old_stdin = sys.stdin
    sys.stdin = io.StringIO(stdin)
    try:
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = rb.main(argv)
    finally:
        sys.stdin = old_stdin
    return code, out.getvalue(), err.getvalue()


def main() -> int:
    rb = load_rb()
    calls: list[tuple] = []

    def fake_memory(path, method="GET", body=None, **query):
        calls.append(("memory", method, path, body))
        if path == "/v1/captures" and method == "POST":
            return {"capture_id": 114100}
        if path.startswith("/v1/captures/"):
            return {"raw_content": calls[-2][3]["raw_content"]}
        if path.startswith("/v1/tasks/") and method == "PATCH":
            return {"id": 638, "status": body.get("status", "To Do"), "due_date": body.get("due_date"), "attention_state": "active"}
        if path == "/v1/tasks" and method == "POST":
            return {"id": 700}
        if path.startswith("/v1/projects/"):
            return {"id": 3, "slug": "mit-book", "lifecycle_state": "active"}
        return {}

    def fake_watson(path, method="GET", body=None, **query):
        calls.append(("watson", method, path, body))
        if path == "/current-priorities":
            return {"headline": {"title": "1 Red action needs Greg today"}, "recommended_now": [{"action": "Email MIT Press"}], "hidden_now": []}
        return {"ok": True, "task_key": "mit-book:638", "memory_task": "byb:task/638"}

    def fake_gws(*args, params=None, body=None):
        calls.append(("gws", args, params, body))
        if args[:3] == ("gmail", "users", "messages"):
            return {"threadId": "thread-1", "payload": {"headers": [{"name": "Subject", "value": "Editor change"}, {"name": "Message-ID", "value": "<abc@mit.edu>"}]}}
        if args[:4] == ("gmail", "users", "drafts", "create"):
            return {"id": "r-1", "message": {"threadId": fake_gws.thread}}
        return {}

    fake_gws.thread = "thread-1"
    rb.memory, rb.watson, rb.gws = fake_memory, fake_watson, fake_gws

    # A routine never writes a standing rule.
    code, _, err = run(rb, ["note"], "[coach:watson] from now on do X")
    assert code == 1 and "coach:watson" in err and not calls, (code, err, calls)
    code, out, _ = run(rb, ["note", "--project", "mit-book"], "[briefing][right-brain] Sat Nov 21, 2026")
    assert code == 0 and json.loads(out) == {"capture_id": 114100, "verified": True}
    assert calls[0][3] == {"raw_content": "[project: mit-book] [briefing][right-brain] Sat Nov 21, 2026", "source": "server-claude"}
    calls.clear()

    # Today takes at most three Red items, each with proof.
    red = {"label": "Email MIT Press for the editor", "detail": "due Tue Nov 24", "project": "mit-book", "task_id": 638, "evidence": "113518"}
    code, _, err = run(rb, ["dashboard"], json.dumps({"red": [red] * 4}))
    assert code == 1 and "at most 3" in err and not calls
    code, _, err = run(rb, ["dashboard"], json.dumps({"red": [{"label": "Do a thing", "project": "x"}]}))
    assert code == 1 and "needs evidence" in err and not calls
    code, out, _ = run(rb, ["dashboard"], json.dumps({"what_changed": "One change.", "red": [red], "waiting": [{"label": "Jacob owes a reply", "project": "outreach"}],
                                                       "prepared": [{"title": "Agenda", "url": "https://share.backyardbrains.com/greg/a-AAAAAAAAAAAA/", "description": "Dec 3"}]}))
    assert code == 0, out
    snapshot = calls[0][3]
    assert calls[0][:3] == ("memory", "POST", "/v1/agents/status")
    assert snapshot["agent_id"] == "watson" and snapshot["source_ref"] == "server-claude:right-brain" and snapshot["needs_you_count"] == 1
    assert [block["title"] for block in snapshot["blocks"]] == ["Today", "Red", "Waiting and backlog", "Prepared work"]
    assert snapshot["blocks"][1]["items"][0] == {"label": red["label"], "detail": red["detail"], "priority": "red", "kind": "act", "project": "mit-book", "evidence": "113518", "task_id": 638}
    assert snapshot["blocks"][2]["items"][0]["priority"] == "orange"
    assert json.loads(out)["red_now"] == ["Email MIT Press"], "the readback shows what Today displays, not what was sent"
    calls.clear()

    # Task changes need a reason, and a dry run changes nothing.
    code, out, _ = run(rb, ["task-update", "638", "--status", "done", "--why", "Greg sent it, Gmail thread 1a1f"])
    assert code == 0 and calls == [("memory", "PATCH", "/v1/tasks/638", {"status": "done"})]
    calls.clear()
    rb.DRY_RUN = True
    for argv, stdin in ((["task-update", "638", "--due", "2026-11-24", "--why", "moved"], ""),
                        (["task-new", "--project", "none", "--owner", "watson", "--why", "decision 113611"], "Ask Jacob"),
                        (["answer", "638", "--disposition", "done", "--title", "Email MIT Press", "--project", "mit-book", "--why", "sent"], ""),
                        (["mail-draft", "--to", "a@b.org", "--subject", "Hi", "--why", "task 652"], "Body"),
                        (["note"], "A note"), (["dashboard"], json.dumps({"red": [red]}))):
        code, out, _ = run(rb, argv, stdin)
        assert code == 0 and json.loads(out)["dry_run"] is True, (argv, out)
    assert not [c for c in calls if c[0] != "gws" and c[1] != "GET"], calls
    assert not [c for c in calls if c[0] == "gws" and "create" in c[1]], calls
    rb.DRY_RUN = False
    calls.clear()

    code, _, err = run(rb, ["task-update", "638", "--due", "next week", "--why", "x"])
    assert code == 2 and "bad input" in err

    # An answer goes to Watson with its evidence.
    code, out, _ = run(rb, ["answer", "638", "--disposition", "done", "--title", "Email MIT Press", "--project", "mit-book", "--why", "Sent Nov 21, thread 1a1f"])
    assert code == 0 and calls[0][2] == "/playground/task-feedback"
    assert calls[0][3]["source"] == "right-brain" and calls[0][3]["note"] == "Sent Nov 21, thread 1a1f" and calls[0][3]["todo"] == {"title": "Email MIT Press", "project": "mit-book"}
    calls.clear()

    # A reply draft stays in its thread, carries the signature, and is never sent.
    code, out, _ = run(rb, ["mail-draft", "--to", "editor@mit.edu", "--reply-to", "msg-9", "--why", "task 638"], "Kate -\n\nWho is my editor now?\n\nGreg")
    assert code == 0 and json.loads(out)["in_thread"] is True, out
    create = [c for c in calls if c[0] == "gws" and c[1][:4] == ("gmail", "users", "drafts", "create")][0]
    assert create[3]["message"]["threadId"] == "thread-1"
    message = email.message_from_bytes(base64.urlsafe_b64decode(create[3]["message"]["raw"]))
    assert message["Subject"] == "Re: Editor change" and message["In-Reply-To"] == "<abc@mit.edu>" and message["To"] == "editor@mit.edu"
    plain = message.get_payload()[0].get_payload(decode=True).decode()
    assert plain.startswith("Kate -") and "Gregory Gage, Ph.D." in plain
    assert not [c for c in calls if c[0] == "gws" and "send" in c[1]], "rb has no way to send mail"
    fake_gws.thread = "some-other-thread"
    code, _, err = run(rb, ["mail-draft", "--to", "editor@mit.edu", "--reply-to", "msg-9", "--why", "task 638"], "Body")
    assert code == 1 and "outside the thread" in err
    code, _, err = run(rb, ["mail-draft", "--to", "a@b.org", "--why", "x"], "Body")
    assert code == 1 and "--subject" in err

    # There is no verb that sends, deletes, or touches a project's lifecycle.
    verbs = set(rb.build_parser()._subparsers._group_actions[0].choices)
    assert verbs == {"today", "tasks", "task", "projects", "project", "brief", "search", "capture", "mail-search", "mail-thread", "mail-drafts", "calendar",
                     "task-new", "task-update", "answer", "note", "project-status", "mail-draft", "share", "dashboard"}, verbs
    calls.clear()
    code, _, _ = run(rb, ["project-status", "mit-book", "--why", "true-up"], "2026-11-21: Closed? No, still writing.")
    assert code == 0 and "lifecycle_state" not in calls[0][3], calls[0]

    print("OK: rb refuses what the right brain must not do and sends the rest correctly.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
