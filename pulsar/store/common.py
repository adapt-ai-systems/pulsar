"""Shared pieces of the store modules.

The ``pulsar.store`` logger, the ``_UNSET`` sentinel, the ``_Raw`` SQL marker,
the base ``StoreError`` and small id/time helpers.
"""

from __future__ import annotations

import logging
import uuid

from datetime import datetime, timezone


logger = logging.getLogger("pulsar.store")


_UNSET = object()


class _Raw(str):
    """A SQL fragment ``_update_one`` inlines into SET instead of binding
    (``_Raw("checks_done + 1")``). Store-internal literals only, never input."""


class StoreError(Exception):
    """Base class for store errors."""


def _now_iso() -> str:
    """ISO-8601 UTC, second precision, e.g. ``2026-07-26T19:22:03+00:00``."""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _new_id() -> str:
    return uuid.uuid4().hex


def _like(q: str) -> str:
    """``%q%`` with backslash, % and _ escaped, for ``LIKE ? ESCAPE '\\'``."""
    escaped = (q.replace("\\", "\\\\")
               .replace("%", "\\%").replace("_", "\\_"))
    return f"%{escaped}%"
