"""Delegation watches.

When a pulse delegates to another — spawns it with ``from_pulse_id`` or sends
it a pulse-to-pulse message — the daemon records a watch row. This module is
the checker: the scheduler calls :func:`tick` every pass, and each due watch
reads the TARGET's real signals (heartbeat state, context growth, unconsumed
inbox) and messages the SPAWNER with the verdict.

Two phases (v12 "stall watcher"):

  initial  the first look (~5 min after delegating): work is happening → one
           "working" confirmation (spawn watches only; message watches stay
           silent) and the watch ARMS INTO THE FOLLOW PHASE instead of closing.
           Nothing happening → a LOUD stall alert with diagnostics, one
           recheck, then a final alert.

  follow   the watch keeps watching the delegate for the life of the
           delegation (~every 5 min, 24h cap) — a helper that works for a
           while and THEN hangs used to fall off the radar the moment it
           showed one sign of life. A hung helper keeps a FRESH heartbeat
           (the heartbeat task is independent of the vessel stream), so
           staleness alone proves nothing here. The follow signals are:

           - heartbeat stale/absent    runner dead (backstop; reconcile
                                       usually flips these to stopped first)
           - busy too long + frozen ctx  the turn looks HUNG (busy_since age
                                       past BUSY_STALL_S with no ctx growth
                                       since the last check — a legitimately
                                       long tool call grows ctx between turns
                                       or resets busy_since on the next one)
           - idle, ctx frozen N checks  zombie idle: it finished its turn and
                                       never replied, and the spawner waits
                                       forever (this is the common failure)
           - target stopped             the delegation ENDED (normal or crash
                                       — stop_reason discriminates); close
                                       with a one-line FYI so a helper that
                                       quit without replying isn't a silent miss

  Each stall alert escalates: first alert → recheck in 10 min → final alert
  and close. Any ctx growth clears the streak and re-arms. No delegation may
  fail silently.

Born from the 2026-08-06 incident: a lead spawned three helpers with briefs in
an unsupported ``first_message`` field, the API dropped the field silently,
and the helpers sat empty for over an hour before a human noticed.

All state lives in the store: a daemon restart never loses a watch,
and due-ness is recomputed from ``next_check_at`` alone — no in-memory timers.
Watch messages are written with ``source="watch"`` and NEVER create watches
themselves (no recursion).
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

from pulsar.daemon import mailbox, paths
from pulsar.daemon.timeutil import parse_iso as _parse_iso
from pulsar.store import DelegationWatch, Store, UnknownPulse

logger = logging.getLogger("pulsar.daemon.watcher")

# First look N minutes after the delegation; if idle, alert and look once more
# N minutes later; still idle then = final alert + close. Two checks total.
FIRST_CHECK_MINUTES = 5
RECHECK_MINUTES = 10
MAX_CHECKS = 2

# Follow phase (v12): recheck cadence once the delegate has shown life.
FOLLOW_INTERVAL_MINUTES = 5
# Busy this long with no ctx growth since the last check = the turn looks
# HUNG. High enough to never alarm on a legitimately long tool call (those
# grow ctx between API turns or reset busy_since on the next one).
BUSY_STALL_S = 30 * 60
# Consecutive follow checks with no ctx growth before the zombie-idle alert
# (~15 min of frozen ctx at the follow cadence): the helper finished its turn
# and never replied.
ZOMBIE_CHECKS = 3
# A follow watch closes after this long — nothing follows a delegate forever.
WATCH_TTL_S = 24 * 3600


def next_check_iso(now: Optional[datetime] = None,
                   minutes: int = FIRST_CHECK_MINUTES) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return (now + timedelta(minutes=minutes)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Target signals + verdict
# --------------------------------------------------------------------------- #
def target_signals(pulse_id: str) -> dict[str, Any]:
    """Read the target's observable state: heartbeat + unconsumed inbox."""
    beat = paths.read_heartbeat(pulse_id)
    try:
        pending = len([p for p in paths.inbox_new(pulse_id).iterdir()
                       if not p.name.endswith(".tmp")])
    except OSError:
        pending = 0
    return {
        "beat": beat,
        "fresh": paths.heartbeat_fresh(beat),
        "state": (beat or {}).get("state"),
        "ctx": (beat or {}).get("last_ctx_tokens"),
        "busy_since": (beat or {}).get("busy_since"),
        "pending_inbox": pending,
    }


