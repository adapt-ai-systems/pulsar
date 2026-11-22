"""Grids (workspaces): row type, errors and the Store methods that read and
write them.
"""

from __future__ import annotations

import sqlite3

from dataclasses import dataclass
from typing import Any, Optional

from pulsar.store.common import StoreError, _UNSET, _new_id, _now_iso, logger


class UnknownGrid(StoreError):
    """Referenced a grid id that does not exist."""


class GridNotEmpty(StoreError):
    """Attempted to delete a grid that still holds pulses."""


@dataclass(frozen=True)
class Grid:
    id: str
    name: str
    position: int
    parent_id: Optional[str] = None  # sub-workspace: the workspace it sits in


def _grid_from_row(row: sqlite3.Row) -> Grid:
    return Grid(id=row["id"], name=row["name"], position=row["position"],
                parent_id=row["parent_id"])


class GridStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def _check_parent(self, parent_id: Optional[str], child_id: Optional[str] = None) -> None:
        """A sub-workspace's parent must exist and be top level (one layer
        only), and a workspace that has children can't itself be nested.
        Raises :class:`UnknownGrid` / :class:`ValueError`."""
        if parent_id is None:
            return
        if child_id is not None and parent_id == child_id:
            raise ValueError("a workspace can't be inside itself")
        parent = self.get_grid(parent_id)
        if parent.parent_id is not None:
            raise ValueError("sub-workspaces can't hold sub-workspaces (one layer only)")
        if child_id is not None and self._child_count(child_id):
            raise ValueError("a workspace with sub-workspaces can't be nested")

    def _child_count(self, grid_id: str) -> int:
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM grids WHERE parent_id = ?;", (grid_id,)
        ).fetchone()
        return int(row["n"])

    def create_grid(
        self,
        name: str,
        position: Optional[int] = None,
        parent_id: Optional[str] = None,
    ) -> Grid:
        """Create a grid. If ``position`` is None, append after the last one.
        ``parent_id`` makes it a sub-workspace of that (top-level) grid."""
        self._check_parent(parent_id)
        gid = _new_id()
        try:
            if position is None:
                row = self._conn.execute(
                    "SELECT COALESCE(MAX(position), -1) + 1 AS pos FROM grids;"
                ).fetchone()
                position = row["pos"]
            self._conn.execute(
                "INSERT INTO grids (id, name, position, parent_id) VALUES (?, ?, ?, ?);",
                (gid, name, position, parent_id),
            )
        except sqlite3.Error as exc:
            logger.error("grid.create name=%r FAILED err=%s", name, exc)
            raise
        logger.info("grid.create id=%s name=%r position=%d parent=%s ok",
                    gid, name, position, parent_id)
        return Grid(id=gid, name=name, position=position, parent_id=parent_id)

    def get_grid(self, grid_id: str) -> Grid:
        row = self._conn.execute(
            "SELECT * FROM grids WHERE id = ?;", (grid_id,)
        ).fetchone()
        if row is None:
            logger.error("grid.get id=%s NOT-FOUND", grid_id)
            raise UnknownGrid(grid_id)
        return _grid_from_row(row)

    def list_grids(self) -> list[Grid]:
        """Every grid, flat, in TAB order: each top-level grid by position,
        immediately followed by its sub-workspaces by position. A child whose
        parent is gone is treated as top level rather than dropped."""
        rows = self._conn.execute(
            "SELECT * FROM grids ORDER BY position, name;"
        ).fetchall()
        grids = [_grid_from_row(r) for r in rows]
        ids = {g.id for g in grids}
        tops = [g for g in grids if g.parent_id is None or g.parent_id not in ids]
        out: list[Grid] = []
        for t in tops:
            out.append(t)
            out.extend(g for g in grids if g.parent_id == t.id)
        return out

    def update_grid(
        self,
        grid_id: str,
        name: Optional[str] = None,
        position: Optional[int] = None,
        parent_id: Any = _UNSET,
    ) -> Grid:
        """Rename, reorder and/or re-parent a grid.

        Only the fields passed are changed (``parent_id=None`` explicitly
        lifts a sub-workspace back to top level); an all-unset call is a
        no-op that still returns the current row. Raises :class:`UnknownGrid`
        if the grid (or the new parent) does not exist and :class:`ValueError`
        when the nesting would go deeper than one layer.
        """
        current = self.get_grid(grid_id)
        new_name = current.name if name is None else name
        new_position = current.position if position is None else position
        new_parent = current.parent_id if parent_id is _UNSET else parent_id
        if name is None and position is None and parent_id is _UNSET:
            logger.info("grid.update id=%s no-op (nothing to change)", grid_id)
            return current
        if new_parent != current.parent_id:
            self._check_parent(new_parent, child_id=grid_id)
        try:
            self._conn.execute(
                "UPDATE grids SET name = ?, position = ?, parent_id = ? WHERE id = ?;",
                (new_name, new_position, new_parent, grid_id),
            )
        except sqlite3.Error as exc:
            logger.error("grid.update id=%s FAILED err=%s", grid_id, exc)
            raise
        logger.info("grid.update id=%s name=%r position=%d parent=%s ok",
                    grid_id, new_name, new_position, new_parent)
        return Grid(id=grid_id, name=new_name, position=new_position,
                    parent_id=new_parent)

    def reorder_grids(self, ids: list[str]) -> None:
        """Persist a grid drag-reorder: ``ids`` is the new left-to-right order.

        Mirrors :meth:`reorder_pulses` — the listed grids redistribute over
        their OWN position slots, so a subset leaves every other grid where it
        was. Raises :class:`UnknownGrid` on any unknown id and
        :class:`ValueError` on empty/duplicate input.
        """
        if not ids:
            logger.error("grid.reorder EMPTY ids")
            raise ValueError("ids must be a non-empty list")
        if len(set(ids)) != len(ids):
            logger.error("grid.reorder DUPLICATE ids=%r", ids)
            raise ValueError("ids contains duplicates")
        placeholders = ",".join("?" for _ in ids)
        rows = self._conn.execute(
            f"SELECT id, position FROM grids WHERE id IN ({placeholders});", ids
        ).fetchall()
        if len(rows) != len(ids):
            found = {r["id"] for r in rows}
            missing = [i for i in ids if i not in found]
            logger.error("grid.reorder UNKNOWN ids=%r", missing)
            raise UnknownGrid(missing[0])
        slots = sorted(r["position"] for r in rows)
        try:
            with self._conn.transaction() as conn:
                for gid, pos in zip(ids, slots):
                    conn.execute(
                        "UPDATE grids SET position = ? WHERE id = ?;", (pos, gid)
                    )
        except sqlite3.Error as exc:
            logger.error("grid.reorder FAILED rolled-back err=%s", exc)
            raise
        logger.info("grid.reorder count=%d ok", len(ids))

    def delete_grid(self, grid_id: str, detach: bool = False) -> int:
        """Delete a grid and return how many pulses it held.

        Raises :class:`UnknownGrid` if absent. With ``detach=False`` a grid that
        still holds any pulse (of any state, including archived) raises
        :class:`GridNotEmpty`. With ``detach=True`` those pulses are moved out of
        the grid — grid_id NULL, they keep everything else — and the grid goes:
        deleting a workspace never deletes its pulses (even archived ones).
        """
        self.get_grid(grid_id)
        row = self._conn.execute(
            "SELECT COUNT(*) AS n FROM pulses WHERE grid_id = ?;", (grid_id,)
        ).fetchone()
        held = int(row["n"])
        children = self._child_count(grid_id)
        if held and not detach:
            logger.error("grid.delete id=%s NOT-EMPTY count=%d", grid_id, held)
            raise GridNotEmpty(f"{grid_id} holds {held} pulse(s)")
        if children and not detach:
            logger.error("grid.delete id=%s HAS-CHILDREN count=%d", grid_id, children)
            raise GridNotEmpty(f"{grid_id} holds {children} sub-workspace(s)")
        # Detach + delete is one transaction (the connection is autocommit):
        # pulses left in a grid that no longer exists would be invisible in every
        # tab, including ALL.
        try:
            with self._conn.transaction() as conn:
                if held:
                    conn.execute(
                        "UPDATE pulses SET grid_id = NULL, updated_at = ? "
                        "WHERE grid_id = ?;",
                        (_now_iso(), grid_id),
                    )
                if children:
                    # Sub-workspaces come out to top level; nothing is deleted.
                    conn.execute(
                        "UPDATE grids SET parent_id = NULL WHERE parent_id = ?;",
                        (grid_id,),
                    )
                conn.execute("DELETE FROM grids WHERE id = ?;", (grid_id,))
        except sqlite3.Error as exc:
            logger.error("grid.delete id=%s FAILED err=%s", grid_id, exc)
            raise
        logger.info("grid.delete id=%s detached=%d ok", grid_id, held)
        return held
