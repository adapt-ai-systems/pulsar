"""Pulse lifecycle routes: pause, interrupt, restore, reset, fork, switch model, archive, delete and handoff."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from fastapi import APIRouter, Request

from pulsar import models as models_mod
from pulsar.daemon import (
    events_io,
    failover as failover_mod,
    handoff as handoff_mod,
    lifecycle,
    paths,
    sockclient,
)
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    invalidate_roster as _invalidate_roster,
    ok as _ok,
    store as _store,
)
from pulsar.daemon.routes.common import _resolve_slot, _validate_effort
from pulsar.daemon.schemas import (
    ArchiveIdleBody,
    ArchivePulseBody,
    ForkPulseBody,
    SwitchModelBody,
    print_mode_compaction_error,
)
from pulsar.daemon.views import pulse_view as _pulse_view
from pulsar.launch import context as context_mod
from pulsar.launch import units as units_mod
from pulsar.store import InvalidStateTransition, Pulse, Store, StoreError
from pulsar.vessel import efforts_for, FAST_VESSELS

logger = logging.getLogger("pulsar.daemon")


# Fork copies a pulse's JSONL history a chunk at a time, never whole-file.
FORK_COPY_CHUNK = 1024 * 1024

# Delete waits this long, this many times, for a lingering transient unit to
# leave systemd before it refuses (a socket-stopped runner sits in
# "deactivating" for a moment).
DELETE_UNIT_SETTLE_S = 0.5
DELETE_UNIT_SETTLE_TRIES = 10
DELETED_RECORD_FILE = "pulse-record.json"  # a deleted pulse's last view

# --------------------------------------------------------------------------- #
# Home-dir teardown (rename, never rm -rf — boring and reversible)
# --------------------------------------------------------------------------- #
def _archive_home_dir(pulse_id: str,
                      record: Optional[dict] = None) -> Optional[str]:
    """Rename a deleted pulse's home to ``<id>.deleted-<utcstamp>``.

    Boring and reversible: the transcript/heartbeat/inbox tree survives on disk
    under a new name rather than being erased, so a mistaken delete is
    recoverable. Returns the new dir name, or None if there was no home to move.
    A collision (same-second re-delete) appends the microsecond stamp already in
    the name, so it's effectively unique; a genuine clash is logged and skipped.
    """
    home = paths.pulse_home(pulse_id)
    if not home.exists():
        logger.info("delete pulse=%s no home dir to move", pulse_id)
        return None
    # Keep a record of every deleted pulse — the row is gone, so its last view
    # goes in the home before rename.
    if record is not None:
        try:
            (home / DELETED_RECORD_FILE).write_text(
                json.dumps({**record, "deleted_at":
                            datetime.now(timezone.utc).isoformat()},
                           indent=2, default=str), encoding="utf-8")
        except OSError as exc:
            logger.error("delete pulse=%s record write FAILED err=%s",
                         pulse_id, exc)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    target = home.with_name(f"{pulse_id}.deleted-{stamp}")
    try:
        home.rename(target)
    except OSError as exc:
        # Do not fail the delete — the store row is already gone. Log loud.
        logger.error("delete pulse=%s home rename FAILED err=%s (row already "
                     "removed; home left in place at %s)",
                     pulse_id, exc, home)
        return None
    logger.info("delete pulse=%s home moved %s -> %s",
                pulse_id, home.name, target.name)
    return target.name

def _unit_loaded(pulse_id: str) -> bool:
    """True while systemd still has this pulse's transient unit (blocking)."""
    return pulse_id in units_mod.list_pulse_units()

# --------------------------------------------------------------------------- #
# Stop helper (socket first, systemd fallback)
# --------------------------------------------------------------------------- #
def _stop_or_502_locked(pulse_id: str) -> str:
    """Stop a running pulse's runner (``lifecycle.stop_locked``): socket op,
    systemd fallback. The caller holds the pulse lock. Returns the path taken
    ("socket" or "unit"). Raises :class:`ApiError` only if BOTH fail."""
    try:
        return lifecycle.stop_locked(pulse_id)
    except lifecycle.StopFailed as exc:
        raise ApiError("stop_failed", str(exc), 502)