def is_working(sig: dict[str, Any], baseline_ctx: Optional[int]) -> bool:
    """True iff the target shows real progress since the watch began.

    Progress = a live runner that is either busy right now or whose context
    has grown past the baseline (it did a turn — even a fast one that already
    finished). A stale/absent heartbeat is never "working", whatever the ctx
    says: numbers from a dead runner prove nothing about now.
    """
    if not sig["fresh"]:
        return False
    if sig["state"] == "busy":
        return True
    base = baseline_ctx or 0
    ctx = sig["ctx"]
    return ctx is not None and ctx > base


def _describe(sig: dict[str, Any], baseline_ctx: Optional[int],
              stop_reason: Optional[str] = None) -> str:
    """One human line of diagnostics for the spawner's message."""
    if sig["beat"] is None:
        parts = ["no heartbeat — the runner never started"]
    elif not sig["fresh"]:
        parts = ["heartbeat STALE — the runner looks dead"]
        if stop_reason:
            parts.append(f"stop reason: {stop_reason}")
    else:
        parts = [f"state {sig['state'] or '?'}"]
    ctx = sig["ctx"]
    parts.append(f"ctx {ctx:,} tokens (baseline {baseline_ctx or 0:,})"
                 if ctx is not None else "ctx unknown")
    if sig["pending_inbox"]:
        parts.append(f"{sig['pending_inbox']} message(s) sitting UNCONSUMED "
                     "in its inbox")
    return ", ".join(parts)


# --------------------------------------------------------------------------- #
# Delivery to the spawner
# --------------------------------------------------------------------------- #
def _notify_spawner(store: Store, watch: DelegationWatch, text: str) -> None:
    """Wake-if-stopped then write the watch verdict into the spawner's inbox.

    Mirrors the messages engine's contract but with ``source="watch"`` so the
    message route never mistakes it for a pulse-to-pulse send (no recursive
    watches). Raises on failure — the caller logs and retries next window.
    """
    spawner = store.get_pulse(watch.spawner_id)
    # An archive is not a temporary sleep. Never undo it for a delegation
    # notification, even if the checker loaded the watch before the archive.
    name = mailbox.deliver(
        store, spawner, text,
        on_archived="skip", remember=True,
        prefix=mailbox.WATCH, source="watch",
        extra=mailbox.origin_keys("daemon", "watch"),
    )
    if name is not None:
        logger.info("watch.notify spawner=%s file=%s ok",
                    watch.spawner_id, name)


def _file_inbox(store: Store, watch: DelegationWatch, target,
                text: str) -> None:
    """Mirror a STALL alert into the Inbox (channel system).

    A learned score of 1 on this kind drops the item inside
    ``inbox.produce`` — the alert still reaches the spawner as a pulse message,
    the Inbox just never stores the noise. A muted system channel drops it too.
    """
    try:
        from pulsar.daemon import inbox as inbox_mod
        title = f"Delegation stall: {target.title!r}"
        try:
            chan = store.get_channel("system")
            if inbox_mod.effective_channel_mode(chan.mode, chan.mute_until) == "muted":
                logger.info("watch.inbox spawner=%s target=%s → DROPPED, muted channel",
                            watch.spawner_id, watch.target_id)
                return
        except Exception:
            pass  # no channel row yet ⇒ default file-anyway behavior
        inbox_mod.produce(
            store, title=title,
            body=text, channel="system", source_kind="watch",
            source_label="delegation-watch",
            # Tap-to-open: the stalled target pulse, so the phone tap lands on
            # the pulse that needs looking at (watch items are not
            # pulse-sourced, so this does not enable an inbox reply).
            source_pulse_id=target.id, validate=False)
    except Exception as exc:
        logger.error("watch.inbox spawner=%s FAILED err=%s",
                     watch.spawner_id, exc)


