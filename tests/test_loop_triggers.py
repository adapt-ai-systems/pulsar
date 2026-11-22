"""Loop triggers (v25): a loop fires on timer / handoff / message.

    venv/bin/python -m pytest tests -k loop_triggers -q
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import loop_triggers, paths
from pulsar.daemon import scheduler as sched_mod
from pulsar.daemon.app import create_app
from pulsar.store import Store


@pytest.fixture()
def anyio_backend():
    return "asyncio"


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    s = Store(str(root / "pulsar.db"))
    yield s
    s.close()


def _pulse(store):
    return store.create_pulse("T", "claude-opus-5-5", "claude-code")


def test_parse_and_normalize():
    assert loop_triggers.parse(None) == {"timer"}
    assert loop_triggers.parse("") == {"timer"}
    assert loop_triggers.parse("junk") == {"timer"}
    assert loop_triggers.parse("message,handoff") == {"message", "handoff"}
    assert loop_triggers.normalize(None, "loop") is None
    assert loop_triggers.normalize(["message", "timer"], "loop") == "message,timer"
    assert loop_triggers.normalize(["tools:20", "replies:5"], "loop") == "replies:5,tools:20"
    assert loop_triggers.parse("replies:5,tools:20") == {"replies", "tools"}
    assert loop_triggers.every("replies:5,tools:20", "tools") == 20
    assert loop_triggers.every("timer", "tools") is None
    assert loop_triggers.as_list("tools:20,timer") == ["timer", "tools:20"]
    assert loop_triggers.as_list(None) == ["timer"]
    with pytest.raises(ValueError):
        loop_triggers.normalize(["spawn"], "loop")
    with pytest.raises(ValueError):
        loop_triggers.normalize(["replies:0"], "loop")
    with pytest.raises(ValueError):
        loop_triggers.normalize([], "loop")
    with pytest.raises(ValueError):
        loop_triggers.normalize(["timer"], "once")


def test_store_roundtrip_and_default(store):
    p = _pulse(store)
    old = store.create_schedule(p.id, "loop", "tick", interval_minutes=5)
    assert old.triggers is None and loop_triggers.parse(old.triggers) == {"timer"}
    s = store.create_schedule(p.id, "loop", "rules", interval_minutes=30,
                              triggers="handoff,message")
    assert store.get_schedule(s.id).triggers == "handoff,message"
    s2 = store.update_schedule(s.id, triggers="message")
    assert s2.triggers == "message" and s2.prompt == "rules"


def test_message_only_loop_never_due_by_clock(store):
    p = _pulse(store)
    s = store.create_schedule(p.id, "loop", "rules", interval_minutes=1,
                              triggers="message")
    t = store.create_schedule(p.id, "loop", "tick", interval_minutes=1,
                              triggers="message,timer")
    later = datetime.now(timezone.utc) + timedelta(hours=1)
    assert sched_mod.is_due(s, later) is False
    assert sched_mod.is_due(t, later) is True


def test_blocks(store):
    p = _pulse(store)
    assert loop_triggers.append_to_message(store, p.id, "hi") == "hi"
    assert loop_triggers.handoff_block(store, p.id) == ""
    store.create_schedule(p.id, "loop", "Fast bench. No soaks.", interval_minutes=30,
                          triggers="handoff,message,timer")
    off = store.create_schedule(p.id, "loop", "DISABLED", interval_minutes=30,
                                triggers="message")
    store.set_schedule_enabled(off.id, False)
    out = loop_triggers.append_to_message(store, p.id, "do deepseek")
    assert out.startswith("do deepseek\n\n---\nStanding loop (every 30 min, every message, every handoff):\nFast bench. No soaks.")
    assert "DISABLED" not in out
    hb = loop_triggers.handoff_block(store, p.id)
    assert "Fast bench. No soaks." in hb and "bind you" in hb


@pytest.mark.anyio
async def test_endpoints(store, monkeypatch):
    monkeypatch.setattr(units_mod, "spawn_runner", lambda *a, **k: None)
    app = create_app(store)
    p = _pulse(store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://t") as c, \
            app.router.lifespan_context(app):
        r = await c.post(f"/pulses/{p.id}/schedules", json={
            "kind": "loop", "prompt": "Rules here", "interval_minutes": 30,
            "triggers": ["message", "timer"]})
        assert r.status_code == 201, r.text
        sid = r.json()["value"]["id"]
        assert r.json()["value"]["triggers"] == ["message", "timer"]
        assert r.json()["value"]["trigger_mode"] == "any"
        r = await c.patch(f"/schedules/{sid}", json={"trigger_mode": "all"})
        assert r.status_code == 200 and r.json()["value"]["trigger_mode"] == "all"
        r = await c.post(f"/pulses/{p.id}/schedules", json={
            "kind": "loop", "prompt": "x", "interval_minutes": 30,
            "triggers": ["spawn"]})
        assert r.status_code == 400
        r = await c.patch(f"/schedules/{sid}", json={"triggers": ["handoff"]})
        assert r.status_code == 200 and r.json()["value"]["triggers"] == ["handoff"]
        r = await c.patch(f"/schedules/{sid}", json={"triggers": ["message"]})
        assert r.status_code == 200
        # The user's message carries the block; the pane shows only their text.
        r = await c.post(f"/pulses/{p.id}/messages", json={"text": "go deepseek"})
        assert r.status_code == 202, r.text
        name = r.json()["value"]["queued"]
        msg = json.loads((paths.pulse_home(p.id) / "inbox" / "new" / name).read_text())
        assert msg["text"].startswith("go deepseek\n\n---\nStanding loop (every message):\nRules here")
        assert msg["display_text"] == "go deepseek"
        # A pulse-to-pulse message does not get the block.
        q = store.create_pulse("Sender", "claude-opus-5-5", "claude-code")
        r = await c.post(f"/pulses/{p.id}/messages",
                         json={"text": "peer", "from_pulse_id": q.id})
        assert r.status_code == 202, r.text
        name = r.json()["value"]["queued"]
        msg = json.loads((paths.pulse_home(p.id) / "inbox" / "new" / name).read_text())
        assert "Standing loop" not in msg["text"]


def _beat(pulse_id, replies, tools):
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    (home / "heartbeat.json").write_text(json.dumps({
        "ts": "2026-10-26T00:00:00+0000", "state": "idle", "protocol": 1,
        "replies_total": replies, "tool_calls_total": tools}))


def _due(store, s, now=None):
    now = now or datetime.now(timezone.utc)
    return loop_triggers.loop_due(store, store.get_schedule(s.id), now, {}, sched_mod.is_due)


def test_loop_due_counters_any(store):
    p = _pulse(store)
    s = store.create_schedule(p.id, "loop", "rules", interval_minutes=600,
                              triggers="replies:3,timer,tools:10")
    assert _due(store, s) is False                       # no heartbeat yet
    _beat(p.id, 100, 1000)
    assert _due(store, s) is False                       # first sight = baseline
    assert json.loads(store.get_schedule(s.id).trigger_state) == {"replies": 100, "tools": 1000}
    _beat(p.id, 102, 1005)
    assert _due(store, s) is False
    _beat(p.id, 103, 1005)
    assert _due(store, s) is True                        # 3 replies -> any
    _beat(p.id, 1, 2)                                    # new session: re-baseline
    assert _due(store, s) is False
    assert json.loads(store.get_schedule(s.id).trigger_state) == {"replies": 1, "tools": 2}
    # timer alone also fires in any mode
    assert _due(store, s, datetime.now(timezone.utc) + timedelta(hours=11)) is True
    # message-only loop is never due here
    m = store.create_schedule(p.id, "loop", "x", interval_minutes=1, triggers="message")
    assert _due(store, m, datetime.now(timezone.utc) + timedelta(hours=11)) is False


def test_loop_due_all_mode(store):
    p = _pulse(store)
    s = store.create_schedule(p.id, "loop", "rules", interval_minutes=40,
                              triggers="timer,tools:10", trigger_mode="all")
    _beat(p.id, 0, 0)
    assert _due(store, s) is False
    _beat(p.id, 0, 50)
    assert _due(store, s) is False                       # tools met, timer not
    late = datetime.now(timezone.utc) + timedelta(hours=1)
    assert _due(store, s, late) is True                  # both met
    assert loop_triggers.normalize_mode("ALL", "loop") == "all"
    with pytest.raises(ValueError):
        loop_triggers.normalize_mode("maybe", "loop")


def test_tick_fires_counter_loop(store, monkeypatch):
    monkeypatch.setattr(units_mod, "spawn_runner", lambda *a, **k: None)
    monkeypatch.setattr(units_mod, "list_pulse_units", lambda: [])
    p = _pulse(store)
    s = store.create_schedule(p.id, "loop", "RULES", interval_minutes=600,
                              triggers="replies:2")
    _beat(p.id, 10, 0)
    now = datetime.now(timezone.utc)
    assert sched_mod.tick(store, now)["fired"] == 0       # baseline
    _beat(p.id, 12, 0)
    assert sched_mod.tick(store, now)["fired"] == 1
    files = list((paths.pulse_home(p.id) / "inbox" / "new").glob("sched-*.json"))
    assert len(files) == 1 and "RULES" in files[0].read_text()
    assert sched_mod.tick(store, now)["fired"] == 0       # not again until +2
    # A counter fire pushed the timer out: last_fired_at is now, and the
    # counter baseline moved to the fired value.
    s2 = store.get_schedule(s.id)
    assert s2.last_fired_at is not None
    assert json.loads(s2.trigger_state) == {"replies": 12}
    # Timer fire resets the counter baseline too (any mode, shared reset).
    store.set_schedule_enabled(s.id, False)
    t = store.create_schedule(p.id, "loop", "T", interval_minutes=1,
                              triggers="timer,replies:100")
    _beat(p.id, 20, 0)
    sched_mod.tick(store, now)                            # baseline 20
    later = now + timedelta(minutes=5)
    _beat(p.id, 25, 0)
    assert sched_mod.tick(store, later)["fired"] == 1    # timer
    assert json.loads(store.get_schedule(t.id).trigger_state) == {"replies": 25}
