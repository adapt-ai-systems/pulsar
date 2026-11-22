"""Handoff durable state: the in-flight marker file, the ledger,
the cooldown, the threshold, and the timeouts. Everything that reads or writes
what must survive a daemon restart.

The reply/stop timeouts live here (not in engine.py) because IN_FLIGHT_STALE_S
is derived from them and this module sits below engine.py in the import order.
engine.py reads them as ``state.<NAME>`` at call time."""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from pulsar import models as models_mod
from pulsar.daemon import paths
from pulsar.daemon.timeutil import now_iso as _now_iso
from pulsar.fsutil import atomic_write_json
from pulsar.store import Pulse, Store, UnknownPulse

logger = logging.getLogger("pulsar.daemon.handoff")


# Per-model defaults live in pulsar/models.json (pulsar.models.default_handoff_for).
FALLBACK_THRESHOLD = 200_000

# After a handoff (success OR failure) leave the pulse alone this long so a
# slow-to-report successor or a failed attempt never tight-loops the engine.
COOLDOWN_S = 600.0

# The request reaches the pulse mid-turn (injected), but the reply is only
# capturable at the turn boundary — give the model room to wrap its current
# step first (2026-07-27: 240s was shorter than a single normal turn and the
# handoff aborted on reply_timeout while the pulse blew past 234k ctx).
#
# The reply clock starts when the request's user_message appears in
# events.jsonl (the turn that will answer it has begun), not when the file is
# written — a pulse deep in a non-steerable turn can take minutes just to see
# it (handoff review 2026-10-02: 47 of 1,080 requests delivered >60s late).
# DELIVERY_TIMEOUT_S bounds the wait for that event.
REPLY_TIMEOUT_S = 600.0
DELIVERY_TIMEOUT_S = 600.0
# One re-ask when the note lacks the requested sections (a 259-char "That's
# dispatched…" once became a successor's whole memory). Shorter: the model
# has only to write the note now, no step to wrap up.
REASK_TIMEOUT_S = 300.0
STOP_SETTLE_S = 15.0

# Phases that mean "a handoff owns this pulse right now" (single-flight guard).
IN_FLIGHT_PHASES = frozenset({"requested", "stopping"})
# ...but only while the marker is younger than one whole attempt. An older one
# is debris from a daemon that died mid-handoff, and must not wedge the pulse
# out of handoffs forever.
# A model switch waits this long for the old model's notes, then seeds from
# the transcript — under a reverse proxy's 60s upstream timeout.
SWITCH_REPLY_TIMEOUT_S = 45.0
IN_FLIGHT_STALE_S = (DELIVERY_TIMEOUT_S + REPLY_TIMEOUT_S + REASK_TIMEOUT_S
                     + STOP_SETTLE_S + 60.0)


def threshold_for(pulse: Pulse) -> int:
    """The handoff threshold for a pulse: explicit setting, else family default."""
    if pulse.handoff_tokens:
        return pulse.handoff_tokens
    tokens = models_mod.default_handoff_for(pulse.model)
    return tokens if tokens else FALLBACK_THRESHOLD


# --------------------------------------------------------------------------- #
# Durable state helpers
# --------------------------------------------------------------------------- #
def _state_path(pulse_id: str) -> Path:
    return paths.pulse_home(pulse_id) / "handoff_state.json"


def write_state(pulse_id: str, phase: str, **extra: Any) -> None:
    p = _state_path(pulse_id)
    payload = {"phase": phase, "ts": _now_iso(), **extra}
    atomic_write_json(p, payload)
    logger.info("handoff.state pulse=%s phase=%s", pulse_id, phase)


def read_state(pulse_id: str) -> Optional[dict]:
    p = _state_path(pulse_id)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("handoff.state pulse=%s UNREADABLE err=%s", pulse_id, exc)
        return None


def in_flight(pulse_id: str) -> Optional[str]:
    """The phase of a handoff already running for this pulse, else None.

    The durable marker is the lock: an unparseable/undated/old one reads as
    "not in flight" so a crashed daemon never wedges the pulse.
    """
    state = read_state(pulse_id)
    if not state or state.get("phase") not in IN_FLIGHT_PHASES:
        return None
    try:
        stamped = datetime.fromisoformat(str(state.get("ts")).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("handoff.state pulse=%s in-flight marker has bad ts %r "
                       "— treating as stale", pulse_id, state.get("ts"))
        return None
    if time.time() - stamped.timestamp() >= IN_FLIGHT_STALE_S:
        logger.warning("handoff.state pulse=%s stale %r marker from %s — "
                       "treating as abandoned", pulse_id, state.get("phase"),
                       state.get("ts"))
        return None
    return str(state.get("phase"))


def reserve(pulse_id: str, reason: str) -> bool:
    """Atomically reserve an in-flight handoff marker if none is active."""
    if in_flight(pulse_id) is not None:
        return False
    write_state(pulse_id, "requested", reason=reason)
    return True


# --------------------------------------------------------------------------- #
# Handoff ledger (handoff review 2026-10-02): one JSON line per attempt,
# appended at the attempt's exit. The journal expires in days and
# handoff_state.json keeps only the last phase; this is the durable record
# the seed experiments are judged on.
# --------------------------------------------------------------------------- #
LEDGER_FILE = "handoffs.jsonl"


def ledger_path() -> Path:
    return paths.data_root() / LEDGER_FILE


def append_ledger(rec: dict) -> None:
    """Append one record. Never raises — a ledger failure must not fail a
    handoff (logged loudly instead)."""
    try:
        line = json.dumps(rec, default=str)
        with ledger_path().open("a") as fh:
            fh.write(line + "\n")
    except Exception:
        logger.exception("handoff.ledger pulse=%s append FAILED",
                         rec.get("pulse_id"))


def store_state(store: Store, pulse_id: str) -> Optional[str]:
    """The pulse's CURRENT store state, or None when it was deleted. A
    handoff waits minutes; the Pulse it was handed is stale by then (live
    2026-10-13: an archived pulse got a handoff request, and another was
    respawned by a handoff that raced its archive)."""
    try:
        return store.get_pulse(pulse_id).state
    except UnknownPulse:
        return None


def in_cooldown(pulse: Pulse, now: Optional[float] = None) -> bool:
    last = pulse.settings.get("last_handoff_at")
    if not last:
        return False
    try:
        dt = datetime.fromisoformat(str(last).replace("Z", "+00:00"))
    except ValueError:
        logger.warning("handoff.cooldown pulse=%s bad stamp %r — ignoring",
                       pulse.id, last)
        return False
    age = (now or time.time()) - dt.timestamp()
    return age < COOLDOWN_S
