"""Characterization tests for the single-row Store setters.

Each converted setter is pinned on four points:

(a) the returned object equals the getter's result after the write;
(b) an unknown id raises today's exception type, with today's single
    ``NOT-FOUND`` log line;
(c) exactly one ``ok`` log line with today's text (none for ``set_follow``);
(d) a forced ``sqlite3.Error`` on the UPDATE logs exactly one ``FAILED`` line
    and re-raises.

    venv/bin/python -m pytest tests/test_store_setters.py -q
"""

from __future__ import annotations

import logging
import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Optional

import pytest

from pulsar.store import (
    Store,
    UnknownChannel,
    UnknownConditionWatch,
    UnknownGrid,
    UnknownInboxItem,
    UnknownPulse,
    UnknownSchedule,
    UnknownWatch,
)

T = "2026-10-01T00:00:00+00:00"


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "pulsar.db"))
    try:
        yield s
    finally:
        s.close()


def _pulse(s: Store) -> str:
    return s.create_pulse("title", "m1", "v1").id


def _schedule(s: Store) -> str:
    return s.create_schedule(_pulse(s), "loop", "p", interval_minutes=5).id


def _watch(s: Store) -> str:
    return s.create_watch(_pulse(s), _pulse(s), "spawn", next_check_at=T).id


def _condwatch(s: Store) -> str:
    return s.create_condition_watch(_pulse(s), "file", {"path": "/x"}, T).id


def _inbox(s: Store) -> str:
    return s.create_inbox_item("ch", "pulse", "t", "b", needs_response=True).id


@dataclass
class Case:
    name: str
    setup: Callable[[Store], str]
    call: Callable[[Store, str], Any]
    get: Callable[[Store, str], Any]
    unknown: type
    notfound: str                        # log line for an unknown id "nope"
    ok: Optional[Callable[[str], str]]   # None = the setter logs nothing on success
    fail: Callable[[str], str]           # text of the FAILED line (err=boom)
    check: Callable[[Any], None]