def _alert_spawner(store: Store, watch: DelegationWatch, target,
                   text: str) -> None:
    """Deliver a STALL alert: spawner message + Inbox mirror (score-gated).
    Never breaks the message path (wrap + log).
    """
    try:
        _notify_spawner(store, watch, text)
    except Exception as exc:
        logger.error("watch.notify spawner=%s FAILED err=%s",
                     watch.spawner_id, exc)
    _file_inbox(store, watch, target, text)


# --------------------------------------------------------------------------- #
# One watch check
# --------------------------------------------------------------------------- #
def _stall_ladder(store: Store, watch: DelegationWatch, target,
                  detail: str, label: str, now: datetime,
                  started: float, first_text: str,
                  final_text: str) -> str:
    """The shared escalation ladder: first alert → recheck in 10 min →
    final alert + close. Used by both the initial and the follow phase."""
    final = watch.checks_done + 1 >= MAX_CHECKS
    if final:
        store.close_watch(watch.id, "stalled")
        _alert_spawner(store, watch, target, final_text)
        outcome = "stalled"
    else:
        store.bump_watch(watch.id, next_check_iso(now, RECHECK_MINUTES))
        _alert_spawner(store, watch, target, first_text)
        outcome = "idle_alerted"
    logger.info("watch.check id=%s target=%s %s (checks=%d) in %.3fs",
                watch.id, watch.target_id, outcome.upper(),
                watch.checks_done + 1, time.monotonic() - started)
    return outcome


