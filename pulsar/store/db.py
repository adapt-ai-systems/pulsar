"""Pulsar SQLite store — the single source of truth for pulses and grids.

Stdlib ``sqlite3`` only, no ORM. WAL mode. Numbered migrations applied at
open (see :mod:`pulsar.store.migrations`); reopening an existing db applies
none.

The row types, errors and Store methods live in one module per domain
(grids, pulses, schedules, watches, condwatches, inbox, settings,
questions); this module composes them into ``Store``.

Standing rule: no silent fail/wait/retry path. Every
write path and every failure path emits ONE timestamped log line with enough
context to reconstruct who/what/outcome. Timestamps come from stdlib
``logging`` (configured by the app); ISO-8601 UTC strings are stored in every
``*_at`` column.
"""

from __future__ import annotations

import sqlite3
import threading
from typing import Any, Callable, Optional

from pulsar.store.common import _Raw, logger
from pulsar.store.migrations import MIGRATIONS, LATEST_VERSION

# The row types, errors and helpers moved to one module per domain. They
# are re-exported here so ``pulsar.store.db`` keeps its public surface.
from pulsar.store.common import StoreError  # noqa: F401
from pulsar.store.grids import (  # noqa: F401
    Grid, GridNotEmpty, GridStore, UnknownGrid)
from pulsar.store.pulses import (  # noqa: F401
    InvalidStateTransition, Pulse, PulseState, PulseStore, UnknownPulse)
from pulsar.store.schedules import (  # noqa: F401
    Schedule, ScheduleKind, ScheduleStore, UnknownSchedule)
from pulsar.store.watches import (  # noqa: F401
    DelegationWatch, UnknownWatch, WatchStore)
from pulsar.store.condwatches import (  # noqa: F401
    COND_WATCH_KINDS, COND_WATCH_MAX_OPEN, CondWatchStore, ConditionWatch,
    UnknownConditionWatch)
from pulsar.store.inbox import (  # noqa: F401
    InboxChannel, InboxItem, InboxRule, InboxStore, UnknownChannel,
    UnknownInboxItem, inbox_kind_key)
from pulsar.store.settings import SettingsStore
from pulsar.store.questions import (  # noqa: F401
    QUESTION_CHOICES_MAX, QUESTION_MAX_OPEN, QUESTION_TEXT_MAX, Question,
    QuestionStore, UnknownQuestion)


# --------------------------------------------------------------------------- #
# Store
# --------------------------------------------------------------------------- #
class _FrozenCursor:
    __slots__ = ("_rows", "_index", "lastrowid", "rowcount")

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        self._rows = cursor.fetchall()
        self._index = 0
        self.lastrowid = cursor.lastrowid
        self.rowcount = cursor.rowcount

    def fetchone(self):
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def fetchall(self):
        rows = self._rows[self._index:]
        self._index = len(self._rows)
        return rows

    def __iter__(self):
        return iter(self.fetchall())


class _Txn:
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def __enter__(self) -> sqlite3.Connection:
        self._lock.acquire()
        try:
            self._conn.execute("BEGIN IMMEDIATE;")
        except BaseException:
            self._lock.release()
            raise
        return self._conn

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            try:
                self._conn.execute("ROLLBACK;" if exc_type else "COMMIT;")
            except BaseException:
                # A failed COMMIT can leave the transaction open. Make one
                # best-effort rollback, but preserve the original exception.
                if self._conn.in_transaction:
                    try:
                        self._conn.execute("ROLLBACK;")
                    except sqlite3.Error:
                        pass
                raise
        finally:
            self._lock.release()


class _LockedConn:
    """The only connection exposed after startup; every reachable operation locks."""
    def __init__(self, conn: sqlite3.Connection, lock: threading.RLock) -> None:
        self._conn = conn
        self._lock = lock

    def execute(self, sql, parameters=()):
        with self._lock:
            return _FrozenCursor(self._conn.execute(sql, parameters))

    def executemany(self, sql, seq):
        with self._lock:
            return _FrozenCursor(self._conn.executemany(sql, seq))

    def executescript(self, sql):
        with self._lock:
            self._conn.executescript(sql)

    def transaction(self):
        return _Txn(self._conn, self._lock)

    def close(self) -> None:
        with self._lock:
            self._conn.close()


