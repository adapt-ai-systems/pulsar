"""Model failover — a pulse whose model fails moves to its backup.

When a pulse's model fails, Pulsar switches the pulse to its backup model
automatically, with a summary of the work, in the same pane, and re-sends the
message it was working on. It posts one line saying why it switched and stays
on the backup until the user switches it back. No question is asked.

A model FAILS when either:

  (a) the pulse is stopped with a failure ``settings.stop_reason`` — the
      runner died (crash, vessel error, cli died, memory cap, a dead slot),
      or its spawn failed. A deliberate pause/stop/archive/reset sets no
      stop_reason, so it never fails over.
  (b) the model ended 2 turns in a row on a provider/API error — the runner
      counts them and publishes ``provider_error_turns`` in its heartbeat
      (runner.py); a good turn resets the count.

The move is the handoff engine's forced path (``run_handoff(force=True)``):
a transcript seed + a fresh session on the backup, same pulse id and pane,
with the backup's effort.

The re-sent message:

  (a) every message the dead runner had claimed (``inbox/feeding/``) goes
      back to ``inbox/new/`` before the handoff, so the successor answers it
      after the seed (otherwise the handoff would retire it as "already
      received"). A turn that failed with a vessel error is already back in
      ``new/`` (the runner un-claims it). Queued messages stay queued.
  (b) the inbox file of the second failed turn (heartbeat
      ``provider_error_msg``) moves from ``inbox/acked/`` back to ``new/``.

No ping-pong, at most one automatic failover per failure:

  * a moved pulse carries ``settings.failover`` = {from, to, reason, at}.
    While it still runs on ``to`` it never fails over again: a further
    failure leaves it stopped with stop_reason "failover: <to> (backup) also
    failed: ..." and one pane line. The user switching the model clears it.
  * a failed move leaves the pulse stopped with stop_reason "failover to
    <model> failed: ..." and one pane line.
  * a stop_reason starting with "failover" never triggers a failover.
  * no backup, or a backup equal to the pulse's own model: nothing happens
    (the pulse stays as it is).

Boot guard: pulses already failure-stopped when the monitor starts, and
untouched for BOOT_GRACE_S, are old failures — they are skipped until they
run again, so a daemon start never revives every long-dead pulse. A pulse
whose runner died while the daemon was down is marked stopped by the boot
reconcile just before (fresh row) and still fails over.

Archive guard: an archived pulse keeps its last stop_reason. Unarchiving it
(the drawer, or a watch waking it) is a deliberate act, not a failure, so a
reason seen while the pulse was archived is skipped the same way until the
pulse runs again (found live 2026-10-14: an unarchived pulse failed over).
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pulsar import models as models_mod
from pulsar.daemon import events_io, lifecycle, paths
from pulsar.store import Store

logger = logging.getLogger("pulsar.daemon.failover")

TICK_SECONDS = 5.0

# Turns in a row that must end on a provider error before (b) fires.
PROVIDER_ERROR_TURNS = 2

# A pulse failure-stopped when the monitor starts and untouched this long is
# an old failure (see the boot guard above).
BOOT_GRACE_S = 120.0

FAILOVER_KEY = "failover"

# Our own stop reasons start with this — they never trigger a failover.
OWN_PREFIX = "failover"

# Stop reasons that are not a failure (watch_sleep.finish: a watching pulse
# whose watches are all done).
NON_FAILURE_REASONS = frozenset({"watch work complete"})

# App-settings key for the user-chosen default backup model for new pulses.
DEFAULT_BACKUP_SETTING_KEY = "default_backup"

# The stop-and-ask marker this module replaced (retired at boot).
STOP_ASK_KEY = "stop_ask"


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def backup_for(pulse) -> Optional[dict[str, Any]]:
    """The pulse's backup config ``{model, effort?}``, or None."""
    cfg = (pulse.settings or {}).get("backup")
    if not isinstance(cfg, dict) or not cfg.get("model"):
        return None
    return dict(cfg)


def default_backup_from_store(store: Store) -> Optional[dict[str, Any]]:
    """The user-chosen default backup ``{model, effort?}`` for new pulses,
    or None when unset."""
    cfg = store.get_setting(DEFAULT_BACKUP_SETTING_KEY)
    if not isinstance(cfg, dict) or not cfg.get("model"):
        return None
    return dict(cfg)


