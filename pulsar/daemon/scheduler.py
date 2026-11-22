"""The Pulsar scheduler — loops + wake schedules, all state on disk.

An asyncio task started in the daemon lifespan. Every ``TICK_SECONDS`` it scans
every enabled schedule and decides due-ness from ``last_fired_at`` + the row
ALONE — no in-memory timers that die with the daemon (disposable
daemon, all state on disk). A restart never double-fires or loses a schedule:
the store is the only memory. A window missed while the daemon was down fires
exactly ONCE on the next tick (catch-up), never N times.

Firing = **wake-if-stopped then deliver**:
  1. If the pulse is ``stopped`` → respawn the runner exactly like the restore
     route (resume id from the heartbeat, memory_max from settings), mark it
     ``running``.
  2. Write the prompt into ``inbox/new/`` exactly like the messages route
     (atomic tmp+rename), with a ``sched-`` filename prefix.
An ``archived`` pulse is skipped with a log line — never woken. ``mark_fired``
is stamped on every ATTEMPT (success or failure), so a failed fire does not
retry until its next due window (no tight retry loops).

Standing rule: every decision — due, fired, skipped,
failed — is ONE timestamped log line carrying schedule id + pulse id + outcome
+ duration. A tick that scans and fires nothing logs at DEBUG only.
"""

from __future__ import annotations

import logging
import time
from datetime import datetime, timezone
from typing import Optional

from pulsar.daemon import loop_triggers
from pulsar.store import Store, Schedule
from pulsar.daemon import mailbox
from pulsar.daemon.timeutil import parse_iso as _parse_iso

logger = logging.getLogger("pulsar.daemon.scheduler")

TICK_SECONDS = 30.0

# csv day tokens -> Python weekday() (Mon=0 .. Sun=6), for the daily `days` filter.
_DAY_INDEX = {
    "mon": 0, "tue": 1, "wed": 2, "thu": 3, "fri": 4, "sat": 5, "sun": 6,
}


# --------------------------------------------------------------------------- #
# Time helpers
# --------------------------------------------------------------------------- #
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Due-ness (pure functions — recomputed every tick from the row + now)
# --------------------------------------------------------------------------- #
def is_due(sched: Schedule, now: datetime) -> bool:
    """True iff ``sched`` should fire at ``now``, from stored state ONLY.

    Boring per-kind rules. Malformed rows (a loop with no interval,
    a daily with an unparseable cron) are NOT due — the caller logs the skip.
    Catch-up is inherent: due-ness is a threshold on ``last_fired_at``, so a
    window missed while the daemon was down is still just "due now" — one fire.
    """
    if sched.kind == "loop":
        if not sched.interval_minutes or sched.interval_minutes <= 0:
            return False
        # v25: a loop whose triggers leave out the timer fires only on
        # handoff / message events, never by the clock.
        if "timer" not in loop_triggers.parse(sched.triggers):
            return False
        # First fire = created_at + interval; thereafter last_fired_at + interval.
        anchor = _parse_iso(sched.last_fired_at) or _parse_iso(sched.created_at)
        if anchor is None:
            return False
        delta_min = (now - anchor).total_seconds() / 60.0
        return delta_min >= sched.interval_minutes

    if sched.kind == "once":
        if sched.last_fired_at is not None:
            return False  # one-shot already fired
        at = _parse_iso(sched.at_iso)
        if at is None:
            return False
        return now >= at

    if sched.kind == "daily":
        cron = parse_hhmm(sched.cron_hhmm)
        if cron is None:
            return False
        # Optional days filter (mon..sun csv). Absent/empty = every day.
        if sched.days:
            allowed = parse_days(sched.days)
            if allowed is not None and now.weekday() not in allowed:
                return False
        hh, mm = cron
        # Fire once per day: due when today's HH:MM has passed AND we haven't
        # fired yet today (UTC date). Catch-up: a fire missed earlier today
        # still counts as "not fired today" -> one fire on the next tick.
        if (now.hour, now.minute) < (hh, mm):
            return False
        last = _parse_iso(sched.last_fired_at)
        if last is not None and last.date() >= now.date():
            return False
        return True

    return False  # unknown kind — never due (row shouldn't exist)


def parse_hhmm(cron_hhmm: Optional[str]) -> Optional[tuple[int, int]]:
    if not cron_hhmm:
        return None
    parts = cron_hhmm.split(":")
    if len(parts) != 2:
        return None
    try:
        hh, mm = int(parts[0]), int(parts[1])
    except ValueError:
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh, mm