class Store(GridStore, PulseStore, ScheduleStore, WatchStore, CondWatchStore,
            InboxStore, SettingsStore, QuestionStore):
    """Typed SQLite-backed store for pulses and grids.

    Open with a filesystem path (``":memory:"`` works for tests too). The
    connection is opened in WAL mode and migrated to the latest schema on
    construction. Not thread-safe across threads by default — one Store per
    thread, or serialize access.
    """

    def __init__(self, path: str) -> None:
        self._path = str(path)
        logger.info("store.open path=%s starting", self._path)
        self._lock = threading.RLock()
        try:
            self._conn = sqlite3.connect(
                self._path, isolation_level=None, check_same_thread=False
            )
        except sqlite3.Error as exc:
            logger.error("store.open path=%s FAILED err=%s", self._path, exc)
            raise
        self._conn.row_factory = sqlite3.Row
        self._configure()
        applied = self._migrate()
        self._conn = _LockedConn(self._conn, self._lock)
        logger.info(
            "store.open path=%s ready version=%d migrations_applied=%d",
            self._path,
            LATEST_VERSION,
            applied,
        )

    # -- lifecycle ---------------------------------------------------------- #
    def _configure(self) -> None:
        try:
            self._conn.execute("PRAGMA journal_mode=WAL;")
            self._conn.execute("PRAGMA foreign_keys=ON;")
            self._conn.execute("PRAGMA synchronous=NORMAL;")
        except sqlite3.Error as exc:
            logger.error("store.configure path=%s FAILED err=%s", self._path, exc)
            raise

    def _migrate(self) -> int:
        """Apply every migration newer than the recorded version. Idempotent.

        Returns the count of migrations applied this call (0 on reopen).
        """
        conn = self._conn
        try:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS schema_meta (version INTEGER NOT NULL);"
            )
            row = conn.execute("SELECT version FROM schema_meta LIMIT 1;").fetchone()
            current = row["version"] if row is not None else 0
        except sqlite3.Error as exc:
            logger.error("store.migrate path=%s read-version FAILED err=%s",
                         self._path, exc)
            raise

        applied = 0
        for version, sql in MIGRATIONS:
            if version <= current:
                continue
            logger.info("store.migrate path=%s applying version=%d", self._path,
                        version)
            # executescript() implicitly commits, so drive the migration and its
            # version bump as individual statements inside one explicit txn.
            statements = [s.strip() for s in sql.split(";") if s.strip()]
            first_version = current == 0 and applied == 0
            try:
                conn.execute("BEGIN;")
                for stmt in statements:
                    conn.execute(stmt)
                if first_version:
                    conn.execute("INSERT INTO schema_meta (version) VALUES (?);",
                                 (version,))
                else:
                    conn.execute("UPDATE schema_meta SET version = ?;", (version,))
                conn.execute("COMMIT;")
            except sqlite3.Error as exc:
                try:
                    conn.execute("ROLLBACK;")
                except sqlite3.Error:
                    logger.error("store.migrate path=%s version=%d rollback-noop",
                                 self._path, version)
                logger.error(
                    "store.migrate path=%s version=%d FAILED rolled-back err=%s",
                    self._path, version, exc,
                )
                raise
            current = version
            applied += 1
            logger.info("store.migrate path=%s applied version=%d ok",
                        self._path, version)

        if applied == 0:
            logger.info("store.migrate path=%s up-to-date version=%d",
                        self._path, current)
        return applied

    def close(self) -> None:
        logger.info("store.close path=%s", self._path)
        try:
            self._conn.close()
        except sqlite3.Error as exc:
            logger.error("store.close path=%s FAILED err=%s", self._path, exc)
            raise

    def __enter__(self) -> "Store":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _update_one(
        self, table: str, key_col: str, key: str, changes: dict[str, Any], *,
        getter: Callable[[str], Any], from_row: Callable[[sqlite3.Row], Any],
        ok: Optional[tuple], fail: tuple,
    ) -> Any:
        """``UPDATE table SET <changes> WHERE key_col = key RETURNING *`` for
        one row, with the standing one-log-line-per-write/failure rule.

        ``ok`` / ``fail`` are ``(format, *args)`` tuples: ``ok`` is the whole
        success line (``None`` = log nothing, as ``set_follow`` does today);
        ``fail`` is the line before `` FAILED err=%s``. A ``_Raw`` value in
        ``changes`` is inlined as SQL. No row matched: ``getter(key)`` logs the
        NOT-FOUND line and raises the table's ``Unknown*`` error.
        """
        sets = ", ".join(f"{col} = {val}" if isinstance(val, _Raw) else f"{col} = ?"
                         for col, val in changes.items())
        params = [val for val in changes.values() if not isinstance(val, _Raw)]
        try:
            row = self._conn.execute(
                f"UPDATE {table} SET {sets} WHERE {key_col} = ? RETURNING *;",
                params + [key],
            ).fetchone()
        except sqlite3.Error as exc:
            logger.error(fail[0] + " FAILED err=%s", *fail[1:], exc)
            raise
        if row is None:
            getter(key)
            raise RuntimeError(f"{table} row {key!r} vanished during update")
        if ok is not None:
            logger.info(ok[0], *ok[1:])
        return from_row(row)