def build_router() -> APIRouter:
    router = APIRouter()

    # -- pause ------------------------------------------------------------- #
    @router.post("/pulses/{pulse_id}/pause")
    async def pause_pulse(request: Request, pulse_id: str):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)

        def _pause_tx() -> Pulse:
            with lifecycle.pulse_lock(pulse.id):
                cur = st.get_pulse(pulse.id)
                if cur.state != "running":
                    logger.info("pause pulse=%s no-op (state=%s)",
                                cur.id, cur.state)
                    return cur
                via = _stop_or_502_locked(cur.id)
                try:
                    updated = st.set_state(cur.id, "stopped")
                except InvalidStateTransition as exc:
                    raise ApiError("bad_state", str(exc), 409)
                # The turn the user stopped left its message in feeding/,
                # and restore's crash recovery replayed it. Note which messages
                # were in flight; restore retires exactly those (the runner is
                # gone by then, so nothing races it). A crash keeps its
                # at-least-once replay.
                home = paths.pulse_home(cur.id)
                in_flight = sorted(f.name for f in
                                   (home / "inbox" / "feeding").glob("*.json"))
                if in_flight:
                    (home / "stopped_in_flight.json").write_text(
                        json.dumps(in_flight))
                events_io.emit(cur.id, {"type": "pulse_stopped", "by": "user"})
                logger.info("pause pulse=%s stopped via=%s in_flight=%d",
                            cur.id, via, len(in_flight))
                return updated

        return _ok(_pulse_view(await asyncio.to_thread(_pause_tx)))

    # -- interrupt (abort the in-flight turn, pulse stays up) --------------- #
    @router.post("/pulses/{pulse_id}/interrupt")
    async def interrupt_pulse(request: Request, pulse_id: str):
        """Abort the pulse's current vessel turn without stopping the pulse.

        Socket-only (no systemd fallback — killing the unit is what pause is
        for). 409 when the pulse is not running; the runner itself answers
        ``not_interruptible`` for vessels with no turn abort (claude/qwen) and
        ``noop`` when it was idle anyway.
        """
        pulse = _get_pulse(request, pulse_id)
        if pulse.state != "running":
            raise ApiError("bad_state",
                           f"pulse is {pulse.state}, nothing to interrupt", 409)
        try:
            reply = await asyncio.to_thread(
                sockclient.interrupt, paths.sock_path(pulse.id), pulse.id)
        except sockclient.SocketError as exc:
            logger.error("interrupt pulse=%s socket FAILED: %s", pulse.id, exc)
            raise ApiError("interrupt_failed", str(exc), 502)
        if not reply.get("ok"):
            raise ApiError(reply.get("error") or "interrupt_failed",
                           "the runner refused the interrupt "
                           f"({reply.get('error')})", 409)
        logger.info("interrupt pulse=%s ok noop=%s", pulse.id,
                    reply.get("noop", False))
        return _ok({"interrupted": not reply.get("noop", False)})

    # -- restore ----------------------------------------------------------- #
    @router.post("/pulses/{pulse_id}/restore")
    async def restore_pulse(request: Request, pulse_id: str):
        started = time.monotonic()
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)

        def _restore_tx() -> Pulse:
            with lifecycle.pulse_lock(pulse.id):
                cur = st.get_pulse(pulse.id)
                if cur.state == "running":
                    logger.info("restore pulse=%s already running — no-op",
                                cur.id)
                    return cur
                # Archived is refused BEFORE the spawn, not after: the store
                # forbids archived -> running, so spawning first left a live
                # runner attached to a pulse the store still called archived,
                # and the 409 came back with nothing cleaned up (the
                # dead-runner sweep only scans running rows).
                if cur.state == "archived":
                    logger.error("restore pulse=%s REFUSED (archived) — "
                                 "unarchive first", cur.id)
                    raise ApiError("bad_state",
                                   "cannot restore an archived pulse — "
                                   "unarchive it first (that lands it in "
                                   "stopped)", 409)

                beat = paths.read_heartbeat(cur.id)
                resume = beat.get("cli_session_id") if beat else None
                logger.info("restore pulse=%s resume=%s", cur.id,
                            resume or "-")
                # A turn the user stopped must not replay on restore.
                lifecycle.retire_stopped_in_flight(
                    cur.id, paths.pulse_home(cur.id))
                try:
                    lifecycle.start_locked(st, cur, resume=resume)
                except Exception as exc:
                    logger.error("restore pulse=%s SPAWN-FAILED err=%s in "
                                 "%.3fs", cur.id, exc,
                                 time.monotonic() - started)
                    raise ApiError("spawn_failed",
                                   f"runner did not start: {exc}", 502)

                # Clear any stale stop_reason now that it's live again.
                try:
                    st.update_settings(cur.id, {"stop_reason": None})
                    updated = st.set_state(cur.id, "running")
                except InvalidStateTransition as exc:
                    raise ApiError("bad_state", str(exc), 409)
                logger.info("restore pulse=%s running resume=%s in %.3fs",
                            cur.id, resume or "-", time.monotonic() - started)
                return updated

        return _ok(_pulse_view(await asyncio.to_thread(_restore_tx)))

    # -- reset (fresh context) ---------------------------------------------- #
    @router.post("/pulses/{pulse_id}/reset")
    async def reset_pulse(request: Request, pulse_id: str):
        """Fresh-context restart: stop the runner (if live) and respawn with
        resume=None. The successor CLI session starts with an empty context
        and loads only the pulse's diet (skills/memory re-materialize on
        every spawn) — for pulses reused across one-off tasks. Unlike a
        handoff, nothing is carried over on purpose."""
        started = time.monotonic()
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        if pulse.state == "archived":
            raise ApiError("bad_state", "cannot reset an archived pulse", 409)

        def _reset_tx() -> Pulse:
            with lifecycle.pulse_lock(pulse.id):
                cur = st.get_pulse(pulse.id)
                if cur.state == "archived":
                    raise ApiError("bad_state",
                                   "cannot reset an archived pulse", 409)
                via = "-"
                if cur.state == "running":
                    via = _stop_or_502_locked(cur.id)

                home = paths.pulse_home(cur.id)
                home.mkdir(parents=True, exist_ok=True)
                # Messages are acked only at the turn boundary, so a reset
                # landing right after a pulse reports done used to redeliver
                # the turn's message and the fresh session redid the whole
                # task (2026-08-15). The runner claims in-turn
                # messages into inbox/feeding/; a deliberate reset retires
                # those as consumed. Queued-unseen messages in new/ survive on
                # purpose.
                feeding = home / "inbox" / "feeding"
                if feeding.is_dir():
                    acked = home / "inbox" / "acked"
                    acked.mkdir(parents=True, exist_ok=True)
                    for f in feeding.glob("*.json"):
                        try:
                            f.replace(acked / f.name)
                            logger.info("reset pulse=%s retired in-flight "
                                        "message %s", cur.id, f.name)
                        except OSError as exc:
                            logger.warning("reset pulse=%s could not retire "
                                           "%s: %s", cur.id, f.name, exc)
                try:
                    # resume=None IS the reset — a brand-new CLI session.
                    lifecycle.start_locked(st, cur, resume=None)
                except Exception as exc:
                    try:
                        st.set_state(cur.id, "stopped")
                        st.update_settings(
                            cur.id, {"stop_reason": "reset respawn failed"})
                    except StoreError:
                        logger.exception("reset pulse=%s state cleanup failed",
                                         cur.id)
                    logger.error("reset pulse=%s RESPAWN FAILED err=%s in "
                                 "%.3fs", cur.id, exc,
                                 time.monotonic() - started)
                    raise ApiError("spawn_failed",
                                   f"runner did not restart: {exc}", 502)

                try:
                    st.update_settings(cur.id, {"stop_reason": None})
                    updated = st.set_state(cur.id, "running")
                except InvalidStateTransition as exc:
                    raise ApiError("bad_state", str(exc), 409)
                # A marker in the transcript: without it the pane read as one
                # conversation and the pulse "forgot" what sat right above.
                events_io.emit(cur.id, {"type": "context_reset"})
                logger.info("reset pulse=%s fresh session (stop via=%s) in "
                            "%.3fs", cur.id, via, time.monotonic() - started)
                return updated

        return _ok(_pulse_view(await asyncio.to_thread(_reset_tx)))

    # -- fork (branch the conversation into a new pulse) --------------------- #
    def _copy_trimmed_jsonl(src: Path, dst: Path) -> None:
        """Copy a JSONL file keeping only complete lines.

        A RUNNING source pulse may be mid-append; a torn final line would be
        junk history for the CLI resuming from the copy. Trim to the last
        newline instead of copying the raw tail.

        Copied in chunks and truncated at the end: a long-lived history can be
        hundreds of megabytes, and holding the whole file plus a sliced copy in
        the daemon competes with the runners it supervises.
        """
        written = 0
        keep = 0  # bytes up to and including the last newline seen
        with src.open("rb") as fh_in, dst.open("wb") as fh_out:
            while True:
                chunk = fh_in.read(FORK_COPY_CHUNK)
                if not chunk:
                    break
                fh_out.write(chunk)
                nl = chunk.rfind(b"\n")
                if nl >= 0:
                    keep = written + nl + 1
                written += len(chunk)
            if keep != written:
                fh_out.truncate(keep)

    def _fork_copy_history(vessel: str, src_hist: Path, home: Path,
                           cli_sid: str, source_id: str,
                           new_id: str) -> None:
        """Copy a fork's CLI history + pane transcript (blocking disk work).

        Runs in a worker thread via asyncio.to_thread — a multi-hundred-MB
        copytree must not freeze every in-flight request.
        """
        from pulsar.vessel.profiles import profile
        dst_src = profile(vessel).history_source(home / "cli", cli_sid)
        assert dst_src is not None
        dst_hist, kind = dst_src
        if kind == "dir":
            dst_hist.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(src_hist, dst_hist)
        else:
            dst_hist.parent.mkdir(parents=True, exist_ok=True)
            _copy_trimmed_jsonl(src_hist, dst_hist)
        # The pane transcript rides along so the fork's tile shows the
        # conversation it branched from (same trim rule — live source).
        src_events = paths.events_path(source_id)
        if src_events.exists():
            _copy_trimmed_jsonl(src_events, paths.events_path(new_id))

    @router.post("/pulses/{pulse_id}/fork")
    async def fork_pulse(request: Request, pulse_id: str, body: ForkPulseBody):
        """Branch a pulse: a NEW pulse whose CLI session starts with the
        source's full conversation history up to now, then diverges (the pi
        agent's ``/tree`` fork, pulse-shaped). The source keeps running
        untouched — CLI history lives per-project-cwd, so the copy under the
        new pulse's home is a genuinely separate timeline.

        Inherited: model, vessel, grid, diet, effort, fast, memory cap,
        handoff threshold, and the pane transcript (events.jsonl). NOT
        inherited: loop/schedules (a forked loop would double-fire) and
        stop_reason/handoff bookkeeping.
        """
        from pulsar.vessel.profiles import PROFILES
        started = time.monotonic()
        st = _store(request)
        source = _get_pulse(request, pulse_id)
        if source.state == "archived":
            raise ApiError("bad_state", "cannot fork an archived pulse", 409)
        prof = PROFILES.get(source.vessel)
        if prof is None or prof.history_source(Path("."), "x") is None:
            raise ApiError("fork_unsupported",
                           f"fork is not supported for the {source.vessel} vessel yet", 400)

        beat = paths.read_heartbeat(source.id)
        cli_sid = (beat or {}).get("cli_session_id")
        if not cli_sid:
            raise ApiError("no_session",
                           "pulse has no CLI session to fork yet", 409)

        # Pre-flight the source history BEFORE creating the row, so a missing
        # file 409s without leaving a half-made pulse behind.
        src_home = paths.pulse_home(source.id)
        src_info = prof.history_source(src_home / "cli", cli_sid)
        assert src_info is not None
        src_hist, hist_kind = src_info
        hist_ok = src_hist.is_dir() if hist_kind == "dir" else src_hist.is_file()
        if not hist_ok:
            logger.error("fork pulse=%s source history missing at %s",
                         source.id, src_hist)
            raise ApiError("no_history",
                           f"source CLI history not found ({src_hist})", 409)

        settings = dict(source.settings)
        settings.pop("stop_reason", None)
        settings.pop("last_handoff_at", None)
        title = body.title or f"{source.title} (fork)"
        try:
            pulse = st.create_pulse(
                title=title, model=source.model, vessel=source.vessel,
                grid_id=source.grid_id, state="running",
                handoff_tokens=source.handoff_tokens, settings=settings)
        except Exception as exc:
            logger.error("fork pulse=%s create FAILED err=%s", source.id, exc)
            raise ApiError("fork_failed", str(exc), 400)
        # New pulse row: /live must never serve a frame without it.
        _invalidate_roster(request)

        home = paths.pulse_home(pulse.id)
        (home / "cli").mkdir(parents=True, exist_ok=True)
        # A forked helper keeps its lead (settings were copied wholesale).
        if settings.get("role") == "helper" and settings.get("lead_id"):
            lead = st.get_pulse(settings["lead_id"])
            context_mod.write_role(home, "helper", lead_id=settings["lead_id"],
                             lead_title=lead.title if lead else None)
        try:
            # Copying hundreds of MB of history + the transcript is blocking
            # disk work — off the event loop so the daemon keeps serving.
            await asyncio.to_thread(
                _fork_copy_history, source.vessel, src_hist, home,
                cli_sid, source.id, pulse.id)
        except OSError as exc:
            logger.error("fork pulse=%s history copy FAILED err=%s in %.3fs",
                         source.id, exc, time.monotonic() - started)
            try:
                st.update_settings(pulse.id,
                                   {"stop_reason": f"fork copy failed: {exc}"})
                st.set_state(pulse.id, "stopped")
            except StoreError:
                logger.exception("fork pulse=%s cleanup after copy fail raised",
                                 pulse.id)
            raise ApiError("fork_failed", f"history copy failed: {exc}", 500)

        try:
            await lifecycle.astart(st, pulse, resume=cli_sid,
                                   settings=settings)
        except Exception as exc:
            logger.error("fork pulse=%s SPAWN-FAILED err=%s in %.3fs — "
                         "marking stopped", pulse.id, exc,
                         time.monotonic() - started)
            try:
                st.update_settings(pulse.id,
                                   {"stop_reason": f"spawn failed: {exc}"})
                st.set_state(pulse.id, "stopped")
            except StoreError:
                logger.exception("fork pulse=%s cleanup after spawn fail raised",
                                 pulse.id)
            raise ApiError("spawn_failed", f"runner did not start: {exc}", 502)

        logger.info("fork source=%s new=%s title=%r resume=%s in %.3fs",
                    source.id, pulse.id, title, cli_sid,
                    time.monotonic() - started)
        return _ok(_pulse_view(st.get_pulse(pulse.id)), status=201)

    # -- manual handoff (context-capture button, ChatPane actions row) ----- #
    @router.post("/pulses/{pulse_id}/handoff")
    async def handoff_pulse(request: Request, pulse_id: str):
        """Manually trigger the context-handoff engine for a pulse. Mirrors
        the reset button's two-step confirm in the UI; here the daemon just
        queues the engine off the request path (the handoff waits for the
        live session to write its handoff prose, which can take minutes) and
        returns immediately with ``{"started": true}``."""
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        # Optional JSON body {"force": true}: the old model is dead — skip
        # asking it and seed the successor with the transcript tail + file path
        # (handoff.forced_seed).
        force = False
        note_path = None
        try:
            body = await request.json()
            force = bool(isinstance(body, dict) and body.get("force"))
            if isinstance(body, dict) and body.get("note_path"):
                note_path = str(body["note_path"])
        except Exception:
            force = False
        # Optional {"note_path": "/abs/file.md"} (handoff review 2026-10-02):
        # the pulse wrote its own note after the early warning — seed from
        # it, skip the request and the wait.
        note_text = None
        if note_path is not None:
            if force:
                raise ApiError("bad_request",
                               "note_path and force are exclusive", 400)
            try:
                note_text = handoff_mod.read_note_file(note_path)
            except ValueError as exc:
                raise ApiError("bad_note", str(exc), 400)
        if pulse.state == "archived":
            raise ApiError("bad_state", "cannot hand off an archived pulse",
                           409)
        if pulse.state != "running" and not force:
            raise ApiError("not_running",
                           "pulse is not running — force the handoff to seed "
                           "a successor from the transcript, or restore it "
                           "first", 409)
        # Single-flight: run_handoff has its own guard via the durable
        # handoff_state marker, but we surface it here as a clean 409 instead
        # of silently dropping the request in a background task.
        # Reserve atomically: the check and write are both before any await
        # in this handler, so a concurrent manual handoff cannot slip in
        # after we mark it in-flight.
        reason = "forced" if force else ("note_file" if note_text else "manual")
        if not handoff_mod.reserve(pulse.id, reason):
            raise ApiError("handoff_in_flight",
                           "a handoff is already in progress for this pulse",
                           409)
        extra = {"note_text": note_text} if note_text is not None else {}

        async def _manual_handoff_task():
            try:
                await handoff_mod.engine.run_handoff(st, pulse, reason=reason,
                                              reserved=True, force=force,
                                              **extra)
            except Exception:
                logger.exception("handoff-manual pulse=%s FAILED", pulse.id)

        asyncio.create_task(_manual_handoff_task())
        logger.info("handoff-manual pulse=%s queued force=%s", pulse.id, force)
        return _ok({"started": True, "forced": force})

    # -- model switch ------------------------------------------------------- #
    @router.patch("/pulses/{pulse_id}/model")
    async def switch_model(request: Request, pulse_id: str,
                           body: SwitchModelBody):
        """Mid-session model switch. Same vessel = respawn with resume (the
        CLI keeps the conversation). Cross-vessel = a full context handoff
        via the handoff engine."""
        started = time.monotonic()
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        if pulse.state == "archived":
            raise ApiError("bad_state", "cannot switch an archived pulse", 409)
        slot_binding = await _resolve_slot(request, body.model)
        new_vessel = body.vessel or pulse.vessel
        if slot_binding is not None:
            new_vessel = "pi"
        elif (not body.vessel and body.model != pulse.model
                and models_mod.model_by_id(body.model) is not None):
            # A registered model declares its CLI; follow it rather than
            # keep a vessel that cannot run the new model.
            new_vessel = models_mod.vessel_for(body.model)
        # Harness overrides ride the stored diet into every future spawn, so
        # a switch onto a print-mode pi model must refuse compaction up
        # front — accepting would leave the knob set but never effective.
        # Same gate as the create route.
        stored_ho = ((pulse.settings or {}).get("diet") or {}).get(
            "harness_overrides")
        comp_err = print_mode_compaction_error(
            new_vessel, body.model, stored_ho)
        if comp_err:
            raise ApiError("bad_harness_overrides", comp_err, 422)
        # Effort: None = keep the stored level, "" = clear to the CLI default,
        # a value = set (validated against the TARGET vessel). A kept level
        # that the target vessel doesn't speak (e.g. claude "max" -> codex) is
        # dropped rather than smuggled into a spawn that would choke on it.
        cur_effort = pulse.settings.get("effort")
        if body.effort is not None and body.effort != "":
            body.effort = models_mod.canonical_effort(
                body.model, body.effort,
                slot_binding["served_id"] if slot_binding is not None else None)
        if body.effort is None:
            new_effort = models_mod.canonical_effort(
                body.model, cur_effort,
                slot_binding["served_id"] if slot_binding is not None else None)
            allowed_efforts = (models_mod.slot_efforts(slot_binding["served_id"])
                               if slot_binding is not None
                               else efforts_for(body.model, new_vessel))
            if new_effort and new_effort not in allowed_efforts:
                logger.info("switch pulse=%s effort %r invalid for %s/%s — "
                            "cleared", pulse.id, new_effort, body.model,
                            new_vessel)
                new_effort = None
        elif body.effort == "":
            new_effort = None
        else:
            if slot_binding is not None:
                allowed_efforts = models_mod.slot_efforts(
                    slot_binding["served_id"])
                if body.effort not in allowed_efforts:
                    raise ApiError("bad_effort",
                                   f"effort {body.effort!r} is not supported "
                                   f"by model {slot_binding['served_id']!r}", 400)
            else:
                _validate_effort(body.effort, new_vessel, body.model)
            new_effort = body.effort
        # A stored fast flag can't follow the pulse onto a vessel whose CLI
        # has no service-tier switch — clear it rather than smuggle it along.
        if pulse.settings.get("fast") and new_vessel not in FAST_VESSELS:
            logger.info("switch pulse=%s fast mode invalid for %s — cleared",
                        pulse.id, new_vessel)
            st.update_settings(pulse.id, {"fast": None})
            pulse = st.get_pulse(pulse.id)

        same = (body.model == pulse.model and new_vessel == pulse.vessel
                and new_effort == cur_effort)
        if same and not body.force:
            logger.info("switch pulse=%s no-op (already %s/%s effort=%s)",
                        pulse.id, pulse.model, pulse.vessel, cur_effort or "-")
            return _ok(_pulse_view(pulse))
        if (pulse.settings or {}).get(failover_mod.FAILOVER_KEY):
            # The user chose the model: the pulse is no longer parked on its
            # failover target, so a later failure may fail over again.
            pulse = st.update_settings(pulse.id,
                                       {failover_mod.FAILOVER_KEY: None})

        if body.force or (pulse.state == "stopped" and new_vessel != pulse.vessel):
            # A foreign CLI cannot resume the old heartbeat's session id.
            # The old runner is unavailable to summarize its context, so use
            # the existing forced handoff (transcript seed + fresh session).
            if new_effort != cur_effort:
                pulse = st.update_settings(pulse.id, {"effort": new_effort})
            ok = await handoff_mod.engine.run_handoff(
                st, pulse, new_model=body.model, new_vessel=new_vessel,
                reason="model_switch", force=True)
            if not ok:
                raise ApiError("handoff_failed",
                               "forced model handoff did not complete "
                               "(see daemon log)", 502)
            return _ok(_pulse_view(st.get_pulse(pulse.id)))

        if pulse.state == "stopped":
            # Same CLI: the old session remains resumable on restore.
            updated = st.set_model(pulse.id, body.model, new_vessel)
            if new_effort != cur_effort:
                st.update_settings(pulse.id, {"effort": new_effort})
            logger.info("switch pulse=%s (stopped) -> %s/%s effort=%s ok "
                        "in %.3fs", pulse.id, body.model, new_vessel,
                        new_effort or "-", time.monotonic() - started)
            return _ok(_pulse_view(st.get_pulse(pulse.id)))

        if new_vessel == pulse.vessel:
            # Same CLI: stop, respawn with resume + the new model/effort.
            def _switch_tx() -> None:
                with lifecycle.pulse_lock(pulse.id):
                    cur = st.get_pulse(pulse.id)
                    if cur.state == "stopped":
                        # Paused while this request waited for the lock: the
                        # old session stays resumable on restore.
                        st.set_model(cur.id, body.model, new_vessel)
                        if new_effort != cur_effort:
                            st.update_settings(cur.id, {"effort": new_effort})
                        return
                    if cur.state != "running":
                        raise ApiError("bad_state",
                                       f"cannot switch a {cur.state} pulse",
                                       409)
                    beat = paths.read_heartbeat(cur.id)
                    resume = beat.get("cli_session_id") if beat else None
                    via = _stop_or_502_locked(cur.id)
                    updated = st.set_model(cur.id, body.model, new_vessel)
                    if new_effort != cur_effort:
                        updated = st.update_settings(cur.id,
                                                     {"effort": new_effort})
                    try:
                        lifecycle.start_locked(
                            st, updated, resume=resume, model=body.model,
                            vessel=new_vessel, effort=new_effort)
                    except Exception as exc:
                        st.update_settings(
                            cur.id,
                            {"stop_reason": f"switch respawn failed: {exc}"})
                        st.set_state(cur.id, "stopped")
                        logger.error("switch pulse=%s RESPAWN FAILED err=%s "
                                     "in %.3fs", cur.id, exc,
                                     time.monotonic() - started)
                        raise ApiError("spawn_failed",
                                       f"runner did not restart: {exc}", 502)
                    logger.info("switch pulse=%s -> %s (same vessel, "
                                "resume=%s, stop via=%s) ok in %.3fs", cur.id,
                                body.model, resume or "-", via,
                                time.monotonic() - started)

            await asyncio.to_thread(_switch_tx)
            return _ok(_pulse_view(st.get_pulse(pulse.id)))

        # Cross-vessel: context handoff, then fresh session on the new CLI.
        logger.info("switch pulse=%s CROSS-VESSEL %s/%s -> %s/%s via handoff",
                    pulse.id, pulse.model, pulse.vessel, body.model, new_vessel)
        if new_effort != cur_effort:
            # Store first, then re-fetch: run_handoff spawns with the effort
            # it reads off the pulse's settings snapshot.
            st.update_settings(pulse.id, {"effort": new_effort})
            pulse = st.get_pulse(pulse.id)
        ok = await handoff_mod.engine.run_handoff(
            st, pulse, new_model=body.model, new_vessel=new_vessel,
            reason="model_switch",
            reply_timeout_s=handoff_mod.state.SWITCH_REPLY_TIMEOUT_S,
            fallback_to_transcript=True)
        if not ok:
            raise ApiError("handoff_failed",
                           "cross-vessel switch needs a context handoff and "
                           "it did not complete — pulse unchanged (see daemon "
                           "log)", 502)
        logger.info("switch pulse=%s cross-vessel ok in %.3fs",
                    pulse.id, time.monotonic() - started)
        return _ok(_pulse_view(st.get_pulse(pulse.id)))

    # -- archive ----------------------------------------------------------- #
    def _archive_one(st: Store, pulse: Pulse) -> Pulse:
        """Archive one pulse (pausing it first if running). Raises ApiError.

        Sync, under the pulse lock: callers run it with ``to_thread``."""
        with lifecycle.pulse_lock(pulse.id):
            pulse = st.get_pulse(pulse.id)
            if pulse.state == "archived":
                logger.info("archive pulse=%s already archived — no-op",
                            pulse.id)
                return pulse
            if pulse.state == "running":
                # Must be stopped first — pause it, then archive.
                logger.info("archive pulse=%s running — pausing first",
                            pulse.id)
                _stop_or_502_locked(pulse.id)
                try:
                    st.set_state(pulse.id, "stopped")
                except InvalidStateTransition as exc:
                    raise ApiError("bad_state", str(exc), 409)
            try:
                updated = st.archive_pulse(pulse.id)
            except InvalidStateTransition as exc:
                raise ApiError("bad_state", str(exc), 409)
            # Close any open questions when archiving (no inbox delivery; the
            # pulse is going away, and /questions also excludes archived pulses).
            closed = st.close_open_questions(pulse.id, outcome="dismissed")
            if closed:
                logger.info("archive pulse=%s questions closed=%d", pulse.id,
                            closed)
            logger.info("archive pulse=%s archived", pulse.id)
            return updated

    def _live_monitoring(st: Store, pulse_id: str) -> dict[str, int]:
        """How much a pulse is still watching: open condition watches +
        enabled schedules. Archive needs a choice when either is non-zero."""
        return {
            "watches": len(st.list_condition_watches(state="watching",
                                                     pulse_id=pulse_id)),
            "schedules": len(st.list_schedules(pulse_id=pulse_id,
                                               enabled_only=True)),
        }

    def _stop_monitoring(st: Store, pulse_id: str) -> dict[str, int]:
        """Close every open watch (outcome owner_archived) and disable every
        schedule, so an archived pulse is never woken again."""
        counts = {"watches": 0, "schedules": 0}
        for w in st.list_condition_watches(state="watching", pulse_id=pulse_id):
            st.close_condition_watch(w.id, "owner_archived")
            counts["watches"] += 1
        for sched in st.list_schedules(pulse_id=pulse_id, enabled_only=True):
            st.set_schedule_enabled(sched.id, False)
            counts["schedules"] += 1
        logger.info("archive pulse=%s monitoring stopped watches=%d schedules=%d",
                    pulse_id, counts["watches"], counts["schedules"])
        return counts

    @router.post("/pulses/{pulse_id}/archive")
    async def archive_pulse(request: Request, pulse_id: str,
                            body: Optional[ArchivePulseBody] = None):
        """Archive a pulse. One that still owns live watches/schedules needs
        ``monitoring`` = keep (becomes a watching-mode pulse, NOT archived)
        or stop (monitoring closed, then archived) — Watchers, 2026-09-07."""
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        choice = body.monitoring if body else None
        if pulse.state != "archived":
            live = _live_monitoring(st, pulse.id)
            if live["watches"] or live["schedules"]:
                if choice is None:
                    logger.info("archive pulse=%s REFUSED watches=%d schedules=%d "
                                "— monitoring choice required", pulse.id,
                                live["watches"], live["schedules"])
                    raise ApiError(
                        "monitoring_choice_required",
                        f"pulse {pulse.title!r} still watches "
                        f"{live['watches']} condition(s) and has "
                        f"{live['schedules']} enabled schedule(s); send "
                        "{\"monitoring\": \"keep\"} to switch it to watching "
                        "mode instead, or {\"monitoring\": \"stop\"} to close "
                        "them and archive", 409)
                if choice == "keep":
                    def _keep_tx() -> Pulse:
                        with lifecycle.pulse_lock(pulse.id):
                            if st.get_pulse(pulse.id).state == "running":
                                _stop_or_502_locked(pulse.id)
                                try:
                                    st.set_state(pulse.id, "stopped")
                                except InvalidStateTransition as exc:
                                    raise ApiError("bad_state", str(exc), 409)
                            return st.update_settings(pulse.id,
                                                      {"mode": "watching"})

                    updated = await asyncio.to_thread(_keep_tx)
                    logger.info("archive pulse=%s KEEP monitoring → mode=watching "
                                "(not archived)", pulse.id)
                    _invalidate_roster(request)
                    return _ok(_pulse_view(updated, include_live=False))
                _stop_monitoring(st, pulse.id)
        updated = await asyncio.to_thread(_archive_one, st, pulse)
        _invalidate_roster(request)
        return _ok(_pulse_view(updated, include_live=False))

    def _last_used_epoch(pulse: Pulse) -> float:
        """When the pulse was last used: the later of its events.jsonl mtime
        (every turn/message appends) and the store's updated_at."""
        best = 0.0
        try:
            best = datetime.fromisoformat(
                pulse.updated_at.replace("Z", "+00:00")).timestamp()
        except (TypeError, ValueError):
            pass
        try:
            best = max(best, paths.events_path(pulse.id).stat().st_mtime)
        except OSError:
            pass
        return best

    @router.post("/pulses/archive-idle")
    async def archive_idle(request: Request, body: ArchiveIdleBody):
        """Archive every non-archived pulse unused for more than idle_hours.
        Running pulses are paused first, same as a single archive."""
        st = _store(request)
        cutoff = time.time() - body.idle_hours * 3600
        archived: list[dict[str, Any]] = []
        skipped: list[dict[str, str]] = []
        for pulse in st.list_pulses(include_archived=False):
            if _last_used_epoch(pulse) > cutoff:
                continue
            live = _live_monitoring(st, pulse.id)
            if live["watches"] or live["schedules"]:
                # Idle is the NORMAL state of a watcher — never sweep one up.
                skipped.append({"id": pulse.id, "title": pulse.title,
                                "error": f"still monitoring ({live['watches']} "
                                         f"watches, {live['schedules']} schedules)"})
                continue
            try:
                updated = await asyncio.to_thread(_archive_one, st, pulse)
            except ApiError as exc:
                skipped.append({"id": pulse.id, "title": pulse.title,
                                "error": exc.message})
                continue
            archived.append({"id": updated.id, "title": updated.title})
        logger.info("archive-idle hours=%s archived=%d skipped=%d",
                    body.idle_hours, len(archived), len(skipped))
        return _ok({"idle_hours": body.idle_hours, "archived": archived,
                    "skipped": skipped})

    # -- unarchive --------------------------------------------------------- #
    @router.post("/pulses/{pulse_id}/unarchive")
    async def unarchive_pulse(request: Request, pulse_id: str):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        if pulse.state != "archived":
            logger.info("unarchive pulse=%s no-op (state=%s)",
                        pulse.id, pulse.state)
            return _ok(_pulse_view(pulse))
        try:
            updated = st.unarchive_pulse(pulse.id)
        except InvalidStateTransition as exc:
            logger.error("unarchive pulse=%s FAILED err=%s", pulse.id, exc)
            raise ApiError("bad_state", str(exc), 409)
        logger.info("unarchive pulse=%s restored to stopped", pulse.id)
        return _ok(_pulse_view(updated))

    # -- hard delete ------------------------------------------------------- #
    def _owned_watchers(st: Store, pulse_id: str) -> dict[str, list[dict[str, Any]]]:
        """The watchers a pulse OWNS — its open condition watches and the
        delegation watches it spawned — as the pane shows them before a
        delete."""
        cond = [asdict(w) for w in st.list_condition_watches(state="watching",
                                                             pulse_id=pulse_id)]
        deleg = []
        for dw in st.list_watches(state="watching"):
            if dw.spawner_id != pulse_id:
                continue
            d = asdict(dw)
            try:
                d["target_title"] = st.get_pulse(dw.target_id).title
            except Exception:
                d["target_title"] = None
            deleg.append(d)
        return {"condition": cond, "delegation": deleg}

    @router.get("/pulses/{pulse_id}/watchers")
    async def pulse_watchers(request: Request, pulse_id: str):
        """What deleting this pulse would close (see _owned_watchers)."""
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        return _ok(_owned_watchers(st, pulse.id))

    @router.delete("/pulses/{pulse_id}")
    async def delete_pulse(request: Request, pulse_id: str,
                           close_watchers: bool = False):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        # Delete is terminal: a gone pulse can never be notified, so the
        # watchers it OWNS (its condition watches, the delegation watches it
        # spawned) go with it — but only once the caller has SEEN them:
        # without ?close_watchers=true this is a 409 has_watchers, and the
        # pane shows the list from GET /pulses/{id}/watchers and asks.
        # Watches TARGETING it close themselves as target_gone.
        def _delete_tx() -> Optional[str]:
            # Sync, under the pulse lock: the settle poll below sleeps in this
            # worker thread, holding only this pulse's lock.
            nonlocal pulse
            with lifecycle.pulse_lock(pulse.id):
                pulse = st.get_pulse(pulse.id)
                if st.has_pulse_watchers(pulse.id):
                    if not close_watchers:
                        logger.info("delete pulse=%s REFUSED (has watchers; no "
                                    "close_watchers)", pulse.id)
                        raise ApiError("has_watchers",
                                       "pulse has active watchers; confirm with "
                                       "?close_watchers=true to close them and delete",
                                       409)
                    closed_c = closed_d = 0
                    for w in st.list_condition_watches(state="watching",
                                                       pulse_id=pulse.id):
                        st.close_condition_watch(w.id, "owner_deleted")
                        closed_c += 1
                    for dw in st.list_watches(state="watching"):
                        if dw.spawner_id == pulse.id:
                            st.close_watch(dw.id, "spawner_deleted")
                            closed_d += 1
                    logger.info("delete pulse=%s closed its watchers first: condition=%d "
                                "delegation=%d", pulse.id, closed_c, closed_d)
                # A running pulse must be stopped/archived first — never yank a live
                # process out from under itself (delete is terminal).
                if pulse.state == "running":
                    logger.error("delete pulse=%s REFUSED (running)", pulse.id)
                    raise ApiError("bad_state",
                                   "pulse must be stopped or archived before delete",
                                   409)
                # The store row is not proof the process is gone. Pause sets stopped as
                # soon as the runner's socket handler REPLIES, while its shutdown is
                # still finishing — a pause-then-delete race could rename the home dir
                # out from under a live CLI, which would then keep writing heartbeat
                # and events into a .deleted- directory nothing tracks. Ask systemd.
                if _unit_loaded(pulse.id):
                    logger.error("delete pulse=%s unit still loaded (state=%s) — "
                                 "stopping it before touching the home dir",
                                 pulse.id, pulse.state)
                    units_mod.stop_unit(pulse.id)
                    for _ in range(DELETE_UNIT_SETTLE_TRIES):
                        time.sleep(DELETE_UNIT_SETTLE_S)
                        if not _unit_loaded(pulse.id):
                            break
                    else:
                        logger.error("delete pulse=%s REFUSED — unit still loaded after "
                                     "stop", pulse.id)
                        raise ApiError("bad_state",
                                       "the pulse's runner unit is still running and "
                                       "would not stop — delete refused", 409)
                    logger.info("delete pulse=%s unit stopped and gone", pulse.id)
                # Remove the store row, then move the home dir aside (never rm -rf).
                try:
                    st.delete_pulse(pulse.id)
                except InvalidStateTransition as exc:
                    raise ApiError("has_watchers", str(exc), 409)
                except Exception as exc:
                    logger.error("delete pulse=%s store-delete FAILED err=%s",
                                 pulse.id, exc)
                    raise ApiError("delete_failed", str(exc), 500)
                moved_to = _archive_home_dir(
                    pulse.id, record=_pulse_view(pulse, include_live=False))
                logger.info("delete pulse=%s row removed home=%s", pulse.id,
                            moved_to or "-")
                return moved_to

        moved_to = await asyncio.to_thread(_delete_tx)
        # The web refetches the roster right after a mutation; a cached
        # snapshot from before the delete would resurrect the pane for a frame.
        _invalidate_roster(request)
        return _ok({"deleted": pulse.id, "home_moved_to": moved_to})

    return router
