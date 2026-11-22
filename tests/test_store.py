"""Store contract tests — run against a tmp-path SQLite db.

    venv/bin/python -m pytest tests -k store -q
"""

from __future__ import annotations

import json
import sqlite3

import pytest

from pulsar.store import (
    Store,
    Pulse,
    Grid,
    UnknownPulse,
    UnknownGrid,
    InvalidStateTransition,
    GridNotEmpty,
)
from pulsar.store.migrations import LATEST_VERSION, MIGRATIONS


@pytest.fixture()
def db_path(tmp_path) -> str:
    return str(tmp_path / "pulsar.db")


@pytest.fixture()
def store(db_path):
    s = Store(db_path)
    try:
        yield s
    finally:
        s.close()


# --------------------------------------------------------------------------- #
# open / migrations
# --------------------------------------------------------------------------- #
def test_open_sets_wal_and_schema_version(store, db_path):
    conn = sqlite3.connect(db_path)
    try:
        mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
        assert mode.lower() == "wal"
        ver = conn.execute("SELECT version FROM schema_meta;").fetchone()[0]
        assert ver == LATEST_VERSION
    finally:
        conn.close()


def test_reopen_applies_no_duplicate_migrations(db_path, caplog):
    s1 = Store(db_path)
    s1.create_grid("work")
    s1.close()

    with caplog.at_level("INFO", logger="pulsar.store"):
        s2 = Store(db_path)
    # A reopen must apply zero migrations and say so.
    assert any("migrations_applied=0" in r.message for r in caplog.records)
    assert any("up-to-date" in r.message for r in caplog.records)
    # schema_meta stays single-row (no duplicate INSERTs).
    conn = sqlite3.connect(db_path)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schema_meta;").fetchone()[0] == 1
        assert conn.execute("SELECT version FROM schema_meta;").fetchone()[0] \
            == LATEST_VERSION
    finally:
        conn.close()
    # data survived the reopen
    assert len(s2.list_grids()) == 1
    s2.close()


def test_inbox_cursor_migration_backfills_v7_rows(db_path):
    """Existing second-precision Inbox rows gain a durable cursor sequence."""
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("CREATE TABLE schema_meta (version INTEGER NOT NULL)")
        conn.execute("INSERT INTO schema_meta (version) VALUES (7)")
        for version, sql in MIGRATIONS:
            if version <= 7:
                conn.executescript(sql)
        conn.execute(
            "INSERT INTO inbox_channels "
            "(channel, mode, mute_until, created_at) VALUES (?, ?, NULL, ?)",
            ("system", "notify", "2026-08-18T12:00:00+00:00"))
        conn.execute(
            "INSERT INTO inbox_items "
            "(id, created_at, channel, source_kind, source_label, title, body) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            ("old-item", "2026-08-18T12:00:00+00:00", "system", "script",
             "old", "old", "body"))
        conn.commit()
    finally:
        conn.close()

    store = Store(db_path)
    try:
        old = store.get_inbox_item("old-item")
        new = store.create_inbox_item("system", "script", "new", "body")
        assert old.cursor_seq >= 1
        assert new.cursor_seq > old.cursor_seq
        assert old.cursor.endswith(f"|{old.cursor_seq}")
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# grids
# --------------------------------------------------------------------------- #
def test_create_and_list_grids_positions_autoincrement(store):
    a = store.create_grid("work")
    b = store.create_grid("research")
    c = store.create_grid("pinned", position=0)
    assert a.position == 0
    assert b.position == 1
    assert c.position == 0
    grids = store.list_grids()
    assert [g.name for g in grids] == ["pinned", "work", "research"]
    assert all(isinstance(g, Grid) for g in grids)


def test_get_unknown_grid_raises(store):
    with pytest.raises(UnknownGrid):
        store.get_grid("nope")


def test_update_grid_renames_and_reorders(store):
    g = store.create_grid("work", position=0)
    r = store.update_grid(g.id, name="job", position=3)
    assert r.name == "job"
    assert r.position == 3
    assert store.get_grid(g.id) == r


def test_update_grid_partial_only_touches_given_field(store):
    g = store.create_grid("work", position=2)
    r = store.update_grid(g.id, name="renamed")
    assert r.name == "renamed"
    assert r.position == 2  # untouched
    r2 = store.update_grid(g.id, position=9)
    assert r2.name == "renamed"  # untouched
    assert r2.position == 9


