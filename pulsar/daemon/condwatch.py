"""Condition watches.

Declarative, daemon-evaluated watches: a pulse registers a condition once and
the scheduler calls :func:`tick` every pass; when the condition trips the
daemon drops a message into the owner's inbox (waking it if stopped). Zero
model cost while quiet — unlike a watcher pulse on a loop, nothing runs unless
the condition actually fires.

Kinds (all stdlib, no new deps):

  pulse_state  a target pulse stops / goes idle (turn ended) / its context passes a token ceiling
  file         a path starts existing (or stops, ``"exists": false``)
  log          a regex appears in bytes appended to a file since last check
  http         an endpoint stops answering 2xx (or misses a body substring)
  cmd          a shell command exits non-zero (escape hatch)

Modes: ``once`` closes the watch after firing; ``edge`` (default) refires only
after a clean check in between — no alert-per-tick spam. Any spec may carry
``"invert": true`` to fire when the condition is NOT met — the way to wait for
an endpoint to come UP (``http`` + invert) or a command to start succeeding. All state lives in
the store row: restart-safe, no in-memory timers. Fire messages use
``source="watch"`` so the message route never mistakes them for pulse-to-pulse
sends (no recursive watches).
"""

from __future__ import annotations

import logging
import re
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from pulsar.daemon import mailbox, paths
from pulsar.store import ConditionWatch, Store, UnknownPulse

logger = logging.getLogger("pulsar.daemon.condwatch")

DEFAULT_INTERVAL_S = 60
HTTP_TIMEOUT_S = 5.0
CMD_TIMEOUT_S = 30.0
# Hard ceiling on a per-watch timeout_s. Checks run one after another inside
# the scheduler pass, so one slow cmd delays every loop/wake schedule behind
# it (2026-08-29: an ssh ConnectTimeout stalled the pass 8s).
MAX_TIMEOUT_S = 20.0
# How much trailing output a cmd/log fire message carries (chars) — details in
# the message, whole files do not.
_DETAIL_CAP = 2000