CASES = [
    Case("set_title", _pulse,
         lambda s, i: s.set_title(i, "  new  "),
         lambda s, i: s.get_pulse(i), UnknownPulse,
         "pulse.get id=nope NOT-FOUND",
         lambda i: f"pulse.set_title id={i} title='new' ok",
         lambda i: f"pulse.set_title id={i} FAILED err=boom",
         lambda p: p.title == "new"),
    Case("set_handoff_tokens", _pulse,
         lambda s, i: s.set_handoff_tokens(i, 123),
         lambda s, i: s.get_pulse(i), UnknownPulse,
         "pulse.get id=nope NOT-FOUND",
         lambda i: f"pulse.set_handoff_tokens id={i} tokens=123 ok",
         lambda i: f"pulse.set_handoff_tokens id={i} FAILED err=boom",
         lambda p: p.handoff_tokens == 123),
    Case("set_model", _pulse,
         lambda s, i: s.set_model(i, "m2", "v2"),
         lambda s, i: s.get_pulse(i), UnknownPulse,
         "pulse.get id=nope NOT-FOUND",
         lambda i: f"pulse.set_model id={i} m1/v1 -> m2/v2 ok",
         lambda i: f"pulse.set_model id={i} FAILED err=boom",
         lambda p: (p.model, p.vessel) == ("m2", "v2")),
    Case("set_schedule_enabled", _schedule,
         lambda s, i: s.set_schedule_enabled(i, False),
         lambda s, i: s.get_schedule(i), UnknownSchedule,
         "schedule.get id=nope NOT-FOUND",
         lambda i: f"schedule.set_enabled id={i} enabled=False ok",
         lambda i: f"schedule.set_enabled id={i} FAILED err=boom",
         lambda r: r.enabled is False),
    Case("mark_fired", _schedule,
         lambda s, i: s.mark_fired(i, T),
         lambda s, i: s.get_schedule(i), UnknownSchedule,
         "schedule.get id=nope NOT-FOUND",
         lambda i: f"schedule.mark_fired id={i} ts={T} ok",
         lambda i: f"schedule.mark_fired id={i} FAILED err=boom",
         lambda r: r.last_fired_at == T),
    Case("bump_watch", _watch,
         lambda s, i: s.bump_watch(i, "2026-10-02T00:00:00+00:00"),
         lambda s, i: s.get_watch(i), UnknownWatch,
         "watch.get id=nope NOT-FOUND",
         lambda i: f"watch.bump id={i} next_check=2026-10-02T00:00:00+00:00 ok",
         lambda i: f"watch.bump id={i} FAILED err=boom",
         lambda w: w.checks_done == 1 and w.state == "watching"),
    Case("close_watch", _watch,
         lambda s, i: s.close_watch(i, "working"),
         lambda s, i: s.get_watch(i), UnknownWatch,
         "watch.get id=nope NOT-FOUND",
         lambda i: f"watch.close id={i} outcome=working ok",
         lambda i: f"watch.close id={i} FAILED err=boom",
         lambda w: (w.state, w.outcome, w.checks_done) == ("closed", "working", 1)
         and w.closed_at),
    Case("arm_follow", _watch,
         lambda s, i: s.arm_follow(i, T, 5),
         lambda s, i: s.get_watch(i), UnknownWatch,
         "watch.get id=nope NOT-FOUND",
         lambda i: f"watch.arm_follow id={i} last_ctx=5 baseline=None ok",
         lambda i: f"watch.arm_follow id={i} FAILED err=boom",
         lambda w: (w.phase, w.last_ctx, w.follow_checks) == ("follow", 5, 0)),
    Case("arm_follow_baseline", _watch,
         lambda s, i: s.arm_follow(i, T, 5, baseline_ctx=9),
         lambda s, i: s.get_watch(i), UnknownWatch,
         "watch.get id=nope NOT-FOUND",
         lambda i: f"watch.arm_follow id={i} last_ctx=5 baseline=9 ok",
         lambda i: f"watch.arm_follow id={i} FAILED err=boom",
         lambda w: (w.phase, w.last_ctx, w.baseline_ctx) == ("follow", 5, 9)),
    Case("set_follow", _watch,
         lambda s, i: s.set_follow(i, T, 7, 2, checks_done=3),
         lambda s, i: s.get_watch(i), UnknownWatch,
         "watch.get id=nope NOT-FOUND",
         None,
         lambda i: f"watch.set_follow id={i} FAILED err=boom",
         lambda w: (w.last_ctx, w.follow_checks, w.checks_done) == (7, 2, 3)),
    Case("bump_condition_watch", _condwatch,
         lambda s, i: s.bump_condition_watch(i, T, True, {"k": 1}),
         lambda s, i: s.get_condition_watch(i), UnknownConditionWatch,
         "condwatch.get id=nope NOT-FOUND",
         lambda i: f"condwatch.bump id={i} next_check={T} bad=True ok",
         lambda i: f"condwatch.bump id={i} FAILED err=boom",
         lambda w: w.checks_done == 1 and w.bad == 1 and w.detail == {"k": 1}),
    Case("mark_condition_fired", _condwatch,
         lambda s, i: s.mark_condition_fired(i, "delivered", ts=T),
         lambda s, i: s.get_condition_watch(i), UnknownConditionWatch,
         "condwatch.get id=nope NOT-FOUND",
         lambda i: f"condwatch.fired id={i} delivery=delivered ok",
         lambda i: f"condwatch.fired id={i} FAILED err=boom",
         lambda w: (w.last_fired_at, w.last_delivery) == (T, "delivered")),
    Case("close_condition_watch", _condwatch,
         lambda s, i: s.close_condition_watch(i, "fired"),
         lambda s, i: s.get_condition_watch(i), UnknownConditionWatch,
         "condwatch.get id=nope NOT-FOUND",
         lambda i: f"condwatch.close id={i} outcome=fired ok",
         lambda i: f"condwatch.close id={i} FAILED err=boom",
         lambda w: (w.state, w.outcome) == ("closed", "fired") and w.closed_at),
    Case("set_inbox_read", _inbox,
         lambda s, i: s.set_inbox_read(i, True),
         lambda s, i: s.get_inbox_item(i), UnknownInboxItem,
         "inbox.get id=nope NOT-FOUND",
         lambda i: f"inbox.set_read id={i} read=True ok",
         lambda i: f"inbox.set_read id={i} read=True FAILED err=boom",
         lambda r: r.read_at is not None),
    Case("set_inbox_starred", _inbox,
         lambda s, i: s.set_inbox_starred(i, True),
         lambda s, i: s.get_inbox_item(i), UnknownInboxItem,
         "inbox.get id=nope NOT-FOUND",
         lambda i: f"inbox.set_starred id={i} starred=True ok",
         lambda i: f"inbox.set_starred id={i} starred=True FAILED err=boom",
         lambda r: r.starred is True),
    Case("clear_inbox_response", _inbox,
         lambda s, i: s.clear_inbox_response(i),
         lambda s, i: s.get_inbox_item(i), UnknownInboxItem,
         "inbox.get id=nope NOT-FOUND",
         lambda i: f"inbox.clear_response id={i} ok",
         lambda i: f"inbox.clear_response id={i} FAILED err=boom",
         lambda r: r.needs_response is False),
    Case("mark_inbox_replied", _inbox,
         lambda s, i: s.mark_inbox_replied(i, T),
         lambda s, i: s.get_inbox_item(i), UnknownInboxItem,
         "inbox.get id=nope NOT-FOUND",
         lambda i: f"inbox.replied id={i} ts={T} ok",
         lambda i: f"inbox.replied id={i} FAILED err=boom",
         lambda r: r.replied_at == T and r.needs_response is False),
    Case("set_inbox_thread", _inbox,
         lambda s, i: s.set_inbox_thread(i, "tk"),
         lambda s, i: s.get_inbox_item(i), UnknownInboxItem,
         "inbox.get id=nope NOT-FOUND",
         lambda i: f"inbox.set_thread id={i} key=tk ok",
         lambda i: f"inbox.set_thread id={i} key=tk FAILED err=boom",
         lambda r: r.thread_key == "tk"),
    Case("set_inbox_score", _inbox,
         lambda s, i: s.set_inbox_score(i, 2),
         lambda s, i: s.get_inbox_item(i), UnknownInboxItem,
         "inbox.get id=nope NOT-FOUND",
         lambda i: f"inbox.set_score id={i} score=2 ok",
         lambda i: f"inbox.set_score id={i} score=2 FAILED err=boom",
         lambda r: r.score == 2),
]

