#!/usr/bin/env python3
"""
Regression test: an answer Greg gives on a Today item has to stick.

Covers the failures behind the November 2026 "I marked it done and it is red
again" reports: answers that never reached the disposition store, answers lost
when the next dashboard packet repeated the item, decisions left as notes with
no owner, open tasks hidden by a status-vocabulary filter, and live projects
hidden because their status prose contained a word like "merged".

Uses a temporary state directory and an in-memory stand-in for the Memory API.
"""

from __future__ import annotations

import os
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


class FakeMemory:
    def __init__(self) -> None:
        self.tasks: dict[int, dict] = {}
        self.projects: dict[str, dict] = {}
        self.captures: list[dict] = []
        self.snapshot: dict | None = None
        self.task_queries: list[dict] = []
        self.next_task_id = 700
        self.next_capture_id = 114000

    def add_task(self, task_id: int, description: str, status: str = "To Do", **extra) -> dict:
        row = {"id": task_id, "description": description, "status": status, "attention_state": "active",
               "snooze_until": None, "due_date": None, "partOf": None, "@id": f"byb:task/{task_id}", **extra}
        self.tasks[task_id] = row
        return row

    def get(self, path: str, params: dict | None = None):
        params = params or {}
        if path == "/v1/projects":
            state = params.get("state") or ("all" if params.get("include_closed") else "active")
            if state in {"all", "any"}:
                return list(self.projects.values())
            return [row for row in self.projects.values() if row["lifecycle_state"] == state]
        if path.startswith("/v1/projects/"):
            slug = path.rsplit("/", 1)[1]
            if slug not in self.projects:
                raise RuntimeError("404")
            return self.projects[slug]
        if path == "/v1/tasks":
            self.task_queries.append(dict(params))
            rows = list(self.tasks.values())
            if params.get("include_complete") is False:
                rows = [row for row in rows if not any(word in row["status"].lower() for word in ("done", "complete", "dropped", "closed"))]
            if params.get("status"):
                rows = [row for row in rows if str(params["status"]).lower() in row["status"].lower()]
            return rows[: int(params.get("limit") or 50)]
        if path.startswith("/v1/tasks/"):
            task_id = int(path.rsplit("/", 1)[1])
            if task_id not in self.tasks:
                raise RuntimeError("404")
            return self.tasks[task_id]
        if path == "/v1/agents/status/latest":
            return {"statuses": [self.snapshot] if self.snapshot else []}
        if path == "/v1/search":
            return [{"capture_id": self.next_capture_id - 1, "id": self.next_capture_id - 1}]
        if path == "/v1/events":
            return []
        raise AssertionError(f"unexpected GET {path}")

    def post(self, path: str, json: dict, params: dict | None = None, timeout: int = 30):
        if path == "/v1/captures":
            capture = {"capture_id": self.next_capture_id, "id": self.next_capture_id, **json}
            self.next_capture_id += 1
            self.captures.append(capture)
            return capture
        if path == "/v1/tasks":
            row = self.add_task(self.next_task_id, json["description"], json.get("status") or "To Do")
            row["project_id"] = json.get("project_id")
            self.next_task_id += 1
            return row
        raise AssertionError(f"unexpected POST {path}")

    def patch(self, url: str, headers=None, json=None, timeout=30):
        task_id = int(url.rsplit("/", 1)[1])
        self.tasks[task_id].update(json or {})
        row = self.tasks[task_id]

        class Response:
            def raise_for_status(self) -> None:
                return None

            def json(self) -> dict:
                return row

        return Response()


