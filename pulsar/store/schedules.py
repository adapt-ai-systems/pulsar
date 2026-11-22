"""Schedules: row type, errors and the Store methods that read and write them.
"""

from __future__ import annotations

import sqlite3

from dataclasses import dataclass
from typing import Any, Literal, Optional

from pulsar.store.common import StoreError, _new_id, _now_iso, logger


# Schedule kind: loop=every N min, once=one-shot, daily=recurring.
ScheduleKind = Literal["loop", "once", "daily"]


_VALID_KINDS: frozenset[str] = frozenset({"loop", "once", "daily"})


class UnknownSchedule(StoreError):
    """Referenced a schedule id that does not exist."""


@dataclass(frozen=True)
class Schedule:
    id: str
    pulse_id: str
    kind: ScheduleKind
    prompt: str
    interval_minutes: Optional[int]
    at_iso: Optional[str]
    cron_hhmm: Optional[str]
    days: Optional[str]
    enabled: bool
    last_fired_at: Optional[str]
    created_at: str
    target_pulse_id: Optional[str] = None
    triggers: Optional[str] = None  # csv timer|handoff|message|replies:N|tools:N; None = timer
    trigger_state: Optional[str] = None  # JSON counter baselines for replies/tools
    trigger_mode: Optional[str] = None  # any|all; None = any


def _schedule_from_row(row: sqlite3.Row) -> Schedule:
    return Schedule(
        id=row["id"],
        pulse_id=row["pulse_id"],
        kind=row["kind"],
        prompt=row["prompt"],
        interval_minutes=row["interval_minutes"],
        at_iso=row["at_iso"],
        cron_hhmm=row["cron_hhmm"],
        days=row["days"],
        enabled=bool(row["enabled"]),
        last_fired_at=row["last_fired_at"],
        created_at=row["created_at"],
        target_pulse_id=row["target_pulse_id"],
        triggers=row["triggers"],
        trigger_state=row["trigger_state"],
        trigger_mode=row["trigger_mode"],
    )


class ScheduleStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def create_schedule(
        self,
        pulse_id: str,
        kind: ScheduleKind,
        prompt: str,
        interval_minutes: Optional[int] = None,
        at_iso: Optional[str] = None,
        cron_hhmm: Optional[str] = None,
        days: Optional[str] = None,
        enabled: bool = True,
        target_pulse_id: Optional[str] = None,
        triggers: Optional[str] = None,
        trigger_mode: Optional[str] = None,
    ) -> Schedule:
        """Create a schedule for a pulse.

        Validates only the essentials the store owns: the pulse exists and the
        kind is known. Per-kind field validation (interval for loop, at_iso for
        once, cron_hhmm for daily) is the daemon route's job — the store stays
        boring and stores what it's given. Raises :class:`UnknownPulse` if the
        pulse is absent and :class:`ValueError` on a bad kind.
        """
        if kind not in _VALID_KINDS:
            logger.error("schedule.create pulse=%s bad-kind=%r", pulse_id, kind)
            raise ValueError(f"invalid schedule kind: {kind!r}")
        # Fail loud rather than lean on the FK to surface a cryptic error.
        self.get_pulse(pulse_id)
        if target_pulse_id is not None:
            self.get_pulse(target_pulse_id)

        sid = _new_id()
        now = _now_iso()
        try:
            self._conn.execute(
                "INSERT INTO schedules (id, pulse_id, kind, prompt, "
                "interval_minutes, at_iso, cron_hhmm, days, enabled, "
                "last_fired_at, created_at, target_pulse_id, triggers, "
                "trigger_mode) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL, ?, ?, ?, ?);",
                (sid, pulse_id, kind, prompt, interval_minutes, at_iso,
                 cron_hhmm, days, 1 if enabled else 0, now, target_pulse_id,
                 triggers, trigger_mode),
            )
        except sqlite3.Error as exc:
            logger.error("schedule.create pulse=%s kind=%s FAILED err=%s",
                         pulse_id, kind, exc)
            raise
        logger.info(
            "schedule.create id=%s pulse=%s kind=%s enabled=%s ok",
            sid, pulse_id, kind, enabled,
        )
        return self.get_schedule(sid)

    def get_schedule(self, schedule_id: str) -> Schedule:
        row = self._conn.execute(
            "SELECT * FROM schedules WHERE id = ?;", (schedule_id,)
        ).fetchone()
        if row is None:
            logger.error("schedule.get id=%s NOT-FOUND", schedule_id)
            raise UnknownSchedule(schedule_id)
        return _schedule_from_row(row)

    def list_schedules(
        self,
        pulse_id: Optional[str] = None,
        enabled_only: bool = False,
    ) -> list[Schedule]:
        """List schedules, oldest first (created order — deterministic).

        ``pulse_id`` filters to one pulse; ``enabled_only`` drops disabled ones
        (the scheduler's per-tick source).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if pulse_id is not None:
            clauses.append("pulse_id = ?")
            params.append(pulse_id)
        if enabled_only:
            clauses.append("enabled = 1")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM schedules{where} ORDER BY created_at, rowid;",
            params,
        ).fetchall()
        return [_schedule_from_row(r) for r in rows]

    def set_schedule_enabled(self, schedule_id: str, enabled: bool) -> Schedule:
        """Enable/disable a schedule (start/stop the loop separately)."""
        return self._update_one(
            "schedules", "id", schedule_id, {"enabled": 1 if enabled else 0},
            getter=self.get_schedule, from_row=_schedule_from_row,
            ok=("schedule.set_enabled id=%s enabled=%s ok", schedule_id, enabled),
            fail=("schedule.set_enabled id=%s", schedule_id))

    def update_schedule(
        self,
        schedule_id: str,
        prompt: Optional[str] = None,
        interval_minutes: Optional[int] = None,
        triggers: Optional[str] = None,
        trigger_mode: Optional[str] = None,
    ) -> Schedule:
        """Edit a schedule's prompt, interval_minutes, triggers and/or
        trigger_mode (loop editing).

        Only the fields provided are touched; per-kind applicability (loops
        only) is the daemon route's job. Raises :class:`UnknownSchedule` if
        the schedule is absent.
        """
        self.get_schedule(schedule_id)
        if (prompt is None and interval_minutes is None and triggers is None
                and trigger_mode is None):
            return self.get_schedule(schedule_id)
        try:
            self._conn.execute(
                "UPDATE schedules SET prompt = COALESCE(?, prompt), "
                "interval_minutes = COALESCE(?, interval_minutes), "
                "triggers = COALESCE(?, triggers), "
                "trigger_mode = COALESCE(?, trigger_mode) "
                "WHERE id = ?;",
                (prompt, interval_minutes, triggers, trigger_mode, schedule_id),
            )
        except sqlite3.Error as exc:
            logger.error("schedule.update id=%s FAILED err=%s",
                         schedule_id, exc)
            raise
        logger.info("schedule.update id=%s prompt=%s interval=%s ok",
                    schedule_id, prompt is not None, interval_minutes)
        return self.get_schedule(schedule_id)

    def set_trigger_state(self, schedule_id: str, state_json: str) -> Schedule:
        """Store the heartbeat counter baselines for replies/tools triggers (v25)."""
        return self._update_one(
            "schedules", "id", schedule_id, {"trigger_state": state_json},
            getter=self.get_schedule, from_row=_schedule_from_row,
            ok=("schedule.trigger_state id=%s ok", schedule_id),
            fail=("schedule.trigger_state id=%s", schedule_id))

    def mark_fired(self, schedule_id: str, ts: str) -> Schedule:
        """Record a fire attempt by stamping ``last_fired_at``.

        Called on every fire ATTEMPT (success or failure) so a failed fire does
        not retry until the next due window — no tight retry loops. ``ts`` is an
        ISO-8601 UTC string (the scheduler passes its frozen tick ``now``).
        """
        return self._update_one(
            "schedules", "id", schedule_id, {"last_fired_at": ts},
            getter=self.get_schedule, from_row=_schedule_from_row,
            ok=("schedule.mark_fired id=%s ts=%s ok", schedule_id, ts),
            fail=("schedule.mark_fired id=%s", schedule_id))

    def delete_schedule(self, schedule_id: str) -> None:
        """Hard-delete a schedule."""
        self.get_schedule(schedule_id)
        try:
            self._conn.execute(
                "DELETE FROM schedules WHERE id = ?;", (schedule_id,))
        except sqlite3.Error as exc:
            logger.error("schedule.delete id=%s FAILED err=%s", schedule_id, exc)
            raise
        logger.info("schedule.delete id=%s ok", schedule_id)
