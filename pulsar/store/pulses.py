"""Pulses: row type, lifecycle states, errors and the Store methods that read
and write them.
Cross-table writes (set_state, delete_pulse) live here.
"""

from __future__ import annotations

import json
import sqlite3

from dataclasses import dataclass
from typing import Any, Literal, Optional

from pulsar.store.common import StoreError, _new_id, _now_iso, logger


# state ∈ running|stopped|archived (store never says "thinking" — that's live).
PulseState = Literal["running", "stopped", "archived"]


_VALID_STATES: frozenset[str] = frozenset({"running", "stopped", "archived"})


# Allowed state transitions. running<->stopped freely; either can be archived;
# archived restores to stopped (a restored pulse is not live until respawned).
_TRANSITIONS: dict[str, frozenset[str]] = {
    "running": frozenset({"running", "stopped", "archived"}),
    "stopped": frozenset({"stopped", "running", "archived"}),
    "archived": frozenset({"archived", "stopped"}),
}


class UnknownPulse(StoreError):
    """Referenced a pulse id that does not exist."""


class InvalidStateTransition(StoreError):
    """Attempted a state change the lifecycle does not allow."""


@dataclass(frozen=True)
class Pulse:
    id: str
    title: str
    model: str
    vessel: str
    grid_id: Optional[str]
    state: PulseState
    handoff_tokens: Optional[int]
    created_at: str
    updated_at: str
    archived_at: Optional[str]
    settings: dict[str, Any]
    position: int
    last_input_at: Optional[str] = None  # user's last message to it (v17)


_BAD_SETTINGS_LOGGED: set[str] = set()


def _pulse_settings(row: sqlite3.Row) -> dict:
    """The row's settings dict; a corrupt settings_json must not break
    list_pulses for every other pulse, so log it once and use {}."""
    try:
        return json.loads(row["settings_json"])
    except (TypeError, ValueError) as exc:
        if row["id"] not in _BAD_SETTINGS_LOGGED:
            _BAD_SETTINGS_LOGGED.add(row["id"])
            logger.error("pulse.settings pulse=%s bad settings_json err=%s "
                         "— using {}", row["id"], exc)
        return {}


def _pulse_from_row(row: sqlite3.Row) -> Pulse:
    return Pulse(
        id=row["id"],
        title=row["title"],
        model=row["model"],
        vessel=row["vessel"],
        grid_id=row["grid_id"],
        state=row["state"],
        handoff_tokens=row["handoff_tokens"],
        created_at=row["created_at"],
        updated_at=row["updated_at"],
        archived_at=row["archived_at"],
        settings=_pulse_settings(row),
        position=row["position"],
        last_input_at=row["last_input_at"],
    )


class PulseStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def create_pulse(
        self,
        title: str,
        model: str,
        vessel: str,
        grid_id: Optional[str] = None,
        state: PulseState = "running",
        handoff_tokens: Optional[int] = None,
        settings: Optional[dict[str, Any]] = None,
    ) -> Pulse:
        """Insert a new pulse and return it.

        ``state`` defaults to ``running`` (a freshly created pulse is started —
        one-tap flow: created, started, placed in the grid). Raises
        :class:`UnknownGrid` if ``grid_id`` is given but absent, and
        :class:`ValueError` on a bad state.
        """
        if state not in _VALID_STATES:
            logger.error("pulse.create title=%r bad-state=%r", title, state)
            raise ValueError(f"invalid state: {state!r}")
        if grid_id is not None:
            # Fail loud rather than lean on the FK to surface a cryptic error.
            self.get_grid(grid_id)

        pid = _new_id()
        now = _now_iso()
        archived_at = now if state == "archived" else None
        settings = settings or {}
        try:
            settings_json = json.dumps(settings)
        except (TypeError, ValueError) as exc:
            logger.error("pulse.create title=%r bad-settings err=%s", title, exc)
            raise
        try:
            # Append after the last pulse (wall order): a new pane lands at the
            # far right, never the front.
            pos_row = self._conn.execute(
                "SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM pulses;"
            ).fetchone()
            position = pos_row["pos"]
            self._conn.execute(
                "INSERT INTO pulses (id, title, model, vessel, grid_id, state, "
                "handoff_tokens, created_at, updated_at, archived_at, "
                "settings_json, position) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?);",
                (pid, title, model, vessel, grid_id, state, handoff_tokens,
                 now, now, archived_at, settings_json, position),
            )
        except sqlite3.Error as exc:
            logger.error("pulse.create title=%r model=%s FAILED err=%s",
                         title, model, exc)
            raise
        logger.info(
            "pulse.create id=%s title=%r model=%s vessel=%s grid=%s state=%s ok",
            pid, title, model, vessel, grid_id, state,
        )
        return self.get_pulse(pid)

    def get_pulse(self, pulse_id: str) -> Pulse:
        row = self._conn.execute(
            "SELECT * FROM pulses WHERE id = ?;", (pulse_id,)
        ).fetchone()
        if row is None:
            logger.error("pulse.get id=%s NOT-FOUND", pulse_id)
            raise UnknownPulse(pulse_id)
        return _pulse_from_row(row)

    def list_pulses(
        self,
        grid_id: Optional[str] = None,
        state: Optional[PulseState] = None,
        include_archived: bool = False,
    ) -> list[Pulse]:
        """List pulses in wall order (position ASC; drag-reorder persists it).

        ``grid_id`` / ``state`` filter when given. Archived pulses are hidden
        by default (drawer filter); pass ``include_archived=True`` or
        an explicit ``state="archived"`` to see them.
        """
        clauses: list[str] = []
        params: list[Any] = []
        if grid_id is not None:
            clauses.append("grid_id = ?")
            params.append(grid_id)
        if state is not None:
            if state not in _VALID_STATES:
                logger.error("pulse.list bad-state=%r", state)
                raise ValueError(f"invalid state: {state!r}")
            clauses.append("state = ?")
            params.append(state)
        elif not include_archived:
            clauses.append("state != 'archived'")
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            # rowid is the tiebreaker so equal positions (shouldn't happen, but
            # nothing enforces uniqueness) still order deterministically.
            f"SELECT * FROM pulses{where} ORDER BY position ASC, rowid ASC;",
            params,
        ).fetchall()
        return [_pulse_from_row(r) for r in rows]

    def reorder_pulses(self, ids: list[str]) -> None:
        """Persist a drag-reorder: ``ids`` is the new left-to-right order.

        ``ids`` may be a SUBSET of all pulses (the wall shows one grid tab) —
        the listed pulses swap among their OWN position slots, so pulses in
        other grids keep their global placement. Raises :class:`UnknownPulse`
        on any unknown id and :class:`ValueError` on empty/duplicate input.
        """
        if not ids:
            logger.error("pulse.reorder EMPTY ids")
            raise ValueError("ids must be a non-empty list")
        if len(set(ids)) != len(ids):
            logger.error("pulse.reorder DUPLICATE ids=%r", ids)
            raise ValueError("ids contains duplicates")
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT id, position FROM pulses WHERE id IN ({placeholders});",
            ids,
        ).fetchall()
        if len(rows) != len(ids):
            found = {r["id"] for r in rows}
            missing = [i for i in ids if i not in found]
            logger.error("pulse.reorder UNKNOWN ids=%r", missing)
            raise UnknownPulse(missing[0])
        # The moved pulses redistribute over their own (sorted) slots — first
        # id gets the smallest slot, and so on.
        slots = sorted(r["position"] for r in rows)
        now = _now_iso()
        try:
            with self._conn.transaction() as conn:
                for pid, pos in zip(ids, slots):
                    conn.execute(
                        "UPDATE pulses SET position = ?, updated_at = ? WHERE id = ?;",
                        (pos, now, pid),
                    )
        except sqlite3.Error as exc:
            logger.error("pulse.reorder FAILED rolled-back err=%s", exc)
            raise
        logger.info("pulse.reorder count=%d ok", len(ids))

    def set_state(self, pulse_id: str, state: PulseState) -> Pulse:
        """Transition a pulse's lifecycle state.

        Enforces the allowed-transition table; sets/clears ``archived_at``.
        Raises :class:`InvalidStateTransition` on a disallowed move.
        """
        if state not in _VALID_STATES:
            logger.error("pulse.set_state id=%s bad-state=%r", pulse_id, state)
            raise ValueError(f"invalid state: {state!r}")
        current = self.get_pulse(pulse_id)
        if state not in _TRANSITIONS[current.state]:
            logger.error(
                "pulse.set_state id=%s DISALLOWED %s->%s",
                pulse_id, current.state, state,
            )
            raise InvalidStateTransition(f"{current.state} -> {state}")

        now = _now_iso()
        archived_at = now if state == "archived" else None
        try:
            with self._conn.transaction() as conn:
                conn.execute(
                    "UPDATE pulses SET state = ?, archived_at = ?, updated_at = ? "
                    "WHERE id = ?;",
                    (state, archived_at, now, pulse_id),
                )
                if state == "archived":
                    # Archive ends delegation in both directions, including
                    # reverse watches created by helpers reporting back.
                    # Close silently: a closure message would wake old helpers.
                    conn.execute(
                        "UPDATE delegation_watches SET state = 'closed', "
                        "outcome = CASE WHEN spawner_id = ? THEN 'owner_archived' "
                        "ELSE 'target_gone' END, closed_at = ? "
                        "WHERE state = 'watching' "
                        "AND (spawner_id = ? OR target_id = ?);",
                        (pulse_id, now, pulse_id, pulse_id),
                    )
        except sqlite3.Error as exc:
            logger.error("pulse.set_state id=%s %s->%s FAILED err=%s",
                         pulse_id, current.state, state, exc)
            raise
        logger.info("pulse.set_state id=%s %s->%s ok", pulse_id,
                    current.state, state)
        return self.get_pulse(pulse_id)

    def archive_pulse(self, pulse_id: str) -> Pulse:
        """Convenience: transition a pulse to ``archived``."""
        return self.set_state(pulse_id, "archived")

    def unarchive_pulse(self, pulse_id: str) -> Pulse:
        """Convenience: restore an archived pulse to ``stopped``.

        A restored pulse is not live until respawned, so the store lands it in
        ``stopped`` (per the transition table archived->stopped). Raises
        :class:`InvalidStateTransition` if the pulse is not currently archived.
        """
        return self.set_state(pulse_id, "stopped")

    def update_settings(
        self, pulse_id: str, settings: dict[str, Any], merge: bool = True
    ) -> Pulse:
        """Update a pulse's ``settings`` blob.

        ``merge=True`` (default) shallow-merges into existing settings;
        ``merge=False`` replaces wholesale.
        """
        current = self.get_pulse(pulse_id)
        new_settings = dict(current.settings) if merge else {}
        new_settings.update(settings)
        try:
            settings_json = json.dumps(new_settings)
        except (TypeError, ValueError) as exc:
            logger.error("pulse.update_settings id=%s bad-settings err=%s",
                         pulse_id, exc)
            raise
        now = _now_iso()
        try:
            self._conn.execute(
                "UPDATE pulses SET settings_json = ?, updated_at = ? WHERE id = ?;",
                (settings_json, now, pulse_id),
            )
        except sqlite3.Error as exc:
            logger.error("pulse.update_settings id=%s FAILED err=%s", pulse_id, exc)
            raise
        logger.info("pulse.update_settings id=%s keys=%s merge=%s ok",
                    pulse_id, sorted(settings.keys()), merge)
        return self.get_pulse(pulse_id)

    def set_grid(self, pulse_id: str, grid_id: Optional[str]) -> Pulse:
        """Move a pulse to another grid (one grid at a time)."""
        # Both pre-checks stay, in this order: pulse missing wins over grid missing.
        self.get_pulse(pulse_id)
        if grid_id is not None:
            self.get_grid(grid_id)
        return self._update_one(
            "pulses", "id", pulse_id,
            {"grid_id": grid_id, "updated_at": _now_iso()},
            getter=self.get_pulse, from_row=_pulse_from_row,
            ok=("pulse.set_grid id=%s grid=%s ok", pulse_id, grid_id),
            fail=("pulse.set_grid id=%s grid=%s", pulse_id, grid_id))

    def set_title(self, pulse_id: str, title: str) -> Pulse:
        """Rename a pulse. Lifecycle state is untouched; a live runner keeps
        the old title in its spawn-time context until its next respawn."""
        title = title.strip()
        if not title:
            logger.error("pulse.set_title id=%s empty title", pulse_id)
            raise ValueError("title must be non-empty")
        return self._update_one(
            "pulses", "id", pulse_id,
            {"title": title, "updated_at": _now_iso()},
            getter=self.get_pulse, from_row=_pulse_from_row,
            ok=("pulse.set_title id=%s title=%r ok", pulse_id, title),
            fail=("pulse.set_title id=%s", pulse_id))

    def touch_input(self, pulse_id: str, when: Optional[str] = None) -> None:
        """The user just sent this pulse a message directly (v17). Only direct
        user inputs count — pulse-to-pulse mail, schedules and watches never call
        this. Does not bump updated_at (that is store recency, a different
        sort)."""
        try:
            self._conn.execute(
                "UPDATE pulses SET last_input_at = ? WHERE id = ?;",
                (when or _now_iso(), pulse_id),
            )
        except sqlite3.Error as exc:
            logger.error("pulse.touch_input id=%s FAILED err=%s", pulse_id, exc)
            raise

    def set_model(self, pulse_id: str, model: str,
                  vessel: Optional[str] = None) -> Pulse:
        """Change a pulse's model (and optionally vessel) — mid-session
        model switch. Lifecycle state is untouched; the daemon owns respawning."""
        if not model:
            logger.error("pulse.set_model id=%s empty model", pulse_id)
            raise ValueError("model must be non-empty")
        # Pre-read stays: new_vessel defaults to the old one and the log line
        # prints the old model/vessel, which RETURNING cannot give.
        current = self.get_pulse(pulse_id)
        new_vessel = vessel or current.vessel
        return self._update_one(
            "pulses", "id", pulse_id,
            {"model": model, "vessel": new_vessel, "updated_at": _now_iso()},
            getter=self.get_pulse, from_row=_pulse_from_row,
            ok=("pulse.set_model id=%s %s/%s -> %s/%s ok", pulse_id,
                current.model, current.vessel, model, new_vessel),
            fail=("pulse.set_model id=%s", pulse_id))

    def set_handoff_tokens(self, pulse_id: str,
                           handoff_tokens: Optional[int]) -> Pulse:
        """Set the auto-handoff threshold for a pulse."""
        return self._update_one(
            "pulses", "id", pulse_id,
            {"handoff_tokens": handoff_tokens, "updated_at": _now_iso()},
            getter=self.get_pulse, from_row=_pulse_from_row,
            ok=("pulse.set_handoff_tokens id=%s tokens=%s ok",
                pulse_id, handoff_tokens),
            fail=("pulse.set_handoff_tokens id=%s", pulse_id))

    def has_pulse_watchers(self, pulse_id: str) -> bool:
        """Whether the pulse owns an active condition or delegation watcher."""
        return self._conn.execute(
            "SELECT 1 FROM condition_watches WHERE pulse_id = ? AND state = 'watching' "
            "UNION ALL SELECT 1 FROM delegation_watches "
            "WHERE spawner_id = ? AND state = 'watching' LIMIT 1;",
            (pulse_id, pulse_id),
        ).fetchone() is not None

    def delete_pulse(self, pulse_id: str) -> None:
        """Hard-delete a pulse, its schedules, and its delegation watches
        (deleted — gone). Active owned watchers block deletion.
        Inbox messages deliberately survive: sender identity and message content
        are snapshots, not dependents of the pulse row or home directory.

        Dependents reference the pulse (FK), so drop them first inside one
        txn — a deleted pulse must not leave orphan rows the scheduler would
        try to fire or check against a nonexistent pulse.
        """
        self.get_pulse(pulse_id)
        try:
            with self._conn.transaction() as conn:
                if self.has_pulse_watchers(pulse_id):
                    raise InvalidStateTransition(
                        "pulse has active watchers; remove its watchers before deleting it")
                n = conn.execute(
                    "DELETE FROM schedules WHERE pulse_id = ?;", (pulse_id,)
                ).rowcount
                w = conn.execute(
                    "DELETE FROM delegation_watches "
                    "WHERE spawner_id = ? OR target_id = ?;", (pulse_id, pulse_id)
                ).rowcount
                cw = conn.execute(
                    "DELETE FROM condition_watches WHERE pulse_id = ?;",
                    (pulse_id,)).rowcount
                conn.execute("DELETE FROM questions WHERE pulse_id = ?;",
                             (pulse_id,))
                conn.execute("DELETE FROM pulses WHERE id = ?;", (pulse_id,))
        except sqlite3.Error as exc:
            logger.error("pulse.delete id=%s FAILED err=%s", pulse_id, exc)
            raise
        logger.info("pulse.delete id=%s schedules_removed=%d watches_removed=%d "
                    "condwatches_removed=%d ok", pulse_id, n, w, cw)
