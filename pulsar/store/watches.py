"""Delegation watches: row type, error and Store methods."""

from __future__ import annotations

import sqlite3

from dataclasses import dataclass
from typing import Any, Optional

from pulsar.store.common import StoreError, _Raw, _new_id, _now_iso, logger


class UnknownWatch(StoreError):
    """Referenced a delegation watch id that does not exist."""


@dataclass(frozen=True)
class DelegationWatch:
    id: str
    spawner_id: str
    target_id: str
    kind: str                     # spawn|message
    confirm: bool                 # report "working" too, not just stalls
    state: str                    # watching|closed
    baseline_ctx: Optional[int]   # target ctx tokens when the watch began
    checks_done: int
    next_check_at: str
    outcome: Optional[str]        # working|stalled|target_gone|stopped|expired
    created_at: str
    closed_at: Optional[str]
    phase: str = "initial"        # initial | follow (v12 stall watcher)
    last_ctx: Optional[int] = None   # target ctx at the last follow check
    follow_checks: int = 0        # consecutive follow checks with no ctx growth


def _watch_from_row(row: sqlite3.Row) -> DelegationWatch:
    return DelegationWatch(
        id=row["id"],
        spawner_id=row["spawner_id"],
        target_id=row["target_id"],
        kind=row["kind"],
        confirm=bool(row["confirm"]),
        state=row["state"],
        baseline_ctx=row["baseline_ctx"],
        checks_done=row["checks_done"],
        next_check_at=row["next_check_at"],
        outcome=row["outcome"],
        created_at=row["created_at"],
        closed_at=row["closed_at"],
        phase=row["phase"] or "initial",
        last_ctx=row["last_ctx"],
        follow_checks=row["follow_checks"] or 0,
    )


class WatchStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def create_watch(
        self,
        spawner_id: str,
        target_id: str,
        kind: str,
        next_check_at: str,
        confirm: bool = True,
        baseline_ctx: Optional[int] = None,
    ) -> DelegationWatch:
        """Record a delegation watch.

        Validates both pulses exist and the kind is spawn|message. Raises
        :class:`UnknownPulse` / :class:`ValueError` loudly — a watch that
        can't be recorded is itself a silent-failure risk.
        """
        if kind not in ("spawn", "message"):
            logger.error("watch.create spawner=%s target=%s bad-kind=%r",
                         spawner_id, target_id, kind)
            raise ValueError(f"invalid watch kind: {kind!r}")
        self.get_pulse(spawner_id)
        self.get_pulse(target_id)
        wid = _new_id()
        now = _now_iso()
        try:
            self._conn.execute(
                "INSERT INTO delegation_watches (id, spawner_id, target_id, "
                "kind, confirm, state, baseline_ctx, checks_done, "
                "next_check_at, outcome, created_at, closed_at) "
                "VALUES (?, ?, ?, ?, ?, 'watching', ?, 0, ?, NULL, ?, NULL);",
                (wid, spawner_id, target_id, kind, 1 if confirm else 0,
                 baseline_ctx, next_check_at, now),
            )
        except sqlite3.Error as exc:
            logger.error("watch.create spawner=%s target=%s FAILED err=%s",
                         spawner_id, target_id, exc)
            raise
        logger.info("watch.create id=%s spawner=%s target=%s kind=%s "
                    "next_check=%s ok", wid, spawner_id, target_id, kind,
                    next_check_at)
        return self.get_watch(wid)

    def get_watch(self, watch_id: str) -> DelegationWatch:
        row = self._conn.execute(
            "SELECT * FROM delegation_watches WHERE id = ?;", (watch_id,)
        ).fetchone()
        if row is None:
            logger.error("watch.get id=%s NOT-FOUND", watch_id)
            raise UnknownWatch(watch_id)
        return _watch_from_row(row)

    def list_watches(self, state: Optional[str] = None) -> list[DelegationWatch]:
        """List delegation watches, oldest first; ``state`` filters."""
        where, params = "", []
        if state is not None:
            where, params = " WHERE state = ?", [state]
        rows = self._conn.execute(
            f"SELECT * FROM delegation_watches{where} ORDER BY created_at, "
            "rowid;", params).fetchall()
        return [_watch_from_row(r) for r in rows]

    def prune_closed_watches(self) -> int:
        """Delete closed delegation_watches (they accumulate forever)."""
        try:
            cur = self._conn.execute(
                "DELETE FROM delegation_watches WHERE state = 'closed';"
            )
        except sqlite3.Error as exc:
            logger.error("watches.prune FAILED err=%s", exc)
            raise
        n = cur.rowcount if cur.rowcount is not None else 0
        logger.info("watches.prune closed_removed=%d", n)
        return n

    def has_active_watch(self, spawner_id: str, target_id: str) -> bool:
        """True iff a watching-state watch already covers this pair (dedup —
        a burst of messages must not spawn a watch per message)."""
        row = self._conn.execute(
            "SELECT 1 FROM delegation_watches WHERE spawner_id = ? AND "
            "target_id = ? AND state = 'watching' LIMIT 1;",
            (spawner_id, target_id)).fetchone()
        return row is not None

    def bump_watch(self, watch_id: str, next_check_at: str) -> DelegationWatch:
        """Record a completed check that keeps watching (checks_done += 1)."""
        return self._update_one(
            "delegation_watches", "id", watch_id,
            {"checks_done": _Raw("checks_done + 1"), "next_check_at": next_check_at},
            getter=self.get_watch, from_row=_watch_from_row,
            ok=("watch.bump id=%s next_check=%s ok", watch_id, next_check_at),
            fail=("watch.bump id=%s", watch_id))

    def close_watch(self, watch_id: str, outcome: str) -> DelegationWatch:
        """Close a watch with its outcome (working|stalled|target_gone|
        stopped|expired)."""
        return self._update_one(
            "delegation_watches", "id", watch_id,
            {"state": "closed", "outcome": outcome, "closed_at": _now_iso(),
             "checks_done": _Raw("checks_done + 1")},
            getter=self.get_watch, from_row=_watch_from_row,
            ok=("watch.close id=%s outcome=%s ok", watch_id, outcome),
            fail=("watch.close id=%s", watch_id))

    def active_watch(self, spawner_id: str,
                     target_id: str) -> Optional[DelegationWatch]:
        """The watching-state watch covering this pair, if any (v12: lets a
        NEW pulse-to-pulse message re-baseline a follow-phase watch instead
        of being silently swallowed by the dedup)."""
        row = self._conn.execute(
            "SELECT * FROM delegation_watches WHERE spawner_id = ? AND "
            "target_id = ? AND state = 'watching' LIMIT 1;",
            (spawner_id, target_id)).fetchone()
        return _watch_from_row(row) if row is not None else None

    def arm_follow(self, watch_id: str, next_check_at: str,
                   last_ctx: Optional[int],
                   baseline_ctx: Optional[int] = None) -> DelegationWatch:
        """Move a watch into the follow phase (v12 stall watcher): after the
        first working check the watch keeps watching instead of closing.
        Resets the ladder counters — the follow phase runs its own ladder.
        ``baseline_ctx`` re-baselines too (a new message landing on an
        already-following pair)."""
        changes: dict[str, Any] = {
            "phase": "follow", "last_ctx": last_ctx,
            "follow_checks": 0, "checks_done": 0,
            "next_check_at": next_check_at}
        if baseline_ctx is not None:
            changes["baseline_ctx"] = baseline_ctx
        return self._update_one(
            "delegation_watches", "id", watch_id, changes,
            getter=self.get_watch, from_row=_watch_from_row,
            ok=("watch.arm_follow id=%s last_ctx=%s baseline=%s ok",
                watch_id, last_ctx, baseline_ctx),
            fail=("watch.arm_follow id=%s", watch_id))

    def set_follow(self, watch_id: str, next_check_at: str,
                   last_ctx: Optional[int], follow_checks: int,
                   checks_done: int = 0) -> DelegationWatch:
        """Record a clean follow check: update progress counters and
        next_check_at. ``checks_done`` defaults to 0 (a ctx GROWTH re-arms
        the ladder); the caller passes the current count to keep an
        already-escalating ladder alive across a signal change (v12: hang
        alert then idle-frozen must still reach its final)."""
        return self._update_one(
            "delegation_watches", "id", watch_id,
            {"last_ctx": last_ctx, "follow_checks": follow_checks,
             "checks_done": checks_done, "next_check_at": next_check_at},
            getter=self.get_watch, from_row=_watch_from_row,
            ok=None, fail=("watch.set_follow id=%s", watch_id))