# set_grid's lines carry the grid id, which only exists inside the call: it is
# pinned by the dedicated set_grid tests below, not by the table.
_TABLE = CASES
_IDS = [c.name for c in _TABLE]


def _records(caplog) -> list[logging.LogRecord]:
    return [r for r in caplog.records if r.name == "pulsar.store"]


def _lines(caplog) -> list[tuple[str, str]]:
    return [(r.levelname, r.getMessage()) for r in _records(caplog)]


@pytest.fixture(autouse=True)
def _info(caplog):
    caplog.set_level(logging.INFO, logger="pulsar.store")


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_returns_getter_result_and_applies_change(store, case):
    sid = case.setup(store)
    out = case.call(store, sid)
    assert out == case.get(store, sid)
    assert case.check(out)


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_unknown_id_raises_with_one_notfound_line(store, caplog, case):
    case.setup(store)
    caplog.clear()
    with pytest.raises(case.unknown):
        case.call(store, "nope")
    assert _lines(caplog) == [("ERROR", case.notfound)]


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_exactly_one_ok_line(store, caplog, case):
    sid = case.setup(store)
    caplog.clear()
    case.call(store, sid)
    if case.ok is None:
        assert _lines(caplog) == []
    else:
        assert _lines(caplog) == [("INFO", case.ok(sid))]


@pytest.mark.parametrize("case", _TABLE, ids=_IDS)
def test_sqlite_error_logs_one_failed_line_and_reraises(store, caplog, case,
                                                        monkeypatch):
    sid = case.setup(store)
    real = store._conn.execute

    def boom(sql, parameters=()):
        if sql.lstrip().upper().startswith("UPDATE"):
            raise sqlite3.OperationalError("boom")
        return real(sql, parameters)

    monkeypatch.setattr(store._conn, "execute", boom)
    caplog.clear()
    with pytest.raises(sqlite3.OperationalError):
        case.call(store, sid)
    assert _lines(caplog) == [("ERROR", case.fail(sid))]


# --------------------------------------------------------------------------- #
# set_grid: pulse is checked first, then the grid
# --------------------------------------------------------------------------- #
def test_set_grid_returns_getter_result_and_logs_one_ok_line(store, caplog):
    pid, gid = _pulse(store), store.create_grid("g").id
    caplog.clear()
    out = store.set_grid(pid, gid)
    assert out == store.get_pulse(pid)
    assert out.grid_id == gid
    assert _lines(caplog)[0] == ("INFO", f"pulse.set_grid id={pid} grid={gid} ok")
    assert len(_lines(caplog)) == 1


def test_set_grid_none_clears_the_grid(store, caplog):
    pid = store.create_pulse("t", "m", "v", grid_id=store.create_grid("g").id).id
    caplog.clear()
    out = store.set_grid(pid, None)
    assert out.grid_id is None
    assert _lines(caplog) == [("INFO", f"pulse.set_grid id={pid} grid=None ok")]


def test_set_grid_both_missing_raises_unknown_pulse(store, caplog):
    caplog.clear()
    with pytest.raises(UnknownPulse):
        store.set_grid("nope", "nogrid")
    assert _lines(caplog) == [("ERROR", "pulse.get id=nope NOT-FOUND")]


def test_set_grid_missing_pulse_raises_unknown_pulse(store, caplog):
    gid = store.create_grid("g").id
    caplog.clear()
    with pytest.raises(UnknownPulse):
        store.set_grid("nope", gid)
    assert _lines(caplog) == [("ERROR", "pulse.get id=nope NOT-FOUND")]


