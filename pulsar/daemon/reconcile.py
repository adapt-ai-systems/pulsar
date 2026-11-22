"""Boot reconcile: make the store agree with reality before serving.

Two passes, every decision logged as ONE timestamped line:

1. **Heartbeat sweep** — for every store pulse marked ``running``, read its
   heartbeat. Fresh (<60s) → leave it (a live runner survived the daemon
   restart). Stale or missing → ask systemd; a still-active unit is kept
   (a loaded box delays beats). Unit gone → the runner is dead: read
   systemd's verdict via ``fence_report()``, mark the pulse ``stopped``,
   and record *why* in ``settings.stop_reason`` ("hit memory cap" when OOM,
   else the systemd result). Then hard-stop the unit so a half-dead scope
   can't linger.

2. **Orphan sweep** — every live ``pulsar-pulse-*`` systemd unit whose id is
   not a ``running`` pulse in the store is an orphan burning tokens nobody
   tracks. Log a LOUD error line and ``stop_unit`` it.

Standing rule: nothing here fails, waits, or skips silently.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any

from pulsar.launch import units as units_mod
from pulsar.store import Store, UnknownPulse
from pulsar.daemon import paths

logger = logging.getLogger("pulsar.daemon.reconcile")

# Systemd result -> human stop reason. OOM is the one users must see plainly.
_OOM_REASON = "hit memory cap"


def _stop_reason_from_fence(report: dict[str, Any]) -> str:
    if report.get("oom"):
        return _OOM_REASON
    result = report.get("result", "unknown")
    if result == "success":
        # The scope exited cleanly but left no verdict — "runner died
        # (success)" reads as gibberish on a tile (live find 2026-07-26).
        return "runner exited without recording a reason"
    return f"runner died ({result})"


def _pulse_updated_ts(pulse) -> float:
    """UTC epoch seconds for ``pulse.updated_at``, or 0.0 if unreadable."""
    try:
        dt = datetime.fromisoformat(pulse.updated_at)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except (ValueError, TypeError):
        return 0.0


def is_fresh_dead_beat(pulse, beat: dict[str, Any] | None) -> bool:
    """True when ``beat`` says the runner recorded a failure (``cli_dead``: the
    agent tool failed to start or died) at or after ``pulse.updated_at``, so it
    belongs to this run and not to a heartbeat left from before a restore.

    A normal stop writes ``stopped`` and is NOT a dead beat: pause, reset,
    handoff and model switch all stop the old runner on purpose, and reset,
    handoff and switch start a new one right after it."""
    if not beat or beat.get("state") != "cli_dead":
        return False
    beat_ts = paths._parse_ts(beat.get("ts") or "")
    if beat_ts is None:
        return False
    return beat_ts >= _pulse_updated_ts(pulse)


def _reap(store: Store, pulse, beat: dict[str, Any] | None, reason: str,
          *, recheck: bool) -> bool | None:
    """Mark one dead pulse stopped and stop its unit. True = marked, False =
    the store write failed, None = skipped because the pulse changed.

    ``recheck`` (the caller holds the pulse lock): look again first. Reset,
    handoff and model switch stop the old runner and start a new one under
    that lock. A sweep that saw the old runner's last beat must not stop the
    new runner (stage find 2026-11-20: a reset left the pulse stopped with a
    false failure reason). So the pulse is reaped only if it is still running
    in the store, its heartbeat did not change, and systemd does not say that
    its unit is active."""
    pid = pulse.id
    if recheck:
        try:
            cur = store.get_pulse(pid)
        except UnknownPulse:
            return None
        now_beat = paths.read_heartbeat(pid) or {}
        seen = beat or {}
        changed = (cur.state != "running"
                   or now_beat.get("ts") != seen.get("ts")
                   or now_beat.get("state") != seen.get("state"))
        if not changed:
            try:
                changed = units_mod.unit_active(pid) is True
            except Exception:
                logger.exception("reconcile pulse=%s unit_active raised", pid)
        if changed:
            logger.info("reconcile pulse=%s changed during the sweep — kept",
                        pid)
            return None
    marked = True
    try:
        store.update_settings(pid, {"stop_reason": reason})
        store.set_state(pid, "stopped")
    except Exception:
        logger.exception("reconcile pulse=%s FAILED to mark stopped", pid)
        marked = False
    # Reap any half-dead scope. Best-effort; stop_unit logs its own outcome.
    try:
        units_mod.stop_unit(pid)
    except Exception:
        logger.exception("reconcile pulse=%s stop_unit raised", pid)
    return marked


def sweep_dead_runners(store: Store, grace_s: float = 0.0,
                       quiet_alive: bool = False,
                       lock: bool = False) -> dict[str, Any]:
    """Pass 1: mark store-``running`` pulses with dead runners ``stopped``.

    Also runs PERIODICALLY (daemon dead-runner sweep) — a runner that dies
    mid-flight (vessel error, crash, external kill) must become an honest
    stopped tile with a reason, not an eternal "stalled". ``grace_s`` skips pulses whose row changed in the last N
    seconds so the periodic sweep can't reap a runner mid-spawn before its
    first heartbeat — UNLESS the runner already recorded a failure for this
    run (a fresh ``cli_dead`` beat). ``lock`` (the periodic sweep; worker
    threads only) reaps under the pulse lock and checks the pulse again first,
    so a reset or handoff in progress is never reaped.
    Returns {checked, kept, stopped, live_ids}.
    """
    out: dict[str, Any] = {"checked": 0, "kept": 0, "stopped": 0,
                           "live_ids": set()}
    running = store.list_pulses(state="running")
    out["checked"] = len(running)
    now = time.time()
    for pulse in running:
        pid = pulse.id
        beat = paths.read_heartbeat(pid)
        if grace_s:
            # updated_at is UTC; time.mktime read it as LOCAL time, which
            # skewed the grace window by the host's UTC offset (7h on PDT:
            # dead runners went unreaped for hours, live 2026-10-13).
            updated = _pulse_updated_ts(pulse)
            if now - updated < grace_s and not is_fresh_dead_beat(pulse, beat):
                out["kept"] += 1
                out["live_ids"].add(pid)
                continue
        # A fresh heartbeat is NOT enough: a runner's final beat says
        # state=stopped/cli_dead (and carries its own reason, e.g. "hit
        # memory cap" from the cgroup oom counter) — trust the state field.
        from pulsar.daemon.watch_sleep import finish
        if finish(store, pulse, beat):
            out["stopped"] += 1
            continue
        hb_state = (beat or {}).get("state")
        if paths.heartbeat_fresh(beat) and hb_state not in ("stopped", "cli_dead"):
            if not quiet_alive:
                age = paths.heartbeat_age(beat)
                logger.info("reconcile pulse=%s ALIVE heartbeat_age=%.1fs — kept",
                            pid, age if age is not None else -1.0)
            out["kept"] += 1
            out["live_ids"].add(pid)
            continue

        age = paths.heartbeat_age(beat)
        age_str = f"{age:.1f}s" if age is not None else "missing"
        if hb_state not in ("stopped", "cli_dead"):
            # A stale beat alone does not prove death: under heavy CPU load a
            # busy runner misses beats while its unit is fine (live
            # 2026-10-03, load 16-23: four live pulses reaped mid-turn).
            # Reap only when systemd says the unit is gone.
            try:
                active = units_mod.unit_active(pid)
            except Exception:
                logger.exception("reconcile pulse=%s unit_active raised", pid)
                active = None
            if active is not False:
                logger.warning(
                    "reconcile pulse=%s STALE heartbeat=%s hb_state=%s but "
                    "unit %s — kept", pid, age_str, hb_state,
                    "active" if active else "state unknown")
                out["kept"] += 1
                out["live_ids"].add(pid)
                continue
        reason = (beat or {}).get("reason")  # the runner's own verdict wins
        if not reason:
            report = units_mod.fence_report(pid)
            reason = _stop_reason_from_fence(report)
        logger.error(
            "reconcile pulse=%s DEAD heartbeat=%s hb_state=%s reason=%r — "
            "marking stopped + stopping unit",
            pid, age_str, hb_state, reason,
        )
        if lock:
            from pulsar.daemon import lifecycle
            with lifecycle.pulse_lock(pid):
                reaped = _reap(store, pulse, beat, reason, recheck=True)
        else:
            reaped = _reap(store, pulse, beat, reason, recheck=False)
        if reaped is None:
            out["kept"] += 1
            out["live_ids"].add(pid)
        elif reaped:
            out["stopped"] += 1
    return out


def reconcile(store: Store) -> dict[str, Any]:
    """Run both sweeps against ``store``. Returns a summary dict (also logged)."""
    started = time.monotonic()
    logger.info("reconcile start")
    summary = {
        "checked_running": 0,
        "kept_alive": 0,
        "marked_stopped": 0,
        "orphans_stopped": 0,
    }

    # -- pass 1: heartbeat sweep ------------------------------------------- #
    swept = sweep_dead_runners(store)
    summary["checked_running"] = swept["checked"]
    summary["kept_alive"] = swept["kept"]
    summary["marked_stopped"] = swept["stopped"]
    live_ids: set[str] = swept["live_ids"]

    # -- pass 2: orphan sweep ---------------------------------------------- #
    try:
        unit_ids = units_mod.list_pulse_units()
    except Exception:
        logger.exception("reconcile orphan-sweep list_pulse_units raised")
        unit_ids = []
    for uid in unit_ids:
        if uid in live_ids:
            continue
        # systemd user units are shared by every daemon on the host; a second
        # daemon (a test app, a smoke run with its own PULSAR_DATA) would see
        # every live pulse as an orphan and stop them all. Only stop a unit
        # for a pulse this store knows.
        try:
            store.get_pulse(uid)
        except UnknownPulse:
            logger.warning(
                "reconcile orphan-sweep pulse=%s not in this store — not ours, "
                "left running", uid)
            continue
        logger.error(
            "reconcile ORPHAN pulse=%s — live systemd unit not a running pulse; "
            "stopping it (never leave a runner burning tokens untracked)", uid,
        )
        try:
            units_mod.stop_unit(uid)
        except Exception:
            logger.exception("reconcile orphan pulse=%s stop_unit raised", uid)
        else:
            summary["orphans_stopped"] += 1

    dur = time.monotonic() - started
    logger.info(
        "reconcile done in %.3fs checked=%d kept=%d stopped=%d orphans=%d",
        dur, summary["checked_running"], summary["kept_alive"],
        summary["marked_stopped"], summary["orphans_stopped"],
    )
    return summary
