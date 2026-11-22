"""Manual handoff endpoint (POST /pulses/{id}/handoff) contract tests.

httpx ASGI client + fake spawn/socket, exactly like test_daemon.py. The
manual handoff queues ``handoff.run_handoff`` off the request path as a
background task, so tests assert the immediate response shape plus that
run_handoff was invoked (monkeypatched) with the right reason — not the full
handoff flow (which needs a live session to reply).
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

import pulsar.launch.diet as diet_mod
import pulsar.launch.units as units_mod
from pulsar.store import Store
from pulsar.daemon.app import create_app
from pulsar.daemon import paths
from pulsar.daemon import sockclient as sockclient_mod
import pulsar.daemon.handoff as handoff_mod


# --------------------------------------------------------------------------- #
# Fakes (mirror test_daemon)
# --------------------------------------------------------------------------- #
def _write_heartbeat(pulse_id: str) -> None:
    import time
    from datetime import datetime, timezone
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(time.time(), tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    beat = {"ts": ts, "pid": 1234, "cli_pid": 4242, "state": "idle",
            "cli_session_id": f"sess-{pulse_id}", "protocol": 1}
    (home / "heartbeat.json").write_text(json.dumps(beat))


class _SockErr(Exception):
    pass


class FakeSpawn:
    def __init__(self):
        self.stopped = []
        self.units = []

    def spawn_runner(self, pulse_id, home, model, vessel, resume=None,
                     memory_max="6G", tasks_max=512, diet=None, title=None,
                     effort=None, fast=False):
        diet_mod.materialize_diet(pulse_id, home, diet)
        _write_heartbeat(pulse_id)

    def stop_unit(self, pulse_id):
        self.stopped.append(pulse_id)
        self.units = [u for u in self.units if u != pulse_id]
        return True

    def list_pulse_units(self):
        return list(self.units)

    def fence_report(self, pulse_id):
        return {"result": "success", "oom": False, "detail": {}}


class FakeSock:
    def __init__(self):
        self.stopped = []
        self.SocketError = _SockErr

    def stop(self, sock_path, pulse_id):
        self.stopped.append(pulse_id)
        return {"v": 1, "ok": True}

    def status(self, sock_path, pulse_id):
        return {"v": 1, "ok": True, "state": "idle",
                "cli_pid": 4242, "cli_session_id": f"sess-{pulse_id}"}

    def probe(self, sock_path, pulse_id):
        return {"v": 1, "ok": True, "state": "idle",
                "cli_pid": 4242, "cli_session_id": f"sess-{pulse_id}"}


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    return root


@pytest.fixture()
def fake_spawn(data_root, monkeypatch):
    fs = FakeSpawn()
    monkeypatch.setattr(units_mod, "spawn_runner", fs.spawn_runner)
    monkeypatch.setattr(units_mod, "stop_unit", fs.stop_unit)
    monkeypatch.setattr(units_mod, "list_pulse_units", fs.list_pulse_units)
    monkeypatch.setattr(units_mod, "fence_report", fs.fence_report)
    return fs


@pytest.fixture()
def fake_sock(monkeypatch):
    fk = FakeSock()
    monkeypatch.setattr(sockclient_mod, "stop", fk.stop)
    monkeypatch.setattr(sockclient_mod, "status", fk.status)
    monkeypatch.setattr(sockclient_mod, "probe", fk.probe)
    monkeypatch.setattr(sockclient_mod, "SocketError", _SockErr)
    return fk


@pytest.fixture()
def store(data_root):
    s = Store(str(data_root / "pulsar.db"))
    yield s
    s.close()


@pytest.fixture()
async def client(store, fake_spawn, fake_sock):
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as c:
        async with app.router.lifespan_context(app):
            yield c


async def _mk_pulse(client, store, state="running"):
    """Create a pulse then force it into the requested state."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    assert r.status_code in (200, 201), r.text
    pid = r.json()["value"]["id"]
    if state == "archived":
        ar = await client.post(f"/pulses/{pid}/archive")
        assert ar.status_code == 200, ar.text
    elif state == "stopped":
        pr = await client.post(f"/pulses/{pid}/pause")
        assert pr.status_code == 200, pr.text
    assert store.get_pulse(pid).state == state
    return pid


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_handoff_archived_refused(client, store, fake_spawn, fake_sock):
    pid = await _mk_pulse(client, store, state="archived")
    r = await client.post(f"/pulses/{pid}/handoff")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "bad_state"
    assert "archived" in r.json()["error"]["message"]


@pytest.mark.anyio
async def test_handoff_stopped_refused(client, store, fake_spawn, fake_sock):
    pid = await _mk_pulse(client, store, state="stopped")
    r = await client.post(f"/pulses/{pid}/handoff")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "not_running"
    assert "not running" in r.json()["error"]["message"]