def _check_follow(store: Store, watch: DelegationWatch, target, sig,
                  label: str, now: datetime, started: float) -> str:
    """The follow phase (v12): keep watching a delegate that already showed
    life. See the module docstring for the signal set."""

    # The delegation ENDED — normal completion or a crash (stop_reason
    # discriminates). Either way the watch's job is done; one-line FYI so a
    # helper that quit without replying isn't a silent miss.
    if target.state == "stopped":
        reason = target.settings.get("stop_reason") or "no stop reason"
        store.close_watch(watch.id, "stopped")
        try:
            _notify_spawner(
                store, watch,
                f"{label} ℹ️ the delegate STOPPED ({reason}). If you expected "
                f"a reply and none came, nudge it (message with "
                f"from_pulse_id) or re-delegate. (Watch closed.)")
        except Exception as exc:
            logger.error("watch.check id=%s notify(stopped) FAILED err=%s",
                         watch.id, exc)
        logger.info("watch.check id=%s target=%s STOPPED in %.3fs",
                    watch.id, watch.target_id, time.monotonic() - started)
        return "stopped"

    # Stall conditions that need the ladder.
    hang: Optional[str] = None
    if not sig["fresh"]:
        hang = "heartbeat STALE or absent — the runner looks dead"
    else:
        busy_age = None
        if sig["state"] == "busy" and isinstance(sig["busy_since"],
                                                 (int, float)):
            busy_age = now.timestamp() - sig["busy_since"]
        grew = (sig["ctx"] is not None and watch.last_ctx is not None
                and sig["ctx"] > watch.last_ctx)
        if busy_age is not None and busy_age >= BUSY_STALL_S and not grew:
            hang = (f"busy for {busy_age / 60:.0f} min with no ctx growth "
                    f"— the turn looks HUNG")

    if hang:
        detail = f"{hang}, ctx {sig['ctx']:,} tokens" \
            if isinstance(sig["ctx"], int) else hang
        if sig["pending_inbox"]:
            detail += (f", {sig['pending_inbox']} message(s) sitting "
                       "UNCONSUMED in its inbox")
        return _stall_ladder(
            store, watch, target, detail, label, now, started,
            first_text=(f"{label} ⚠️ {detail}. Your delegate may be stalled "
                        f"mid-task. Check it (roster: GET /pulses) — I will "
                        f"check once more in {RECHECK_MINUTES} minutes."),
            final_text=(f"{label} 🛑 STILL stalled — {detail}. This "
                        f"delegation looks DEAD. Intervene now: re-send the "
                        f"brief as a message with from_pulse_id, or reset "
                        f"the pulse and re-dispatch. (Final alert; watch "
                        f"closed.)"))

    # No hang. Progress accounting: any ctx growth clears the streak; a BUSY
    # delegate is demonstrably alive mid-turn so it never accumulates zombie
    # checks (a legitimately long tool call has frozen ctx — the hang path
    # above owns busy stalls via BUSY_STALL_S); frozen IDLE ctx across
    # ZOMBIE_CHECKS = finished its turn and never replied.
    grew = (sig["ctx"] is not None and watch.last_ctx is not None
            and sig["ctx"] > watch.last_ctx)
    if grew or sig["state"] == "busy":
        follow_checks = 0
    elif _has_enabled_schedule(store, watch.target_id):
        # A pulse on a schedule (loop / daily / once) is idle BETWEEN wakes by
        # design. Frozen idle ctx there is not a zombie; it is the schedule
        # working.
        follow_checks = 0
    elif _in_watching_mode(target):
        # Watching mode IS "idle between wakes" declared by the pulse itself.
        follow_checks = 0
    elif _replied_to_spawner(watch):
        # The ladder's claim is "finished its turn and never replied". A
        # delegate that has messaged its spawner since the watch began DID
        # reply; the spawner has the report. (A lead pulse got 104 of
        # these soft alerts in 6 days and called 71 of them false.)
        follow_checks = 0
    else:
        follow_checks = watch.follow_checks + 1
    if follow_checks >= ZOMBIE_CHECKS:
        detail = (f"no progress for {ZOMBIE_CHECKS} checks "
                  f"(~{ZOMBIE_CHECKS * FOLLOW_INTERVAL_MINUTES} min), "
                  f"state {sig['state'] or '?'}, ctx "
                  f"{sig['ctx']:,} tokens"
                  if isinstance(sig["ctx"], int) else
                  f"no progress for {ZOMBIE_CHECKS} checks "
                  f"(~{ZOMBIE_CHECKS * FOLLOW_INTERVAL_MINUTES} min), "
                  f"state {sig['state'] or '?'}, ctx unknown")
        if sig["pending_inbox"]:
            detail += (f", {sig['pending_inbox']} message(s) sitting "
                       "UNCONSUMED in its inbox")
        return _stall_ladder(
            store, watch, target, detail, label, now, started,
            first_text=(f"{label} ⚠️ {detail} — it may have finished without "
                        f"replying. If you expected a response and none "
                        f"came, nudge it. I will check once more in "
                        f"{RECHECK_MINUTES} minutes."),
            final_text=(f"{label} 🛑 STILL no progress — {detail}. If you "
                        f"expected a reply and none came, the delegation "
                        f"looks DROPPED: nudge it (message with "
                        f"from_pulse_id) or re-delegate. (Final alert; watch "
                        f"closed.)"))

    store.set_follow(watch.id,
                     next_check_iso(now, FOLLOW_INTERVAL_MINUTES),
                     last_ctx=sig["ctx"] if sig["ctx"] is not None
                     else watch.last_ctx,
                     follow_checks=follow_checks,
                     checks_done=0 if grew else watch.checks_done)
    logger.info("watch.check id=%s target=%s clean (follow_checks=%d) "
                "in %.3fs", watch.id, watch.target_id, follow_checks,
                time.monotonic() - started)
    return "clean"



def _in_watching_mode(target) -> bool:
    try:
        return (target.settings or {}).get("mode") == "watching"
    except Exception:  # noqa: BLE001
        return False