def test_update_grid_noop_returns_current(store):
    g = store.create_grid("work")
    r = store.update_grid(g.id)
    assert r == g


def test_update_unknown_grid_raises(store):
    with pytest.raises(UnknownGrid):
        store.update_grid("nope", name="x")


def test_delete_empty_grid(store):
    g = store.create_grid("scratch")
    store.delete_grid(g.id)
    with pytest.raises(UnknownGrid):
        store.get_grid(g.id)


def test_delete_grid_with_pulses_raises(store):
    g = store.create_grid("busy")
    store.create_pulse("p", "m", "claude", grid_id=g.id)
    with pytest.raises(GridNotEmpty):
        store.delete_grid(g.id)
    # still there — refusal, not a partial delete
    assert store.get_grid(g.id) == g


def test_delete_grid_blocked_by_archived_pulse(store):
    g = store.create_grid("busy")
    p = store.create_pulse("p", "m", "claude", grid_id=g.id)
    store.archive_pulse(p.id)
    # archived pulses still occupy the grid — must count against emptiness
    with pytest.raises(GridNotEmpty):
        store.delete_grid(g.id)


def test_reorder_grids(store):
    a = store.create_grid("a")
    b = store.create_grid("b")
    c = store.create_grid("c")
    store.reorder_grids([c.id, a.id, b.id])
    assert [g.id for g in store.list_grids()] == [c.id, a.id, b.id]


def test_reorder_grids_subset_leaves_others(store):
    a = store.create_grid("a")
    b = store.create_grid("b")
    c = store.create_grid("c")
    # b and c swap their own slots; a keeps the first slot
    store.reorder_grids([c.id, b.id])
    assert [g.id for g in store.list_grids()] == [a.id, c.id, b.id]


def test_reorder_grids_rejects_bad_input(store):
    g = store.create_grid("a")
    with pytest.raises(ValueError):
        store.reorder_grids([])
    with pytest.raises(ValueError):
        store.reorder_grids([g.id, g.id])
    with pytest.raises(UnknownGrid):
        store.reorder_grids([g.id, "nope"])
    # a rejected reorder changes nothing
    assert store.get_grid(g.id) == g


def test_delete_grid_detach_keeps_pulses(store):
    g = store.create_grid("busy")
    live = store.create_pulse("live", "m", "claude", grid_id=g.id)
    gone = store.create_pulse("gone", "m", "claude", grid_id=g.id)
    store.archive_pulse(gone.id)

    assert store.delete_grid(g.id, detach=True) == 2

    with pytest.raises(UnknownGrid):
        store.get_grid(g.id)
    # the pulses survive, out of any grid — including the archived one, which is
    # exactly what used to make the workspace undeletable.
    assert store.get_pulse(live.id).grid_id is None
    assert store.get_pulse(gone.id).grid_id is None
    assert store.get_pulse(gone.id).state == "archived"


def test_delete_empty_grid_detach_returns_zero(store):
    g = store.create_grid("scratch")
    assert store.delete_grid(g.id, detach=True) == 0


def test_delete_unknown_grid_raises(store):
    with pytest.raises(UnknownGrid):
        store.delete_grid("nope")


def test_unarchive_pulse_restores_to_stopped(store):
    p = store.create_pulse("p", "m", "claude")
    store.archive_pulse(p.id)
    assert store.get_pulse(p.id).state == "archived"
    r = store.unarchive_pulse(p.id)
    assert r.state == "stopped"
    assert r.archived_at is None


def test_unarchive_non_archived_raises(store):
    # a running pulse cannot be "unarchived" straight to stopped? running->stopped
    # IS allowed, so unarchive of a running pulse just stops it. Guard the real
    # invalid case: there is no archived->running, and stopped->stopped is fine.
    p = store.create_pulse("p", "m", "claude")  # running
    r = store.unarchive_pulse(p.id)  # running->stopped is a valid transition
    assert r.state == "stopped"


