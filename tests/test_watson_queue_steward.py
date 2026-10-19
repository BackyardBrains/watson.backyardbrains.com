#!/usr/bin/env python3
"""
Local smoke test for Watson's background queue steward.

Uses a temporary queue DB so it never touches production queue state.
"""

from __future__ import annotations

import sqlite3
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path


def make_stale(db_path: Path, task_id: int) -> None:
    stale_at = (datetime.now(timezone.utc) - timedelta(days=3)).replace(microsecond=0).isoformat()
    with sqlite3.connect(db_path) as conn:
        conn.execute(
            "UPDATE background_tasks SET created_at = ?, updated_at = ? WHERE id = ?",
            (stale_at, stale_at, task_id),
        )


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    service_dir = repo_root

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db_path = tmp_path / "watson_background_queue.sqlite3"

        sys.path.insert(0, str(service_dir))
        from background_queue import BackgroundQueue, QueueConfig  # type: ignore

        queue = BackgroundQueue(QueueConfig(db_path=db_path, key_salt="test-only-salt-change-me"))

        queue.register_agent(
            agent_id="inactive-agent",
            agent_key="inactive-key",
            capabilities=["reports"],
            enabled=True,
        )
        queue.register_agent(
            agent_id="active-agent",
            agent_key="active-key",
            capabilities=["reports", "general"],
            enabled=True,
        )
        queue.touch_agent("active-agent")

        test_task = queue.enqueue_task(
            target_agent_id="inactive-agent",
            instructions="test",
            created_by="spike",
        )
        reassign_task = queue.enqueue_task(
            target_agent_id="inactive-agent",
            instructions="Prepare a concise queue health report.",
            created_by="steward-smoke",
        )
        linked_task = queue.enqueue_task(
            target_agent_id="inactive-agent",
            instructions="Background work already completed in Memory.",
            linked_task_id=42,
            created_by="steward-smoke",
        )

        for task in (test_task, reassign_task, linked_task):
            make_stale(db_path, int(task["id"]))

        steward = queue.run_steward(
            fallback_agent_id="active-agent",
            completed_linked_task_ids=[42],
        )

        assert steward["summary"]["stale_tasks"] == 3, steward
        assert steward["summary"]["actions_taken"] == 3, steward
        assert steward["summary"]["escalations"] == 0, steward

        test_status = queue.get_task_status(int(test_task["id"]))
        reassign_status = queue.get_task_status(int(reassign_task["id"]))
        linked_status = queue.get_task_status(int(linked_task["id"]))

        assert test_status["task"]["status"] == "failed", test_status
        assert reassign_status["task"]["status"] == "queued", reassign_status
        assert reassign_status["task"]["target_agent_id"] == "active-agent", reassign_status
        assert linked_status["task"]["status"] == "completed", linked_status

        print("OK: Watson queue steward smoke test passed.")
        print(steward["summary"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
