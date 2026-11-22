"""Inbox: items, learned rules, channels, their errors
and Store methods.
"""

from __future__ import annotations

import re
import sqlite3

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Optional

from pulsar.store.common import StoreError, _UNSET, _like, _new_id, _now_iso, logger


class UnknownInboxItem(StoreError):
    """Referenced an inbox item id that does not exist."""


class UnknownChannel(StoreError):
    """Referenced an inbox channel that does not exist."""


@dataclass(frozen=True)
class InboxItem:
    id: str
    created_at: str
    cursor_seq: int
    channel: str
    thread_key: Optional[str]
    source_kind: str          # pulse|watch|scheduler|loopguard|script|system
    source_pulse_id: Optional[str]
    source_label: str
    title: str
    body: str
    priority: str             # notify|quiet
    needs_response: bool
    starred: bool
    read_at: Optional[str]    # NULL = Inbox (unread); set = Archive
    replied_at: Optional[str]
    score: Optional[int] = None  # NULL|1|2|3 (1 hide, 2 ok, 3 notify)

    @property
    def cursor(self) -> str:
        """Stable API cursor ordered by ``(created_at, cursor_seq)``."""
        return f"{self.created_at}|{self.cursor_seq}"

    @property
    def kind_key(self) -> str:
        return inbox_kind_key(self.channel, self.source_label, self.title)


@dataclass(frozen=True)
class InboxRule:
    """A learned per-kind handling rule (v11). ``score`` ∈ 1|2|3."""
    kind_key: str
    score: int
    channel: str
    source_label: str
    example_title: str
    updated_at: str


_KIND_SPLIT_RE = re.compile(r"\s+[—–-]\s+|:")


_KIND_STRIP_RE = re.compile(r"[^a-z0-9#\s]+")


def inbox_kind_key(channel: str, source_label: str, title: str) -> str:
    """Derive the "kind of message" key a score teaches a rule for.

    ``<channel>|<source_label lowercased>|<title signature>`` where the
    signature is the title prefix before the first ``:`` or `` — `` (the part
    producers keep stable — "Delegation stall", "Build status",
    "Monitor alert"), lowercased, digit runs collapsed to ``#``, punctuation
    dropped, at most six tokens.  A prefix shorter than two tokens falls back
    to the first four tokens of the whole title so a bare "Re:" never becomes
    a catch-all kind.
    """
    def _sig(text: str, cap: int) -> list[str]:
        t = re.sub(r"\d+", "#", (text or "").lower())
        t = _KIND_STRIP_RE.sub(" ", t)
        return t.split()[:cap]

    prefix = _KIND_SPLIT_RE.split(title or "", maxsplit=1)[0]
    toks = _sig(prefix, 6)
    if len(toks) < 2:
        toks = _sig(title, 4)
    return f"{(channel or '').lower()}|{(source_label or '').strip().lower()}|" \
           f"{' '.join(toks)}"


@dataclass(frozen=True)
class InboxChannel:
    channel: str
    mode: str                 # notify|quiet|muted
    mute_until: Optional[str]
    created_at: str


def _inbox_now_iso() -> str:
    """High-resolution Inbox creation time for legacy timestamp cutoffs.

    Composite cursors provide the rigorous total order.  Microseconds also
    keep older clients that still send a plain ``created_at`` cutoff from
    collapsing two ordinary arrivals into the same second.
    """
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _inbox_filters(
    channel: Optional[str], thread_key: Optional[str], starred: bool,
    needs_response: bool, q: Optional[str],
) -> tuple[list[str], list[Any]]:
    """WHERE clauses + params shared by ``list_inbox_items`` and
    ``read_all_inbox`` (the same filters, in the same order)."""
    clauses: list[str] = []
    params: list[Any] = []
    if channel is not None:
        clauses.append("channel = ?")
        params.append(channel)
    if thread_key is not None:
        clauses.append("thread_key = ?")
        params.append(thread_key)
    if starred:
        clauses.append("starred = 1")
    if needs_response:
        clauses.append("needs_response = 1")
    if q:
        clauses.append("(title LIKE ? ESCAPE '\\' OR body LIKE ? "
                       "ESCAPE '\\' OR source_label LIKE ? ESCAPE "
                       "'\\')")
        like = _like(q)
        params += [like, like, like]
    return clauses, params