def test_set_grid_missing_grid_raises_unknown_grid_and_does_not_write(store, caplog):
    pid = _pulse(store)
    before = store.get_pulse(pid)
    caplog.clear()
    with pytest.raises(UnknownGrid):
        store.set_grid(pid, "nogrid")
    assert _lines(caplog) == [("ERROR", "grid.get id=nogrid NOT-FOUND")]
    assert store.get_pulse(pid) == before


def test_set_grid_sqlite_error_logs_one_failed_line(store, caplog, monkeypatch):
    pid, gid = _pulse(store), store.create_grid("g").id
    real = store._conn.execute

    def boom(sql, parameters=()):
        if sql.lstrip().upper().startswith("UPDATE"):
            raise sqlite3.OperationalError("boom")
        return real(sql, parameters)

    monkeypatch.setattr(store._conn, "execute", boom)
    caplog.clear()
    with pytest.raises(sqlite3.OperationalError):
        store.set_grid(pid, gid)
    assert _lines(caplog) == [
        ("ERROR", f"pulse.set_grid id={pid} grid={gid} FAILED err=boom")]


# --------------------------------------------------------------------------- #
# validation that runs before / around the write
# --------------------------------------------------------------------------- #
def test_set_title_empty_raises_value_error_before_the_pulse_lookup(store, caplog):
    caplog.clear()
    with pytest.raises(ValueError):
        store.set_title("nope", "   ")
    assert _lines(caplog) == [("ERROR", "pulse.set_title id=nope empty title")]


def test_set_model_empty_raises_value_error_before_the_pulse_lookup(store, caplog):
    caplog.clear()
    with pytest.raises(ValueError):
        store.set_model("nope", "")
    assert _lines(caplog) == [("ERROR", "pulse.set_model id=nope empty model")]


def test_set_model_without_vessel_keeps_the_old_vessel_in_the_log(store, caplog):
    pid = _pulse(store)
    caplog.clear()
    out = store.set_model(pid, "m9")
    assert (out.model, out.vessel) == ("m9", "v1")
    assert _lines(caplog) == [("INFO", f"pulse.set_model id={pid} m1/v1 -> m9/v1 ok")]


def test_set_inbox_score_invalid_value_on_known_item_raises_without_logging(
        store, caplog):
    iid = _inbox(store)
    caplog.clear()
    with pytest.raises(ValueError):
        store.set_inbox_score(iid, 7)
    assert _lines(caplog) == []
    assert store.get_inbox_item(iid).score is None


def test_set_inbox_score_unknown_item_beats_invalid_value(store, caplog):
    caplog.clear()
    with pytest.raises(UnknownInboxItem):
        store.set_inbox_score("nope", 7)
    assert _lines(caplog) == [("ERROR", "inbox.get id=nope NOT-FOUND")]


def test_set_inbox_score_none_clears(store):
    iid = _inbox(store)
    store.set_inbox_score(iid, 3)
    assert store.set_inbox_score(iid, None).score is None


def test_set_inbox_read_false_clears_read_at(store):
    iid = _inbox(store)
    assert store.set_inbox_read(iid, True).read_at is not None
    assert store.set_inbox_read(iid, False).read_at is None


# --------------------------------------------------------------------------- #
# shared inbox filters: list_inbox_items and read_all_inbox take the same ones
# --------------------------------------------------------------------------- #
def _filter_fixture(store):
    a = store.create_inbox_item("ch1", "pulse", "100% sure", "x", thread_key="t1",
                                needs_response=True)
    b = store.create_inbox_item("ch2", "pulse", "plain", "y_z")
    store.set_inbox_starred(b.id, True)
    return a, b


@pytest.mark.parametrize("kwargs,pick", [
    ({"channel": "ch1"}, "a"),
    ({"thread_key": "t1"}, "a"),
    ({"starred": True}, "b"),
    ({"needs_response": True}, "a"),
    ({"q": "100%"}, "a"),
    ({"q": "y_z"}, "b"),
    ({"q": "100_"}, None),
])
def test_read_all_inbox_applies_the_same_filters_as_list(store, kwargs, pick):
    a, b = _filter_fixture(store)
    items = {"a": a, "b": b}
    cutoff = "9999-01-01T00:00:00+00:00"
    listed = [i.id for i in store.list_inbox_items(**kwargs)]
    assert listed == ([items[pick].id] if pick else [])
    changed = store.read_all_inbox(cutoff, **kwargs)
    assert changed == len(listed)
    unread = {i.id for i in store.list_inbox_items(unread_only=True)}
    assert unread == {a.id, b.id} - set(listed)
