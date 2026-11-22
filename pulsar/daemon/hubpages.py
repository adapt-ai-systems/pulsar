"""Hub — optional static pages hub integration built into Pulsar.

Pages live in one directory (or symlink to one) per slug under ``ROOT``
(configured via ``PULSAR_HUB_PAGES_DIR``; unset disables listing). This module
lists them so the web Hub surface can show a catalog and frame a page directly
from the user's configured ``hub.url``, never proxying page content through
the daemon.

Routes:

  GET /hub/pages         every page: slug, title (from <title>), mtime — newest first
  GET /hub/pins          the sidebar tabs, in order: [{slug, title}]
  POST /hub/pins         {"slug": ..., "title"?: ...} — pin a page (agents can
                         pin pages into the pinned bar). Already pinned = no-op.
  DELETE /hub/pins/{slug}  unpin

Pins live in the settings KV under ``PINS_KEY``. Every change goes through
these routes as one read-modify-write on the event loop, so an agent's pin and
a drag-to-remove in the UI cannot overwrite each other.

Self-contained on purpose: delete this file + the ``include_router`` line in
app.py and the feature is gone.
"""

from __future__ import annotations

import html
import logging
import os
import re
import time
from pathlib import Path

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pulsar.daemon.http import ApiError, ok as _ok

logger = logging.getLogger("pulsar.daemon.hubpages")

_ROOT_ENV = os.environ.get("PULSAR_HUB_PAGES_DIR", "").strip()
ROOT: Path | None = Path(_ROOT_ENV) if _ROOT_ENV else None
# The <title> sits in <head>, so a 16 KB read finds it cheaply across many pages.
TITLE_READ = 16384
PINS_KEY = "hub.pins"
_SLUG = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,199}$")
_TITLE = re.compile(r"<title[^>]*>(.*?)</title>", re.S | re.I)


def _title(index: Path, slug: str) -> str:
    try:
        with index.open("r", errors="ignore") as f:
            m = _TITLE.search(f.read(TITLE_READ))
    except OSError:
        m = None
    if m and m.group(1).strip():
        return html.unescape(re.sub(r"\s+", " ", m.group(1)).strip())
    return slug.replace("-", " ").title()


def list_pages(root: Path | None = None) -> list[dict]:
    """Every page under ``root``: one dict per slug directory, newest first."""
    root = ROOT if root is None else root
    if root is None:
        return []
    out = []
    try:
        names = os.listdir(root)
    except OSError as exc:
        logger.warning("hub pages: cannot list %s: %s", root, exc)
        return []
    for slug in names:
        if slug.startswith("."):
            continue
        p = root / slug
        if not p.is_dir():  # follows the symlink; a dangling link is skipped
            continue
        index = p / "index.html"
        try:
            mtime = (index if index.is_file() else p).stat().st_mtime
        except OSError:
            continue
        out.append({"slug": slug, "title": _title(index, slug),
                    "mtime": mtime, "has_index": index.is_file()})
    out.sort(key=lambda d: d["mtime"], reverse=True)
    return out


def page_title(slug: str, root: Path | None = None) -> str | None:
    """The page's title, or None when no such page is published."""
    root = ROOT if root is None else root
    if root is None:
        return None
    if not _SLUG.match(slug or "") or ".." in slug:
        return None
    p = root / slug
    if not p.is_dir():
        return None
    return _title(p / "index.html", slug)


def read_pins(store) -> list[dict]:
    raw = store.get_setting(PINS_KEY)
    if not isinstance(raw, list):
        return []
    return [{"slug": p["slug"], "title": str(p.get("title") or p["slug"])}
            for p in raw if isinstance(p, dict) and isinstance(p.get("slug"), str)]


def build_router() -> APIRouter:
    router = APIRouter()

    @router.get("/hub/pages")
    def pages():
        started = time.monotonic()
        items = list_pages()
        logger.info("hub pages served n=%d in %.3fs", len(items),
                    time.monotonic() - started)
        return JSONResponse({"ok": True, "value": items})

    # async on purpose: each read-modify-write runs on the event loop with no
    # await inside, so two writers can never interleave.
    @router.get("/hub/pins")
    async def get_pins(request: Request):
        return _ok(read_pins(request.app.state.store))

    @router.post("/hub/pins")
    async def add_pin(request: Request):
        try:
            body = await request.json()
        except ValueError:
            raise ApiError("bad_request", "body must be JSON {\"slug\": ...}", 400)
        slug = body.get("slug") if isinstance(body, dict) else None
        if not isinstance(slug, str) or not slug.strip():
            raise ApiError("bad_request", "slug is required", 400)
        slug = slug.strip().strip("/")
        title = page_title(slug)
        if title is None:
            raise ApiError("unknown_page",
                           f"no hub page '{slug}' under {ROOT} — publish it first", 404)
        if isinstance(body.get("title"), str) and body["title"].strip():
            title = body["title"].strip()[:200]
        st = request.app.state.store
        pins = read_pins(st)
        if any(p["slug"] == slug for p in pins):
            return _ok(pins)
        pins.append({"slug": slug, "title": title})
        st.put_setting(PINS_KEY, pins)
        logger.info("hub pin added slug=%s n=%d", slug, len(pins))
        return _ok(pins, status=201)

    @router.delete("/hub/pins/{slug}")
    async def remove_pin(request: Request, slug: str):
        st = request.app.state.store
        pins = read_pins(st)
        keep = [p for p in pins if p["slug"] != slug]
        if len(keep) != len(pins):
            st.put_setting(PINS_KEY, keep)
            logger.info("hub pin removed slug=%s n=%d", slug, len(keep))
        return _ok(keep)

    return router