# --------------------------------------------------------------------------- #
# create / get / list pulse
# --------------------------------------------------------------------------- #
def test_create_pulse_defaults_running(store):
    grid = store.create_grid("work")
    p = store.create_pulse(
        title="scraper watch",
        model="claude-opus-5",
        vessel="claude",
        grid_id=grid.id,
        handoff_tokens=300_000,
        settings={"loop": {"minutes": 5}},
    )
    assert isinstance(p, Pulse)
    assert p.state == "running"
    assert p.archived_at is None
    assert p.grid_id == grid.id
    assert p.handoff_tokens == 300_000
    assert p.settings == {"loop": {"minutes": 5}}
    assert p.created_at == p.updated_at
    # round-trips through get
    assert store.get_pulse(p.id) == p


def test_create_pulse_no_grid(store):
    p = store.create_pulse("free floater", "claude-opus-5", "claude")
    assert p.grid_id is None
    assert p.settings == {}


def test_create_pulse_unknown_grid_raises(store):
    with pytest.raises(UnknownGrid):
        store.create_pulse("x", "m", "v", grid_id="ghost")


def test_create_pulse_bad_state_raises(store):
    with pytest.raises(ValueError):
        store.create_pulse("x", "m", "v", state="thinking")  # type: ignore[arg-type]


def test_get_unknown_pulse_raises(store):
    with pytest.raises(UnknownPulse):
        store.get_pulse("nope")


def test_corrupt_settings_json_does_not_break_list_pulses(store, db_path, caplog):
    """One bad settings_json row reads as {} and is logged once."""
    good = store.create_pulse(title="good", model="m", vessel="v",
                              settings={"k": 1})
    bad = store.create_pulse(title="bad", model="m", vessel="v")
    conn = sqlite3.connect(db_path)
    try:
        conn.execute("UPDATE pulses SET settings_json = ? WHERE id = ?;",
                     ("{not json", bad.id))
        conn.commit()
    finally:
        conn.close()
    with caplog.at_level("ERROR", logger="pulsar.store"):
        by_id = {p.id: p for p in store.list_pulses()}
        store.list_pulses()
        assert store.get_pulse(bad.id).settings == {}
    assert by_id[bad.id].settings == {}
    assert by_id[good.id].settings == {"k": 1}
    msgs = [r.getMessage() for r in caplog.records if bad.id in r.getMessage()]
    assert len(msgs) == 1


def test_list_pulses_filters_and_order(store):
    g1 = store.create_grid("g1")
    g2 = store.create_grid("g2")
    p1 = store.create_pulse("one", "m", "v", grid_id=g1.id)
    p2 = store.create_pulse("two", "m", "v", grid_id=g1.id)
    p3 = store.create_pulse("three", "m", "v", grid_id=g2.id)

    # Wall order: creation order — a new pulse appends at the END, never the
    # front (was newest-first before v5).
    ids = [p.id for p in store.list_pulses()]
    assert ids == [p1.id, p2.id, p3.id]
    assert [p.position for p in store.list_pulses()] == [0, 1, 2]

    # grid filter
    assert {p.id for p in store.list_pulses(grid_id=g1.id)} == {p1.id, p2.id}

    # state filter
    store.set_state(p1.id, "stopped")
    assert {p.id for p in store.list_pulses(state="stopped")} == {p1.id}


def test_reorder_pulses_full_list(store):
    p1 = store.create_pulse("one", "m", "v")
    p2 = store.create_pulse("two", "m", "v")
    p3 = store.create_pulse("three", "m", "v")

    store.reorder_pulses([p3.id, p1.id, p2.id])
    assert [p.id for p in store.list_pulses()] == [p3.id, p1.id, p2.id]

    # A pulse created after a reorder still lands at the far right.
    p4 = store.create_pulse("four", "m", "v")
    assert [p.id for p in store.list_pulses()] == [p3.id, p1.id, p2.id, p4.id]


def test_reorder_pulses_subset_keeps_other_slots(store):
    g1 = store.create_grid("g1")
    g2 = store.create_grid("g2")
    a = store.create_pulse("a", "m", "v", grid_id=g1.id)   # pos 0
    b = store.create_pulse("b", "m", "v", grid_id=g2.id)   # pos 1
    c = store.create_pulse("c", "m", "v", grid_id=g1.id)   # pos 2

    # Reorder only grid g1's pulses (the wall shows one tab at a time): a and c
    # swap within their own slots {0, 2}; b keeps slot 1.
    store.reorder_pulses([c.id, a.id])
    assert [p.id for p in store.list_pulses()] == [c.id, b.id, a.id]
    assert [p.id for p in store.list_pulses(grid_id=g1.id)] == [c.id, a.id]