def retire_stop_ask(store: Store) -> int:
    """Close what the removed stop-and-ask left behind: dismiss its open
    pinned questions and clear the ``stop_ask`` markers. Idempotent (run at
    daemon boot). Returns the number of pulses cleaned."""
    cleaned = 0
    for pulse in store.list_pulses(include_archived=True):
        marker = (pulse.settings or {}).get(STOP_ASK_KEY)
        if marker is None:
            continue
        qid = marker.get("question_id") if isinstance(marker, dict) else None
        if isinstance(qid, str) and qid:
            try:
                if store.get_question(qid).state == "open":
                    store.resolve_question(qid, "dismissed")
            except Exception:
                logger.exception("failover.retire_stop_ask pulse=%s "
                                 "question=%s not dismissed", pulse.id, qid)
        store.update_settings(pulse.id, {STOP_ASK_KEY: None})
        cleaned += 1
    if cleaned:
        logger.info("failover.retire_stop_ask: %d pulse(s) cleaned", cleaned)
    return cleaned


def failure_reason(pulse) -> Optional[str]:
    """The failure stop_reason of a stopped pulse, or None (running, a
    deliberate stop, a non-failure reason, or our own failover reason)."""
    if pulse.state != "stopped":
        return None
    reason = str((pulse.settings or {}).get("stop_reason") or "")
    if not reason or reason in NON_FAILURE_REASONS:
        return None
    if reason.startswith(OWN_PREFIX):
        return None
    return reason


def _marker(pulse) -> Optional[dict[str, Any]]:
    marker = (pulse.settings or {}).get(FAILOVER_KEY)
    return marker if isinstance(marker, dict) else None


def _on_failover_target(pulse) -> bool:
    """True while the pulse still runs on the model it failed over TO."""
    marker = _marker(pulse)
    return bool(marker and marker.get("to") == pulse.model)


def _fail_line(pulse_id: str, text: str) -> None:
    """The one pane line for a failover that did not happen (red)."""
    events_io.emit(pulse_id, {"type": "runner_error", "where": "failover",
                              "reason": text})


def _requeue_in_flight(pulse_id: str) -> list[str]:
    """Trigger (a): the claimed messages of the dead runner back to new/."""
    home = paths.pulse_home(pulse_id)
    new_dir = home / "inbox" / "new"
    moved: list[str] = []
    for path in sorted((home / "inbox" / "feeding").glob("*.json")):
        if path.name.startswith("handoff"):
            continue  # the handoff writes a fresh seed
        new_dir.mkdir(parents=True, exist_ok=True)
        try:
            path.replace(new_dir / path.name)
            moved.append(path.name)
        except OSError as exc:
            logger.error("failover.resend pulse=%s %s FAILED err=%s",
                         pulse_id, path.name, exc)
    return moved


def _requeue_acked(pulse_id: str, name: Optional[str]) -> list[str]:
    """Trigger (b): the second failed turn's message back to new/."""
    if not name or name.startswith("handoff"):
        return []
    home = paths.pulse_home(pulse_id)
    src = home / "inbox" / "acked" / name
    if not src.is_file():
        logger.warning("failover.resend pulse=%s %s not in acked/ — nothing "
                       "re-sent", pulse_id, name)
        return []
    dest = home / "inbox" / "new" / name
    dest.parent.mkdir(parents=True, exist_ok=True)
    try:
        src.replace(dest)
    except OSError as exc:
        logger.error("failover.resend pulse=%s %s FAILED err=%s", pulse_id,
                     name, exc)
        return []
    return [name]


def _stop_running(store: Store, pulse_id: str, reason: str) -> None:
    """Leave a still-running pulse stopped with ``reason`` (trigger (b) when
    no move happens)."""
    with lifecycle.pulse_lock(pulse_id):
        cur = store.get_pulse(pulse_id)
        if cur.state == "running":
            try:
                lifecycle.stop_locked(pulse_id)
            except lifecycle.StopFailed:
                logger.error("failover pulse=%s could not stop the runner",
                             pulse_id)
            store.set_state(pulse_id, "stopped")
        store.update_settings(pulse_id, {"stop_reason": reason})