def _parse_inbox_cursor(cursor: str) -> tuple[str, Optional[int]]:
    """Parse a composite Inbox cursor, accepting legacy timestamps too."""
    created_at, separator, raw_seq = cursor.rpartition("|")
    if separator and created_at and raw_seq.isdigit():
        return created_at, int(raw_seq)
    return cursor, None


def _inbox_item_from_row(row: sqlite3.Row) -> InboxItem:
    return InboxItem(
        id=row["id"],
        created_at=row["created_at"],
        cursor_seq=row["cursor_seq"],
        channel=row["channel"],
        thread_key=row["thread_key"],
        source_kind=row["source_kind"],
        source_pulse_id=row["source_pulse_id"],
        source_label=row["source_label"],
        title=row["title"],
        body=row["body"],
        priority=row["priority"],
        needs_response=bool(row["needs_response"]),
        starred=bool(row["starred"]),
        read_at=row["read_at"],
        replied_at=row["replied_at"],
        score=row["score"],
    )


def _inbox_rule_from_row(row: sqlite3.Row) -> InboxRule:
    return InboxRule(
        kind_key=row["kind_key"], score=int(row["score"]),
        channel=row["channel"], source_label=row["source_label"],
        example_title=row["example_title"], updated_at=row["updated_at"])


def _inbox_channel_from_row(row: sqlite3.Row) -> InboxChannel:
    return InboxChannel(
        channel=row["channel"],
        mode=row["mode"],
        mute_until=row["mute_until"],
        created_at=row["created_at"],
    )