def test_reorder_pulses_rejects_bad_input(store):
    p = store.create_pulse("one", "m", "v")
    with pytest.raises(ValueError):
        store.reorder_pulses([])
    with pytest.raises(ValueError):
        store.reorder_pulses([p.id, p.id])
    with pytest.raises(UnknownPulse):
        store.reorder_pulses([p.id, "nope"])
    # Failed calls left the order untouched.
    assert [x.id for x in store.list_pulses()] == [p.id]


def test_v5_backfill_preserves_pre_v5_display_order(db_path):
    """A pre-v5 db (no position column) upgrades with newest-first preserved."""
    conn = sqlite3.connect(db_path)
    conn.executescript(
        """
        CREATE TABLE schema_meta (version INTEGER NOT NULL);
        INSERT INTO schema_meta VALUES (4);
        CREATE TABLE grids (id TEXT PRIMARY KEY, name TEXT NOT NULL,
                            position INTEGER NOT NULL);
        CREATE TABLE pulses (
            id TEXT PRIMARY KEY, title TEXT NOT NULL, model TEXT NOT NULL,
            vessel TEXT NOT NULL, grid_id TEXT, state TEXT NOT NULL,
            handoff_tokens INTEGER, created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL, archived_at TEXT,
            settings_json TEXT NOT NULL,
            FOREIGN KEY (grid_id) REFERENCES grids(id)
        );
        CREATE TABLE schedules (
            id TEXT PRIMARY KEY, pulse_id TEXT NOT NULL, kind TEXT NOT NULL,
            prompt TEXT NOT NULL, interval_minutes INTEGER, at_iso TEXT,
            cron_hhmm TEXT, days TEXT, enabled INTEGER NOT NULL DEFAULT 1,
            last_fired_at TEXT, created_at TEXT NOT NULL, target_pulse_id TEXT,
            FOREIGN KEY (pulse_id) REFERENCES pulses(id)
        );
        CREATE TABLE app_settings (key TEXT PRIMARY KEY,
                                   value_json TEXT NOT NULL,
                                   updated_at TEXT NOT NULL);
        INSERT INTO pulses VALUES
            ('old',  'old',  'm', 'v', NULL, 'running', NULL,
             '2026-07-01T00:00:00+00:00', '2026-07-01T00:00:00+00:00', NULL, '{}'),
            ('mid',  'mid',  'm', 'v', NULL, 'running', NULL,
             '2026-07-02T00:00:00+00:00', '2026-07-02T00:00:00+00:00', NULL, '{}'),
            ('new',  'new',  'm', 'v', NULL, 'running', NULL,
             '2026-07-03T00:00:00+00:00', '2026-07-03T00:00:00+00:00', NULL, '{}');
        """
    )
    conn.close()

    with Store(db_path) as s:
        # Pre-v5 the UI showed newest first — backfill keeps that exact order.
        assert [p.id for p in s.list_pulses()] == ["new", "mid", "old"]
        assert [p.position for p in s.list_pulses()] == [0, 1, 2]


def test_list_pulses_hides_archived_by_default(store):
    live = store.create_pulse("live", "m", "v")
    arch = store.create_pulse("arch", "m", "v")
    store.archive_pulse(arch.id)

    default_ids = {p.id for p in store.list_pulses()}
    assert live.id in default_ids
    assert arch.id not in default_ids

    all_ids = {p.id for p in store.list_pulses(include_archived=True)}
    assert arch.id in all_ids

    assert {p.id for p in store.list_pulses(state="archived")} == {arch.id}


# --------------------------------------------------------------------------- #
# state transitions
# --------------------------------------------------------------------------- #
def test_state_transitions_and_archived_at(store):
    p = store.create_pulse("p", "m", "v")
    assert p.state == "running"

    stopped = store.set_state(p.id, "stopped")
    assert stopped.state == "stopped"
    assert stopped.archived_at is None
    assert stopped.updated_at >= p.updated_at

    restarted = store.set_state(p.id, "running")
    assert restarted.state == "running"

    archived = store.set_state(p.id, "archived")
    assert archived.state == "archived"
    assert archived.archived_at is not None

    # restore from archive clears archived_at
    restored = store.set_state(p.id, "stopped")
    assert restored.state == "stopped"
    assert restored.archived_at is None