def red_packet(items: list[dict], generated_at: str | None = None) -> dict:
    stamp = generated_at or datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
    return {"agent_id": "watson", "generated_at": stamp, "blocks": [{"type": "actions", "title": "Red", "items": items}]}


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_PATH"] = tmp
        os.environ["WATSON_CAPTURE_QUALITY_DB"] = str(Path(tmp) / "quality.sqlite3")
        os.environ["WATSON_TASK_STATE_DB"] = str(Path(tmp) / "task_state.sqlite3")
        try:
            import httpx  # noqa: F401
        except ImportError:  # deploy.sh runs on the host, outside the image
            import types
            sys.modules["httpx"] = types.ModuleType("httpx")
        import memory_client as mc  # type: ignore
        from task_state import answer_task_key  # type: ignore

        memory = FakeMemory()
        mc._get = memory.get
        mc._post = memory.post
        mc.httpx.patch = memory.patch
        mc.get_reconciliation_cadence = lambda agent_id="watson": {"status": "stubbed"}

        def shown() -> tuple[list[str], dict[str, str]]:
            summary = mc.get_summary()
            return (
                [row["action"] for row in summary["recommended_now"]],
                {row["action"]: row["reason"] for row in summary["hidden_now"]},
            )

        # --- The key a client sends back is the key the row was given.
        scanlon_key = "family-schedule:pay-scanlon-vocal-invoices-2752-2765-2775"
        assert answer_task_key("family-schedule", scanlon_key) == scanlon_key
        assert answer_task_key("mit-book", "449") == "mit-book:449"
        assert mc._memory_task_ref(scanlon_key) is None, "a key ending in digits is not a Memory task id"
        assert mc._memory_task_ref("byb:task/629") == "629"

        # --- Done on a Red item with no Memory task survives the next packet.
        scanlon = {"label": "Pay Scanlon vocal invoices 2752, 2765, 2775", "project": "family-schedule",
                   "detail": "$25 each, personal, not Xero", "evidence": "113467, 113531", "task_id": None}
        thurston = {"label": "Decide on Thurston Elementary science fair table", "project": "outreach",
                    "detail": "Fri Jan 29, 2027; not accepted, not calendared", "evidence": "113507", "task_id": None}
        book = {"label": "Email MIT Press for editor and deadline", "project": "mit-book",
                "detail": "Two pings already sent with no action from the press", "evidence": "113518", "task_id": None}
        memory.snapshot = red_packet([scanlon, thurston, book])
        actions, hidden = shown()
        assert actions == [scanlon["label"], thurston["label"], book["label"]], actions
        assert hidden == {}, hidden

        row = next(r for r in mc.get_summary()["recommended_now"] if r["action"] == scanlon["label"])
        assert row["task_key"] == scanlon_key, row
        result = mc.record_task_answer(
            item_id=row["task_key"], disposition="done", actor="Greg Gage", source="cortex-board",
            todo={"title": row["action"], "project": row["project"]},
        )
        assert result["task_key"] == scanlon_key, result
        done_capture = memory.captures[-1]
        assert done_capture["memory_visibility"] == "knowledge", done_capture
        assert "marked done via Cortex" in done_capture["raw_content"], done_capture
        assert result["disposition"]["metadata"]["evidence_watermark"] == done_capture["capture_id"], result

        actions, hidden = shown()
        assert scanlon["label"] not in actions, actions
        assert hidden[scanlon["label"]].startswith("done on "), hidden

        memory.snapshot = red_packet([dict(scanlon), thurston, book])  # the next true-up repeats it
        actions, hidden = shown()
        assert scanlon["label"] not in actions, "a repeated packet must not bring a done item back"

        # --- Evidence newer than the answer reopens it, and says why.
        memory.snapshot = red_packet([{**scanlon, "evidence": f"113467, {done_capture['capture_id'] + 50}"}, thurston, book])
        summary = mc.get_summary()
        reopened = next(r for r in summary["recommended_now"] if r["action"] == scanlon["label"])
        assert reopened["reason"].startswith("New evidence ("), reopened
        memory.snapshot = red_packet([scanlon, thurston, book])

        # --- A decision hides the question and becomes a task with an owner.
        result = mc.record_task_answer(
            item_id="outreach:decide-on-thurston-elementary-science-fair-table", disposition="answered",
            note="Answer: ask Jacob whether he wants to do it; he can be paid.", actor="Greg Gage", source="cortex-board",
            todo={"title": thurston["label"], "project": "outreach"},
        )
        follow_up = memory.tasks[result["follow_up_task"]["id"]]
        assert "Carry out Greg's decision" in follow_up["description"], follow_up
        assert "ask Jacob" in follow_up["description"], follow_up
        assert "Owner: watson" in follow_up["description"], follow_up
        assert mc.normalize_task(follow_up)["owner"] == "watson"
        assert not mc.owner_is_greg_action(mc.normalize_task(follow_up)["owner"]), "the follow-up is not Greg's to do"
        actions, hidden = shown()
        assert thurston["label"] not in actions, actions
        assert hidden[thurston["label"]].startswith("answered on "), hidden

        # --- A comment leaves the item up and still gets an owner.
        result = mc.record_task_answer(
            item_id="mit-book:email-mit-press-for-editor-and-deadline", disposition="discuss",
            note="Who is my editor now?", actor="Greg Gage", source="cortex-board",
            todo={"title": book["label"], "project": "mit-book"},
        )
        assert result["disposition"] is None and result["follow_up_task"]["id"] in memory.tasks, result
        assert "Respond to Greg's comment" in memory.tasks[result["follow_up_task"]["id"]]["description"]
        actions, hidden = shown()
        assert actions == [book["label"]], actions  # detail saying "no action" no longer drops it

        # --- Snooze hides until the date, then the item returns.
        tomorrow = (mc._local_today() + timedelta(days=1)).isoformat()
        mc.record_task_answer(item_id="mit-book:email-mit-press-for-editor-and-deadline", disposition="snoozed",
                              snooze_until=tomorrow, source="cortex-board", todo={"title": book["label"], "project": "mit-book"})
        actions, hidden = shown()
        assert hidden.get(book["label"]) == f"snoozed until {tomorrow}", hidden
        mc.record_task_answer(item_id="mit-book:email-mit-press-for-editor-and-deadline", disposition="snoozed",
                              snooze_until=mc._local_today().isoformat(), source="cortex-board",
                              todo={"title": book["label"], "project": "mit-book"})
        actions, hidden = shown()
        assert book["label"] in actions, actions

        # --- Undo.
        mc.record_task_answer(item_id=scanlon_key, disposition="reopen", source="cortex-board",
                              todo={"title": scanlon["label"], "project": "family-schedule"})
        actions, hidden = shown()
        assert scanlon["label"] in actions, actions

        # --- A Red item tied to a Memory task follows that task, even once closed.
        memory.add_task(629, "NIMH: submit revised Abstract, Narrative and Aims", status="To Do")
        nimh = {"label": "Submit revised NIMH documents", "project": "nimh-grant", "task_id": 629, "evidence": "113300"}
        memory.snapshot = red_packet([nimh])
        assert shown()[0] == [nimh["label"]]
        mc.record_task_answer(item_id="629", disposition="done", source="cortex-board",
                              todo={"title": nimh["label"], "project": "nimh-grant"})
        assert memory.tasks[629]["status"] == "done", memory.tasks[629]
        actions, hidden = shown()
        assert actions != [nimh["label"]] and hidden[nimh["label"]] == "task 629 is done", (actions, hidden)
        memory.add_task(630, "Follow up with David Donley", status="Done")  # closed by another agent, no answer saved
        donley = {"label": "Follow up with David Donley", "project": "spikestation-r25", "task_id": "630"}
        memory.snapshot = red_packet([donley])
        actions, hidden = shown()
        assert hidden.get(donley["label"]) == "task 630 is done", (actions, hidden)

        # --- Labels that say "no action" are left out, and reported.
        memory.snapshot = red_packet([{"label": "FYI: HHD wire is waiting on BofA", "project": "spikerbot-ks"}])
        actions, hidden = shown()
        assert hidden == {"FYI: HHD wire is waiting on BofA": "label reads as no action for Greg"}, (actions, hidden)

        # --- Nothing past the limit disappears without a trace.
        memory.snapshot = red_packet([{"label": f"Send report {n}", "project": "byb-ops"} for n in range(1, 8)])
        actions, hidden = shown()
        assert len(actions) == mc.MAX_TODAY_RECOMMENDATIONS, actions
        assert hidden == {"Send report 6": "over the Today limit of 5", "Send report 7": "over the Today limit of 5"}, hidden

        # --- Open tasks in the "To Do" vocabulary are no longer filtered out upstream.
        memory.tasks.clear()
        memory.task_queries.clear()
        memory.add_task(301, "Legacy row", status="open")
        memory.add_task(638, "Email MIT Press", status="To Do")
        memory.add_task(639, "Call Jeremy", status="In Progress")
        memory.add_task(640, "Old news", status="Done")
        open_ids = [task["id"] for task in mc.get_tasks(include_complete=False, limit=50)]
        assert open_ids == [639, 638, 301], open_ids
        assert all("status" not in query for query in memory.task_queries), memory.task_queries
        assert [task["id"] for task in mc.get_tasks(status="open", limit=50)] == [638, 301]

        # --- The fallback list skips rows that went stale long ago.
        memory.tasks.clear()
        long_ago = (mc._local_today() - timedelta(days=40)).isoformat()
        soon = (mc._local_today() + timedelta(days=1)).isoformat()
        memory.add_task(625, "Decide the product-page layout", status="To Do", due_date=long_ago, dueDate=long_ago)
        memory.add_task(638, "Email MIT Press for the editor", status="To Do", due_date=soon, dueDate=soon)
        memory.snapshot = None
        actions, hidden = shown()
        assert actions == ["Email MIT Press for the editor"], actions
        assert hidden == {"Decide the product-page layout": "more than 14 days past due; needs triage"}, hidden

        # --- A project is closed when Memory says so, not when its status mentions a word.
        memory.projects = {
            "spikerecorder": {"id": 7, "slug": "spikerecorder", "title": "SpikeRecorder", "priority": "High",
                              "status": "PR 529 merged; Microsoft canceled the old listing", "lifecycle_state": "active"},
            "urop": {"id": 8, "slug": "urop", "title": "UROP", "priority": "Medium",
                     "status": "IRB role disclosed to Luca", "lifecycle_state": "active"},
            "swarm": {"id": 13, "slug": "swarm", "title": "Swarm", "priority": "Low",
                      "status": "Still writing", "lifecycle_state": "closed"},
        }
        assert sorted(project["slug"] for project in mc.get_projects()) == ["spikerecorder", "urop"]
        assert sorted(project["slug"] for project in mc.get_projects(state="closed")) == ["swarm"]

    print("OK: answers on Today items stick, and nothing is hidden without a reason.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
