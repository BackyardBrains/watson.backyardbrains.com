"""
Publish a document to Greg's share site.

"My share" is https://share.backyardbrains.com. Each page lives in its own
folder under greg/, named <slug>-<12 random characters>, so the link cannot be
guessed and is not listed anywhere.

Documents arrive as Markdown and are rendered here with raw HTML switched off,
so a caller can publish text, tables and links but never a script.
"""

from __future__ import annotations

import html
import json
import os
import re
import secrets
import string
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

DATA_PATH = Path(os.environ.get("DATA_PATH", Path(__file__).resolve().parent.parent.parent))
SHARE_ROOT = Path(os.environ.get("WATSON_SHARE_ROOT", DATA_PATH / "workspaces" / "main" / "share"))
SHARE_BASE_URL = os.environ.get("WATSON_SHARE_BASE_URL", "https://share.backyardbrains.com").rstrip("/")
OWNER_FOLDER = "greg"
MARKER_FILE = ".published-by-watson.json"
PAGE_ID_RE = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*-[A-Za-z0-9]{12}")
MAX_TITLE_CHARS = 140
MAX_DOCUMENT_CHARS = 400_000
TOKEN_ALPHABET = string.ascii_letters + string.digits
RENDER_SECONDS = 15
RENDER_MEMORY_BYTES = 768 * 1024 * 1024

# Some Markdown makes the renderer use memory or time out of all proportion to
# its size (100,000 characters of "*a " needs gigabytes). The renderer therefore
# runs in a child process that limits itself first, so a bad document fails on
# its own instead of taking the service down with it.
RENDER_CHILD = """
import resource, sys
resource.setrlimit(resource.RLIMIT_AS, (int(sys.argv[1]), int(sys.argv[1])))
resource.setrlimit(resource.RLIMIT_CPU, (int(sys.argv[2]), int(sys.argv[2])))
from markdown_it import MarkdownIt
text = sys.stdin.buffer.read().decode("utf-8")
sys.stdout.buffer.write(MarkdownIt("commonmark", {"html": False}).enable("table").render(text).encode("utf-8"))
"""

PAGE_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="robots" content="noindex, nofollow">
<title>{title}</title>
<style>
  body {{ margin: 0; background: #fff; color: #1a1a1a; font: 17px/1.6 system-ui, -apple-system, "Segoe UI", Roboto, Helvetica, Arial, sans-serif; }}
  main {{ max-width: 44rem; margin: 0 auto; padding: 2.5rem 1.25rem 4rem; }}
  h1 {{ font-size: 1.9rem; line-height: 1.2; margin: 0 0 1.5rem; }}
  h2 {{ font-size: 1.35rem; margin: 2.2rem 0 0.6rem; }}
  h3 {{ font-size: 1.1rem; margin: 1.6rem 0 0.4rem; }}
  p, ul, ol, table, pre, blockquote {{ margin: 0 0 1rem; }}
  a {{ color: #0b57d0; }}
  table {{ border-collapse: collapse; width: 100%; display: block; overflow-x: auto; }}
  th, td {{ border: 1px solid #d0d0d0; padding: 0.4rem 0.6rem; text-align: left; vertical-align: top; }}
  th {{ background: #f4f4f4; }}
  code {{ font: 0.9em ui-monospace, SFMono-Regular, Menlo, Consolas, monospace; background: #f4f4f4; padding: 0.1em 0.3em; border-radius: 3px; }}
  pre {{ background: #f4f4f4; padding: 0.9rem 1rem; border-radius: 6px; overflow-x: auto; }}
  pre code {{ background: none; padding: 0; }}
  blockquote {{ border-left: 3px solid #d0d0d0; margin-left: 0; padding-left: 1rem; color: #444; }}
  img {{ max-width: 100%; height: auto; }}
</style>
</head>
<body>
<main>
<h1>{title}</h1>
{body}
</main>
</body>
</html>
"""


def _slug(title: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")[:48].strip("-")
    return slug or "doc"


def _render_markdown(text: str) -> str:
    try:
        import markdown_it  # noqa: F401
    except ImportError:
        # The image ships markdown-it-py. Without it, publish readable plain
        # text rather than fail; everything is escaped either way.
        blocks = [block.strip() for block in re.split(r"\n\s*\n", text) if block.strip()]
        return "\n".join("<p>" + html.escape(block).replace("\n", "<br>\n") + "</p>" for block in blocks)
    try:
        child = subprocess.run(
            [sys.executable, "-c", RENDER_CHILD, str(RENDER_MEMORY_BYTES), str(RENDER_SECONDS)],
            input=text.encode("utf-8"), capture_output=True, timeout=RENDER_SECONDS + 5, cwd="/",
        )
    except subprocess.TimeoutExpired:
        child = None
    if child is None or child.returncode != 0:
        raise ValueError("the document could not be rendered within the time and memory allowed; simplify its formatting")
    return child.stdout.decode("utf-8")


def _write(path: Path, content: str) -> None:
    temporary = path.with_name(path.name + ".tmp")
    temporary.write_text(content, encoding="utf-8")
    os.chmod(temporary, 0o644)
    temporary.replace(path)


def publish_page(title: str, markdown_text: str, page_id: str | None = None, actor: str = "watson") -> dict[str, Any]:
    """Create a share page, or replace the content of one this module created."""
    clean_title = " ".join(str(title or "").split())[:MAX_TITLE_CHARS]
    if not clean_title:
        raise ValueError("title is required")
    text = str(markdown_text or "").strip()
    if not text:
        raise ValueError("the document is empty")
    if len(text) > MAX_DOCUMENT_CHARS:
        raise ValueError(f"the document is longer than {MAX_DOCUMENT_CHARS} characters")
    owner = SHARE_ROOT / OWNER_FOLDER
    if not owner.is_dir():
        raise RuntimeError("the share folder is not available on this server")

    body = _render_markdown(text)
    now = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
    record: dict[str, Any] = {"title": clean_title, "published_at": now, "updated_at": now, "actor": actor}
    if page_id:
        page_id = str(page_id).strip().strip("/")
        if not PAGE_ID_RE.fullmatch(page_id):
            raise ValueError("page_id is not a share page id")
        folder = owner / page_id
        marker = folder / MARKER_FILE
        if not marker.is_file():
            # Greg's other pages are built by hand; this tool must not overwrite them.
            raise ValueError("only a page published with this tool can be replaced")
        try:
            record["published_at"] = json.loads(marker.read_text(encoding="utf-8")).get("published_at") or now
        except (ValueError, OSError):
            pass
    else:
        token = "".join(secrets.choice(TOKEN_ALPHABET) for _ in range(12))
        folder = owner / f"{_slug(clean_title)}-{token}"
        folder.mkdir(mode=0o755)
        os.chmod(folder, 0o755)

    _write(folder / "index.html", PAGE_TEMPLATE.format(title=html.escape(clean_title), body=body))
    _write(folder / "source.md", text + "\n")
    _write(folder / MARKER_FILE, json.dumps(record, indent=1) + "\n")
    return {
        "ok": True,
        "url": f"{SHARE_BASE_URL}/{OWNER_FOLDER}/{folder.name}/",
        "page_id": folder.name,
        "title": clean_title,
        "replaced": bool(page_id),
    }