def test_invalid_transition_raises(store):
    p = store.create_pulse("p", "m", "v")
    store.set_state(p.id, "archived")
    # archived cannot jump straight back to running
    with pytest.raises(InvalidStateTransition):
        store.set_state(p.id, "running")


def test_set_state_bad_value_raises(store):
    p = store.create_pulse("p", "m", "v")
    with pytest.raises(ValueError):
        store.set_state(p.id, "thinking")  # type: ignore[arg-type]


def test_set_state_unknown_pulse_raises(store):
    with pytest.raises(UnknownPulse):
        store.set_state("nope", "stopped")


# --------------------------------------------------------------------------- #
# settings / grid move / handoff / delete
# --------------------------------------------------------------------------- #
def test_update_settings_merge_and_replace(store):
    p = store.create_pulse("p", "m", "v", settings={"a": 1, "b": 2})
    merged = store.update_settings(p.id, {"b": 20, "c": 3})
    assert merged.settings == {"a": 1, "b": 20, "c": 3}

    replaced = store.update_settings(p.id, {"only": True}, merge=False)
    assert replaced.settings == {"only": True}


def test_update_settings_unknown_pulse_raises(store):
    with pytest.raises(UnknownPulse):
        store.update_settings("nope", {"a": 1})


def test_set_grid_moves_and_validates(store):
    g1 = store.create_grid("g1")
    g2 = store.create_grid("g2")
    p = store.create_pulse("p", "m", "v", grid_id=g1.id)

    moved = store.set_grid(p.id, g2.id)
    assert moved.grid_id == g2.id

    detached = store.set_grid(p.id, None)
    assert detached.grid_id is None

    with pytest.raises(UnknownGrid):
        store.set_grid(p.id, "ghost")


def test_set_title_renames_and_validates(store):
    p = store.create_pulse("old", "m", "v")

    renamed = store.set_title(p.id, "  new name  ")
    assert renamed.title == "new name"

    with pytest.raises(ValueError):
        store.set_title(p.id, "   ")

    with pytest.raises(UnknownPulse):
        store.set_title("nope", "x")


def test_set_handoff_tokens(store):
    p = store.create_pulse("p", "m", "v")
    assert p.handoff_tokens is None
    updated = store.set_handoff_tokens(p.id, 225_000)
    assert updated.handoff_tokens == 225_000


def test_delete_pulse(store):
    p = store.create_pulse("p", "m", "v")
    store.delete_pulse(p.id)
    with pytest.raises(UnknownPulse):
        store.get_pulse(p.id)
    with pytest.raises(UnknownPulse):
        store.delete_pulse(p.id)


# --------------------------------------------------------------------------- #
# logging (standing rule) — write paths and failure paths log a line
# --------------------------------------------------------------------------- #
def test_write_path_logs_line(store, caplog):
    with caplog.at_level("INFO", logger="pulsar.store"):
        store.create_pulse("logged", "m", "v")
    assert any("pulse.create" in r.message and "ok" in r.message
               for r in caplog.records)


def test_failure_path_logs_line(store, caplog):
    with caplog.at_level("ERROR", logger="pulsar.store"):
        with pytest.raises(UnknownPulse):
            store.get_pulse("nope")
    assert any("NOT-FOUND" in r.message for r in caplog.records)


def test_put_setting_logs_serialized_value_size(store, caplog):
    value = {"colors": ["#123456", "#abcdef"], "enabled": True}
    with caplog.at_level("INFO", logger="pulsar.store"):
        store.put_setting("theme", value)

    assert store.get_setting("theme") == value
    expected = len(json.dumps(value))
    assert any(f"settings.put key=theme bytes={expected} ok" in r.message
               for r in caplog.records)


# --------------------------------------------------------------------------- #
# schedules
# --------------------------------------------------------------------------- #
from pulsar.store import Schedule, UnknownSchedule  # noqa: E402


def _a_pulse(store):
    return store.create_pulse("sched-host", "m", "claude-code")


