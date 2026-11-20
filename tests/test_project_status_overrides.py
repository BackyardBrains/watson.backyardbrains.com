#!/usr/bin/env python3
"""
Regression test: legacy [project-status-current] captures must never cover the
canonical project row (July captures pinned NIMH, MIT book and Grass pages for
months). Uses temporary state and stubbed Memory calls only.
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))

    with tempfile.TemporaryDirectory() as tmp:
        os.environ["DATA_PATH"] = tmp
        os.environ["WATSON_CAPTURE_QUALITY_DB"] = str(Path(tmp) / "quality.sqlite3")
        try:
            import httpx  # noqa: F401
        except ImportError:  # deploy.sh runs on the host, outside the image
            import types
            sys.modules["httpx"] = types.ModuleType("httpx")
        import memory_client as mc  # type: ignore

        rows = [
            {"slug": "nimh-grant", "title": "NIMH", "status": "Nov row", "priority": "High",
             "lifecycle_state": "active", "last_activity_at": "2026-11-19T21:48:49"},
            {"slug": "fresh-capture", "title": "Fresh", "status": "old row", "priority": "Low",
             "lifecycle_state": "active", "last_activity_at": "2026-07-01T00:00:00"},
            {"slug": "no-stamp", "title": "No stamp", "status": "row text", "priority": "Low",
             "lifecycle_state": "active"},
        ]

        def capture(cid: int, slug: str, status: str, created_at: str) -> dict:
            return {
                "id": cid,
                "capture_id": cid,
                "created_at": created_at,
                "raw_content": f'[project-status-current] project={slug} priority=High status="{status}"',
            }

        captures = [
            capture(105504, "nimh-grant", "July text", "2026-07-23T10:00:00"),
            capture(200001, "fresh-capture", "newer capture text", "2026-07-02T00:00:00"),
            capture(200000, "fresh-capture", "older capture text", "2026-06-01T00:00:00"),
            capture(200002, "no-stamp", "capture text", "2026-07-02T00:00:00"),
        ]

        mc._get = lambda path, params=None: rows if path == "/v1/projects" else []
        mc.search_captures = lambda q="", limit=10, include_quarantined=False: list(captures)

        by_slug = {p["slug"]: p for p in mc.get_projects(state="all")}

        nimh = by_slug["nimh-grant"]
        assert nimh["status"] == "Nov row", nimh
        assert nimh["status_source"] == "project row", nimh

        fresh = by_slug["fresh-capture"]
        assert fresh["status"] == "old row", fresh
        assert fresh["status_source"] == "project row", fresh

        no_stamp = by_slug["no-stamp"]
        assert no_stamp["status"] == "row text", no_stamp
        assert no_stamp["status_source"] == "project row", no_stamp

    print("OK: project rows win over project-status-current captures.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
