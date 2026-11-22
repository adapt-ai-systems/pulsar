"""Watch routes: delegation watches and condition watches."""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import Optional

from fastapi import APIRouter, Request

from pulsar.daemon import condwatch as condwatch_mod
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    ok as _ok,
    store as _store,
)
from pulsar.daemon.schemas import CreateConditionWatchBody
from pulsar.store import UnknownConditionWatch, UnknownPulse, UnknownWatch

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- delegation watches ------------------------------------------------ #
    @router.get("/watches")
    async def list_watches(request: Request, state: Optional[str] = "watching"):
        """Inspect delegation watches. Defaults to live (``watching``);
        pass ``?state=all`` for every row including closed."""
        if state in (None, "", "all"):
            watches = _store(request).list_watches()
        else:
            watches = _store(request).list_watches(state=state)
        return _ok([asdict(w) for w in watches])

    @router.delete("/watches/{watch_id}")
    async def delete_delegation_watch(request: Request, watch_id: str):
        """Close a delegation watch early (outcome ``cancelled``). A pulse that
        messaged a helper with from_pulse_id and no longer wants to be
        followed — e.g. a pulse on a loop or waiting on its own watches —
        had no way to stop the stall alerts (2026-09-09). 404 unknown id;
        an already-closed watch is a no-op that returns it as is."""
        st = _store(request)
        try:
            watch = st.get_watch(watch_id)
        except UnknownWatch:
            raise ApiError("not_found", f"no delegation watch {watch_id}", 404)
        if watch.state == "closed":
            return _ok(asdict(watch))
        closed = st.close_watch(watch_id, "cancelled")
        logger.info("watch.delete id=%s spawner=%s target=%s ok",
                    watch_id, watch.spawner_id, watch.target_id)
        return _ok(asdict(closed))

    # -- condition watches -------------------------------------------------- #
    @router.post("/watches/conditions")
    async def create_condition_watch(request: Request,
                                     body: CreateConditionWatchBody):
        """Register a daemon-evaluated condition watch."""
        st = _store(request)
        pulse = _get_pulse(request, body.pulse_id)
        errs = condwatch_mod.validate_spec(body.kind, body.spec)
        if errs:
            raise ApiError("bad_request", "; ".join(errs), 400)
        if body.interval_seconds < 30:
            raise ApiError("bad_request", "interval_seconds must be >= 30", 400)
        try:
            watch = st.create_condition_watch(
                pulse.id, body.kind, body.spec,
                next_check_at=condwatch_mod.next_check_iso(
                    seconds=body.interval_seconds),
                mode=body.mode, interval_s=body.interval_seconds,
                note=body.note)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        except UnknownPulse as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok(asdict(watch), status=201)

    @router.get("/watches/conditions")
    async def list_condition_watches(request: Request,
                                     pulse_id: Optional[str] = None,
                                     state: Optional[str] = None):
        """Inspect condition watches (``?state=watching`` for live ones)."""
        watches = _store(request).list_condition_watches(
            state=state, pulse_id=pulse_id)
        return _ok([asdict(w) for w in watches])

    @router.delete("/watches/conditions/{watch_id}")
    async def delete_condition_watch(request: Request, watch_id: str):
        """Delete one condition watch (owner cleanup / manual removal)."""
        st = _store(request)
        try:
            st.delete_condition_watch(watch_id)
        except UnknownConditionWatch as exc:
            raise ApiError("not_found", str(exc), 404)
        return _ok({"deleted": watch_id})

    return router
