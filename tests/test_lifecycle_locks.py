"""Blocking stop/spawn calls run in worker threads under a per-pulse lock.

    TZ=UTC venv/bin/python -m pytest tests/test_lifecycle_locks.py -q

Latency (the loop stays free), stop-failure mapping, deterministic overlap of
two operations on one pulse, the lock's guards, the scheduler's cross-pulse
send, delete and archive against held operations.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import lifecycle, paths, sockclient
from pulsar.daemon import scheduler as scheduler_mod
# Shared fixtures: Spy fakes, store, ASGI client.
from tests.test_lifecycle_sites import (  # noqa: F401
    client, data_root, spy, store, _create, _write_heartbeat)

pytestmark = pytest.mark.anyio


def _mk(store, title, state="running"):
    p = store.create_pulse(title, "claude-opus-5", "claude-code", state=state)
    _write_heartbeat(p.id, "idle" if state == "running" else "stopped",
                     f"sess-{p.id}")
    return p


def _inbox_files(pid):
    d = paths.inbox_new(pid)
    return sorted(d.glob("*.json")) if d.is_dir() else []


async def _until(event: threading.Event, timeout: float = 5.0) -> None:
    """Wait (without blocking the loop) for a worker thread to set ``event``."""
    end = time.monotonic() + timeout
    while not event.is_set():
        assert time.monotonic() < end, "worker never reached the held call"
        await asyncio.sleep(0.01)


def _hold_stop(monkeypatch, spy):
    """Make the socket stop wait on a gate; returns (entered, gate)."""
    entered, gate = threading.Event(), threading.Event()
    orig = spy.stop

    def held(sock_path, pulse_id):
        entered.set()
        assert gate.wait(10)
        return orig(sock_path, pulse_id)
    monkeypatch.setattr(sockclient, "stop", held)
    return entered, gate


def _hold_spawn(monkeypatch, spy):
    """Make spawn_runner wait on a gate; returns (entered, gate)."""
    entered, gate = threading.Event(), threading.Event()
    orig = spy.spawn_runner

    def held(*args, **kwargs):
        entered.set()
        assert gate.wait(10)
        return orig(*args, **kwargs)
    monkeypatch.setattr(units_mod, "spawn_runner", held)
    return entered, gate


# --------------------------------------------------------------------------- #
# 1. Latency: the event loop stays free
# --------------------------------------------------------------------------- #
async def test_slow_stop_during_pause_does_not_block_loop(
        client, store, spy, monkeypatch):
    pid = await _create(client)
    orig = spy.stop

    def slow_stop(sock_path, pulse_id):
        time.sleep(0.5)
        return orig(sock_path, pulse_id)
    monkeypatch.setattr(sockclient, "stop", slow_stop)

    pause = asyncio.ensure_future(client.post(f"/pulses/{pid}/pause"))
    await asyncio.sleep(0.05)
    t = time.monotonic()
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert time.monotonic() - t < 0.25
    assert (await pause).status_code == 200


async def test_slow_spawn_during_message_does_not_block_loop(
        client, store, spy, monkeypatch):
    a, b = _mk(store, "a"), _mk(store, "b", state="stopped")
    orig = spy.spawn_runner

    def slow_spawn(*args, **kwargs):
        time.sleep(0.5)
        return orig(*args, **kwargs)
    monkeypatch.setattr(units_mod, "spawn_runner", slow_spawn)

    send = asyncio.ensure_future(client.post(
        f"/pulses/{b.id}/messages",
        json={"text": "wake up", "from_pulse_id": a.id}))
    await asyncio.sleep(0.05)
    t = time.monotonic()
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert time.monotonic() - t < 0.25
    assert (await send).status_code == 202
    assert store.get_pulse(b.id).state == "running"


# --------------------------------------------------------------------------- #
# 2. Stop failure on the single routes
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("route", ["pause", "reset", "switch", "archive",
                                   "archive_keep"])
async def test_stop_failure_is_502_stop_failed(client, store, spy, route):
    pid = await _create(client)
    if route == "archive_keep":
        store.create_schedule(pid, "loop", "ping", interval_minutes=5)
    spy.sock_fails = True
    spy.unit_fails = True
    if route == "pause":
        r = await client.post(f"/pulses/{pid}/pause")
    elif route == "reset":
        r = await client.post(f"/pulses/{pid}/reset")
    elif route == "switch":
        r = await client.patch(f"/pulses/{pid}/model",
                               json={"model": "claude-fable-5"})
    elif route == "archive":
        r = await client.post(f"/pulses/{pid}/archive")
    else:
        r = await client.post(f"/pulses/{pid}/archive",
                              json={"monitoring": "keep"})
    assert r.status_code == 502, r.text
    assert r.json()["error"]["code"] == "stop_failed"
    assert store.get_pulse(pid).state == "running"


# --------------------------------------------------------------------------- #
# 3. Archive-idle keeps its batch contract
# --------------------------------------------------------------------------- #
async def test_archive_idle_continues_past_a_stop_failure(
        client, store, spy, monkeypatch):
    bad, good = _mk(store, "bad"), _mk(store, "good")
    orig_stop = spy.stop

    def stop(sock_path, pulse_id):
        if pulse_id == bad.id:
            raise sockclient.SocketError("down (test)")
        return orig_stop(sock_path, pulse_id)
    monkeypatch.setattr(sockclient, "stop", stop)
    monkeypatch.setattr(units_mod, "stop_unit",
                        lambda pid: pid != bad.id)

    r = await client.post("/pulses/archive-idle", json={"idle_hours": 1e-9})
    assert r.status_code == 200, r.text
    v = r.json()["value"]
    assert v["skipped"] == [{
        "id": bad.id, "title": "bad",
        "error": f"could not stop runner for {bad.id}"}]
    assert [x["id"] for x in v["archived"]] == [good.id]
    assert store.get_pulse(bad.id).state == "running"
    assert store.get_pulse(good.id).state == "archived"


# --------------------------------------------------------------------------- #
# 4. Overlap, deterministic
# --------------------------------------------------------------------------- #
async def test_two_sends_to_a_stopped_pulse_spawn_once(
        client, store, spy, monkeypatch):
    a, b = _mk(store, "a"), _mk(store, "b", state="stopped")
    entered, gate = _hold_spawn(monkeypatch, spy)
    sends = [asyncio.ensure_future(client.post(
        f"/pulses/{b.id}/messages",
        json={"text": f"m{i}", "from_pulse_id": a.id})) for i in range(2)]
    await _until(entered)
    await asyncio.sleep(0.1)          # the second send is parked on the lock
    assert not any(s.done() for s in sends)
    gate.set()
    for s in sends:
        assert (await s).status_code == 202
    assert len(spy.calls) == 1
    assert len(_inbox_files(b.id)) == 2
    assert store.get_pulse(b.id).state == "running"


async def test_send_during_held_pause_ends_as_pause_then_send(
        client, store, spy, monkeypatch):
    a, b = _mk(store, "a"), _mk(store, "b")
    entered, gate = _hold_stop(monkeypatch, spy)
    pause = asyncio.ensure_future(client.post(f"/pulses/{b.id}/pause"))
    await _until(entered)
    send = asyncio.ensure_future(client.post(
        f"/pulses/{b.id}/messages",
        json={"text": "hello", "from_pulse_id": a.id}))
    await asyncio.sleep(0.1)
    assert not send.done()            # parked behind the pause
    gate.set()
    assert (await pause).status_code == 200
    assert (await send).status_code == 202
    # Serial order: paused first, then the send woke the pulse.
    assert spy.sock_stopped == [b.id]
    assert len(spy.calls) == 1
    assert len(_inbox_files(b.id)) == 1
    assert store.get_pulse(b.id).state == "running"


# --------------------------------------------------------------------------- #
# 5. Lock guards
# --------------------------------------------------------------------------- #
async def test_pulse_lock_refuses_the_event_loop_thread():
    with pytest.raises(RuntimeError, match="event loop"):
        with lifecycle.pulse_lock("p-loop"):
            pass


async def test_pulse_lock_reentry_raises_and_does_not_hang():
    def reenter():
        with lifecycle.pulse_lock("p-re"):
            with lifecycle.pulse_lock("p-re"):
                pass

    with pytest.raises(RuntimeError, match="re-entry"):
        await asyncio.wait_for(asyncio.to_thread(reenter), 5)

    def again():                       # the failed attempt released the lock
        with lifecycle.pulse_lock("p-re"):
            return True
    assert await asyncio.wait_for(asyncio.to_thread(again), 5)


# --------------------------------------------------------------------------- #
# 6. Scheduler cross-pulse send
# --------------------------------------------------------------------------- #
async def test_scheduler_cross_pulse_send_takes_the_target_lock(store, spy):
    a, b = _mk(store, "worker"), _mk(store, "advisor", state="stopped")
    store.create_schedule(a.id, "loop", "hourly check-in",
                          interval_minutes=1, target_pulse_id=b.id)
    now = datetime.now(timezone.utc) + timedelta(minutes=2)
    summary = await asyncio.to_thread(scheduler_mod.tick, store, now=now)
    assert summary["fired"] == 1
    files = _inbox_files(b.id)
    assert len(files) == 1
    assert "hourly check-in" in json.loads(files[0].read_text())["text"]
    assert store.get_pulse(b.id).state == "running"


# --------------------------------------------------------------------------- #
# 7. Delete against a held pause or wake
# --------------------------------------------------------------------------- #
async def test_delete_during_held_pause_runs_after_it(
        client, store, spy, monkeypatch):
    p = _mk(store, "victim")
    entered, gate = _hold_stop(monkeypatch, spy)
    pause = asyncio.ensure_future(client.post(f"/pulses/{p.id}/pause"))
    await _until(entered)
    delete = asyncio.ensure_future(client.delete(f"/pulses/{p.id}"))
    await asyncio.sleep(0.1)
    assert not delete.done()
    gate.set()
    assert (await pause).status_code == 200
    r = await delete
    assert r.status_code == 200, r.text         # serial order: pause, delete
    assert r.json()["value"]["deleted"] == p.id
    assert not paths.pulse_home(p.id).exists()


async def test_delete_during_held_wake_is_refused_and_keeps_the_home(
        client, store, spy, monkeypatch):
    a, b = _mk(store, "a"), _mk(store, "b", state="stopped")
    entered, gate = _hold_spawn(monkeypatch, spy)
    send = asyncio.ensure_future(client.post(
        f"/pulses/{b.id}/messages",
        json={"text": "wake", "from_pulse_id": a.id}))
    await _until(entered)
    delete = asyncio.ensure_future(client.delete(f"/pulses/{b.id}"))
    await asyncio.sleep(0.1)
    assert not delete.done()
    gate.set()
    assert (await send).status_code == 202
    r = await delete
    assert r.status_code == 409, r.text         # it is running now
    assert r.json()["error"]["code"] == "bad_state"
    assert paths.pulse_home(b.id).is_dir()      # not moved from under the wake
    assert store.get_pulse(b.id).state == "running"


# --------------------------------------------------------------------------- #
# 8. Archive against a held handoff spawn
# --------------------------------------------------------------------------- #
async def test_archive_during_held_handoff_spawn(
        client, store, spy, monkeypatch):
    p = _mk(store, "h")
    entered, gate = _hold_spawn(monkeypatch, spy)
    handoff = asyncio.ensure_future(handoff_mod.engine.run_handoff(
        store, p, force=True, reason="model_switch"))
    await _until(entered)
    archive = asyncio.ensure_future(client.post(f"/pulses/{p.id}/archive"))
    await asyncio.sleep(0.1)
    assert not archive.done()         # waits for the swap closure to finish
    gate.set()
    assert await handoff is True
    r = await archive
    assert r.status_code == 200, r.text
    # The archive ran after the swap and stopped the successor normally.
    assert store.get_pulse(p.id).state == "archived"
    assert spy.sock_stopped == [p.id, p.id]   # old runner, then the successor
    assert handoff_mod.read_state(p.id)["phase"] == "done"