def parse_days(days: str) -> Optional[set[int]]:
    """Parse a mon..sun csv to weekday indices; None if all tokens are junk."""
    out: set[int] = set()
    for tok in days.split(","):
        tok = tok.strip().lower()
        if tok in _DAY_INDEX:
            out.add(_DAY_INDEX[tok])
    return out or None


def validate_timing(kind: str, interval_minutes: Optional[int] = None,
                    at_iso: Optional[str] = None,
                    cron_hhmm: Optional[str] = None,
                    days: Optional[str] = None) -> None:
    """Raise :class:`ValueError` unless the timing fields fit ``kind``.

    Boring, explicit rules. Keeps garbage out of the store so the
    scheduler's due-ness math never has to guess.
    """
    if kind not in ("loop", "once", "daily"):
        raise ValueError(f"kind must be loop|once|daily, got {kind!r}")
    if kind == "loop":
        if not interval_minutes or interval_minutes <= 0:
            raise ValueError("loop requires interval_minutes > 0")
    elif kind == "once":
        if not at_iso or _parse_iso(at_iso) is None:
            raise ValueError("once requires a parseable ISO at_iso")
    elif kind == "daily":
        if parse_hhmm(cron_hhmm) is None:
            raise ValueError("daily requires cron_hhmm as 'HH:MM'")
        if days and parse_days(days) is None:
            raise ValueError(
                "days must be a mon..sun csv (e.g. 'mon,wed,fri')")


# --------------------------------------------------------------------------- #
# Firing (mirrors the restore + messages routes exactly)
# --------------------------------------------------------------------------- #
def _fire(store: Store, sched: Schedule, now: datetime) -> str:
    """Fire one due schedule: wake-if-stopped then deliver. Returns outcome.

    ``mark_fired`` is stamped FIRST (on the attempt) so a mid-fire crash or a
    spawn failure does not leave the schedule re-firing every tick. An archived
    pulse is skipped (never woken); a firing failure logs loudly and relies on
    the next due window to try again — no tight retry loop here.
    """
    started = time.monotonic()
    ts = _iso(now)
    try:
        pulse = store.get_pulse(sched.pulse_id)
    except Exception as exc:
        # Pulse vanished under the schedule. Stamp so we stop retrying, log loud.
        store.mark_fired(sched.id, ts)
        logger.error(
            "scheduler.fire sched=%s pulse=%s FAILED (pulse gone: %s) in %.3fs",
            sched.id, sched.pulse_id, exc, time.monotonic() - started)
        return "failed"

    if pulse.state == "archived":
        # Never wake an archived pulse. Stamp it so a daily/loop doesn't spin on
        # it every tick; a once auto-disables below anyway.
        store.mark_fired(sched.id, ts)
        if sched.kind == "once":
            store.set_schedule_enabled(sched.id, False)
        logger.info(
            "scheduler.fire sched=%s pulse=%s SKIPPED (archived) in %.3fs",
            sched.id, pulse.id, time.monotonic() - started)
        return "skipped"

    # Stamp the attempt BEFORE doing the work — a failure must not retry-loop.
    store.mark_fired(sched.id, ts)
    # A once-schedule auto-disables after firing, regardless of the
    # deliver outcome — it had its shot.
    if sched.kind == "once":
        store.set_schedule_enabled(sched.id, False)

    try:
        if sched.target_pulse_id:
            # Scheduled cross-pulse message: the prompt goes to
            # the TARGET, named as from this schedule's own pulse. A reply is
            # the target's own message back — nothing watches for it.
            # Lazy import — messages imports this module's wake helper.
            from pulsar.daemon import messages
            messages.send(store, sched.pulse_id, sched.target_pulse_id,
                          sched.prompt, source=f"schedule:{sched.id}")
        else:
            watching = (pulse.settings or {}).get("mode") == "watching"
            # A watching-mode pulse sleeps between wakes (Watchers,
            # 2026-09-07): write first and remember its resting state so
            # watch_sleep returns it there once the scheduled work is acked +
            # idle. Ordinary pulses keep today's behaviour (wake then write).
            mailbox.deliver(
                store, pulse, sched.prompt,
                on_archived="skip", remember=watching, write_first=watching,
                prefix=mailbox.SCHED, source="scheduler",
                extra=mailbox.origin_keys("daemon", "schedule"),
            )
    except Exception as exc:
        logger.error(
            "scheduler.fire sched=%s pulse=%s kind=%s FAILED err=%s in %.3fs "
            "— will not retry until next due window",
            sched.id, pulse.id, sched.kind, exc, time.monotonic() - started)
        # Inbox item: a schedule-fire FAILURE lands in the
        # inbox (channel system, QUIET — badge only). Never breaks the fire
        # path above (wrap + log).
        try:
            from pulsar.daemon import inbox as inbox_mod
            inbox_mod.produce(
                store, title=f"Schedule fire failed: {pulse.title or pulse.id}",
                body=f"schedule {sched.id} (kind={sched.kind}) failed to "
                     f"deliver for pulse {pulse.id}: {exc}",
                channel="system", source_kind="scheduler",
                source_label="scheduler", priority="quiet",
                # Tap-to-open: the pulse the schedule belongs to.
                source_pulse_id=pulse.id, validate=False)
        except Exception as iexc:
            logger.error("scheduler.fire sched=%s inbox FAILED err=%s",
                         sched.id, iexc)
        return "failed"

    logger.info(
        "scheduler.fire sched=%s pulse=%s kind=%s FIRED in %.3fs",
        sched.id, pulse.id, sched.kind, time.monotonic() - started)
    return "fired"


