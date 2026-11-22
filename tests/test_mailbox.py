"""Unit tests for pulsar/daemon/mailbox.py.

    TZ=UTC venv/bin/python -m pytest tests/test_mailbox.py -q
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from pulsar.daemon import lifecycle, mailbox, paths, watch_sleep
from pulsar.store import Store


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    return root


@pytest.fixture()
def store(data_root):
    s = Store(str(data_root / "pulsar.db"))
    yield s
    s.close()


def _inbox_files(pid: str) -> list[Path]:
    d = paths.inbox_new(pid)
    return sorted(d.glob("*.json")) if d.is_dir() else []


def test_constants_and_new_name():
    assert mailbox.USER == ""
    assert mailbox.SCHED == "sched"
    assert mailbox.WATCH == "watch"
    assert mailbox.CONDWATCH == "condwatch"
    assert mailbox.HANDOFF == "handoff"
    assert mailbox.HANDOFF_SEED == "handoff-seed"
    assert mailbox.HANDOFF_WARN == "handoff-warn"
    assert mailbox.LOOPGUARD == "loopguard"

    plain = mailbox.new_name()
    assert re.match(r"^\d{8}T\d{12}Z-[0-9a-f]{8}\.json$", plain)
    sched = mailbox.new_name(mailbox.SCHED)
    assert re.match(r"^sched-\d{8}T\d{12}Z-[0-9a-f]{8}\.json$", sched)


def test_write_atomic_and_optional_fields(data_root):
    pid = "p-write"
    # source=None omits "source"
    name1 = mailbox.write(pid, "hello")
    d = paths.inbox_new(pid)
    assert list(d.glob("*.tmp")) == []
    body1 = json.loads((d / name1).read_text())
    assert set(body1.keys()) == {"text", "queued_at"}
    assert body1["text"] == "hello"

    # source and extra fields included when set; empty/None extras omitted
    name2 = mailbox.write(
        pid, "with extra", prefix=mailbox.SCHED, source="scheduler",
        extra={
            "display_text": "",
            "attachments": [{"filename": "a.txt"}],
            "reply_to": None,
        },
    )
    body2 = json.loads((d / name2).read_text())
    assert set(body2.keys()) == {"text", "queued_at", "source", "display_text", "attachments"}
    assert body2["source"] == "scheduler"
    assert body2["display_text"] == ""
    assert body2["attachments"] == [{"filename": "a.txt"}]


def test_write_supersede_and_cross_prefix(data_root):
    pid = "p-sup"
    w1 = mailbox.write(pid, "warn 1", prefix=mailbox.HANDOFF_WARN,
                       source="handoff", supersede=True)
    w2 = mailbox.write(pid, "warn 2", prefix=mailbox.HANDOFF_WARN,
                       source="handoff", supersede=True)
    s1 = mailbox.write(pid, "seed 1", prefix=mailbox.HANDOFF_SEED,
                       source="handoff", supersede=True)
    lg1 = mailbox.write(pid, "steer 1", prefix=mailbox.LOOPGUARD,
                        source="handoff", supersede=True)
    lg2 = mailbox.write(pid, "steer 2", prefix=mailbox.LOOPGUARD,
                        source="handoff", supersede=True)
    names = [f.name for f in _inbox_files(pid)]
    assert w1 not in names and w2 in names
    assert lg1 not in names and lg2 in names
    assert s1 in names

    # A "handoff" write with supersede=True unlinks f"handoff-*.json", which
    # also removes queued handoff-warn-* and handoff-seed-* files, while leaving
    # loopguard-* untouched.
    h1 = mailbox.write(pid, "req 1", prefix=mailbox.HANDOFF,
                       source="handoff", supersede=True)
    names_after = [f.name for f in _inbox_files(pid)]
    assert sorted(names_after) == sorted([h1, lg2])


def test_deliver_on_archived_modes(store, monkeypatch):
    events: list[str] = []

    def fake_wake(st, pulse):
        events.append(f"wake:{pulse.state}")
        st.set_state(pulse.id, "running")

    monkeypatch.setattr(lifecycle, "wake_if_stopped", fake_wake)

    # 1. skip: returns None, no remember, no wake, no file
    p_skip = store.create_pulse("skip", "m", "v", state="archived")
    res = mailbox.deliver(
        store, p_skip, "msg", on_archived="skip", remember=True,
        prefix=mailbox.WATCH, source="watch",
    )
    assert res is None
    assert events == []
    assert _inbox_files(p_skip.id) == []
    assert watch_sleep.KEY not in store.get_pulse(p_skip.id).settings

    # 2. raise: raises mailbox.Archived, no wake, no file
    p_raise = store.create_pulse("raise", "m", "v", state="archived")
    with pytest.raises(mailbox.Archived):
        mailbox.deliver(store, p_raise, "msg", on_archived="raise", source="pulse")
    assert events == []
    assert _inbox_files(p_raise.id) == []

    # 3. unarchive: remembers "archived" BEFORE unarchiving into watching mode, then wakes, then writes
    p_unarch = store.create_pulse("unarch", "m", "v", state="archived")
    fname = mailbox.deliver(
        store, p_unarch, "cond msg", on_archived="unarchive", remember=True,
        prefix=mailbox.CONDWATCH, source="watch",
    )
    assert fname is not None and fname.startswith("condwatch-")
    after = store.get_pulse(p_unarch.id)
    assert after.settings["mode"] == "watching"
    saved = after.settings[watch_sleep.KEY]
    assert saved == {"state": "archived", "message": fname}
    assert events == ["wake:stopped"]
    assert [f.name for f in _inbox_files(p_unarch.id)] == [fname]


def test_deliver_write_first_ordering(store, monkeypatch):
    order: list[str] = []
    p = store.create_pulse("watching-p", "m", "v", state="stopped")

    def fake_wake(st, pulse):
        # At wake time, verify whether the file is already on disk and whether
        # watch_sleep.remember has already recorded it.
        files = _inbox_files(pulse.id)
        saved = st.get_pulse(pulse.id).settings.get(watch_sleep.KEY)
        order.append(f"wake(files={len(files)},remembered={bool(saved)})")

    monkeypatch.setattr(lifecycle, "wake_if_stopped", fake_wake)

    # Default (write_first=False): wake runs before write
    mailbox.deliver(store, p, "normal", on_archived="skip",
                    prefix=mailbox.SCHED, source="scheduler")
    assert order == ["wake(files=0,remembered=False)"]
    for f in _inbox_files(p.id):
        f.unlink()
    order.clear()

    # write_first=True + remember=True: write and remember run before wake
    fname = mailbox.deliver(
        store, p, "watching", on_archived="skip", remember=True,
        write_first=True, prefix=mailbox.SCHED, source="scheduler",
    )
    assert order == ["wake(files=1,remembered=True)"]
    assert store.get_pulse(p.id).settings[watch_sleep.KEY] == {
        "state": "stopped", "message": fname,
    }