def _replied_to_spawner(watch: DelegationWatch) -> bool:
    """True when the target wrote a pulse-to-pulse message into the
    spawner's inbox (new, feeding or acked) since the watch was created.
    Peer messages carry no filename prefix and start with a UTC stamp, so
    only files stamped after the watch's birth are opened."""
    try:
        since = re.sub(r"[-:]", "", watch.created_at.replace("Z", "")[:19])
        since = since.replace("T", "T")[:15]          # YYYYMMDDTHHMMSS
        inbox = paths.pulse_home(watch.spawner_id) / "inbox"
        for folder in ("new", "feeding", "acked"):
            d = inbox / folder
            if not d.is_dir():
                continue
            for entry in os.scandir(d):
                name = entry.name
                if not name[:1].isdigit() or not name.endswith(".json") \
                        or name[:15] < since:
                    continue
                try:
                    body = json.loads(Path(entry.path).read_text())
                except (OSError, ValueError):
                    continue
                if body.get("from_pulse_id") == watch.target_id:
                    return True
    except Exception as exc:  # noqa: BLE001 — never let this break a check
        logger.warning("watch.check reply scan watch=%s FAILED err=%s",
                       watch.id, exc)
    return False


def _has_enabled_schedule(store: Store, pulse_id: str) -> bool:
    try:
        return bool(store.list_schedules(pulse_id=pulse_id, enabled_only=True))
    except Exception as exc:  # noqa: BLE001 — never let this break a check
        logger.warning("watch.check schedules lookup pulse=%s FAILED err=%s",
                       pulse_id, exc)
        return False

def check_watch(store: Store, watch: DelegationWatch,
                now: Optional[datetime] = None) -> str:
    """Check one due watch and act. Returns the outcome for the tick log."""
    if now is None:
        now = datetime.now(timezone.utc)
    started = time.monotonic()

    # tick's snapshot can predate an archive or a manual watch closure.
    if store.get_watch(watch.id).state != "watching":
        return "closed"
    try:
        spawner = store.get_pulse(watch.spawner_id)
    except UnknownPulse:
        spawner = None
    if spawner is None or spawner.state == "archived":
        store.close_watch(watch.id, "owner_archived")
        return "owner_archived"

    try:
        target = store.get_pulse(watch.target_id)
    except UnknownPulse:
        target = None
    if target is None or target.state == "archived":
        store.close_watch(watch.id, "target_gone")
        why = "deleted" if target is None else "archived"
        # Legacy watches may survive an archive from before the store fix.
        # Archive closure is silent; preserve the deleted-target notification.
        if target is None:
            try:
                _notify_spawner(store, watch,
                                f"[delegation watch] your delegate (pulse "
                                f"{watch.target_id}) is {why} — the watch is "
                                f"closed. If work was still expected, re-delegate.")
            except Exception as exc:
                logger.error("watch.check id=%s notify(target_gone) FAILED err=%s",
                             watch.id, exc)
        logger.info("watch.check id=%s target=%s GONE (%s) in %.3fs",
                    watch.id, watch.target_id, why, time.monotonic() - started)
        return "target_gone"

    sig = target_signals(watch.target_id)
    label = f"[delegation watch: {target.title!r} (pulse {target.id})]"

    # Follow phase: the stall watcher proper.
    if watch.phase == "follow":
        # Nothing follows a delegate forever.
        try:
            created = datetime.fromisoformat(
                watch.created_at.replace("Z", "+00:00"))
            if created.tzinfo is None:
                created = created.replace(tzinfo=timezone.utc)
            if (now - created).total_seconds() >= WATCH_TTL_S:
                store.close_watch(watch.id, "expired")
                try:
                    _notify_spawner(
                        store, watch,
                        f"{label} ℹ️ stall watch expired after 24h and is "
                        f"closed. If the delegate still owes you work, "
                        f"check it or re-delegate.")
                except Exception as exc:
                    logger.error("watch.check id=%s notify(expired) FAILED "
                                 "err=%s", watch.id, exc)
                logger.info("watch.check id=%s target=%s EXPIRED in %.3fs",
                            watch.id, watch.target_id,
                            time.monotonic() - started)
                return "expired"
        except ValueError:
            logger.error("watch.check id=%s created_at=%r unparseable",
                         watch.id, watch.created_at)
        return _check_follow(store, watch, target, sig, label, now, started)

    # ---- initial phase: the first look after delegating ----
    if is_working(sig, watch.baseline_ctx):
        # v12: do NOT close — arm the follow phase so a helper that works
        # for a while and THEN hangs stays watched.
        store.arm_follow(watch.id,
                         next_check_iso(now, FOLLOW_INTERVAL_MINUTES),
                         last_ctx=sig["ctx"])
        if watch.confirm:
            try:
                _notify_spawner(
                    store, watch,
                    f"{label} ✅ work is happening — "
                    f"{_describe(sig, watch.baseline_ctx)}. I keep a quiet "
                    f"stall watch (~every {FOLLOW_INTERVAL_MINUTES} min) "
                    f"until it stops or the watch expires; expect its report "
                    f"as an ordinary message.")
            except Exception as exc:
                logger.error("watch.check id=%s notify(working) FAILED err=%s",
                             watch.id, exc)
        logger.info("watch.check id=%s target=%s WORKING (follow armed) "
                    "in %.3fs", watch.id, watch.target_id,
                    time.monotonic() - started)
        return "working"

    detail = _describe(sig, watch.baseline_ctx,
                       stop_reason=target.settings.get("stop_reason"))
    return _stall_ladder(
        store, watch, target, detail, label, now, started,
        first_text=(f"{label} ⚠️ nothing is happening — {detail}. Your "
                    f"delegation may not have landed. Check it (roster: "
                    f"GET /pulses) and re-send the brief if needed. I will "
                    f"check once more in {RECHECK_MINUTES} minutes."),
        final_text=(f"{label} 🛑 STILL nothing happening — {detail}. This "
                    f"delegation looks DEAD. Intervene now: re-send the "
                    f"brief as a message with from_pulse_id, or reset the "
                    f"pulse and re-dispatch. (Final alert; watch closed.)"))


