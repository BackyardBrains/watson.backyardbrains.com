#!/usr/bin/env python3
"""
Smoke test for Watson's AI Star idea/artifact overlay.
"""

from __future__ import annotations

import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    service_dir = repo_root
    sys.path.insert(0, str(service_dir))

    from watson_ideas import WatsonIdeaStore  # type: ignore

    with tempfile.TemporaryDirectory() as tmp:
        store = WatsonIdeaStore(Path(tmp) / "watson_ideas.sqlite3")
        idea = store.upsert(
            {
                "kind": "research_report",
                "title": "Grass contract packet",
                "summary": "Drafted a concise packet for the remaining Grass contract follow-up.",
                "project": "Grass Foundation",
                "artifact_url": "memory://background-task/999",
                "source_type": "background_task",
                "source_id": "999",
                "confidence": 0.82,
                "evidence": [{"label": "Memory capture", "note": "Contract still unsigned"}],
            }
        )
        assert idea["id"] == "research-report-background-task-999", idea
        assert idea["badge"] == "ai_star", idea
        assert idea["status"] == "pending", idea
        assert len(store.list()) == 1

        dismissed = store.record_feedback(idea["id"], "dismissed", reason="Not useful today")
        assert dismissed["status"] == "dismissed", dismissed

        reseeded = store.upsert(
            {
                "kind": "research_report",
                "title": "Grass contract packet v2",
                "source_type": "background_task",
                "source_id": "999",
            }
        )
        assert reseeded["status"] == "dismissed", reseeded
        assert reseeded["title"] == "Grass contract packet v2", reseeded

        wrong = store.record_feedback(idea["id"], "wrong", reason="Bad source")
        assert wrong["status"] == "wrong", wrong
        assert len(store.list(status="wrong")) == 1
        assert len(store.list(status="pending")) == 0
        assert len(store.list(status="all")) == 1

    print("OK: Watson AI Star idea store smoke test passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
