"""Condition watches: row type, error, limits and Store
methods.
"""

from __future__ import annotations

import json
import sqlite3

from dataclasses import dataclass
from typing import Any, Optional

from pulsar.store.common import StoreError, _Raw, _new_id, _now_iso, logger


# Valid condition-watch kinds. Store validates at create; the
# daemon's evaluator implements one branch per kind.
# Guard against a lead that re-registers on every turn (2026-08-30).
COND_WATCH_MAX_OPEN = 20


COND_WATCH_KINDS = ("pulse_state", "file", "log", "http", "cmd")


class UnknownConditionWatch(StoreError):
    """Referenced a condition watch id that does not exist."""


@dataclass(frozen=True)
class ConditionWatch:
    id: str
    pulse_id: str                 # owner — registers the watch, gets notified
    kind: str                     # pulse_state|file|log|http|cmd
    spec: dict[str, Any]          # kind-specific condition
    mode: str                     # once|edge
    interval_s: int               # seconds between checks
    note: Optional[str]           # human label, echoed in the fire message
    state: str                    # watching|closed
    checks_done: int
    bad: Optional[int]            # last check tripped? (edge tracking)
    detail: Optional[dict[str, Any]]  # last check's observed values
    next_check_at: str
    outcome: Optional[str]        # closed only: fired|owner_gone|target_gone
    created_at: str
    closed_at: Optional[str]
    last_fired_at: Optional[str] = None   # last time the condition tripped + wake attempted
    last_delivery: Optional[str] = None   # "delivered" | "failed: <reason>"


