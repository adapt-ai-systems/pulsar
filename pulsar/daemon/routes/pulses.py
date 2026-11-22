"""Pulse and grid routes: create, list, read, reorder, rename and move pulses."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from pulsar import models as models_mod
from pulsar.daemon import loop_triggers
from pulsar.daemon import (
    events_io,
    failover as failover_mod,
    lifecycle,
    paths,
    scheduler as scheduler_mod,
    watcher as watcher_mod,
)
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    invalidate_roster as _invalidate_roster,
    ok as _ok,
    store as _store,
)
from pulsar.daemon.routes.common import (
    _check_prompt_parts,
    _resolve_slot,
    _validate_effort,
    _write_message,
)
from pulsar.daemon.schemas import (
    CreateGridBody,
    CreatePulseBody,
    MovePulseBody,
    RenamePulseBody,
    ReorderGridsBody,
    ReorderPulsesBody,
    UpdateGridBody,
    harness_overrides_json as _harness_overrides_json,
    print_mode_compaction_error,
)
from pulsar.daemon.views import (
    default_stream_partials as _default_stream_partials,
    grid_dict as _grid_dict,
    pulse_view as _pulse_view,
)
from pulsar.launch import context as context_mod
from pulsar.store import (
    GridNotEmpty, Pulse, Store, UnknownGrid, UnknownPulse,
)
from pulsar.vessel import FAST_VESSELS

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- grids ------------------------------------------------------------- #
    @router.get("/grids")
    async def list_grids(request: Request):
        grids = _store(request).list_grids()
        return _ok([_grid_dict(g) for g in grids])

    @router.post("/grids")
    async def create_grid(request: Request, body: CreateGridBody):
        st = _store(request)
        try:
            grid = st.create_grid(name=body.name, position=body.position,
                                  parent_id=body.parent_id)
        except UnknownGrid as exc:
            raise ApiError("not_found", f"no grid {exc}", 404)
        except Exception as exc:
            logger.error("grid.create name=%r FAILED err=%s", body.name, exc)
            raise ApiError("create_failed", str(exc), 400)
        logger.info("grid.create id=%s name=%r ok", grid.id, grid.name)
        return _ok(_grid_dict(grid), status=201)

    @router.post("/grids/reorder")
    async def reorder_grids(request: Request, body: ReorderGridsBody):
        """Persist a workspace drag-reorder (new left-to-right id order)."""
        try:
            _store(request).reorder_grids(body.ids)
        except UnknownGrid as exc:
            raise ApiError("not_found", f"no grid {exc}", 404)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        logger.info("grid.reorder count=%d ok", len(body.ids))
        return _ok(True)

    @router.patch("/grids/{grid_id}")
    async def update_grid(request: Request, grid_id: str, body: UpdateGridBody):
        st = _store(request)
        try:
            kw = {}
            if "parent_id" in body.model_fields_set:
                kw["parent_id"] = body.parent_id
            grid = st.update_grid(grid_id, name=body.name, position=body.position, **kw)
        except UnknownGrid as exc:
            logger.error("grid.update id=%s NOT-FOUND (%s)", grid_id, exc)
            raise ApiError("not_found", f"no grid {exc}", 404)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        logger.info("grid.update id=%s ok", grid_id)
        return _ok(_grid_dict(grid))

    @router.delete("/grids/{grid_id}")
    async def delete_grid(request: Request, grid_id: str, detach: bool = False):
        """Delete a grid. ``?detach=true`` moves any pulses it still holds out of
        it (grid_id NULL) instead of refusing — the pulses themselves survive."""
        st = _store(request)
        try:
            detached = st.delete_grid(grid_id, detach=detach)
        except UnknownGrid:
            logger.error("grid.delete id=%s NOT-FOUND", grid_id)
            raise ApiError("not_found", f"no grid {grid_id}", 404)
        except GridNotEmpty as exc:
            logger.error("grid.delete id=%s NOT-EMPTY (%s)", grid_id, exc)
            raise ApiError("grid_not_empty", str(exc), 409)
        logger.info("grid.delete id=%s detached=%d ok", grid_id, detached)
        return _ok({"deleted": grid_id, "detached": detached})

    # -- create ------------------------------------------------------------ #
    @router.post("/pulses")
    async def create_pulse(request: Request, body: CreatePulseBody):
        started = time.monotonic()
        st = _store(request)
        registry_model = models_mod.model_by_id(body.model)
        vessel = (registry_model.vessel if registry_model and registry_model.slot
                  else body.vessel or models_mod.vessel_for(body.model))
        slot_binding = await _resolve_slot(request, body.model)
        # Optional loop schedule — both fields or neither (validate BEFORE we
        # create the pulse so a bad loop spec 400s without a half-made pulse).
        want_loop = body.loop_prompt is not None or body.loop_minutes is not None
        if want_loop:
            if body.loop_prompt is None or body.loop_minutes is None:
                raise ApiError("bad_request",
                               "loop_prompt and loop_minutes must be given "
                               "together", 400)
            try:
                scheduler_mod.validate_timing(
                    "loop", body.loop_minutes, None, None, None)
                loop_trig = loop_triggers.normalize(body.loop_triggers, "loop")
                loop_mode = loop_triggers.normalize_mode(body.loop_trigger_mode, "loop")
            except ValueError as exc:
                raise ApiError("bad_request", str(exc), 400)
        # Delegation: a spawning pulse must exist BEFORE we create
        # anything — a bad from_pulse_id 404s loudly, never spawns unwatched.
        spawner: Optional[Pulse] = None
        if body.from_pulse_id:
            try:
                spawner = st.get_pulse(body.from_pulse_id)
            except UnknownPulse:
                raise ApiError("not_found",
                               f"from_pulse_id {body.from_pulse_id!r} is not "
                               f"a known pulse", 404)
        # Per-model concurrency cap (models.json "max_concurrent"): local
        # serves share one KV pool, so a second or third session on the same
        # session on the same model evicts the first. Checked BEFORE anything
        # is created; a breach is a 409 model_busy and nothing is spawned.
        cap = registry_model.max_concurrent if registry_model else None
        if cap is not None:
            live = [p for p in st.list_pulses()
                    if p.model == body.model and p.state != "stopped"]
            if len(live) >= cap:
                raise ApiError(
                    "model_busy",
                    f"{body.model} already has {len(live)} running pulse(s) "
                    f"(cap {cap}: {', '.join(p.id[:8] for p in live)}); "
                    f"stop one or pick another model", 409)
        # Same-workspace default: a helper spawned by a pulse lands in the
        # spawner's grid unless the caller names a grid_id explicitly.
        grid_id = body.grid_id
        if grid_id is None and spawner is not None:
            grid_id = spawner.grid_id
        settings: dict[str, Any] = {}
        if spawner is not None:
            settings["spawned_by"] = spawner.id
        if body.role not in (None, "lead", "helper"):
            raise ApiError("bad_role",
                           f"role must be 'lead' or 'helper', got {body.role!r}",
                           400)
        if body.role == "helper":
            if spawner is None:
                raise ApiError("bad_role", "role 'helper' requires "
                               "from_pulse_id (the lead it reports to)", 400)
            settings["role"] = "helper"
            settings["lead_id"] = spawner.id
        if _default_stream_partials(vessel, body.model):
            settings["stream_partials"] = True
        settings["stream_reasoning"] = True
        if body.memory_max:
            settings["memory_max"] = body.memory_max
        # Clean-by-default diet: stored explicitly so every future
        # spawn (restore/switch/handoff/scheduler wake) applies the same diet.
        settings["diet"] = {"clean": body.clean, "memory": body.memory,
                            "skills": body.skills, "cages": body.cages}
        if body.prompt_parts is not None:
            settings["diet"]["prompt_parts"] = _check_prompt_parts(
                body.prompt_parts)
        # Harness overrides (an experiment): validated by the schema
        # (a 422 names any unknown key); only the keys actually sent are
        # stored, so callers can verify exactly what a pulse got.
        ho = _harness_overrides_json(body.harness_overrides)
        comp_err = print_mode_compaction_error(vessel, body.model, ho)
        if comp_err:
            raise ApiError("bad_harness_overrides", comp_err, 422)
        if ho:
            settings["diet"]["harness_overrides"] = ho
        if slot_binding is not None:
            settings["diet"]["slot"] = slot_binding
        # Copy the user-chosen default backup setting if one is configured
        # (unset = no backup; editable per pulse via PATCH /pulses/{id}/backup).
        default_backup = failover_mod.default_backup_from_store(st)
        if default_backup is not None:
            settings["backup"] = default_backup
        # Reasoning effort (claude/codex): validated against the vessel BEFORE
        # the pulse exists, stored so every future spawn applies it.
        if body.effort:
            body.effort = models_mod.canonical_effort(
                body.model, body.effort,
                slot_binding["served_id"] if slot_binding is not None else None)
            if slot_binding is not None:
                levels = models_mod.slot_efforts(slot_binding["served_id"])
                if body.effort not in levels:
                    allowed = ", ".join(levels) if levels else \
                        "none — this CLI has no effort control"
                    raise ApiError(
                        "bad_effort",
                        f"effort {body.effort!r} is not supported by model "
                        f"{slot_binding['served_id']!r} (allowed: {allowed})",
                        400)
            else:
                _validate_effort(body.effort, vessel, body.model)
            settings["effort"] = body.effort
        # Fast mode (codex-only service tier): refused honestly for vessels
        # whose CLI has no such switch, stored so every future spawn applies it.
        if body.fast:
            if vessel not in FAST_VESSELS:
                raise ApiError("bad_fast",
                               f"fast mode is not supported by vessel "
                               f"{vessel!r} (only: {', '.join(FAST_VESSELS)})",
                               400)
            settings["fast"] = True
        try:
            pulse = st.create_pulse(
                title=body.title,
                model=body.model,
                vessel=vessel,
                grid_id=grid_id,
                state="running",
                handoff_tokens=body.handoff_tokens,
                settings=settings,
            )
        except Exception as exc:
            logger.error("create title=%r FAILED err=%s in %.3fs",
                         body.title, exc, time.monotonic() - started)
            raise ApiError("create_failed", str(exc), 400)

        # Create the loop schedule atomically with the pulse. Already
        # validated above; do it before spawn so a scheduled pulse is complete
        # even if the spawn fails (it'll wake itself when due).
        if want_loop:
            try:
                st.create_schedule(pulse.id, "loop", body.loop_prompt,
                                   interval_minutes=body.loop_minutes,
                                   triggers=loop_trig, trigger_mode=loop_mode)
            except Exception as exc:
                logger.error("create pulse=%s loop-schedule FAILED err=%s",
                             pulse.id, exc)
                raise ApiError("create_failed",
                               f"loop schedule failed: {exc}", 400)

        home = paths.pulse_home(pulse.id)
        home.mkdir(parents=True, exist_ok=True)
        # The store row exists — every /live frame served from here on must
        # include it, even one built from a pre-create cached snapshot.
        _invalidate_roster(request)
        if settings.get("role") == "helper":
            context_mod.write_role(home, "helper", lead_id=spawner.id,
                                   lead_title=spawner.title)
        try:
            await lifecycle.astart(st, pulse, resume=None, settings=settings,
                                   memory_max=body.memory_max)
        except Exception as exc:
            # Spawn failed — the store row exists but nothing is running. Mark
            # it stopped with a reason rather than leave a lie in the grid.
            logger.error("create pulse=%s SPAWN-FAILED err=%s in %.3fs — "
                         "marking stopped", pulse.id, exc,
                         time.monotonic() - started)
            try:
                st.update_settings(pulse.id, {"stop_reason": f"spawn failed: {exc}"})
                st.set_state(pulse.id, "stopped")
            except Exception:
                logger.exception("create pulse=%s cleanup after spawn fail raised",
                                 pulse.id)
            # The row stays (restorable), so include its id in the error detail.
            raise ApiError("spawn_failed", f"runner did not start: {exc}", 502,
                           {"pulse_id": pulse.id})

        logger.info("create pulse=%s title=%r spawned in %.3fs",
                    pulse.id, pulse.title, time.monotonic() - started)

        # Delegation watch BEFORE the brief: even if the brief
        # write fails below, the watch still exists to catch the idle helper.
        delegation: Optional[dict[str, Any]] = None
        if spawner is not None:
            delegation = {}
            try:
                watch = st.create_watch(
                    spawner.id, pulse.id, "spawn",
                    next_check_at=watcher_mod.next_check_iso())
                delegation["watch_id"] = watch.id
            except Exception as exc:
                # The spawn itself succeeded; report the missing watch rather
                # than fail the whole call — but loudly, in log AND response.
                logger.error("create pulse=%s watch FAILED err=%s",
                             pulse.id, exc)
                delegation["watch_error"] = str(exc)

        if body.first_message:
            text = body.first_message
            if spawner is not None:
                text = (f"[message from {spawner.title!r} "
                        f"(pulse {spawner.id})] {text}")
            try:
                name = _write_message(pulse.id, text, spawner=spawner,
                                      via="first_message")
            except OSError as exc:
                logger.error("create pulse=%s first_message FAILED err=%s",
                             pulse.id, exc)
                raise ApiError(
                    "ingest_failed",
                    f"pulse {pulse.id} spawned but the first_message did NOT "
                    f"land: {exc}. Re-send it as a normal message.", 500)
            if delegation is not None:
                delegation["first_message"] = name

        fresh = st.get_pulse(pulse.id)
        view = _pulse_view(fresh)
        # Hot link the spawner pastes into chat: clicking it opens the new
        # pulse's pane in the app (markdown link, same-origin relative URL).
        label = (fresh.title or fresh.id).replace("[", "(").replace("]", ")")
        view["link"] = f"[{label}](/?pulse-focus={fresh.id})"
        # The same link as a marker in the spawner's own pane.
        # Written by the daemon, so it shows whether or not the model pastes
        # `link` into its reply. emit() logs instead of raising.
        if spawner is not None:
            events_io.emit(spawner.id, {
                "type": "pulse_spawned", "pulse_id": fresh.id,
                "title": fresh.title or fresh.id, "model": fresh.model})
        if delegation is not None:
            view["delegation"] = delegation
        return _ok(view, status=201)

    # -- list / get -------------------------------------------------------- #
    # ?slim=true: an archived row keeps only the settings the drawer and
    # Watchers lists read (mode tag). diet, backup and
    # lineage were 1.9 MB of the 3.9 MB archived roster (2026-11-02).
    slim_archived_settings = ("mode",)

    @router.get("/pulses")
    async def list_pulses(request: Request, grid_id: Optional[str] = None,
                          state: Optional[str] = None,
                          include_archived: bool = False,
                          slim: bool = False):
        store = _store(request)

        def build() -> JSONResponse:
            try:
                pulses = store.list_pulses(
                    grid_id=grid_id, state=state, include_archived=include_archived)
            except ValueError as exc:
                raise ApiError("bad_request", str(exc), 400)
            views = []
            for p in pulses:
                view = _pulse_view(p)
                if slim and p.state == "archived":
                    s = view["settings"]
                    view["settings"] = {k: s[k] for k in slim_archived_settings if k in s}
                views.append(view)
            return _ok(views)

        # Off the event loop: with every archived pulse this is 5k+ rows and a
        # multi-megabyte reply — 70 ms that stalled every other request.
        return await asyncio.to_thread(build)

    @router.post("/pulses/reorder")
    async def reorder_pulses(request: Request, body: ReorderPulsesBody):
        """Persist a wall drag-reorder (new left-to-right id order)."""
        try:
            _store(request).reorder_pulses(body.ids)
        except UnknownPulse as exc:
            raise ApiError("unknown_pulse", str(exc), 404)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        logger.info("pulse.reorder count=%d ok", len(body.ids))
        return _ok(True)

    @router.get("/pulses/{pulse_id}")
    async def get_pulse(request: Request, pulse_id: str):
        """One pulse. This is the path that still probes the runner's socket —
        it costs at most one round trip, and a caller asking about a single
        pulse is the one who wants the authoritative answer."""
        pulse = _get_pulse(request, pulse_id)
        return _ok(_pulse_view(pulse, probe=True))

    # -- move to grid ------------------------------------------------------ #
    @router.patch("/pulses/{pulse_id}/grid")
    async def move_pulse_grid(request: Request, pulse_id: str,
                              body: MovePulseBody):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        try:
            updated = st.set_grid(pulse.id, body.grid_id)
        except UnknownGrid:
            logger.error("move pulse=%s grid=%s GRID-NOT-FOUND",
                         pulse.id, body.grid_id)
            raise ApiError("not_found", f"no grid {body.grid_id}", 404)
        logger.info("move pulse=%s grid=%s ok", pulse.id, body.grid_id)
        return _ok(_pulse_view(updated))

    # -- rename ------------------------------------------------------------ #
    @router.patch("/pulses/{pulse_id}/title")
    async def rename_pulse(request: Request, pulse_id: str,
                           body: RenamePulseBody):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        try:
            updated = st.set_title(pulse.id, body.title)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        logger.info("rename pulse=%s title=%r ok", pulse.id, updated.title)
        return _ok(_pulse_view(updated))

    return router