class InboxStore:
    """Store methods for this domain; mixed into ``pulsar.store.db.Store``."""

    def create_inbox_item(
        self,
        channel: str,
        source_kind: str,
        title: str,
        body: str,
        source_label: str = "",
        thread_key: Optional[str] = None,
        source_pulse_id: Optional[str] = None,
        priority: str = "notify",
        needs_response: bool = False,
        score: Optional[int] = None,
        read_at: Optional[str] = None,
    ) -> InboxItem:
        """Insert one inbox item and return it.

        ``score``/``read_at`` let ``produce`` stamp a learned rule's verdict
        at creation (score 1 → born archived, never badged).

        Auto-creates the channel row (default mode ``notify``) the first time
        a name is used — dynamic channels. Body/title are stored
        verbatim; validation/caps are the daemon route's job. ``priority``
        defaults to ``notify``. An item is visible only after full commit
        (atomicity convention).
        """
        if priority not in ("notify", "quiet"):
            logger.error("inbox.create channel=%s bad-priority=%r", channel,
                         priority)
            raise ValueError(f"invalid priority: {priority!r}")
        if not channel:
            logger.error("inbox.create channel empty")
            raise ValueError("channel must be non-empty")
        iid = _new_id()
        now = _inbox_now_iso()
        try:
            self._conn.execute("INSERT OR IGNORE INTO inbox_channels "
                               "(channel, mode, mute_until, created_at) "
                               "VALUES (?, 'notify', NULL, ?);",
                               (channel, now))
            self._conn.execute(
                "INSERT INTO inbox_items (id, created_at, cursor_seq, channel, "
                "thread_key, "
                "source_kind, source_pulse_id, source_label, title, body, "
                "priority, needs_response, starred, read_at, replied_at, "
                "push_status, score) VALUES (?, ?, "
                "(SELECT COALESCE(MAX(cursor_seq), 0) + 1 FROM inbox_items), "
                "?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, NULL, NULL, ?);",
                (iid, now, channel, thread_key, source_kind, source_pulse_id,
                 source_label, title, body, priority, 1 if needs_response else 0,
                 read_at, score),
            )
        except sqlite3.Error as exc:
            logger.error("inbox.create channel=%s kind=%s FAILED err=%s",
                         channel, source_kind, exc)
            raise
        logger.info("inbox.create id=%s channel=%s kind=%s priority=%s ok",
                    iid, channel, source_kind, priority)
        return self.get_inbox_item(iid)

    def get_inbox_item(self, item_id: str) -> InboxItem:
        row = self._conn.execute(
            "SELECT * FROM inbox_items WHERE id = ?;", (item_id,)
        ).fetchone()
        if row is None:
            logger.error("inbox.get id=%s NOT-FOUND", item_id)
            raise UnknownInboxItem(item_id)
        return _inbox_item_from_row(row)

    def list_inbox_items(
        self,
        unread_only: bool = False,
        channel: Optional[str] = None,
        thread_key: Optional[str] = None,
        starred: bool = False,
        needs_response: bool = False,
        q: Optional[str] = None,
        limit: int = 200,
        before: Optional[str] = None,
    ) -> list[InboxItem]:
        """List inbox items, newest first by a stable composite cursor.

        ``unread_only`` = read_at IS NULL. ``before`` accepts the composite
        ``<created_at>|<cursor_seq>`` cursor returned by the API.  A plain
        ``created_at`` remains accepted for older clients.
        ``q`` does a case-insensitive LIKE over title/body/source_label with
        backslash-escaped user input (LIKE ESCAPE).
        """
        clauses: list[str] = []
        params: list[Any] = []
        if unread_only:
            clauses.append("read_at IS NULL")
        filter_clauses, filter_params = _inbox_filters(
            channel, thread_key, starred, needs_response, q)
        clauses += filter_clauses
        params += filter_params
        if before is not None:
            before_at, before_seq = _parse_inbox_cursor(before)
            if before_seq is None:
                clauses.append("created_at < ?")
                params.append(before_at)
            else:
                clauses.append("(created_at < ? OR (created_at = ? AND "
                               "cursor_seq < ?))")
                params += [before_at, before_at, before_seq]
        # bounded-read convention: a hard cap regardless of what the caller
        # asks for (~200 cap; a request can never be an unbounded read).
        limit = max(1, min(int(limit), 200))
        where = (" WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM inbox_items{where} "
            "ORDER BY created_at DESC, cursor_seq DESC LIMIT ?;",
            params + [limit],
        ).fetchall()
        return [_inbox_item_from_row(r) for r in rows]

    def set_inbox_read(self, item_id: str, read: bool) -> InboxItem:
        """Mark an item read (⇒ Archive) or unread (null ⇒ back to Inbox)."""
        return self._update_one(
            "inbox_items", "id", item_id, {"read_at": _now_iso() if read else None},
            getter=self.get_inbox_item, from_row=_inbox_item_from_row,
            ok=("inbox.set_read id=%s read=%s ok", item_id, read),
            fail=("inbox.set_read id=%s read=%s", item_id, read))

    def set_inbox_starred(self, item_id: str, starred: bool) -> InboxItem:
        return self._update_one(
            "inbox_items", "id", item_id, {"starred": 1 if starred else 0},
            getter=self.get_inbox_item, from_row=_inbox_item_from_row,
            ok=("inbox.set_starred id=%s starred=%s ok", item_id, starred),
            fail=("inbox.set_starred id=%s starred=%s", item_id, starred))

    def clear_inbox_response(self, item_id: str) -> InboxItem:
        """Explicitly dismiss the needs_response flag without replying."""
        return self._update_one(
            "inbox_items", "id", item_id, {"needs_response": 0},
            getter=self.get_inbox_item, from_row=_inbox_item_from_row,
            ok=("inbox.clear_response id=%s ok", item_id),
            fail=("inbox.clear_response id=%s", item_id))

    def mark_inbox_replied(self, item_id: str, replied_at: str) -> InboxItem:
        """Stamp a successful reply: sets replied_at, clears needs_response."""
        return self._update_one(
            "inbox_items", "id", item_id, {"replied_at": replied_at, "needs_response": 0},
            getter=self.get_inbox_item, from_row=_inbox_item_from_row,
            ok=("inbox.replied id=%s ts=%s ok", item_id, replied_at),
            fail=("inbox.replied id=%s", item_id))

    def save_inbox_reply(self, item_id: str, body: str,
                         created_at: str) -> InboxItem:
        """Keep the sent text and successful-reply state in one transaction."""
        try:
            with self._conn.transaction() as conn:
                conn.execute(
                    "INSERT INTO inbox_replies (id, item_id, body, created_at) "
                    "VALUES (?, ?, ?, ?);", (_new_id(), item_id, body, created_at))
                self.mark_inbox_replied(item_id, created_at)
        except sqlite3.Error as exc:
            logger.error("inbox.save_reply item=%s FAILED err=%s", item_id, exc)
            raise
        logger.info("inbox.save_reply item=%s ok", item_id)
        return self.get_inbox_item(item_id)

    def inbox_conversation(self, item_id: str) -> tuple[list[InboxItem], list[dict]]:
        """All received messages and sent replies, independent of list filters."""
        item = self.get_inbox_item(item_id)
        if item.thread_key:
            clause, value = "i.thread_key = ?", item.thread_key
        else:
            clause, value = "i.id = ?", item.id
        members = self._conn.execute(
            f"SELECT i.* FROM inbox_items i WHERE {clause} "
            "ORDER BY i.created_at, i.cursor_seq;", (value,)).fetchall()
        replies = self._conn.execute(
            "SELECT r.id, r.item_id, r.body, r.created_at, i.thread_key "
            "FROM inbox_replies r JOIN inbox_items i ON i.id = r.item_id "
            f"WHERE {clause} ORDER BY r.created_at, r.seq;", (value,)).fetchall()
        return ([_inbox_item_from_row(row) for row in members],
                [dict(row) for row in replies])

    def set_inbox_thread(self, item_id: str, thread_key: str) -> InboxItem:
        """Backfill a thread_key on a previously thread-less item (reply path)."""
        return self._update_one(
            "inbox_items", "id", item_id, {"thread_key": thread_key},
            getter=self.get_inbox_item, from_row=_inbox_item_from_row,
            ok=("inbox.set_thread id=%s key=%s ok", item_id, thread_key),
            fail=("inbox.set_thread id=%s key=%s", item_id, thread_key))

    def read_all_inbox(
        self,
        before: str,
        unread_only: bool = False,
        channel: Optional[str] = None,
        thread_key: Optional[str] = None,
        starred: bool = False,
        needs_response: bool = False,
        q: Optional[str] = None,
    ) -> int:
        """Archive every matching item at or before a stable cutoff cursor.

        Requires a ``before`` cutoff so a client can never archive rows it has
        not rendered (avoids the read-all cutoff race). Returns rows changed.
        """
        before_at, before_seq = _parse_inbox_cursor(before)
        if before_seq is None:
            cutoff_clause = "created_at <= ?"
            params: list[Any] = [before_at]
        else:
            cutoff_clause = ("(created_at < ? OR (created_at = ? AND "
                             "cursor_seq <= ?))")
            params = [before_at, before_at, before_seq]
        clauses = ["read_at IS NULL", cutoff_clause]
        filter_clauses, filter_params = _inbox_filters(
            channel, thread_key, starred, needs_response, q)
        clauses += filter_clauses
        params += filter_params
        where = " WHERE " + " AND ".join(clauses)
        try:
            cur = self._conn.execute(
                f"UPDATE inbox_items SET read_at = ?{where};",
                [_now_iso()] + params,
            )
        except sqlite3.Error as exc:
            logger.error("inbox.read_all FAILED err=%s", exc)
            raise
        n = cur.rowcount
        logger.info("inbox.read_all before=%s changed=%d ok", before, n)
        return n

    # -- inbox scoring / learned rules (v11) ---------------------------------- #
    def set_inbox_score(self, item_id: str, score: Optional[int]) -> InboxItem:
        """Stamp the user's 1|2|3 score (or None to clear) on one item."""
        if score is not None and score not in (1, 2, 3):
            # An unknown item still wins over a bad score: look it up first.
            self.get_inbox_item(item_id)
            raise ValueError(f"invalid score: {score!r}")
        return self._update_one(
            "inbox_items", "id", item_id, {"score": score},
            getter=self.get_inbox_item, from_row=_inbox_item_from_row,
            ok=("inbox.set_score id=%s score=%s ok", item_id, score),
            fail=("inbox.set_score id=%s score=%s", item_id, score))

    def get_inbox_rule(self, kind_key: str) -> Optional[InboxRule]:
        row = self._conn.execute(
            "SELECT * FROM inbox_rules WHERE kind_key = ?;", (kind_key,)
        ).fetchone()
        return _inbox_rule_from_row(row) if row is not None else None

    def list_inbox_rules(self) -> list[InboxRule]:
        rows = self._conn.execute(
            "SELECT * FROM inbox_rules ORDER BY updated_at DESC;").fetchall()
        return [_inbox_rule_from_row(r) for r in rows]

    def upsert_inbox_rule(self, kind_key: str, score: int, channel: str,
                          source_label: str, example_title: str) -> InboxRule:
        if score not in (1, 2, 3):
            raise ValueError(f"invalid score: {score!r}")
        try:
            self._conn.execute(
                "INSERT INTO inbox_rules (kind_key, score, channel, "
                "source_label, example_title, updated_at) VALUES "
                "(?, ?, ?, ?, ?, ?) ON CONFLICT(kind_key) DO UPDATE SET "
                "score = excluded.score, example_title = "
                "excluded.example_title, updated_at = excluded.updated_at;",
                (kind_key, score, channel, source_label, example_title,
                 _now_iso()))
        except sqlite3.Error as exc:
            logger.error("inbox.rule.upsert kind=%s score=%s FAILED err=%s",
                         kind_key, score, exc)
            raise
        logger.info("inbox.rule.upsert kind=%s score=%s ok", kind_key, score)
        return self.get_inbox_rule(kind_key)

    def delete_inbox_rule(self, kind_key: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM inbox_rules WHERE kind_key = ?;", (kind_key,))
        logger.info("inbox.rule.delete kind=%s changed=%d", kind_key,
                    cur.rowcount)
        return cur.rowcount > 0

    def apply_inbox_score_to_kind(self, kind_key: str, score: Optional[int],
                                  archive: bool = False) -> int:
        """Stamp ``score`` on every item of this kind; with ``archive`` also
        mark the unread ones read (score 1 retro-cleanup). Returns the number
        of items touched. Kind is derived in Python, so this scans the
        table — bounded by the inbox's practical size, called on a click."""
        rows = self._conn.execute(
            "SELECT id, channel, source_label, title, read_at FROM "
            "inbox_items;").fetchall()
        ids = [r["id"] for r in rows
               if inbox_kind_key(r["channel"], r["source_label"], r["title"])
               == kind_key]
        if not ids:
            return 0
        now = _now_iso()
        try:
            for chunk in range(0, len(ids), 500):
                part = ids[chunk:chunk + 500]
                marks = ",".join("?" * len(part))
                self._conn.execute(
                    f"UPDATE inbox_items SET score = ? WHERE id IN ({marks});",
                    [score, *part])
                if archive:
                    self._conn.execute(
                        f"UPDATE inbox_items SET read_at = ? WHERE id IN "
                        f"({marks}) AND read_at IS NULL;", [now, *part])
        except sqlite3.Error as exc:
            logger.error("inbox.score.kind kind=%s FAILED err=%s", kind_key, exc)
            raise
        logger.info("inbox.score.kind kind=%s score=%s archive=%s n=%d ok",
                    kind_key, score, archive, len(ids))
        return len(ids)

    def inbox_unread_maps(self) -> tuple[dict[str, int], dict[str, int], int]:
        """Per-channel + per-thread unread counts and the total.

        Returns (per_channel, per_thread, total). ``None`` thread key is
        labeled by the daemon; here NULL rows are grouped under the key None.
        """
        try:
            total_row = self._conn.execute(
                "SELECT COUNT(*) AS n FROM inbox_items WHERE read_at IS NULL;"
            ).fetchone()
            channel_rows = self._conn.execute(
                "SELECT channel AS k, COUNT(*) AS n FROM inbox_items WHERE "
                "read_at IS NULL GROUP BY channel;").fetchall()
            thread_rows = self._conn.execute(
                "SELECT thread_key AS k, COUNT(*) AS n FROM inbox_items WHERE "
                "read_at IS NULL GROUP BY thread_key;").fetchall()
        except sqlite3.Error as exc:
            logger.error("inbox.unread FAILED err=%s", exc)
            raise
        per_channel = {r["k"]: r["n"] for r in channel_rows}
        per_thread = {r["k"]: r["n"] for r in thread_rows}
        total = total_row["n"] if total_row else 0
        return per_channel, per_thread, total

    def search_inbox_items(self, q: str, limit: int = 100) -> list[InboxItem]:
        """Whole-archive text search: case-insensitive LIKE over
        title/body/source_label across BOTH states (inbox AND archive).

        This is deliberately a separate, UNFILTERED query from
        list_inbox_items: the view list pages a state-ordered stream, while a
        search must match the ENTIRE archive — the 200-row view cap must never
        cut off matches. Returns newest-first, hard-capped.
        """
        like = _like(q)
        limit = max(1, min(int(limit), 200))
        rows = self._conn.execute(
            "SELECT * FROM inbox_items WHERE title LIKE ? ESCAPE '\\' "
            "OR body LIKE ? ESCAPE '\\' OR source_label LIKE ? "
            "ESCAPE '\\' ORDER BY created_at DESC, cursor_seq DESC "
            "LIMIT ?", (like, like, like, limit)).fetchall()
        return [_inbox_item_from_row(r) for r in rows]


    # -- inbox channels ----------------------------------------------------- #
    def get_channel(self, channel: str) -> InboxChannel:
        row = self._conn.execute(
            "SELECT * FROM inbox_channels WHERE channel = ?;", (channel,)
        ).fetchone()
        if row is None:
            logger.error("inbox.channel.get channel=%s NOT-FOUND", channel)
            raise UnknownChannel(channel)
        return _inbox_channel_from_row(row)

    def list_channels(self) -> list[InboxChannel]:
        rows = self._conn.execute(
            "SELECT * FROM inbox_channels ORDER BY channel;").fetchall()
        return [_inbox_channel_from_row(r) for r in rows]

    def update_channel(self, channel: str, mode: Optional[str] = None,
                       mute_until: Any = _UNSET) -> InboxChannel:
        """Patch a channel's mode and/or mute_until (effective-mode computed at
        the daemon layer). Raises :class:`UnknownChannel` if the row is absent
        and :class:`ValueError` on a bad mode."""
        self.get_channel(channel)
        if mode is not None and mode not in ("notify", "quiet", "muted"):
            logger.error("inbox.channel.update channel=%s bad-mode=%r",
                         channel, mode)
            raise ValueError(f"invalid mode: {mode!r}")
        sets: list[str] = []
        params: list[Any] = []
        if mode is not None:
            sets.append("mode = ?")
            params.append(mode)
        if mute_until is not _UNSET:
            sets.append("mute_until = ?")
            params.append(mute_until)
        if not sets:
            return self.get_channel(channel)
        params.append(channel)
        try:
            self._conn.execute(
                f"UPDATE inbox_channels SET {', '.join(sets)} WHERE channel = "
                "?;", params)
        except sqlite3.Error as exc:
            logger.error("inbox.channel.update channel=%s FAILED err=%s",
                         channel, exc)
            raise
        logger.info("inbox.channel.update channel=%s mode=%s mute_until=%s ok",
                    channel, mode,
                    "<unchanged>" if mute_until is _UNSET else mute_until)
        return self.get_channel(channel)