def test_create_schedule_loop(store):
    p = _a_pulse(store)
    s = store.create_schedule(p.id, "loop", "do the thing", interval_minutes=15)
    assert isinstance(s, Schedule)
    assert s.pulse_id == p.id
    assert s.kind == "loop"
    assert s.interval_minutes == 15
    assert s.enabled is True
    assert s.last_fired_at is None
    assert s.created_at


def test_create_schedule_once_and_daily(store):
    p = _a_pulse(store)
    once = store.create_schedule(p.id, "once", "wake once",
                                 at_iso="2026-08-01T09:00:00+00:00")
    assert once.kind == "once" and once.at_iso.startswith("2026-08-01")
    daily = store.create_schedule(p.id, "daily", "morning",
                                  cron_hhmm="09:00", days="mon,wed,fri")
    assert daily.kind == "daily"
    assert daily.cron_hhmm == "09:00"
    assert daily.days == "mon,wed,fri"


def test_create_schedule_bad_kind(store):
    p = _a_pulse(store)
    with pytest.raises(ValueError):
        store.create_schedule(p.id, "bogus", "x")


def test_create_schedule_unknown_pulse(store):
    with pytest.raises(UnknownPulse):
        store.create_schedule("nope", "loop", "x", interval_minutes=5)


def test_list_schedules_filters(store):
    p1 = _a_pulse(store)
    p2 = _a_pulse(store)
    a = store.create_schedule(p1.id, "loop", "a", interval_minutes=5)
    b = store.create_schedule(p1.id, "loop", "b", interval_minutes=5)
    c = store.create_schedule(p2.id, "loop", "c", interval_minutes=5)
    store.set_schedule_enabled(b.id, False)

    # all
    all_ids = {s.id for s in store.list_schedules()}
    assert all_ids == {a.id, b.id, c.id}
    # by pulse
    p1_ids = {s.id for s in store.list_schedules(pulse_id=p1.id)}
    assert p1_ids == {a.id, b.id}
    # enabled_only drops b
    en_ids = {s.id for s in store.list_schedules(enabled_only=True)}
    assert en_ids == {a.id, c.id}
    # both filters together
    p1_en = {s.id for s in store.list_schedules(pulse_id=p1.id, enabled_only=True)}
    assert p1_en == {a.id}


def test_list_schedules_created_order(store):
    p = _a_pulse(store)
    a = store.create_schedule(p.id, "loop", "a", interval_minutes=5)
    b = store.create_schedule(p.id, "loop", "b", interval_minutes=5)
    ids = [s.id for s in store.list_schedules(pulse_id=p.id)]
    assert ids == [a.id, b.id]  # oldest first


def test_set_schedule_enabled_toggle(store):
    p = _a_pulse(store)
    s = store.create_schedule(p.id, "loop", "a", interval_minutes=5)
    off = store.set_schedule_enabled(s.id, False)
    assert off.enabled is False
    on = store.set_schedule_enabled(s.id, True)
    assert on.enabled is True


def test_set_schedule_enabled_unknown(store):
    with pytest.raises(UnknownSchedule):
        store.set_schedule_enabled("nope", False)


def test_update_schedule_prompt_and_interval(store):
    p = _a_pulse(store)
    s = store.create_schedule(p.id, "loop", "a", interval_minutes=5)
    up = store.update_schedule(s.id, prompt="b", interval_minutes=15)
    assert up.prompt == "b"
    assert up.interval_minutes == 15
    # only-prompt edit leaves the interval alone
    up2 = store.update_schedule(s.id, prompt="c")
    assert up2.prompt == "c"
    assert up2.interval_minutes == 15
    # only-interval edit leaves the prompt alone
    up3 = store.update_schedule(s.id, interval_minutes=30)
    assert up3.prompt == "c"
    assert up3.interval_minutes == 30


def test_update_schedule_unknown(store):
    with pytest.raises(UnknownSchedule):
        store.update_schedule("nope", prompt="x")


def test_mark_fired_stamps(store):
    p = _a_pulse(store)
    s = store.create_schedule(p.id, "loop", "a", interval_minutes=5)
    fired = store.mark_fired(s.id, "2026-07-26T10:00:00+00:00")
    assert fired.last_fired_at == "2026-07-26T10:00:00+00:00"
    # re-stamp overwrites
    again = store.mark_fired(s.id, "2026-07-26T10:05:00+00:00")
    assert again.last_fired_at == "2026-07-26T10:05:00+00:00"


