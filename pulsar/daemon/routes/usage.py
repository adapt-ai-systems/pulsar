"""Usage meters and token rollup routes."""

from __future__ import annotations

import asyncio
import logging
import time
import urllib.parse

from fastapi import APIRouter, Request

from pulsar.daemon.http import (
    ApiError,
    ok as _ok,
)

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- usage / FUEL ------------------------------------------------------- #
    # Stale-while-revalidate (2026-11-02): the rail polls every 60 s and each
    # collector caches 60 s, so nearly every poll waited on a fresh vendor
    # round trip (0.5 s, up to 6.7 s). After the first poll the route answers
    # with the last snapshots at once and refreshes behind the reply.
    usage_last: dict[str, tuple] = {}
    usage_refresh: list[asyncio.Task] = []

    async def poll_usage() -> tuple:
        from pulsar.daemon import usage_limits

        snaps = await asyncio.gather(
            asyncio.to_thread(usage_limits.poll_claude_if_due),
            asyncio.to_thread(usage_limits.poll_codex_if_due),
            asyncio.to_thread(usage_limits.poll_grok_if_due),
            asyncio.to_thread(usage_limits.poll_gemini_if_due),
        )
        usage_last["snaps"] = (time.monotonic(), tuple(snaps))
        return tuple(snaps)

    def refresh_usage_done(task: asyncio.Task) -> None:
        if not task.cancelled() and task.exception() is not None:
            logger.error("usage background refresh FAILED err=%s", task.exception())

    @router.get("/usage")
    async def get_usage():
        """Vendor usage-limit snapshots for the FUEL rail.

        Collectors cache for 60s and never raise — a broken source becomes a
        stale snapshot with a reason, and the rail renders `no data`, never an
        invented number. Only the first call waits on the vendors; later calls
        get the last snapshots (at most one refresh old) and start a refresh
        in the background when they are due.
        """
        from pulsar.daemon import usage_limits

        started = time.monotonic()
        if "snaps" in usage_last:
            polled_at, snaps = usage_last["snaps"]
            if (started - polled_at >= usage_limits.CACHE_TTL_SECONDS
                    and not (usage_refresh and not usage_refresh[0].done())):
                task = asyncio.create_task(poll_usage())
                task.add_done_callback(refresh_usage_done)
                usage_refresh[:] = [task]
        else:
            snaps = await poll_usage()
        claude, codex, grok, gemini = snaps
        for name, snap in (("claude", claude), ("codex", codex), ("grok", grok),
                           ("gemini", gemini)):
            if snap.get("stale_reason"):
                logger.warning("usage vendor=%s STALE reason=%s windows=%d",
                               name, snap["stale_reason"],
                               len(snap.get("windows") or []))
        logger.info(
            "usage served cl_windows=%d cx_windows=%d gx_windows=%d gm_windows=%d in %.3fs",
            len(claude.get("windows") or []),
            len(codex.get("windows") or []),
            len(grok.get("windows") or []),
            len(gemini.get("windows") or []),
            time.monotonic() - started,
        )
        return _ok({"vendors": {"claude": claude, "codex": codex, "grok": grok,
                                 "gemini": gemini}})

    # -- token usage rollup (per-model, from per-pulse events.jsonl) ------- #
    @router.get("/usage/tokens")
    async def get_token_usage(request: Request, days: int = 7,
                              since: str | None = None,
                              until: str | None = None):
        """Token totals per model per day across all pulses.

        Scans every pulse's events.jsonl ("usage" events) and attributes
        each event to the model active in that pulse's runner.log. Runs in
        ~2s for 30 days; cached 60s per window.
        """
        import datetime as _dt

        from pulsar.daemon import token_rollup

        started = time.monotonic()
        today = _dt.date.today()
        try:
            since_d = (_dt.date.fromisoformat(since) if since
                       else today - _dt.timedelta(days=max(1, days) - 1))
            until_d = (_dt.date.fromisoformat(until) + _dt.timedelta(days=1)
                       if until else today + _dt.timedelta(days=1))
        except ValueError:
            raise ApiError("bad_date", "since/until must be YYYY-MM-DD", 422)
        if until_d <= since_d or (until_d - since_d).days > 366:
            raise ApiError("bad_window", "empty or >366-day window", 422)
        payload = await asyncio.to_thread(token_rollup.get_window,
                                          since_d, until_d)
        logger.info("token usage served since=%s until=%s models=%d in %.3fs",
                    payload["since"], payload["until"],
                    len(payload["by_model"]), time.monotonic() - started)
        resp = _ok(payload)
        # Allow cross-origin fetches from the configured pages hub, if any.
        origin = request.headers.get("origin") or ""
        if origin:
            hub_url = request.app.state.store.get_setting("hub.url")
            if isinstance(hub_url, str) and hub_url.strip():
                parsed = urllib.parse.urlsplit(hub_url.strip())
                if parsed.scheme and parsed.netloc:
                    hub_origin = f"{parsed.scheme}://{parsed.netloc}"
                    if origin.rstrip("/") == hub_origin:
                        resp.headers["Access-Control-Allow-Origin"] = origin
        return resp

    return router