async def fail_over(store: Store, pulse, *, why: str, trigger: str,
                    resend_msg: Optional[str] = None) -> bool:
    """Move ``pulse`` onto its backup — ONE attempt. Returns True when the
    pulse now runs on the backup. ``trigger`` is "stopped" (a) or
    "provider_error" (b); ``resend_msg`` is (b)'s heartbeat message file."""
    import asyncio
    from pulsar.daemon import handoff

    pid = pulse.id
    old_model = pulse.model
    if _on_failover_target(pulse):
        # The backup ALSO failed: stay stopped, never ping-pong back.
        reason = f"failover: {old_model} (backup) also failed: {why}"[:300]
        logger.error("failover pulse=%s %s", pid, reason)
        await asyncio.to_thread(_stop_running, store, pid, reason)
        _fail_line(pid, f"{reason} — the pulse is stopped. Switch it to "
                        f"another model to continue.")
        return False
    target = backup_for(pulse)
    if target is None or target["model"] == old_model:
        return False  # no usable backup: the pulse stays as it is
    if handoff.state.in_flight(pid):
        return False  # a handoff owns the pulse right now; next tick
    model = str(target["model"])
    vessel = models_mod.vessel_for(model)
    old_effort = (pulse.settings or {}).get("effort")
    resent = (_requeue_in_flight(pid) if trigger == "stopped"
              else _requeue_acked(pid, resend_msg))
    logger.warning("failover.start pulse=%s %s -> %s/%s trigger=%s why=%r "
                   "resent=%s", pid, old_model, model, vessel, trigger, why,
                   resent or "-")
    # The handoff engine carries settings.effort into the respawn.
    pulse = store.update_settings(pid, {"effort": target.get("effort") or None})
    ok = await handoff.engine.run_handoff(
        store, pulse, new_model=model, new_vessel=vessel,
        reason=f"failover:{trigger}", force=True)
    if ok:
        store.update_settings(pid, {
            FAILOVER_KEY: {"from": old_model, "to": model, "reason": why,
                           "at": _now_iso()},
            "stop_reason": None})
        events_io.emit(pid, {"type": "failover", "from": old_model,
                             "to": model, "reason": why, "resent": resent})
        logger.warning("failover.done pulse=%s now on %s", pid, model)
        return True
    failed = (handoff.state.read_state(pid) or {}).get("reason") or "handoff failed"
    reason = f"failover to {model} failed: {failed}"[:300]
    logger.error("failover.FAILED pulse=%s %s (was: %s)", pid, reason, why)
    store.update_settings(pid, {"effort": old_effort})
    await asyncio.to_thread(_stop_running, store, pid, reason)
    _fail_line(pid, f"{old_model} failed ({why}); {reason} — the pulse is "
                    f"stopped.")
    return False


class Monitor:
    """The failover loop's state: the boot baseline of old failures and the
    provider-error turns already acted on."""

    def __init__(self, store: Store, *, now: Optional[float] = None):
        self.store = store
        self.started = time.time() if now is None else now
        self._baseline: Optional[dict[str, str]] = None
        # pulse id -> provider_error_msg already acted on (one attempt).
        self._acted: dict[str, Optional[str]] = {}

    def take_baseline(self) -> None:
        """Record the old failures. lifespan calls it BEFORE the boot
        migration (retire_stop_ask): its settings writes bump ``updated_at``
        and would make every old failure look fresh. Otherwise the first
        tick takes it."""
        self._baseline = {}
        self._remember_archived()
        for pulse in self.store.list_pulses(state="stopped"):
            reason = failure_reason(pulse)
            if reason is None:
                continue
            try:
                dt = datetime.fromisoformat(pulse.updated_at)
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                touched = dt.timestamp()
            except (TypeError, ValueError):
                touched = 0.0
            if self.started - touched >= BOOT_GRACE_S:
                self._baseline[pulse.id] = reason
        if self._baseline:
            logger.info("failover.boot %d old failure-stopped pulse(s) left "
                        "alone until they run again", len(self._baseline))

    def _remember_archived(self) -> None:
        """Archive guard: an archived pulse's stop_reason is never a new
        failure once it is unarchived."""
        for pulse in self.store.list_pulses(state="archived"):
            reason = str((pulse.settings or {}).get("stop_reason") or "")
            if reason:
                self._baseline[pulse.id] = reason

    async def tick(self) -> int:
        """One pass. Returns the number of pulses moved to their backup."""
        if self._baseline is None:
            self.take_baseline()
        else:
            self._remember_archived()
        moved = 0
        for pulse in self.store.list_pulses(state="stopped"):
            reason = failure_reason(pulse)
            if reason is None:
                continue
            if self._baseline.get(pulse.id) == reason:
                continue
            if await fail_over(self.store, pulse, why=reason,
                               trigger="stopped"):
                moved += 1
        for pulse in self.store.list_pulses(state="running"):
            self._baseline.pop(pulse.id, None)
            beat = paths.read_heartbeat(pulse.id) or {}
            n = beat.get("provider_error_turns")
            if not isinstance(n, int) or n < PROVIDER_ERROR_TURNS:
                self._acted.pop(pulse.id, None)
                continue
            msg = beat.get("provider_error_msg")
            if pulse.id in self._acted and self._acted[pulse.id] == msg:
                continue  # already acted on this failure
            self._acted[pulse.id] = msg
            why = f"provider error on {n} turns in a row"
            if beat.get("reason"):
                why += f": {beat['reason']}"
            if await fail_over(self.store, pulse, why=why[:300],
                               trigger="provider_error", resend_msg=msg):
                moved += 1
        return moved