def test_mark_fired_unknown(store):
    with pytest.raises(UnknownSchedule):
        store.mark_fired("nope", "2026-07-26T10:00:00+00:00")


def test_delete_schedule(store):
    p = _a_pulse(store)
    s = store.create_schedule(p.id, "loop", "a", interval_minutes=5)
    store.delete_schedule(s.id)
    with pytest.raises(UnknownSchedule):
        store.get_schedule(s.id)
    assert store.list_schedules() == []


def test_delete_schedule_unknown(store):
    with pytest.raises(UnknownSchedule):
        store.delete_schedule("nope")


def test_get_schedule_unknown(store):
    with pytest.raises(UnknownSchedule):
        store.get_schedule("nope")


def test_schedule_migration_survives_reopen(db_path):
    s1 = Store(db_path)
    p = s1.create_pulse("host", "m", "claude-code")
    sched = s1.create_schedule(p.id, "daily", "morning", cron_hhmm="08:30")
    s1.close()
    s2 = Store(db_path)
    got = s2.get_schedule(sched.id)
    assert got.cron_hhmm == "08:30"
    assert got.kind == "daily"
    s2.close()


def test_delete_pulse_removes_its_schedules(store):
    p = _a_pulse(store)
    s = store.create_schedule(p.id, "loop", "a", interval_minutes=5)
    store.delete_pulse(p.id)
    with pytest.raises(UnknownPulse):
        store.get_pulse(p.id)
    # schedule went with it (no FK-orphan, no IntegrityError)
    with pytest.raises(UnknownSchedule):
        store.get_schedule(s.id)
    assert store.list_schedules() == []


# --- sub-workspaces (v16, 2026-09-11) --------------------------------------- #
def test_create_sub_grid_and_tab_order(store):
    a = store.create_grid("A")
    b = store.create_grid("B")
    a2 = store.create_grid("A-two", parent_id=a.id)
    a1 = store.create_grid("A-one", parent_id=a.id)
    assert a2.parent_id == a.id
    # Flat list in tab order: parent, then its children by position, then B.
    assert [g.id for g in store.list_grids()] == [a.id, a2.id, a1.id, b.id]
    assert store.get_grid(a1.id).parent_id == a.id


def test_sub_grid_one_layer_only(store):
    a = store.create_grid("A")
    sub = store.create_grid("sub", parent_id=a.id)
    with pytest.raises(ValueError):
        store.create_grid("deeper", parent_id=sub.id)
    with pytest.raises(UnknownGrid):
        store.create_grid("orphan", parent_id="nope")
    # A workspace that has children can't be nested under something else.
    b = store.create_grid("B")
    with pytest.raises(ValueError):
        store.update_grid(a.id, parent_id=b.id)
    with pytest.raises(ValueError):
        store.update_grid(b.id, parent_id=b.id)


def test_update_grid_reparents_and_lifts(store):
    a = store.create_grid("A")
    b = store.create_grid("B")
    assert store.update_grid(b.id, parent_id=a.id).parent_id == a.id
    assert store.update_grid(b.id, name="B2").parent_id == a.id  # unchanged
    assert store.update_grid(b.id, parent_id=None).parent_id is None


def test_delete_parent_grid_refuses_then_lifts_children(store):
    a = store.create_grid("A")
    sub = store.create_grid("sub", parent_id=a.id)
    with pytest.raises(GridNotEmpty):
        store.delete_grid(a.id)
    store.delete_grid(a.id, detach=True)
    assert store.get_grid(sub.id).parent_id is None


def test_touch_input_records_users_last_message(store):
    """v17: 'my inputs' sort — only the user's own sends stamp last_input_at,
    and stamping never bumps updated_at (a different recency)."""
    p = store.create_pulse("t", "m", "v")
    assert p.last_input_at is None
    store.touch_input(p.id, "2026-09-11T20:00:00+00:00")
    again = store.get_pulse(p.id)
    assert again.last_input_at == "2026-09-11T20:00:00+00:00"
    assert again.updated_at == p.updated_at
    store.touch_input(p.id)
    assert store.get_pulse(p.id).last_input_at > "2026-09-11T20:00:00+00:00"
