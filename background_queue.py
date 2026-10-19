"""
Background worker queue for Watson MCP.

Implements targeted pull-based assignment for external agents.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Any


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(ts: datetime | None = None) -> str:
    return (ts or _utc_now()).replace(microsecond=0).isoformat()


@dataclass
class QueueConfig:
    db_path: Path
    key_salt: str
    lease_seconds: int = 6 * 60 * 60
    max_attempts: int = 3


class BackgroundQueue:
    def __init__(self, config: QueueConfig):
        self.config = config
        self._lock = Lock()
        self.config.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.config.db_path, timeout=30, isolation_level=None)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS agents (
                    agent_id TEXT PRIMARY KEY,
                    agent_key_hash TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    capabilities_json TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    last_seen_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS background_tasks (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    target_agent_id TEXT NOT NULL,
                    instructions TEXT NOT NULL,
                    context_json TEXT,
                    priority INTEGER NOT NULL DEFAULT 5,
                    status TEXT NOT NULL DEFAULT 'queued',
                    lease_until TEXT,
                    claimed_by TEXT,
                    claimed_at TEXT,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    max_attempts INTEGER NOT NULL DEFAULT 3,
                    linked_task_id INTEGER,
                    created_by TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL,
                    completed_at TEXT
                )
                """
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS background_results (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER NOT NULL,
                    agent_id TEXT NOT NULL,
                    status TEXT NOT NULL,
                    result_text TEXT NOT NULL,
                    metadata_json TEXT,
                    submitted_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_background_tasks_agent_status ON background_tasks(target_agent_id, status)"
            )
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS background_steward_decisions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    task_id INTEGER,
                    action TEXT NOT NULL,
                    reason TEXT NOT NULL,
                    metadata_json TEXT,
                    created_at TEXT NOT NULL
                )
                """
            )
            conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_background_steward_decisions_task ON background_steward_decisions(task_id)"
            )

    def _hash_key(self, raw_key: str) -> str:
        material = f"{self.config.key_salt}:{raw_key}".encode("utf-8")
        return hashlib.sha256(material).hexdigest()

    def register_agent(self, agent_id: str, agent_key: str, capabilities: list[str] | None = None, enabled: bool = True) -> None:
        now = _to_iso()
        cap_json = json.dumps(capabilities or [])
        with self._connect() as conn:
            conn.execute(
                """
                INSERT INTO agents(agent_id, agent_key_hash, enabled, capabilities_json, created_at, updated_at)
                VALUES(?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id) DO UPDATE SET
                    agent_key_hash=excluded.agent_key_hash,
                    enabled=excluded.enabled,
                    capabilities_json=excluded.capabilities_json,
                    updated_at=excluded.updated_at
                """,
                (agent_id, self._hash_key(agent_key), 1 if enabled else 0, cap_json, now, now),
            )

    def verify_agent(self, agent_id: str, agent_key: str) -> bool:
        with self._connect() as conn:
            row = conn.execute("SELECT agent_key_hash, enabled FROM agents WHERE agent_id = ?", (agent_id,)).fetchone()
            if not row or int(row["enabled"]) != 1:
                return False
            return row["agent_key_hash"] == self._hash_key(agent_key)

    def touch_agent(self, agent_id: str) -> None:
        with self._connect() as conn:
            conn.execute("UPDATE agents SET last_seen_at = ?, updated_at = ? WHERE agent_id = ?", (_to_iso(), _to_iso(), agent_id))

    def enqueue_task(
        self,
        target_agent_id: str,
        instructions: str,
        context: dict[str, Any] | None = None,
        priority: int = 5,
        created_by: str = "watson",
        linked_task_id: int | None = None,
        max_attempts: int | None = None,
    ) -> dict[str, Any]:
        now = _to_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO background_tasks(
                    target_agent_id, instructions, context_json, priority, status, lease_until, attempts, max_attempts,
                    linked_task_id, created_by, created_at, updated_at
                )
                VALUES(?, ?, ?, ?, 'queued', NULL, 0, ?, ?, ?, ?, ?)
                """,
                (
                    target_agent_id,
                    instructions,
                    json.dumps(context or {}),
                    priority,
                    max_attempts or self.config.max_attempts,
                    linked_task_id,
                    created_by,
                    now,
                    now,
                ),
            )
            task_id = int(cur.lastrowid)
        return self.get_task(task_id) or {}

    def list_agents(self) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT agent_id, enabled, capabilities_json, created_at, updated_at, last_seen_at
                FROM agents
                ORDER BY COALESCE(last_seen_at, updated_at) DESC, agent_id ASC
                """
            ).fetchall()
        agents = []
        for row in rows:
            agent = dict(row)
            agent["enabled"] = bool(agent["enabled"])
            agent["capabilities"] = json.loads(agent.pop("capabilities_json") or "[]")
            agents.append(agent)
        return agents

    def list_stale_tasks(self, stale_after_hours: int = 24) -> list[dict[str, Any]]:
        cutoff = _to_iso(_utc_now() - timedelta(hours=stale_after_hours))
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT
                    t.*,
                    a.enabled AS agent_enabled,
                    a.last_seen_at AS agent_last_seen_at,
                    a.capabilities_json AS agent_capabilities_json,
                    (
                        SELECT COUNT(*)
                        FROM background_results r
                        WHERE r.task_id = t.id
                    ) AS result_count
                FROM background_tasks t
                LEFT JOIN agents a ON a.agent_id = t.target_agent_id
                WHERE t.status IN ('queued', 'retry', 'claimed')
                  AND (
                    (t.status IN ('queued', 'retry') AND t.updated_at <= ?)
                    OR (t.lease_until IS NOT NULL AND t.lease_until <= ?)
                  )
                ORDER BY t.created_at ASC, t.id ASC
                """,
                (cutoff, _to_iso()),
            ).fetchall()
        return [self._task_from_row(row) for row in rows]

    def claim_next_task(self, agent_id: str, lease_seconds: int | None = None) -> dict[str, Any] | None:
        lease = lease_seconds or self.config.lease_seconds
        now = _utc_now()
        now_iso = _to_iso(now)
        lease_until = _to_iso(now + timedelta(seconds=lease))
        with self._lock:
            with self._connect() as conn:
                conn.execute("BEGIN IMMEDIATE")
                try:
                    # Requeue expired claims so abandoned work can be retried.
                    conn.execute(
                        """
                        UPDATE background_tasks
                        SET status='retry',
                            lease_until=NULL,
                            claimed_by=NULL,
                            updated_at=?
                        WHERE status='claimed'
                          AND lease_until IS NOT NULL
                          AND lease_until <= ?
                          AND attempts < max_attempts
                        """,
                        (now_iso, now_iso),
                    )
                    # Hard-stop tasks that exhausted attempts.
                    conn.execute(
                        """
                        UPDATE background_tasks
                        SET status='failed',
                            lease_until=NULL,
                            updated_at=?,
                            completed_at=?
                        WHERE status='claimed'
                          AND lease_until IS NOT NULL
                          AND lease_until <= ?
                          AND attempts >= max_attempts
                        """,
                        (now_iso, now_iso, now_iso),
                    )
                    row = conn.execute(
                        """
                        SELECT *
                        FROM background_tasks
                        WHERE target_agent_id = ?
                          AND status IN ('queued', 'retry')
                          AND (lease_until IS NULL OR lease_until <= ?)
                          AND attempts < max_attempts
                        ORDER BY priority ASC, created_at ASC, id ASC
                        LIMIT 1
                        """,
                        (agent_id, now_iso),
                    ).fetchone()
                    if not row:
                        conn.execute("COMMIT")
                        self.touch_agent(agent_id)
                        return None
                    task_id = int(row["id"])
                    conn.execute(
                        """
                        UPDATE background_tasks
                        SET status='claimed',
                            lease_until=?,
                            claimed_by=?,
                            claimed_at=?,
                            attempts=attempts+1,
                            updated_at=?
                        WHERE id=?
                        """,
                        (lease_until, agent_id, now_iso, now_iso, task_id),
                    )
                    conn.execute("COMMIT")
                except Exception:
                    conn.execute("ROLLBACK")
                    raise
        self.touch_agent(agent_id)
        return self.get_task(task_id)

    def submit_result(
        self,
        task_id: int,
        agent_id: str,
        status: str,
        result_text: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _to_iso()
        status_norm = status.strip().lower()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
            if row["target_agent_id"] != agent_id:
                raise ValueError(f"Task {task_id} is assigned to {row['target_agent_id']}, not {agent_id}")
            conn.execute(
                """
                INSERT INTO background_results(task_id, agent_id, status, result_text, metadata_json, submitted_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (task_id, agent_id, status_norm, result_text, json.dumps(metadata or {}), now),
            )
            if status_norm == "expired":
                conn.execute(
                    """
                    UPDATE background_tasks
                    SET status='retry', lease_until=NULL, updated_at=?
                    WHERE id=?
                    """,
                    (now, task_id),
                )
            else:
                completed_at = now if status_norm in ("completed", "failed", "needs_clarification") else None
                conn.execute(
                    """
                    UPDATE background_tasks
                    SET status=?, lease_until=NULL, updated_at=?, completed_at=?
                    WHERE id=?
                    """,
                    (status_norm, now, completed_at, task_id),
                )
        return self.get_task(task_id) or {}

    def reassign_task(
        self,
        task_id: int,
        new_agent_id: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _to_iso()
        new_agent_id = new_agent_id.strip()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
            if row["status"] not in ("queued", "retry", "claimed"):
                raise ValueError(f"Task {task_id} is {row['status']} and cannot be reassigned")
            previous_agent_id = row["target_agent_id"]
            conn.execute(
                """
                UPDATE background_tasks
                SET target_agent_id=?,
                    status='queued',
                    lease_until=NULL,
                    claimed_by=NULL,
                    claimed_at=NULL,
                    attempts=0,
                    updated_at=?
                WHERE id=?
                """,
                (new_agent_id, now, task_id),
            )
        self.record_steward_decision(
            task_id=task_id,
            action="reassign_to_active_worker",
            reason=reason,
            metadata={"previous_agent_id": previous_agent_id, "new_agent_id": new_agent_id, **(metadata or {})},
        )
        return self.get_task(task_id) or {}

    def mark_task_status(
        self,
        task_id: int,
        status: str,
        reason: str,
        metadata: dict[str, Any] | None = None,
        agent_id: str = "watson-steward",
    ) -> dict[str, Any]:
        status_norm = status.strip().lower()
        if status_norm not in {"completed", "failed", "needs_clarification"}:
            raise ValueError("status must be completed, failed, or needs_clarification")
        now = _to_iso()
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                raise ValueError(f"Task {task_id} not found")
            if row["status"] in ("completed", "failed", "needs_clarification"):
                raise ValueError(f"Task {task_id} is already {row['status']}")
            conn.execute(
                """
                INSERT INTO background_results(task_id, agent_id, status, result_text, metadata_json, submitted_at)
                VALUES(?, ?, ?, ?, ?, ?)
                """,
                (task_id, agent_id, status_norm, reason, json.dumps(metadata or {}), now),
            )
            conn.execute(
                """
                UPDATE background_tasks
                SET status=?,
                    lease_until=NULL,
                    updated_at=?,
                    completed_at=?
                WHERE id=?
                """,
                (status_norm, now, now, task_id),
            )
        action = {
            "completed": "mark_resolved_if_linked_task_complete",
            "failed": "mark_failed",
            "needs_clarification": "mark_needs_clarification",
        }[status_norm]
        self.record_steward_decision(task_id=task_id, action=action, reason=reason, metadata=metadata or {})
        return self.get_task(task_id) or {}

    def record_steward_decision(
        self,
        action: str,
        reason: str,
        task_id: int | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        now = _to_iso()
        with self._connect() as conn:
            cur = conn.execute(
                """
                INSERT INTO background_steward_decisions(task_id, action, reason, metadata_json, created_at)
                VALUES(?, ?, ?, ?, ?)
                """,
                (task_id, action, reason, json.dumps(metadata or {}), now),
            )
            decision_id = int(cur.lastrowid)
        return {
            "id": decision_id,
            "task_id": task_id,
            "action": action,
            "reason": reason,
            "metadata": metadata or {},
            "created_at": now,
        }

    def run_steward(
        self,
        stale_after_hours: int = 24,
        inactive_after_hours: int = 24,
        fallback_agent_id: str | None = None,
        dry_run: bool = False,
        completed_linked_task_ids: list[int] | None = None,
    ) -> dict[str, Any]:
        completed_linked_task_ids = completed_linked_task_ids or []
        agents = self.list_agents()
        active_agents = {
            agent["agent_id"]: agent
            for agent in agents
            if self._agent_is_active(agent, inactive_after_hours)
        }
        fallback_agent_id = (fallback_agent_id or "").strip() or self._choose_fallback_agent(active_agents)
        fallback_is_active = bool(fallback_agent_id and fallback_agent_id in active_agents)
        stale_tasks = self.list_stale_tasks(stale_after_hours=stale_after_hours)
        findings = []
        actions_taken = []
        escalations = []

        for task in stale_tasks:
            decision = self._classify_stale_task(
                task=task,
                active_agents=active_agents,
                fallback_agent_id=fallback_agent_id if fallback_is_active else None,
                completed_linked_task_ids=completed_linked_task_ids,
                inactive_after_hours=inactive_after_hours,
            )
            findings.append(decision)
            action = decision["action"]
            if action == "escalate_to_greg":
                escalations.append(decision)
                if not dry_run:
                    self.record_steward_decision(
                        task_id=task["id"],
                        action=action,
                        reason=decision["reason"],
                        metadata=decision,
                    )
                continue
            if dry_run:
                continue
            if action == "reassign_to_active_worker":
                updated = self.reassign_task(
                    task_id=task["id"],
                    new_agent_id=decision["new_agent_id"],
                    reason=decision["reason"],
                    metadata=decision,
                )
                actions_taken.append({"action": action, "task": updated, "reason": decision["reason"]})
            elif action == "mark_failed":
                updated = self.mark_task_status(
                    task_id=task["id"],
                    status="failed",
                    reason=decision["reason"],
                    metadata=decision,
                )
                actions_taken.append({"action": action, "task": updated, "reason": decision["reason"]})
            elif action == "mark_resolved_if_linked_task_complete":
                updated = self.mark_task_status(
                    task_id=task["id"],
                    status="completed",
                    reason=decision["reason"],
                    metadata=decision,
                )
                actions_taken.append({"action": action, "task": updated, "reason": decision["reason"]})

        return {
            "ok": True,
            "dry_run": dry_run,
            "stale_after_hours": stale_after_hours,
            "inactive_after_hours": inactive_after_hours,
            "fallback_agent_id": fallback_agent_id if fallback_is_active else None,
            "agents": agents,
            "findings": findings,
            "actions_taken": actions_taken,
            "escalations": escalations,
            "summary": {
                "stale_tasks": len(stale_tasks),
                "actions_taken": len(actions_taken),
                "escalations": len(escalations),
            },
        }

    def get_task(self, task_id: int) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT * FROM background_tasks WHERE id = ?", (task_id,)).fetchone()
            if not row:
                return None
            return self._task_from_row(row)

    def get_task_status(self, task_id: int) -> dict[str, Any]:
        task = self.get_task(task_id)
        if not task:
            raise ValueError(f"Task {task_id} not found")
        with self._connect() as conn:
            rows = conn.execute(
                """
                SELECT task_id, agent_id, status, result_text, metadata_json, submitted_at
                FROM background_results
                WHERE task_id = ?
                ORDER BY id ASC
                """,
                (task_id,),
            ).fetchall()
        results = []
        for row in rows:
            d = dict(row)
            d["metadata"] = json.loads(d.pop("metadata_json") or "{}")
            results.append(d)
        return {"task": task, "results": results}

    def list_tasks(
        self,
        limit: int = 50,
        status: str | None = None,
        project: str | None = None,
        search: str | None = None,
    ) -> list[dict[str, Any]]:
        limit = max(1, min(int(limit or 50), 200))
        clauses: list[str] = []
        params: list[Any] = []
        if status:
            clauses.append("t.status = ?")
            params.append(status.strip().lower())
        if project:
            clauses.append("LOWER(t.context_json) LIKE ?")
            params.append(f"%{project.strip().lower()}%")
        if search:
            needle = f"%{search.strip().lower()}%"
            clauses.append("(LOWER(t.instructions) LIKE ? OR LOWER(t.context_json) LIKE ?)")
            params.extend([needle, needle])
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        sql = f"""
            SELECT
                t.*,
                a.enabled AS agent_enabled,
                a.last_seen_at AS agent_last_seen_at,
                a.capabilities_json AS agent_capabilities_json,
                (
                    SELECT COUNT(*)
                    FROM background_results r
                    WHERE r.task_id = t.id
                ) AS result_count,
                (
                    SELECT r.status
                    FROM background_results r
                    WHERE r.task_id = t.id
                    ORDER BY r.id DESC
                    LIMIT 1
                ) AS latest_result_status,
                (
                    SELECT r.submitted_at
                    FROM background_results r
                    WHERE r.task_id = t.id
                    ORDER BY r.id DESC
                    LIMIT 1
                ) AS latest_result_at,
                (
                    SELECT substr(r.result_text, 1, 600)
                    FROM background_results r
                    WHERE r.task_id = t.id
                    ORDER BY r.id DESC
                    LIMIT 1
                ) AS latest_result_preview,
                (
                    SELECT r.metadata_json
                    FROM background_results r
                    WHERE r.task_id = t.id
                    ORDER BY r.id DESC
                    LIMIT 1
                ) AS latest_result_metadata_json
            FROM background_tasks t
            LEFT JOIN agents a ON a.agent_id = t.target_agent_id
            {where}
            ORDER BY datetime(t.created_at) DESC, t.id DESC
            LIMIT ?
        """
        params.append(limit)
        with self._connect() as conn:
            rows = conn.execute(sql, params).fetchall()
        tasks = []
        for row in rows:
            task = self._task_from_row(row)
            task["result_count"] = int(task.pop("result_count") or 0)
            metadata_json = task.pop("latest_result_metadata_json", None)
            try:
                task["latest_result_metadata"] = json.loads(metadata_json or "{}")
            except json.JSONDecodeError:
                task["latest_result_metadata"] = {}
            tasks.append(task)
        return tasks

    def update_latest_result_metadata(self, task_id: int, metadata_patch: dict[str, Any]) -> dict[str, Any]:
        now = _to_iso()
        with self._connect() as conn:
            row = conn.execute(
                """
                SELECT id, metadata_json
                FROM background_results
                WHERE task_id = ?
                ORDER BY id DESC
                LIMIT 1
                """,
                (task_id,),
            ).fetchone()
            if not row:
                raise ValueError(f"Task {task_id} has no result")
            try:
                metadata = json.loads(row["metadata_json"] or "{}")
            except json.JSONDecodeError:
                metadata = {}
            metadata.update(metadata_patch)
            conn.execute(
                "UPDATE background_results SET metadata_json = ? WHERE id = ?",
                (json.dumps(metadata), row["id"]),
            )
            conn.execute("UPDATE background_tasks SET updated_at = ? WHERE id = ?", (now, task_id))
        return self.get_task_status(task_id)

    def _task_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        task = dict(row)
        task["context"] = json.loads(task.pop("context_json") or "{}")
        if "agent_enabled" in task:
            agent_enabled = task.pop("agent_enabled")
            task["target_agent"] = {
                "exists": agent_enabled is not None,
                "enabled": bool(agent_enabled) if agent_enabled is not None else False,
                "last_seen_at": task.pop("agent_last_seen_at"),
                "capabilities": json.loads(task.pop("agent_capabilities_json") or "[]"),
            }
        return task

    def _agent_is_active(self, agent: dict[str, Any], inactive_after_hours: int) -> bool:
        if not agent.get("enabled"):
            return False
        last_seen = agent.get("last_seen_at")
        if not last_seen:
            return False
        try:
            seen_at = datetime.fromisoformat(last_seen)
        except ValueError:
            return False
        if seen_at.tzinfo is None:
            seen_at = seen_at.replace(tzinfo=timezone.utc)
        return seen_at >= _utc_now() - timedelta(hours=inactive_after_hours)

    def _choose_fallback_agent(self, active_agents: dict[str, dict[str, Any]]) -> str | None:
        if not active_agents:
            return None
        return sorted(
            active_agents.values(),
            key=lambda agent: agent.get("last_seen_at") or "",
            reverse=True,
        )[0]["agent_id"]

    def _classify_stale_task(
        self,
        task: dict[str, Any],
        active_agents: dict[str, dict[str, Any]],
        fallback_agent_id: str | None,
        completed_linked_task_ids: list[int],
        inactive_after_hours: int,
    ) -> dict[str, Any]:
        target_agent_id = task["target_agent_id"]
        target_agent = task.get("target_agent") or {}
        target_active = target_agent_id in active_agents
        base = {
            "task_id": task["id"],
            "status": task["status"],
            "target_agent_id": target_agent_id,
            "created_at": task["created_at"],
            "attempts": task["attempts"],
            "max_attempts": task["max_attempts"],
            "linked_task_id": task.get("linked_task_id"),
            "target_agent_exists": target_agent.get("exists", False),
            "target_agent_active": target_active,
        }
        if task.get("linked_task_id") in completed_linked_task_ids:
            return {
                **base,
                "action": "mark_resolved_if_linked_task_complete",
                "reason": f"Linked Memory task {task['linked_task_id']} is already complete.",
            }
        if self._looks_like_test_task(task):
            return {
                **base,
                "action": "mark_failed",
                "reason": "Stale background task appears to be a test task.",
            }
        if int(task["attempts"]) >= int(task["max_attempts"]):
            return {
                **base,
                "action": "mark_failed",
                "reason": "Task exhausted max attempts before completion.",
            }
        if not target_active and fallback_agent_id and fallback_agent_id != target_agent_id:
            return {
                **base,
                "action": "reassign_to_active_worker",
                "new_agent_id": fallback_agent_id,
                "reason": (
                    f"Target agent has not checked in within {inactive_after_hours} hours; "
                    f"reassigning to active fallback {fallback_agent_id}."
                ),
            }
        if not target_active:
            return {
                **base,
                "action": "escalate_to_greg",
                "reason": f"Target agent is inactive and no active fallback worker is available.",
            }
        return {
            **base,
            "action": "escalate_to_greg",
            "reason": "Task is stale even though the target agent appears active; needs owner review.",
        }

    def _looks_like_test_task(self, task: dict[str, Any]) -> bool:
        instructions = str(task.get("instructions", "")).strip().lower()
        created_by = str(task.get("created_by", "")).strip().lower()
        origin = str(task.get("context", {}).get("origin", "")).strip().lower()
        return (
            instructions in {"test", "test task"}
            or created_by in {"manual-test", "test", "test_script"}
            or origin in {"test_script", "test_script.py", "test_chatgpt_worker_flow.py"}
        )


def build_queue(data_path: Path) -> BackgroundQueue:
    db_path = Path(os.environ.get("WATSON_BG_QUEUE_DB", data_path / "memory" / "watson_background_queue.sqlite3"))
    key_salt = os.environ.get("WATSON_AGENT_KEY_SALT", "watson-agent-key-salt-change-me")
    lease_seconds = int(os.environ.get("WATSON_BG_LEASE_SECONDS", str(6 * 60 * 60)))
    max_attempts = int(os.environ.get("WATSON_BG_MAX_ATTEMPTS", "3"))
    return BackgroundQueue(
        QueueConfig(
            db_path=db_path,
            key_salt=key_salt,
            lease_seconds=lease_seconds,
            max_attempts=max_attempts,
        )
    )