# --------------------------------------------------------------------------- #
# The tick (call directly in tests with a frozen `now` — no sleeping)
# --------------------------------------------------------------------------- #
def tick(store: Store, now: Optional[datetime] = None) -> dict[str, int]:
    """One scheduler pass. Scan every enabled schedule, fire the due ones.

    Pure of asyncio — a plain sync function so tests drive it directly with a
    frozen ``now``. Returns a counts summary. A tick that fires nothing logs at
    DEBUG only (standing rule: don't spam INFO with quiet ticks).
    """
    started = time.monotonic()
    if now is None:
        now = _now()
    summary = {"scanned": 0, "due": 0, "fired": 0, "skipped": 0, "failed": 0}

    try:
        schedules = store.list_schedules(enabled_only=True)
    except Exception:
        logger.exception("scheduler.tick list_schedules RAISED — skipping tick")
        return summary

    summary["scanned"] = len(schedules)
    beats: dict[str, Optional[dict]] = {}  # heartbeat cache, one read per pulse
    for sched in schedules:
        try:
            # v25: a loop combines its timer and replies/tools counters
            # (any|all); once/daily keep the plain clock rule.
            due = (loop_triggers.loop_due(store, sched, now, beats, is_due)
                   if sched.kind == "loop" else is_due(sched, now))
        except Exception:
            logger.exception("scheduler.tick sched=%s is_due RAISED — treating "
                             "as not due", sched.id)
            continue
        if not due:
            continue
        summary["due"] += 1
        logger.debug("scheduler.tick sched=%s pulse=%s DUE", sched.id,
                     sched.pulse_id)
        try:
            outcome = _fire(store, sched, now)
        except Exception:
            # _fire owns its own logging + mark_fired; a surprise here is a bug.
            logger.exception("scheduler.tick sched=%s _fire RAISED", sched.id)
            summary["failed"] += 1
            continue
        summary[outcome] = summary.get(outcome, 0) + 1
        if sched.kind == "loop":
            # One fire resets EVERY armed condition: a
            # tool-call fire must not be followed by the timer 5 min later.
            try:
                loop_triggers.reset_after_fire(store, sched, beats)
            except Exception:
                logger.exception("scheduler.tick sched=%s reset_after_fire RAISED",
                                 sched.id)

    # Delegation watches ride the same pass — lazy import because
    # watcher imports this module's wake helper. A raised watcher tick never
    # blocks schedule firing.
    try:
        from pulsar.daemon import watcher
        watcher.tick(store, now)
    except Exception:
        logger.exception("scheduler.tick watcher pass RAISED — continuing")

    # Condition watches ride the same pass — same lazy-import +
    # never-blocks-the-pass contract as the delegation watcher above.
    try:
        from pulsar.daemon import condwatch
        condwatch.tick(store, now)
    except Exception:
        logger.exception("scheduler.tick condwatch pass RAISED — continuing")

    dur = time.monotonic() - started
    if summary["due"] == 0:
        logger.debug("scheduler.tick quiet scanned=%d in %.3fs",
                     summary["scanned"], dur)
    else:
        logger.info(
            "scheduler.tick scanned=%d due=%d fired=%d skipped=%d failed=%d "
            "in %.3fs",
            summary["scanned"], summary["due"], summary["fired"],
            summary["skipped"], summary["failed"], dur)
    return summary