def next_check_iso(now: Optional[datetime] = None,
                   seconds: int = DEFAULT_INTERVAL_S) -> str:
    if now is None:
        now = datetime.now(timezone.utc)
    return (now + timedelta(seconds=seconds)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Spec validation (create-time; 400 material)
# --------------------------------------------------------------------------- #
def validate_spec(kind: str, spec: dict[str, Any]) -> list[str]:
    """Return a list of human problems; empty = the spec is valid."""
    errs: list[str] = []
    if not isinstance(spec, dict):
        return ["spec must be a JSON object"]
    if "invert" in spec and not isinstance(spec["invert"], bool):
        errs.append("spec.invert must be true or false")
    if "timeout_s" in spec:
        try:
            if float(spec["timeout_s"]) > MAX_TIMEOUT_S:
                errs.append(f"spec.timeout_s must be <= {MAX_TIMEOUT_S:g}")
        except (TypeError, ValueError):
            errs.append("spec.timeout_s must be a number")
    if kind == "pulse_state":
        if not spec.get("target_id"):
            errs.append("pulse_state needs spec.target_id")
        if not (spec.get("stopped") or spec.get("max_ctx") or spec.get("stale")
                or spec.get("idle")):
            errs.append("pulse_state needs at least one of "
                        "spec.stopped, spec.max_ctx, spec.stale, spec.idle")
    elif kind == "file":
        if not spec.get("path"):
            errs.append("file needs spec.path")
    elif kind == "log":
        if not spec.get("path"):
            errs.append("log needs spec.path")
        if not spec.get("pattern"):
            errs.append("log needs spec.pattern")
        else:
            try:
                re.compile(spec["pattern"])
            except re.error as exc:
                errs.append(f"log spec.pattern invalid regex: {exc}")
    elif kind == "http":
        if not spec.get("url"):
            errs.append("http needs spec.url")
    elif kind == "cmd":
        if not spec.get("cmd"):
            errs.append("cmd needs spec.cmd")
    return errs


# --------------------------------------------------------------------------- #
# Evaluators — pure-ish: return (bad, detail). They never raise for
# "condition not met"; they DO raise on surprise, and the tick treats that as
# a failed check (logged, retried next interval).
# --------------------------------------------------------------------------- #
def _eval_pulse_state(store: Store, spec: dict[str, Any]) -> tuple[bool, dict]:
    target_id = spec["target_id"]
    try:
        target = store.get_pulse(target_id)
    except UnknownPulse:
        return True, {"problem": f"target pulse {target_id} no longer exists"}
    beat = paths.read_heartbeat(target_id)
    detail: dict[str, Any] = {
        "state": target.state,
        "ctx": (beat or {}).get("last_ctx_tokens"),
    }
    bad = False
    if target.state == "archived":
        return True, {"problem": f"target pulse {target_id} is archived"}
    if spec.get("stopped") and target.state == "stopped":
        bad = True
        detail["reason"] = target.settings.get("stop_reason")
    if spec.get("stale") and not paths.heartbeat_fresh(beat):
        bad = True
        detail["reason"] = "heartbeat stale or absent"
    # idle = the target's CLI is alive and waiting for input, i.e. its turn
    # ENDED. A pulse stays store-state "running" between turns, so a
    # stopped/stale watch never sees a helper that merely finished a turn.
    # Use mode "edge" so it refires on every busy->idle transition.
    if (spec.get("idle") and target.state == "running"
            and paths.heartbeat_fresh(beat)
            and (beat or {}).get("state") == "idle"):
        bad = True
        detail["reason"] = "turn ended (runner idle)"
    max_ctx = spec.get("max_ctx")
    ctx = detail["ctx"]
    if max_ctx and isinstance(ctx, int) and ctx >= int(max_ctx):
        bad = True
        detail["reason"] = f"ctx {ctx:,} >= ceiling {int(max_ctx):,}"
    return bad, detail


def _eval_file(spec: dict[str, Any]) -> tuple[bool, dict]:
    from pathlib import Path
    p = Path(spec["path"])
    want = bool(spec.get("exists", True))
    exists = p.exists()
    detail: dict[str, Any] = {"path": str(p), "exists": exists}
    if exists:
        st = p.stat()
        detail["size"] = st.st_size
        detail["mtime"] = st.st_mtime
    # The condition holds when existence matches what we're waiting for
    # (exists=true -> fire when it appears; exists=false -> when it vanishes).
    bad = exists == want
    return bad, detail


def _eval_log(spec: dict[str, Any], prev: Optional[dict[str, Any]]
              ) -> tuple[bool, dict]:
    from pathlib import Path
    p = Path(spec["path"])
    pattern = re.compile(spec["pattern"])
    detail: dict[str, Any] = {"path": str(p), "pattern": spec["pattern"]}
    if not p.exists():
        detail["note"] = "file does not exist (yet)"
        detail["offset"] = 0
        return False, detail
    st = p.stat()
    size, ino = st.st_size, st.st_ino
    if not prev or not isinstance(prev.get("offset"), int):
        # First look: baseline at the current end. "NEW bytes" means bytes
        # appended after the watch existed — a pre-existing match must not
        # fire (would wake the owner on stale history).
        detail["offset"] = size
        detail["ino"] = ino
        detail["note"] = "baselined at current end"
        return False, detail
    offset = prev["offset"]
    if size < offset or prev.get("ino") not in (None, ino):
        # Truncated, or deleted and recreated (new inode): bytes already in
        # the replacement are new history — reread from the top. A pre-inode
        # detail (no "ino") is never rewound on its first post-deploy tick.
        offset = 0
    with p.open("rb") as fh:
        fh.seek(offset)
        chunk = fh.read()
    detail["offset"] = offset + len(chunk)
    detail["ino"] = ino
    text = chunk.decode("utf-8", errors="replace")
    m = pattern.search(text)
    if m:
        start = max(0, m.start() - 200)
        detail["match"] = text[start:m.end() + 200][-_DETAIL_CAP:]
        return True, detail
    return False, detail


def _eval_http(spec: dict[str, Any]) -> tuple[bool, dict]:
    url = spec["url"]
    timeout = min(float(spec.get("timeout_s", HTTP_TIMEOUT_S)), MAX_TIMEOUT_S)
    detail: dict[str, Any] = {"url": url}
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            status = resp.status
            body = resp.read(max(1, int(spec.get("read_bytes", 65536))))
    except urllib.error.HTTPError as exc:
        return True, {"url": url, "status": exc.code, "problem": "HTTP error"}
    except Exception as exc:
        return True, {"url": url, "problem": f"request failed: {exc}"}
    detail["status"] = status
    ok_lo, ok_hi = 200, 299
    if "status_ok" in spec:
        ok_lo = ok_hi = int(spec["status_ok"])
    bad = not (ok_lo <= status <= ok_hi)
    contains = spec.get("contains")
    if contains:
        text = body.decode("utf-8", errors="replace")
        detail["contains_found"] = contains in text
        if not detail["contains_found"]:
            bad = True
    return bad, detail


def _eval_cmd(spec: dict[str, Any]) -> tuple[bool, dict]:
    timeout = min(float(spec.get("timeout_s", CMD_TIMEOUT_S)), MAX_TIMEOUT_S)
    try:
        proc = subprocess.run(
            spec["cmd"], shell=True, capture_output=True, text=True,
            timeout=timeout)
    except subprocess.TimeoutExpired:
        return True, {"cmd": spec["cmd"], "problem": f"timed out after {timeout}s"}
    tail = ((proc.stdout or "") + (proc.stderr or ""))[:_DETAIL_CAP]
    return proc.returncode != 0, {
        "cmd": spec["cmd"], "returncode": proc.returncode, "output": tail,
    }


# --------------------------------------------------------------------------- #
# Fire + tick
# --------------------------------------------------------------------------- #
def _notify_owner(store: Store, watch: ConditionWatch, text: str) -> None:
    """Wake-if-stopped then drop the fire message into the owner's inbox.

    Mirrors watcher._notify_spawner's contract with ``source="watch"`` so the
    messages route never mistakes it for a pulse-to-pulse send. Raises on
    failure — the caller logs and the watch stays due (retry next tick).
    """
    owner = store.get_pulse(watch.pulse_id)
    name = mailbox.deliver(
        store, owner, text,
        on_archived="unarchive", remember=True,
        prefix=mailbox.CONDWATCH, source="watch",
        extra=mailbox.origin_keys("daemon", "condwatch"),
    )
    logger.info("condwatch.notify pulse=%s file=%s ok", watch.pulse_id, name)


def _fmt_detail(detail: dict[str, Any]) -> str:
    parts = []
    for k, v in detail.items():
        if k in ("output", "match") and v:
            parts.append(f"{k}: {v!r}")
        else:
            parts.append(f"{k}: {v}")
    return ", ".join(parts)


def check_watch(store: Store, watch: ConditionWatch,
                now: datetime) -> str:
    """Evaluate one due watch, notify if it trips. Returns the outcome."""
    started = time.monotonic()

    # A missing owner cannot be notified; archived owners still have live watches.
    try:
        owner = store.get_pulse(watch.pulse_id)
    except UnknownPulse:
        store.close_condition_watch(watch.id, "owner_gone")
        return "owner_gone"

    # Evaluate. A pulse_state watch whose target vanished closes outright.
    if watch.kind == "pulse_state":
        try:
            target = store.get_pulse(watch.spec["target_id"])
        except UnknownPulse:
            store.close_condition_watch(watch.id, "target_gone")
            logger.info("condwatch.check id=%s TARGET_GONE in %.3fs",
                        watch.id, time.monotonic() - started)
            return "target_gone"
        if target.state == "archived":
            # Archive is a silent closure, not work for the owner. Sending
            # a closure message here would wake a finished pulse.
            store.close_condition_watch(watch.id, "target_gone")
            logger.info("condwatch.check id=%s TARGET_ARCHIVED closed in %.3fs",
                        watch.id, time.monotonic() - started)
            return "target_gone"
        bad, detail = _eval_pulse_state(store, watch.spec)
    elif watch.kind == "file":
        bad, detail = _eval_file(watch.spec)
    elif watch.kind == "log":
        bad, detail = _eval_log(watch.spec, watch.detail)
    elif watch.kind == "http":
        bad, detail = _eval_http(watch.spec)
    elif watch.kind == "cmd":
        bad, detail = _eval_cmd(watch.spec)
    else:
        store.close_condition_watch(watch.id, "invalid_kind")
        return "invalid_kind"

    if watch.spec.get("invert"):
        bad = not bad
        detail["inverted"] = True
    label = f"[watch: {watch.note or watch.kind}]"
    was_bad = bool(watch.bad)
    outcome = "clean"
    if bad:
        if watch.mode == "once" or not was_bad:
            outcome = "fired"  # once: every trip; edge: healthy→bad only
        else:
            outcome = "bad_quiet"  # still bad, already alerted — stay quiet
        if outcome == "fired":
            text = (f"{label} 🔔 condition met — {_fmt_detail(detail)}. "
                    f"(Watch id {watch.id}, mode {watch.mode}.)")
            if watch.mode == "edge":
                text += " I stay quiet while it stays met; I re-arm when it clears."
            try:
                _notify_owner(store, watch, text)
                store.mark_condition_fired(watch.id, "delivered")
                # A once-watch closes only AFTER the wake landed — closing
                # first lost the message whenever delivery raised.
                if watch.mode == "once":
                    store.close_condition_watch(watch.id, "fired")
            except Exception as exc:
                logger.exception("condwatch.check id=%s notify RAISED", watch.id)
                store.mark_condition_fired(watch.id, f"failed: {exc}"[:200])
                # Delivery failed: record the check as NOT bad so an edge
                # watch re-fires (retries the wake) on the next tick instead
                # of sitting silently "met" with a lost message.
                bad = False
                outcome = "delivery_failed"
    else:
        outcome = "clean"  # includes the re-arm check after a bad streak

    store.bump_condition_watch(watch.id, next_check_iso(now, watch.interval_s),
                               bad, detail)
    logger.info("condwatch.check id=%s kind=%s %s (checks=%d) in %.3fs",
                watch.id, watch.kind, outcome.upper(),
                watch.checks_done + 1, time.monotonic() - started)
    return outcome


def tick(store: Store, now: Optional[datetime] = None) -> dict[str, int]:
    """Check every due condition watch. Sync + frozen-``now`` friendly."""
    if now is None:
        now = datetime.now(timezone.utc)
    summary = {"scanned": 0, "due": 0, "fired": 0, "clean": 0,
               "bad_quiet": 0, "owner_gone": 0,
               "target_gone": 0, "failed": 0}
    try:
        watches = store.list_condition_watches(state="watching")
    except Exception:
        logger.exception("condwatch.tick list_condition_watches RAISED — skip")
        return summary
    summary["scanned"] = len(watches)
    for watch in watches:
        try:
            due_at = datetime.fromisoformat(watch.next_check_at)
            if due_at.tzinfo is None:
                due_at = due_at.replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            logger.error("condwatch.tick id=%s next_check_at=%r unparseable — "
                         "closing", watch.id, watch.next_check_at)
            store.close_condition_watch(watch.id, "invalid")
            summary["failed"] += 1
            continue
        if now < due_at:
            continue
        summary["due"] += 1
        try:
            outcome = check_watch(store, watch, now)
        except Exception:
            # Surprise during evaluation: bump the schedule so we retry at the
            # NEXT interval, not every 30s tick (no tight retry loops).
            logger.exception("condwatch.tick id=%s check RAISED", watch.id)
            try:
                store.bump_condition_watch(
                    watch.id, next_check_iso(now, watch.interval_s),
                    watch.bad, watch.detail)
            except Exception:
                logger.exception("condwatch.tick id=%s failure-bump RAISED",
                                 watch.id)
            summary["failed"] += 1
            continue
        summary[outcome] = summary.get(outcome, 0) + 1
    if summary["due"]:
        logger.info("condwatch.tick scanned=%d due=%d fired=%d clean=%d "
                    "bad_quiet=%d gone=%d failed=%d", summary["scanned"],
                    summary["due"], summary["fired"], summary["clean"],
                    summary["bad_quiet"], summary["owner_gone"]
                    + summary["target_gone"], summary["failed"])
    return summary
