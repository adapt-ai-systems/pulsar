"""Unit tests for pulsar.daemon.lifecycle (spawn / stop / wake / drop rule).

    TZ=UTC venv/bin/python -m pytest tests/test_lifecycle.py -q

The fake spawn and socket are set on the shared ``pulsar.launch.units`` and
``pulsar.daemon.sockclient`` modules, the same objects lifecycle calls.
"""

from __future__ import annotations

import json

import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import lifecycle, paths, sockclient
from pulsar.store import Store


class _SockErr(Exception):
    pass


@pytest.fixture()
def store(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    s = Store(str(root / "pulsar.db"))
    yield s
    s.close()


@pytest.fixture()
def fakes(monkeypatch):
    f = {"spawned": [], "sock": [], "unit": [], "sock_fails": False,
         "unit_fails": False, "spawn_fails": False}

    def spawn_runner(*args, **kwargs):
        f["spawned"].append((args, kwargs))
        if f["spawn_fails"]:
            raise RuntimeError("boom")

    def sock_stop(sock_path, pulse_id):
        if f["sock_fails"]:
            raise _SockErr("down")
        f["sock"].append(pulse_id)

    def stop_unit(pulse_id):
        f["unit"].append(pulse_id)
        return not f["unit_fails"]

    monkeypatch.setattr(units_mod, "spawn_runner", spawn_runner)
    monkeypatch.setattr(units_mod, "stop_unit", stop_unit)
    monkeypatch.setattr(sockclient, "stop", sock_stop)
    monkeypatch.setattr(sockclient, "SocketError", _SockErr)
    return f


def _pulse(store, state="stopped", **settings):
    return store.create_pulse("life", "claude-opus-5", "claude-code",
                              state=state, settings=settings)


def _beat(pid, sid):
    home = paths.pulse_home(pid)
    home.mkdir(parents=True, exist_ok=True)
    (home / "heartbeat.json").write_text(json.dumps({"cli_session_id": sid}))


# --------------------------------------------------------------------------- #
def test_spawn_kwargs_defaults_to_stored_settings(store):
    p = _pulse(store, effort="high", fast=True, memory_max="3G")
    kw = lifecycle.spawn_kwargs(store, p)
    assert set(kw) == {"diet", "title", "effort", "fast", "memory_max"}
    assert (kw["title"], kw["effort"], kw["fast"], kw["memory_max"]) == (
        "life", "high", True, "3G")
    assert kw["diet"] == store.diet_with_defaults(None)


def test_spawn_kwargs_overrides_and_no_memory_max(store):
    p = _pulse(store, effort="high", fast=True)
    kw = lifecycle.spawn_kwargs(store, p, effort=None, fast=False)
    assert "memory_max" not in kw
    assert kw["effort"] is None and kw["fast"] is False


def test_spawn_kwargs_explicit_settings_win(store):
    p = _pulse(store, effort="high")
    kw = lifecycle.spawn_kwargs(store, p, settings={"effort": "low",
                                                    "memory_max": "1G"})
    assert kw["effort"] == "low" and kw["memory_max"] == "1G"


def test_start_calls_spawn_with_identity_and_resume(store, fakes):
    p = _pulse(store, memory_max="3G")
    lifecycle.start(store, p, resume="sid-1")
    (args, kwargs), = fakes["spawned"]
    assert args == (p.id, paths.pulse_home(p.id), "claude-opus-5",
                    "claude-code")
    assert kwargs["resume"] == "sid-1" and kwargs["memory_max"] == "3G"
    assert paths.pulse_home(p.id).is_dir()


def test_start_model_vessel_and_memory_override(store, fakes):
    p = _pulse(store, memory_max="3G")
    lifecycle.start(store, p, resume=None, model="m2", vessel="codex",
                    memory_max="9G")
    (args, kwargs), = fakes["spawned"]
    assert args[2:] == ("m2", "codex")
    assert kwargs["memory_max"] == "9G" and kwargs["resume"] is None


def test_start_propagates_spawn_failure(store, fakes):
    fakes["spawn_fails"] = True
    with pytest.raises(RuntimeError):
        lifecycle.start(store, _pulse(store), resume=None)


@pytest.mark.anyio
async def test_astart_runs_start(store, fakes):
    p = _pulse(store)
    await lifecycle.astart(store, p, resume="s")
    assert fakes["spawned"][0][1]["resume"] == "s"


def test_stop_socket_ok(fakes):
    assert lifecycle.stop("p1") == "socket"
    assert fakes["sock"] == ["p1"] and fakes["unit"] == []


def test_stop_socket_error_falls_back_to_unit(fakes):
    fakes["sock_fails"] = True
    assert lifecycle.stop("p1") == "unit"
    assert fakes["unit"] == ["p1"]


def test_stop_both_fail_raises(fakes):
    fakes["sock_fails"] = fakes["unit_fails"] = True
    with pytest.raises(lifecycle.StopFailed, match="could not stop runner for p1"):
        lifecycle.stop("p1")


@pytest.mark.anyio
async def test_astop(fakes):
    assert await lifecycle.astop("p1") == "socket"


def test_wake_respawns_with_heartbeat_resume_and_marks_running(store, fakes):
    p = _pulse(store, stop_reason="oom")
    _beat(p.id, "sid-9")
    lifecycle.wake_if_stopped(store, p)
    assert fakes["spawned"][0][1]["resume"] == "sid-9"
    row = store.get_pulse(p.id)
    assert row.state == "running" and row.settings.get("stop_reason") is None


@pytest.mark.parametrize("state", ["running", "archived"])
def test_wake_ignores_pulse_not_stopped(store, fakes, state):
    p = _pulse(store, state=state)
    lifecycle.wake_if_stopped(store, p)
    assert fakes["spawned"] == []


def test_wake_spawn_failure_raises_and_leaves_stopped(store, fakes):
    p = _pulse(store)
    fakes["spawn_fails"] = True
    with pytest.raises(RuntimeError):
        lifecycle.wake_if_stopped(store, p)
    assert store.get_pulse(p.id).state == "stopped"


def test_settings_for_target_keeps_valid_values(store):
    p = _pulse(store, effort="max", fast=True)
    assert lifecycle.settings_for_target(store, p, "gpt-6-sol", "codex") == (
        "max", True)
    assert store.get_pulse(p.id).settings.get("effort") == "max"


def test_settings_for_target_migrates_retired_ds41_name(store):
    # 2026-11-02: DS41 dial is 80/70/50/30/none; a stored "low" becomes "50".
    p = _pulse(store, effort="low")
    assert lifecycle.settings_for_target(store, p, "pi-alt-ds41", "pi") == (
        "50", False)
    assert store.get_pulse(p.id).settings.get("effort") == "50"


def test_settings_for_target_drops_invalid_and_clears_store(store):
    p = _pulse(store, effort="none", fast=True)
    assert lifecycle.settings_for_target(
        store, p, "claude-opus-5", "claude-code") == (None, False)
    row = store.get_pulse(p.id)
    assert row.settings.get("effort") is None
    assert row.settings.get("fast") is None
