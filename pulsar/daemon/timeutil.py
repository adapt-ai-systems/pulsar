"""Shared UTC ISO-8601 timestamp helpers for daemon modules."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional


def now_iso() -> str:
    """Return the current UTC time as an ISO-8601 seconds-resolution string."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def parse_iso(ts: Optional[str]) -> Optional[datetime]:
    """Parse an ISO-8601 stamp to a tz-aware UTC datetime, forgiving of ``Z``.

    Naive strings are treated as UTC (the store writes tz-aware, but be
    defensive). Returns None on an empty/unparseable value (logged by caller).
    """
    if not ts:
        return None
    raw = ts.replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)
