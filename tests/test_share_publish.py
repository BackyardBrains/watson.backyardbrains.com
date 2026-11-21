#!/usr/bin/env python3
"""A document published to Greg's share gets its own unguessable folder, and can never carry a script."""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


def main() -> int:
    repo_root = Path(__file__).resolve().parent.parent
    sys.path.insert(0, str(repo_root))
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["WATSON_SHARE_ROOT"] = tmp
        (Path(tmp) / "greg").mkdir()
        hand_built = Path(tmp) / "greg" / "byb-rules-90Oyk26USxkj"
        hand_built.mkdir()
        (hand_built / "index.html").write_text("Greg's own page")
        import share_publish as sp  # type: ignore

        text = "# Plan\n\nFirst line.\n\n| a | b |\n|---|---|\n| 1 | 2 |\n\n<script>alert(1)</script>\n\n[bad](javascript:alert(1)) and [good](https://backyardbrains.com)"
        first = sp.publish_page("Dec 3 monthly meeting: agenda & notes", text)
        assert first["url"].startswith("https://share.backyardbrains.com/greg/dec-3-monthly-meeting-agenda-notes-"), first
        assert sp.PAGE_ID_RE.fullmatch(first["page_id"]) and not first["replaced"], first
        folder = Path(tmp) / "greg" / first["page_id"]
        page = (folder / "index.html").read_text()
        assert "<title>Dec 3 monthly meeting: agenda &amp; notes</title>" in page
        assert 'name="robots" content="noindex, nofollow"' in page
        assert "<script>" not in page and "&lt;script&gt;" in page, "raw HTML must be shown as text"
        assert 'href="javascript:' not in page
        assert (folder / "source.md").read_text().startswith("# Plan")
        assert oct((folder / "index.html").stat().st_mode & 0o777) == "0o644"

        second = sp.publish_page("Another doc", "text")
        assert second["page_id"] != first["page_id"]

        replaced = sp.publish_page("Dec 3 monthly meeting", "New text.", page_id=first["page_id"])
        assert replaced["replaced"] and replaced["url"] == first["url"]
        assert "New text." in (folder / "index.html").read_text()

        for bad_id in ("byb-rules-90Oyk26USxkj", "../greg/x-AAAAAAAAAAAA", "nope", f"{first['page_id']}/../byb-rules-90Oyk26USxkj"):
            try:
                sp.publish_page("x", "y", page_id=bad_id)
                raise AssertionError(f"{bad_id} must be refused")
            except ValueError:
                pass
        assert (hand_built / "index.html").read_text() == "Greg's own page"

        for title, body in (("", "text"), ("Title", "   "), ("Title", "x" * (sp.MAX_DOCUMENT_CHARS + 1))):
            try:
                sp.publish_page(title, body)
                raise AssertionError("empty or oversized input must be refused")
            except ValueError:
                pass

    print("OK: share pages are published safely and only this tool's pages can be replaced.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