# --------------------------------------------------------------------------- #
# The tick (called by the scheduler every pass)
# --------------------------------------------------------------------------- #
def tick(store: Store, now: Optional[datetime] = None) -> dict[str, int]:
    """Check every due watch. Sync + frozen-``now`` friendly, like the
    scheduler's tick. A raised check never kills the pass."""
    if now is None:
        now = datetime.now(timezone.utc)
    summary = {"scanned": 0, "due": 0, "working": 0, "idle_alerted": 0,
               "stalled": 0, "target_gone": 0, "stopped": 0, "expired": 0,
               "clean": 0, "failed": 0}
    try:
        watches = store.list_watches(state="watching")
    except Exception:
        logger.exception("watcher.tick list_watches RAISED — skipping tick")
        return summary
    summary["scanned"] = len(watches)
    for watch in watches:
        due_at = _parse_iso(watch.next_check_at)
        if due_at is None:
            logger.error("watcher.tick watch=%s next_check_at=%r unparseable "
                         "— closing", watch.id, watch.next_check_at)
            store.close_watch(watch.id, "invalid")
            summary["failed"] += 1
            continue
        if now < due_at:
            continue
        summary["due"] += 1
        try:
            outcome = check_watch(store, watch, now)
        except Exception:
            logger.exception("watcher.tick watch=%s check RAISED", watch.id)
            summary["failed"] += 1
            continue
        summary[outcome] = summary.get(outcome, 0) + 1
    if summary["due"]:
        logger.info("watcher.tick scanned=%d due=%d working=%d idle=%d "
                    "stalled=%d stopped=%d expired=%d gone=%d failed=%d",
                    summary["scanned"], summary["due"], summary["working"],
                    summary["idle_alerted"], summary["stalled"],
                    summary["stopped"], summary["expired"],
                    summary["target_gone"], summary["failed"])
    return summary
