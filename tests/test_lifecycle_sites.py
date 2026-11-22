"""Characterization of every place that starts or stops a pulse's runner.

    TZ=UTC venv/bin/python -m pytest tests/test_lifecycle_sites.py -q

Pins, per spawn site (create, restore, reset, fork, switch, scheduler wake,
handoff): the exact ``spawn_runner`` args and kwargs, and what the site does
when the spawn fails and when the failure cleanup itself fails. The three stop
paths (socket, unit fallback, both fail) are pinned too. These tests describe
today's behaviour, including the differences between sites on purpose (write
order and caught exception class in the cleanup); the lifecycle refactor must
keep them green.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone

import httpx
import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import lifecycle
from pulsar.daemon import paths
from pulsar.daemon import sockclient
from pulsar.daemon.app import create_app
from pulsar.store import Store
from pulsar.store.db import StoreError
from pulsar.vessel.claude import session_history_file

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class _SockErr(Exception):
    pass


class Spy:
    """Records every spawn_runner call in full; stands in for spawn + socket."""

    def __init__(self):
        self.calls: list[tuple[tuple, dict]] = []
        self.spawn_fails = False
        self.sock_fails = False
        self.unit_fails = False
        self.sock_stopped: list[str] = []
        self.unit_stopped: list[str] = []

    def spawn_runner(self, *args, **kwargs):
        self.calls.append((args, kwargs))
        if self.spawn_fails:
            raise RuntimeError("systemd-run failed (test)")
        pid = args[0]
        _write_heartbeat(pid, "idle", f"sess-{pid}")

    def stop(self, sock_path, pulse_id):
        if self.sock_fails:
            raise _SockErr("socket down (test)")
        self.sock_stopped.append(pulse_id)
        _write_heartbeat(pulse_id, "stopped", f"sess-{pulse_id}")
        return {"v": 1, "ok": True}

    def stop_unit(self, pulse_id):
        self.unit_stopped.append(pulse_id)
        return not self.unit_fails


def _write_heartbeat(pulse_id: str, state: str, sid: str) -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    (home / "heartbeat.json").write_text(json.dumps({
        "ts": ts, "pid": 1, "cli_pid": 2, "state": state,
        "cli_session_id": sid, "protocol": 1}))


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


@pytest.fixture()
def spy(data_root, monkeypatch):
    sp = Spy()
    # The shared modules: every daemon module reaches them by attribute.
    monkeypatch.setattr(units_mod, "spawn_runner", sp.spawn_runner)
    monkeypatch.setattr(units_mod, "stop_unit", sp.stop_unit)
    monkeypatch.setattr(units_mod, "list_pulse_units", lambda: [])
    monkeypatch.setattr(units_mod, "unit_active", lambda pid: False)
    monkeypatch.setattr(units_mod, "fence_report", lambda pid: {
        "result": "success", "oom": False, "detail": {}})
    monkeypatch.setattr(sockclient, "stop", sp.stop)
    monkeypatch.setattr(sockclient, "SocketError", _SockErr)
    monkeypatch.setattr(handoff_mod.state, "STOP_SETTLE_S", 1.0)
    return sp


@pytest.fixture()
async def client(store, spy):
    """ASGI client; an unhandled server error comes back as a 500, not a raise."""
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c:
        async with app.router.lifespan_context(app):
            yield c


def _inject(store, monkeypatch, method, exc, pred):
    """Make ``store.<method>`` raise ``exc`` whenever ``pred(args)`` is true."""
    orig = getattr(store, method)

    def wrapper(*args, **kwargs):
        if pred(args, kwargs):
            raise exc
        return orig(*args, **kwargs)
    monkeypatch.setattr(store, method, wrapper)


def _is_reason_write(args, kwargs):
    patch = args[1] if len(args) > 1 else kwargs.get("settings") or {}
    return bool(patch.get("stop_reason"))


def _is_stop_write(args, kwargs):
    return (args[1] if len(args) > 1 else kwargs.get("state")) == "stopped"


def _spawn_pin(spy, pid, **expect):
    """Assert the last spawn call: positional (pid, home, model, vessel) and
    the keyword set exactly equal to ``expect`` plus diet/title."""
    args, kwargs = spy.calls[-1]
    assert args[0] == pid
    assert args[1] == paths.pulse_home(pid)
    return args, kwargs


async def _create(client, **body):
    body = {"title": "site-test", "model": "claude-opus-5", **body}
    r = await client.post("/pulses", json=body)
    assert r.status_code == 201, r.text
    return r.json()["value"]["id"]


# --------------------------------------------------------------------------- #
# Spawn arguments, one test per site
# --------------------------------------------------------------------------- #
async def test_create_spawn_args(client, store, spy):
    pid = await _create(client, memory_max="2G", effort="xhigh")
    args, kwargs = spy.calls[-1]
    assert args == (pid, paths.pulse_home(pid), "claude-opus-5", "claude-code")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] is None   # start() always passes it; default None
    assert kwargs["diet"] == store.diet_with_defaults(
        store.get_pulse(pid).settings.get("diet"))
    assert kwargs["title"] == "site-test"
    assert kwargs["effort"] == "xhigh"
    assert kwargs["fast"] is False
    assert kwargs["memory_max"] == "2G"


async def test_create_omits_memory_max_when_unset(client, store, spy):
    await _create(client)
    assert "memory_max" not in spy.calls[-1][1]


async def test_restore_spawn_args(client, store, spy):
    pid = await _create(client, memory_max="2G", effort="xhigh")
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    args, kwargs = spy.calls[-1]
    assert args == (pid, paths.pulse_home(pid), "claude-opus-5", "claude-code")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] == f"sess-{pid}"
    assert kwargs["effort"] == "xhigh"
    assert kwargs["title"] == "site-test"
    assert kwargs["memory_max"] == "2G"
    assert store.get_pulse(pid).state == "running"


async def test_reset_spawn_args(client, store, spy):
    pid = await _create(client, memory_max="2G", effort="xhigh")
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 200, r.text
    args, kwargs = spy.calls[-1]
    assert args == (pid, paths.pulse_home(pid), "claude-opus-5", "claude-code")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] is None
    assert kwargs["effort"] == "xhigh"
    assert kwargs["memory_max"] == "2G"
    assert spy.sock_stopped == [pid]


async def _make_forkable(client, tmp_path, monkeypatch, **body):
    monkeypatch.setenv("PULSAR_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    pid = await _create(client, **body)
    sid = f"sess-{pid}"
    hist = session_history_file(paths.pulse_home(pid) / "cli", sid)
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text('{"a":1}\n')
    return pid, sid


async def test_fork_spawn_args(client, store, spy, tmp_path, monkeypatch):
    pid, sid = await _make_forkable(client, tmp_path, monkeypatch,
                                    memory_max="2G", effort="xhigh")
    r = await client.post(f"/pulses/{pid}/fork", json={})
    assert r.status_code == 201, r.text
    new = r.json()["value"]["id"]
    args, kwargs = spy.calls[-1]
    assert args == (new, paths.pulse_home(new), "claude-opus-5", "claude-code")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] == sid
    assert kwargs["title"] == "site-test (fork)"
    assert kwargs["effort"] == "xhigh"
    assert kwargs["memory_max"] == "2G"


async def test_switch_same_vessel_spawn_args(client, store, spy):
    pid = await _create(client, memory_max="2G", effort="xhigh")
    r = await client.patch(f"/pulses/{pid}/model", json={
        "model": "claude-fable-5", "effort": "high"})
    assert r.status_code == 200, r.text
    args, kwargs = spy.calls[-1]
    assert args == (pid, paths.pulse_home(pid), "claude-fable-5", "claude-code")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] == f"sess-{pid}"
    assert kwargs["effort"] == "high"
    assert kwargs["memory_max"] == "2G"
    assert spy.sock_stopped == [pid]
    assert store.get_pulse(pid).model == "claude-fable-5"


async def test_scheduler_wake_spawn_args(store, spy):
    p = store.create_pulse("w", "claude-opus-5", "claude-code", state="stopped",
                           settings={"memory_max": "2G", "effort": "xhigh"})
    _write_heartbeat(p.id, "stopped", "sess-w")
    await asyncio.to_thread(lifecycle.wake_if_stopped, store, store.get_pulse(p.id))
    args, kwargs = spy.calls[-1]
    assert args == (p.id, paths.pulse_home(p.id), "claude-opus-5",
                    "claude-code")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] == "sess-w"
    assert kwargs["title"] == "w"
    assert kwargs["effort"] == "xhigh"
    assert kwargs["memory_max"] == "2G"
    row = store.get_pulse(p.id)
    assert row.state == "running"
    assert row.settings.get("stop_reason") is None


async def test_scheduler_wake_running_is_untouched(store, spy):
    p = store.create_pulse("w", "claude-opus-5", "claude-code", state="running")
    await asyncio.to_thread(lifecycle.wake_if_stopped, store, p)
    assert spy.calls == []


async def test_scheduler_wake_spawn_failure_raises_and_writes_nothing(
        store, spy):
    p = store.create_pulse("w", "claude-opus-5", "claude-code", state="stopped")
    spy.spawn_fails = True
    with pytest.raises(RuntimeError):
        await asyncio.to_thread(lifecycle.wake_if_stopped, store, p)
    row = store.get_pulse(p.id)
    assert row.state == "stopped"
    assert row.settings.get("stop_reason") is None


async def test_handoff_spawn_args_and_drop_rule(store, spy):
    p = store.create_pulse("h", "claude-opus-5", "claude-code", state="running",
                           settings={"memory_max": "2G", "effort": "max",
                                     "fast": True})
    _write_heartbeat(p.id, "idle", "sess-h")
    # Cross-vessel to codex: "max" is valid there and codex has fast mode.
    ok = await handoff_mod.engine.run_handoff(
        store, p, new_model="gpt-6-sol", new_vessel="codex",
        reason="model_switch", force=True)
    assert ok is True
    args, kwargs = spy.calls[-1]
    assert args == (p.id, paths.pulse_home(p.id), "gpt-6-sol", "codex")
    assert set(kwargs) == {"resume", "diet", "title", "effort", "fast",
                           "memory_max"}
    assert kwargs["resume"] is None
    assert kwargs["effort"] == "max"           # valid on the target: kept
    assert kwargs["fast"] is True              # codex keeps fast
    assert kwargs["memory_max"] == "2G"
    row = store.get_pulse(p.id)
    assert row.settings.get("effort") == "max"
    assert row.state == "running"
    assert row.settings.get("stop_reason") is None


async def test_handoff_drops_invalid_effort_and_fast(store, spy):
    # codex "none" means nothing to claude; claude has no service-tier switch.
    p = store.create_pulse("h", "gpt-6-sol", "codex", state="running",
                           settings={"fast": True, "effort": "none"})
    _write_heartbeat(p.id, "idle", "sess-h")
    ok = await handoff_mod.engine.run_handoff(
        store, p, new_model="claude-opus-5", new_vessel="claude-code",
        reason="model_switch", force=True)
    assert ok is True
    kwargs = spy.calls[-1][1]
    assert kwargs["effort"] is None
    assert kwargs["fast"] is False
    row = store.get_pulse(p.id)
    assert row.settings.get("effort") is None
    assert row.settings.get("fast") is None


# --------------------------------------------------------------------------- #
# Spawn failure + cleanup fault injection, per site
# --------------------------------------------------------------------------- #
async def test_create_spawn_failure_reason_then_state(client, store, spy):
    spy.spawn_fails = True
    r = await client.post("/pulses", json={"title": "t",
                                           "model": "claude-opus-5"})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "spawn_failed"
    (row,) = store.list_pulses()
    assert row.state == "stopped"
    assert row.settings["stop_reason"].startswith("spawn failed: ")


@pytest.mark.parametrize("exc", [StoreError("x"), RuntimeError("x")])
async def test_create_cleanup_update_settings_fails_is_swallowed(
        client, store, spy, monkeypatch, exc):
    spy.spawn_fails = True
    _inject(store, monkeypatch, "update_settings", exc,
            lambda a, k: _is_reason_write(a, k))
    r = await client.post("/pulses", json={"title": "t",
                                           "model": "claude-opus-5"})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "spawn_failed"
    (row,) = store.list_pulses()
    # reason-first: the failed reason write skipped the state write
    assert row.state == "running"
    assert not row.settings.get("stop_reason")


@pytest.mark.parametrize("exc", [StoreError("x"), RuntimeError("x")])
async def test_create_cleanup_set_state_fails_is_swallowed(
        client, store, spy, monkeypatch, exc):
    spy.spawn_fails = True
    _inject(store, monkeypatch, "set_state", exc, _is_stop_write)
    r = await client.post("/pulses", json={"title": "t",
                                           "model": "claude-opus-5"})
    assert r.status_code == 502
    (row,) = store.list_pulses()
    assert row.state == "running"
    assert row.settings["stop_reason"].startswith("spawn failed: ")


async def test_restore_spawn_failure_writes_nothing(client, store, spy,
                                                    monkeypatch):
    pid = await _create(client)
    await client.post(f"/pulses/{pid}/pause")
    spy.spawn_fails = True
    writes = []
    for name in ("set_state", "update_settings"):
        orig = getattr(store, name)
        monkeypatch.setattr(
            store, name,
            lambda *a, _o=orig, _n=name, **k: writes.append(_n) or _o(*a, **k))
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "spawn_failed"
    assert writes == []
    assert store.get_pulse(pid).state == "stopped"


async def test_reset_spawn_failure_state_then_reason(client, store, spy):
    pid = await _create(client)
    spy.spawn_fails = True
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "spawn_failed"
    row = store.get_pulse(pid)
    assert row.state == "stopped"
    assert row.settings["stop_reason"] == "reset respawn failed"


async def test_reset_cleanup_update_settings_storeerror_swallowed(
        client, store, spy, monkeypatch):
    pid = await _create(client)
    spy.spawn_fails = True
    _inject(store, monkeypatch, "update_settings", StoreError("x"),
            _is_reason_write)
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 502
    row = store.get_pulse(pid)
    assert row.state == "stopped"          # state-first: already written
    assert not row.settings.get("stop_reason")


async def test_reset_cleanup_update_settings_runtimeerror_escapes(
        client, store, spy, monkeypatch):
    pid = await _create(client)
    spy.spawn_fails = True
    _inject(store, monkeypatch, "update_settings", RuntimeError("x"),
            _is_reason_write)
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 500            # only StoreError is caught
    assert store.get_pulse(pid).state == "stopped"


async def test_reset_cleanup_set_state_storeerror_swallowed(
        client, store, spy, monkeypatch):
    pid = await _create(client)
    spy.spawn_fails = True
    _inject(store, monkeypatch, "set_state", StoreError("x"), _is_stop_write)
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 502
    row = store.get_pulse(pid)
    assert row.state == "running"          # state write failed first,
    assert not row.settings.get("stop_reason")   # so no reason write


async def test_reset_cleanup_set_state_runtimeerror_escapes(
        client, store, spy, monkeypatch):
    pid = await _create(client)
    spy.spawn_fails = True
    _inject(store, monkeypatch, "set_state", RuntimeError("x"), _is_stop_write)
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 500
    assert store.get_pulse(pid).state == "running"


async def _fork_failing(client, spy, tmp_path, monkeypatch):
    pid, _ = await _make_forkable(client, tmp_path, monkeypatch)
    spy.spawn_fails = True
    return pid


def _fork_row(store, source_id):
    (row,) = [p for p in store.list_pulses() if p.id != source_id]
    return row


async def test_fork_spawn_failure_reason_then_state(client, store, spy,
                                                    tmp_path, monkeypatch):
    pid = await _fork_failing(client, spy, tmp_path, monkeypatch)
    r = await client.post(f"/pulses/{pid}/fork", json={})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "spawn_failed"
    row = _fork_row(store, pid)
    assert row.state == "stopped"
    assert row.settings["stop_reason"].startswith("spawn failed: ")


async def test_fork_cleanup_update_settings_storeerror_swallowed(
        client, store, spy, tmp_path, monkeypatch):
    pid = await _fork_failing(client, spy, tmp_path, monkeypatch)
    _inject(store, monkeypatch, "update_settings", StoreError("x"),
            _is_reason_write)
    r = await client.post(f"/pulses/{pid}/fork", json={})
    assert r.status_code == 502
    row = _fork_row(store, pid)
    assert row.state == "running"          # reason-first: state never written
    assert not row.settings.get("stop_reason")


async def test_fork_cleanup_update_settings_runtimeerror_escapes(
        client, store, spy, tmp_path, monkeypatch):
    pid = await _fork_failing(client, spy, tmp_path, monkeypatch)
    _inject(store, monkeypatch, "update_settings", RuntimeError("x"),
            _is_reason_write)
    r = await client.post(f"/pulses/{pid}/fork", json={})
    assert r.status_code == 500            # only StoreError is caught
    assert _fork_row(store, pid).state == "running"


async def test_fork_cleanup_set_state_storeerror_swallowed(
        client, store, spy, tmp_path, monkeypatch):
    pid = await _fork_failing(client, spy, tmp_path, monkeypatch)
    _inject(store, monkeypatch, "set_state", StoreError("x"), _is_stop_write)
    r = await client.post(f"/pulses/{pid}/fork", json={})
    assert r.status_code == 502
    row = _fork_row(store, pid)
    assert row.state == "running"
    assert row.settings["stop_reason"].startswith("spawn failed: ")


async def test_fork_cleanup_set_state_runtimeerror_escapes(
        client, store, spy, tmp_path, monkeypatch):
    pid = await _fork_failing(client, spy, tmp_path, monkeypatch)
    _inject(store, monkeypatch, "set_state", RuntimeError("x"), _is_stop_write)
    r = await client.post(f"/pulses/{pid}/fork", json={})
    assert r.status_code == 500
    row = _fork_row(store, pid)
    assert row.state == "running"
    assert row.settings["stop_reason"].startswith("spawn failed: ")


async def test_switch_spawn_failure_reason_then_state(client, store, spy):
    pid = await _create(client)
    spy.spawn_fails = True
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-fable-5"})
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "spawn_failed"
    row = store.get_pulse(pid)
    assert row.state == "stopped"
    assert row.settings["stop_reason"].startswith("switch respawn failed: ")


@pytest.mark.parametrize("exc", [StoreError("x"), RuntimeError("x")])
async def test_switch_cleanup_update_settings_failure_escapes(
        client, store, spy, monkeypatch, exc):
    pid = await _create(client)
    spy.spawn_fails = True
    _inject(store, monkeypatch, "update_settings", exc, _is_reason_write)
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-fable-5"})
    assert r.status_code == 500            # the switch cleanup has no try
    assert store.get_pulse(pid).state == "running"   # reason-first


@pytest.mark.parametrize("exc", [StoreError("x"), RuntimeError("x")])
async def test_switch_cleanup_set_state_failure_escapes(
        client, store, spy, monkeypatch, exc):
    pid = await _create(client)
    spy.spawn_fails = True
    _inject(store, monkeypatch, "set_state", exc, _is_stop_write)
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-fable-5"})
    assert r.status_code == 500            # the switch cleanup has no try
    row = store.get_pulse(pid)
    assert row.state == "running"
    assert row.settings["stop_reason"].startswith("switch respawn failed: ")


async def _handoff_failing(store, spy):
    p = store.create_pulse("h", "claude-opus-5", "claude-code", state="running")
    _write_heartbeat(p.id, "idle", "sess-h")
    spy.spawn_fails = True
    return p


async def test_handoff_spawn_failure_state_then_reason(store, spy):
    p = await _handoff_failing(store, spy)
    ok = await handoff_mod.engine.run_handoff(store, p, force=True,
                                       reason="model_switch")
    assert ok is False
    row = store.get_pulse(p.id)
    assert row.state == "stopped"
    assert row.settings["stop_reason"] == "handoff respawn failed"
    state = handoff_mod.read_state(p.id)
    assert state["phase"] == "failed"
    assert state["reason"].startswith("respawn: ")


@pytest.mark.parametrize("exc", [StoreError("x"), RuntimeError("x")])
async def test_handoff_cleanup_update_settings_failure_swallowed(
        store, spy, monkeypatch, exc):
    p = await _handoff_failing(store, spy)
    _inject(store, monkeypatch, "update_settings", exc, _is_reason_write)
    ok = await handoff_mod.engine.run_handoff(store, p, force=True,
                                       reason="model_switch")
    assert ok is False                     # the cleanup catches Exception
    row = store.get_pulse(p.id)
    assert row.state == "stopped"          # state-first
    assert row.settings.get("stop_reason") != "handoff respawn failed"


@pytest.mark.parametrize("exc", [StoreError("x"), RuntimeError("x")])
async def test_handoff_cleanup_set_state_failure_swallowed(
        store, spy, monkeypatch, exc):
    p = await _handoff_failing(store, spy)
    _inject(store, monkeypatch, "set_state", exc, _is_stop_write)
    ok = await handoff_mod.engine.run_handoff(store, p, force=True,
                                       reason="model_switch")
    assert ok is False
    row = store.get_pulse(p.id)
    assert row.state == "running"
    assert row.settings.get("stop_reason") != "handoff respawn failed"


async def test_handoff_archived_during_spawn_aborts_pulse_not_running(
        store, spy, monkeypatch):
    p = store.create_pulse("h", "claude-opus-5", "claude-code", state="running")
    _write_heartbeat(p.id, "idle", "sess-h")
    orig = spy.spawn_runner

    def spawn_then_archive(*args, **kwargs):
        orig(*args, **kwargs)
        store.archive_pulse(p.id)
    monkeypatch.setattr(units_mod, "spawn_runner", spawn_then_archive)
    ok = await handoff_mod.engine.run_handoff(store, p, force=True,
                                       reason="model_switch")
    assert ok is False
    assert spy.sock_stopped == [p.id, p.id]  # old runner, then the successor
    assert store.get_pulse(p.id).state == "archived"
    state = handoff_mod.read_state(p.id)
    assert (state["phase"], state["reason"]) == ("failed", "pulse_archived")


# --------------------------------------------------------------------------- #
# Stop paths (pause route)
# --------------------------------------------------------------------------- #
async def test_pause_stops_via_socket(client, store, spy):
    pid = await _create(client)
    r = await client.post(f"/pulses/{pid}/pause")
    assert r.status_code == 200
    assert spy.sock_stopped == [pid] and spy.unit_stopped == []
    assert store.get_pulse(pid).state == "stopped"


async def test_pause_socket_error_falls_back_to_unit(client, store, spy):
    pid = await _create(client)
    spy.sock_fails = True
    r = await client.post(f"/pulses/{pid}/pause")
    assert r.status_code == 200
    assert spy.sock_stopped == [] and spy.unit_stopped == [pid]
    assert store.get_pulse(pid).state == "stopped"


async def test_pause_both_stop_paths_fail_is_502(client, store, spy):
    pid = await _create(client)
    spy.sock_fails = True
    spy.unit_fails = True
    r = await client.post(f"/pulses/{pid}/pause")
    assert r.status_code == 502
    assert r.json()["error"]["code"] == "stop_failed"
    assert store.get_pulse(pid).state == "running"
