"""Filesystem layout helpers for the daemon.

The data root defaults to ``<repo>/data``, overridable via the
``PULSAR_DATA`` env var (tests point it at a tmp dir). Per-pulse homes live at
``<data>/pulses/<id>`` and mirror the runner's expectations
(``runner.sock``, ``heartbeat.json``, ``inbox/new``, ``events.jsonl``).
"""

from __future__ import annotations

import json
import logging
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger("pulsar.daemon.paths")

DEFAULT_DATA = Path(__file__).resolve().parents[2] / "data"

# A heartbeat older than this (seconds) means the runner is dead/stalled.
# The runner beats every 5s; 60s leaves room for missed beats under heavy
# CPU load (15s reaped live pulses at load 16-23, 2026-10-03).
HEARTBEAT_FRESH_S = 60.0


def data_root() -> Path:
    """Resolve the data root, honoring ``PULSAR_DATA`` (read live, not cached)."""
    env = os.environ.get("PULSAR_DATA")
    return Path(env) if env else DEFAULT_DATA


def pulse_home(pulse_id: str) -> Path:
    return data_root() / "pulses" / pulse_id


def sock_path(pulse_id: str) -> Path:
    return pulse_home(pulse_id) / "runner.sock"


def heartbeat_path(pulse_id: str) -> Path:
    return pulse_home(pulse_id) / "heartbeat.json"


def inbox_new(pulse_id: str) -> Path:
    return pulse_home(pulse_id) / "inbox" / "new"


def attachments_dir(pulse_id: str) -> Path:
    """Uploaded chat attachments, grouped per message: attachments/<msg-id>/<file>."""
    return pulse_home(pulse_id) / "attachments"


def events_path(pulse_id: str) -> Path:
    return pulse_home(pulse_id) / "events.jsonl"


def partial_path(pulse_id: str) -> Path:
    """The runner's ephemeral in-progress reply (streaming mode). Exists only
    while a reply is being generated; atomic-replaced, cleared at the block
    boundary."""
    return pulse_home(pulse_id) / "partial.json"


def reasoning_path(pulse_id: str) -> Path:
    """The runner's ephemeral reasoning trace for the current turn. Written
    while the model thinks, kept after the turn ends (so the last trace stays
    readable), cleared when the next turn starts and on runner start/exit."""
    return pulse_home(pulse_id) / "reasoning.json"


def code_path(pulse_id: str) -> Path:
    """The runner's ephemeral live code: the argument JSON of the tool call
    being generated. Atomic-replaced; cleared when that tool_call lands, at
    the turn boundary, and on runner start/exit."""
    return pulse_home(pulse_id) / "code.json"


def read_heartbeat(pulse_id: str) -> Optional[dict[str, Any]]:
    """Read + parse ``heartbeat.json``; None if missing/unreadable (logged)."""
    path = heartbeat_path(pulse_id)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("heartbeat read pulse=%s FAILED err=%s", pulse_id, exc)
        return None


def _parse_ts(ts: str) -> Optional[float]:
    """Parse a runner heartbeat ts (``%Y-%m-%dT%H:%M:%S%z``) to epoch seconds.

    The runner writes tz-aware strftime timestamps; be forgiving of a stray
    ``Z`` or a missing offset (treat naive as UTC) rather than fail closed.
    """
    if not ts:
        return None
    raw = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        logger.error("heartbeat ts unparseable ts=%r", ts)
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.timestamp()


def heartbeat_age(beat: Optional[dict[str, Any]]) -> Optional[float]:
    """Seconds since a heartbeat was written, or None if unknown/unparseable."""
    if not beat:
        return None
    epoch = _parse_ts(beat.get("ts", ""))
    if epoch is None:
        return None
    return max(0.0, time.time() - epoch)


def heartbeat_fresh(beat: Optional[dict[str, Any]]) -> bool:
    """True iff the heartbeat exists and is younger than the freshness window."""
    age = heartbeat_age(beat)
    return age is not None and age < HEARTBEAT_FRESH_S