def _condition_watch_from_row(row: sqlite3.Row) -> ConditionWatch:
    spec, detail = {}, None
    try:
        spec = json.loads(row["spec_json"])
    except (ValueError, TypeError):
        pass
    if row["detail_json"]:
        try:
            detail = json.loads(row["detail_json"])
        except (ValueError, TypeError):
            pass
    return ConditionWatch(
        id=row["id"],
        pulse_id=row["pulse_id"],
        kind=row["kind"],
        spec=spec,
        mode=row["mode"],
        interval_s=row["interval_s"],
        note=row["note"],
        state=row["state"],
        checks_done=row["checks_done"],
        bad=row["bad"],
        detail=detail,
        next_check_at=row["next_check_at"],
        outcome=row["outcome"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        last_fired_at=row["last_fired_at"],
        last_delivery=row["last_delivery"],
    )


class CondWatchStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def create_condition_watch(
        self,
        pulse_id: str,
        kind: str,
        spec: dict[str, Any],
        next_check_at: str,
        mode: str = "edge",
        interval_s: int = 60,
        note: Optional[str] = None,
    ) -> ConditionWatch:
        """Record a condition watch. Raises UnknownPulse / ValueError
        loudly — a watch that can't be recorded is a silent-failure risk."""
        if kind not in COND_WATCH_KINDS:
            logger.error("condwatch.create pulse=%s bad-kind=%r", pulse_id, kind)
            raise ValueError(f"invalid watch kind: {kind!r}")
        if mode not in ("once", "edge"):
            logger.error("condwatch.create pulse=%s bad-mode=%r", pulse_id, mode)
            raise ValueError(f"invalid watch mode: {mode!r}")
        self.get_pulse(pulse_id)
        open_n = len(self.list_condition_watches(state="watching",
                                                 pulse_id=pulse_id))
        if open_n >= COND_WATCH_MAX_OPEN:
            logger.error("condwatch.create pulse=%s open=%d CAP", pulse_id, open_n)
            raise ValueError(
                f"pulse already has {open_n} open condition watches (cap "
                f"{COND_WATCH_MAX_OPEN}) — delete finished ones first: "
                f"GET /watches/conditions?pulse_id={pulse_id}&state=watching")
        wid = _new_id()
        now = _now_iso()
        try:
            self._conn.execute(
                "INSERT INTO condition_watches (id, pulse_id, kind, spec_json, "
                "mode, interval_s, note, state, checks_done, bad, detail_json, "
                "next_check_at, outcome, created_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, 'watching', 0, NULL, NULL, ?, "
                "NULL, ?, NULL);",
                (wid, pulse_id, kind, json.dumps(spec), mode, int(interval_s),
                 note, next_check_at, now),
            )
        except sqlite3.Error as exc:
            logger.error("condwatch.create pulse=%s FAILED err=%s", pulse_id, exc)
            raise
        logger.info("condwatch.create id=%s pulse=%s kind=%s interval=%ds "
                    "next_check=%s ok", wid, pulse_id, kind, interval_s,
                    next_check_at)
        return self.get_condition_watch(wid)

    def get_condition_watch(self, watch_id: str) -> ConditionWatch:
        row = self._conn.execute(
            "SELECT * FROM condition_watches WHERE id = ?;", (watch_id,)
        ).fetchone()
        if row is None:
            logger.error("condwatch.get id=%s NOT-FOUND", watch_id)
            raise UnknownConditionWatch(watch_id)
        return _condition_watch_from_row(row)

    def list_condition_watches(
        self, state: Optional[str] = None,
        pulse_id: Optional[str] = None,
    ) -> list[ConditionWatch]:
        """List condition watches, oldest first; state + owner filter."""
        where, params = [], []
        if state is not None:
            where.append("state = ?")
            params.append(state)
        if pulse_id is not None:
            where.append("pulse_id = ?")
            params.append(pulse_id)
        clause = f" WHERE {' AND '.join(where)}" if where else ""
        rows = self._conn.execute(
            f"SELECT * FROM condition_watches{clause} ORDER BY created_at, "
            "rowid;", params).fetchall()
        return [_condition_watch_from_row(r) for r in rows]

    def bump_condition_watch(
        self, watch_id: str, next_check_at: str,
        bad: Optional[bool], detail: Optional[dict[str, Any]],
    ) -> ConditionWatch:
        """Record a completed check that keeps watching (checks_done += 1)."""
        return self._update_one(
            "condition_watches", "id", watch_id,
            {"checks_done": _Raw("checks_done + 1"),
             "bad": None if bad is None else 1 if bad else 0,
             "detail_json": json.dumps(detail) if detail is not None else None,
             "next_check_at": next_check_at},
            getter=self.get_condition_watch, from_row=_condition_watch_from_row,
            ok=("condwatch.bump id=%s next_check=%s bad=%s ok",
                watch_id, next_check_at, bad),
            fail=("condwatch.bump id=%s", watch_id))

    def mark_condition_fired(self, watch_id: str, delivery: str,
                             ts: Optional[str] = None) -> ConditionWatch:
        """Stamp a fire attempt: ``last_fired_at`` = now and ``last_delivery``
        = "delivered" or "failed: <reason>" (Watchers view, 2026-09-07)."""
        ts = ts or _now_iso()
        return self._update_one(
            "condition_watches", "id", watch_id,
            {"last_fired_at": ts, "last_delivery": delivery},
            getter=self.get_condition_watch, from_row=_condition_watch_from_row,
            ok=("condwatch.fired id=%s delivery=%s ok", watch_id, delivery),
            fail=("condwatch.fired id=%s", watch_id))

    def close_condition_watch(self, watch_id: str,
                              outcome: str) -> ConditionWatch:
        """Close a condition watch with its outcome (fired|owner_gone|...)."""
        return self._update_one(
            "condition_watches", "id", watch_id,
            {"state": "closed", "outcome": outcome, "closed_at": _now_iso()},
            getter=self.get_condition_watch, from_row=_condition_watch_from_row,
            ok=("condwatch.close id=%s outcome=%s ok", watch_id, outcome),
            fail=("condwatch.close id=%s", watch_id))

    def delete_condition_watch(self, watch_id: str) -> None:
        """Hard-delete one condition watch (owner cleanup / manual removal)."""
        self.get_condition_watch(watch_id)
        try:
            self._conn.execute(
                "DELETE FROM condition_watches WHERE id = ?;", (watch_id,))
        except sqlite3.Error as exc:
            logger.error("condwatch.delete id=%s FAILED err=%s", watch_id, exc)
            raise
        logger.info("condwatch.delete id=%s ok", watch_id)