@pytest.mark.anyio
async def test_handoff_stopped_forced_queues_engine(client, store, fake_spawn,
                                                    fake_sock, monkeypatch):
    """{"force": true} on a dead pulse skips the refusal and runs the engine
    with force=True / reason="forced"."""
    pid = await _mk_pulse(client, store, state="stopped")
    calls = []
    started = asyncio.Event()

    async def fake_run_handoff(st, pulse, *, new_model=None, new_vessel=None,
                               reason="context_threshold", fallback_reply=None,
                               reserved=False, force=False):
        calls.append({"pid": pulse.id, "reason": reason, "force": force})
        started.set()
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_run_handoff)
    r = await client.post(f"/pulses/{pid}/handoff", json={"force": True})
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"started": True, "forced": True}
    await asyncio.wait_for(started.wait(), timeout=5)
    assert calls == [{"pid": pid, "reason": "forced", "force": True}]


@pytest.mark.anyio
async def test_handoff_running_queues_engine(client, store, fake_spawn,
                                             fake_sock, monkeypatch):
    """A running pulse gets an immediate ``{started:true}`` and run_handoff is
    invoked off the request path with reason="manual"."""
    pid = await _mk_pulse(client, store, state="running")

    calls = []
    started = asyncio.Event()

    async def fake_run_handoff(st, pulse, *, new_model=None, new_vessel=None,
                               reason="context_threshold", fallback_reply=None,
                               reserved=False, force=False):
        calls.append({"pid": pulse.id, "reason": reason})
        # Mirror the real engine's durable in-flight marker so the "requested"
        # handoff_state.json appears (as the brief requires asserting on).
        handoff_mod.state.write_state(pulse.id, "requested", reason=reason)
        started.set()
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_run_handoff)

    r = await client.post(f"/pulses/{pid}/handoff")
    assert r.status_code == 200, r.text
    assert r.json() == {"ok": True, "value": {"started": True, "forced": False}}

    # Let the background task run to completion.
    await asyncio.wait_for(started.wait(), timeout=5)
    assert len(calls) == 1
    assert calls[0]["pid"] == pid
    assert calls[0]["reason"] == "manual"

    marker = paths.pulse_home(pid) / "handoff_state.json"
    assert marker.exists()
    data = json.loads(marker.read_text())
    assert data["phase"] == "requested"


@pytest.mark.anyio
async def test_handoff_single_flight_409(client, store, fake_spawn, fake_sock,
                                         monkeypatch):
    """A durable in-flight marker on a running pulse refuses a second manual
    handoff with 409 handoff_in_flight."""
    pid = await _mk_pulse(client, store, state="running")
    await asyncio.to_thread(handoff_mod.state.write_state, pid, "requested",
                            reason="manual")

    async def fake_run_handoff(*args, **kwargs):
        raise AssertionError("run_handoff should not be called when in flight")

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_run_handoff)

    r = await client.post(f"/pulses/{pid}/handoff")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "handoff_in_flight"


@pytest.mark.anyio
async def test_handoff_second_while_reserved_409(client, store, fake_spawn,
                                                 fake_sock, monkeypatch):
    """A second manual handoff right after the first gets 409: the endpoint
    reserves the in-flight marker synchronously, before the background task
    even runs. (Before the fix the marker only appeared inside the
    background run_handoff, so the second request raced past the check.)"""
    pid = await _mk_pulse(client, store, state="running")

    started = asyncio.Event()

    async def fake_run_handoff(st, pulse, *, new_model=None, new_vessel=None,
                               reason="context_threshold", fallback_reply=None,
                               reserved=False, force=False):
        started.set()
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_run_handoff)

    r1 = await client.post(f"/pulses/{pid}/handoff")
    assert r1.status_code == 200, r1.text
    assert r1.json() == {"ok": True, "value": {"started": True, "forced": False}}

    # The marker must now exist even though the task has not run yet...
    marker = paths.pulse_home(pid) / "handoff_state.json"
    assert marker.exists()
    assert json.loads(marker.read_text())["phase"] == "requested"
    # ...so a sequential second request is refused immediately.
    r2 = await client.post(f"/pulses/{pid}/handoff")
    assert r2.status_code == 409, r2.text
    assert r2.json()["error"]["code"] == "handoff_in_flight"

    await asyncio.wait_for(started.wait(), timeout=5)


@pytest.mark.anyio
async def test_handoff_note_path(client, store, fake_spawn, fake_sock,
                                 monkeypatch, tmp_path):
    """{"note_path": ...} seeds from the pulse's own file: a short or missing
    file is a 400; a good one queues the engine with note_text."""
    pid = await _mk_pulse(client, store, state="running")
    calls = []
    started = asyncio.Event()

    async def fake_run_handoff(st, pulse, **kw):
        calls.append(kw)
        started.set()
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_run_handoff)
    short = tmp_path / "short.md"
    short.write_text("done")
    r = await client.post(f"/pulses/{pid}/handoff",
                          json={"note_path": str(short)})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_note"
    r = await client.post(f"/pulses/{pid}/handoff",
                          json={"note_path": str(short), "force": True})
    assert r.status_code == 400

    note = tmp_path / "next.md"
    note.write_text("1. work\n2. state\n3. next\n4. facts\n" * 20)
    r = await client.post(f"/pulses/{pid}/handoff",
                          json={"note_path": str(note)})
    assert r.status_code == 200, r.text
    await asyncio.wait_for(started.wait(), timeout=5)
    assert calls[0]["reason"] == "note_file"
    assert calls[0]["note_text"] == note.read_text().strip()
