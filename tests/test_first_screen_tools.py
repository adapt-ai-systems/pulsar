"""Tests for agent tool detection, fast startup failure detection, and
refusing direct messages to dead or stopped pulses.
"""

from __future__ import annotations

import json
import os
import stat
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

from pulsar.daemon import paths, reconcile as reconcile_mod, views as views_mod
from pulsar.daemon.app import create_app
from pulsar.runner.runner import Runner
from pulsar.store import Store
from pulsar.vessel import tools as tools_mod
from pulsar.vessel.claude import VesselError
from tests.test_daemon import data_root, fake_spawn  # noqa: F401


def _make_exec(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def _clear_tool_env(monkeypatch, tmp_path: Path) -> None:
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    monkeypatch.setenv("HOME", str(fake_home))
    monkeypatch.setenv("PATH", str(tmp_path / "empty-bin"))
    for var in (
        "PULSAR_CLAUDE_BIN",
        "PULSAR_CODEX_BIN",
        "PULSAR_PI_BIN",
        "PULSAR_AGY_BIN",
        "PULSAR_GROK_BIN",
    ):
        monkeypatch.delenv(var, raising=False)


def _write_beat(pulse_id: str, *, state: str, reason: str | None = None,
                offset_s: float = 0.0) -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc) + timedelta(seconds=offset_s)
    beat = {
        "ts": ts.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "state": state,
        "reason": reason,
        "protocol": 1,
    }
    (home / "heartbeat.json").write_text(json.dumps(beat), encoding="utf-8")


# --------------------------------------------------------------------------- #
# A & B: tool detection and GET /tools + GET /models decoration
# --------------------------------------------------------------------------- #
def test_detect_tools_none_installed(tmp_path, monkeypatch):
    _clear_tool_env(monkeypatch, tmp_path)
    summary = tools_mod.tools_summary()
    assert summary["any_available"] is False
    for vid, binary in (
        ("claude-code", "claude"),
        ("codex", "codex"),
        ("pi", "pi"),
        ("antigravity", "agy"),
        ("grok", "grok"),
    ):
        entry = summary["vessels"][vid]
        assert entry["available"] is False
        assert entry["binary"] == binary
        assert entry["path"] is None
        assert entry["reason"] == f"{binary} not found"


def test_detect_tools_single_pi_env_override(tmp_path, monkeypatch):
    _clear_tool_env(monkeypatch, tmp_path)
    pi_bin = _make_exec(tmp_path / "pi-tuned" / "bin" / "pi")
    monkeypatch.setenv("PULSAR_PI_BIN", str(pi_bin))

    summary = tools_mod.tools_summary()
    assert summary["any_available"] is True
    assert summary["vessels"]["pi"]["available"] is True
    assert summary["vessels"]["pi"]["path"] == str(pi_bin)
    assert summary["vessels"]["pi"]["reason"] is None
    assert summary["vessels"]["claude-code"]["available"] is False
    assert summary["vessels"]["claude-code"]["reason"] == "claude not found"


def test_detect_tools_finds_path_and_default_locations(tmp_path, monkeypatch):
    _clear_tool_env(monkeypatch, tmp_path)
    bin_dir = tmp_path / "bin"
    claude = _make_exec(bin_dir / "claude")
    monkeypatch.setenv("PATH", str(bin_dir))
    agy = _make_exec(Path(os.environ["HOME"]) / ".local" / "bin" / "agy")

    vessels = tools_mod.detect_tools()
    assert vessels["claude-code"]["available"] is True
    assert vessels["claude-code"]["path"] == str(claude)
    assert vessels["antigravity"]["available"] is True
    assert vessels["antigravity"]["path"] == str(agy)
    assert vessels["codex"]["available"] is False


@pytest.mark.anyio
async def test_get_tools_and_models_expose_availability(
    tmp_path, data_root, fake_spawn, monkeypatch  # noqa: F811
):
    _clear_tool_env(monkeypatch, tmp_path)
    pi_bin = _make_exec(tmp_path / "custom" / "pi")
    monkeypatch.setenv("PULSAR_PI_BIN", str(pi_bin))

    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://daemon"
        ) as client, app.router.lifespan_context(app):
            rt = await client.get("/tools")
            assert rt.status_code == 200
            val = rt.json()["value"]
            assert val["any_available"] is True
            assert val["vessels"]["pi"]["available"] is True
            assert val["vessels"]["claude-code"]["reason"] == "claude not found"

            rm = await client.get("/models")
            assert rm.status_code == 200
            tools = rm.json()["value"]["tools"]
            assert tools["pi"]["available"] is True
            assert tools["claude-code"]["available"] is False
    finally:
        store.close()


# --------------------------------------------------------------------------- #
# C: fast startup failure reap, runner_error event, and message refusal
# --------------------------------------------------------------------------- #
def test_sweep_dead_runners_reaps_fresh_cli_dead_during_grace(
    data_root, fake_spawn  # noqa: F811
):
    store = Store(str(data_root / "pulsar.db"))
    try:
        pulse = store.create_pulse(
            title="missing cli",
            model="claude-opus-5-5",
            vessel="claude-code",
            state="running",
        )
        _write_beat(
            pulse.id,
            state="cli_dead",
            reason="vessel failed to start: `claude` binary not found on PATH",
            offset_s=1.0,
        )

        swept = reconcile_mod.sweep_dead_runners(store, grace_s=20.0)
        assert swept["stopped"] == 1
        updated = store.get_pulse(pulse.id)
        assert updated.state == "stopped"
        assert "binary not found" in updated.settings["stop_reason"]
    finally:
        store.close()


def test_sweep_dead_runners_still_spares_booting_and_stale_pre_restore_beats(
    data_root, fake_spawn  # noqa: F811
):
    store = Store(str(data_root / "pulsar.db"))
    try:
        booting = store.create_pulse(
            title="booting", model="m", vessel="claude-code", state="running"
        )
        _write_beat(booting.id, state="starting", offset_s=0.0)

        restored = store.create_pulse(
            title="restored", model="m", vessel="claude-code", state="running"
        )
        # Stale terminal beat from 10s before updated_at must not kill a restore.
        _write_beat(
            restored.id, state="cli_dead", reason="old crash", offset_s=-10.0
        )

        swept = reconcile_mod.sweep_dead_runners(store, grace_s=20.0)
        assert swept["stopped"] == 0
        assert swept["kept"] == 2
        assert store.get_pulse(booting.id).state == "running"
        assert store.get_pulse(restored.id).state == "running"
    finally:
        store.close()


def test_live_state_marks_cli_dead_unreachable(data_root):  # noqa: F811
    store = Store(str(data_root / "pulsar.db"))
    try:
        pulse = store.create_pulse(
            title="dead", model="m", vessel="claude-code", state="running"
        )
        _write_beat(
            pulse.id,
            state="cli_dead",
            reason="vessel failed to start: `claude` binary not found on PATH",
        )
        live = views_mod.live_state(pulse)
        assert live is not None
        assert live["reachable"] is False
        assert live["socket_state"] == "cli_dead"
    finally:
        store.close()


@pytest.mark.anyio
async def test_runner_start_failure_emits_runner_error_and_cli_dead(
    tmp_path, monkeypatch
):
    class _BrokenVessel:
        async def start(self):
            raise VesselError("`claude` binary not found on PATH")

    import pulsar.runner.vessel_bridge as bridge_mod

    monkeypatch.setattr(bridge_mod, "make_vessel", lambda *a, **k: _BrokenVessel())
    home = tmp_path / "pulse-home"
    runner = Runner("p-fail", home, "claude-opus-5-5", "claude-code", None)
    rc = await runner.run()
    assert rc == 1

    hb = json.loads((home / "heartbeat.json").read_text(encoding="utf-8"))
    assert hb["state"] == "cli_dead"
    assert "binary not found" in hb["reason"]

    events = [
        json.loads(line)
        for line in (home / "events.jsonl").read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert any(
        e.get("type") == "runner_error"
        and e.get("where") == "vessel.start"
        and "binary not found" in (e.get("reason") or "")
        for e in events
    )


@pytest.mark.anyio
async def test_post_message_refuses_dead_or_stopped_pulse_with_409(
    data_root, fake_spawn  # noqa: F811
):
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://daemon"
        ) as client, app.router.lifespan_context(app):
            r = await client.post(
                "/pulses", json={"title": "dead-test", "model": "claude-opus-5-5"}
            )
            assert r.status_code == 201
            pid = r.json()["value"]["id"]

            # Simulate runner dying right after start before sweep runs.
            _write_beat(
                pid,
                state="cli_dead",
                reason="vessel failed to start: `claude` binary not found on PATH",
                offset_s=1.0,
            )

            # Direct user message must be refused (409), not accepted (202).
            m1 = await client.post(f"/pulses/{pid}/messages", json={"text": "hello"})
            assert m1.status_code == 409
            err = m1.json()["error"]
            assert err["code"] == "pulse_stopped"
            assert "binary not found" in err["message"]
            assert "Install the tool or pick another model" in err["message"]
            assert list(paths.inbox_new(pid).glob("*.json")) == []
            assert store.get_pulse(pid).state == "stopped"

            # Subsequent send to the now-stopped pulse is also refused (409).
            m2 = await client.post(f"/pulses/{pid}/messages", json={"text": "again"})
            assert m2.status_code == 409
            assert list(paths.inbox_new(pid).glob("*.json")) == []

            # Multipart send to a stopped pulse is refused too.
            m3 = await client.post(
                f"/pulses/{pid}/messages",
                data={"text": "with file"},
                files=[("file", ("a.txt", b"hi", "text/plain"))],
            )
            assert m3.status_code == 409
            assert list(paths.inbox_new(pid).glob("*.json")) == []

            # Pulse-to-pulse send (from_pulse_id) still wakes a stopped target.
            sender = store.create_pulse(
                title="lead", model="claude-opus-5-5", vessel="claude-code"
            )
            m4 = await client.post(
                f"/pulses/{pid}/messages",
                json={"text": "wake up", "from_pulse_id": sender.id},
            )
            assert m4.status_code == 202
            assert len(list(paths.inbox_new(pid).glob("*.json"))) == 1
    finally:
        store.close()


@pytest.mark.anyio
async def test_post_message_to_paused_and_normal_stopped_pulse_still_queues_202(
    data_root, fake_spawn  # noqa: F811
):
    """Paused and normally stopped pulses keep the old 202 queued contract,
    and restoring the pulse leaves the queued inbox/new message ready to run."""
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://daemon"
        ) as client, app.router.lifespan_context(app):
            r = await client.post(
                "/pulses", json={"title": "paused-test", "model": "claude-opus-5-5"}
            )
            assert r.status_code == 201
            pid = r.json()["value"]["id"]

            # Pause the pulse (no stop_reason set).
            pr = await client.post(f"/pulses/{pid}/pause")
            assert pr.status_code == 200
            assert store.get_pulse(pid).state == "stopped"
            assert not store.get_pulse(pid).settings.get("stop_reason")

            # Direct user message (JSON) to a paused pulse returns 202 queued.
            m1 = await client.post(
                f"/pulses/{pid}/messages", json={"text": "queued while paused"}
            )
            assert m1.status_code == 202
            queued_name = m1.json()["value"]["queued"]
            assert (paths.inbox_new(pid) / queued_name).is_file()

            # Restore must keep the queued message in inbox/new/ for the runner.
            rr = await client.post(f"/pulses/{pid}/restore")
            assert rr.status_code == 200
            assert store.get_pulse(pid).state == "running"
            assert (paths.inbox_new(pid) / queued_name).is_file()

            # A pulse stopped normally by watch_sleep also accepts 202 queued.
            ws = store.create_pulse(
                title="watch-slept",
                model="claude-opus-5-5",
                vessel="claude-code",
                state="stopped",
                settings={"stop_reason": "watch work complete"},
            )
            m2 = await client.post(
                f"/pulses/{ws.id}/messages", json={"text": "after watch sleep"}
            )
            assert m2.status_code == 202
            assert len(list(paths.inbox_new(ws.id).glob("*.json"))) == 1
    finally:
        store.close()


@pytest.mark.anyio
async def test_other_delivery_paths_still_work_for_stopped_pulse(
    data_root, fake_spawn  # noqa: F811
):
    """Inbox reply, pinned question answer, schedule fire, condition watch, and
    from_pulse_id still wake and deliver to a stopped pulse."""
    from pulsar.daemon import condwatch as condwatch_mod, scheduler as scheduler_mod

    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    try:
        async with httpx.AsyncClient(
            transport=transport, base_url="http://daemon"
        ) as client, app.router.lifespan_context(app):
            # 1. Inbox reply to a stopped pulse
            p_inbox = store.create_pulse(
                title="inbox-target",
                model="claude-opus-5-5",
                vessel="claude-code",
                state="stopped",
            )
            ri = await client.post(
                "/inbox/items",
                json={
                    "title": "Need input",
                    "body": "Please confirm.",
                    "channel": "general",
                    "from_pulse_id": p_inbox.id,
                    "needs_response": True,
                },
            )
            assert ri.status_code == 201
            item_id = ri.json()["value"]["id"]
            rep = await client.post(
                f"/inbox/items/{item_id}/reply", json={"text": "Go ahead"}
            )
            assert rep.status_code == 200
            assert store.get_pulse(p_inbox.id).state == "running"
            assert len(list(paths.inbox_new(p_inbox.id).glob("*.json"))) == 1

            # 2. Pinned question answer to a stopped pulse
            p_q = store.create_pulse(
                title="question-target",
                model="claude-opus-5-5",
                vessel="claude-code",
                state="stopped",
            )
            rq = await client.post(
                "/questions", json={"pulse_id": p_q.id, "text": "Proceed?"}
            )
            assert rq.status_code == 201
            qid = rq.json()["value"]["id"]
            ans = await client.post(
                f"/questions/{qid}/answer", json={"text": "Yes"}
            )
            assert ans.status_code == 200
            assert store.get_pulse(p_q.id).state == "running"
            assert len(list(paths.inbox_new(p_q.id).glob("*.json"))) == 1

            # 3. Schedule delivery to a stopped pulse
            p_sched = store.create_pulse(
                title="sched-target",
                model="claude-opus-5-5",
                vessel="claude-code",
                state="stopped",
            )
            due_iso = (datetime.now(timezone.utc) - timedelta(seconds=5)).isoformat()
            sched = store.create_schedule(
                pulse_id=p_sched.id,
                kind="once",
                prompt="scheduled wake",
                at_iso=due_iso,
            )
            import asyncio

            outcome = await asyncio.to_thread(
                scheduler_mod._fire, store, sched, datetime.now(timezone.utc)
            )
            assert outcome == "fired"
            assert store.get_pulse(p_sched.id).state == "running"
            assert len(list(paths.inbox_new(p_sched.id).glob("*.json"))) == 1

            # 4. Condition watch delivery to a stopped pulse
            p_watch = store.create_pulse(
                title="watch-target",
                model="claude-opus-5-5",
                vessel="claude-code",
                state="stopped",
            )
            cw = store.create_condition_watch(
                pulse_id=p_watch.id,
                kind="file",
                spec={"path": str(data_root / "trigger.txt")},
                next_check_at=due_iso,
                note="file appeared",
            )
            await asyncio.to_thread(
                condwatch_mod._notify_owner, store, cw, "file appeared"
            )
            assert store.get_pulse(p_watch.id).state == "running"
            assert len(list(paths.inbox_new(p_watch.id).glob("*.json"))) == 1

            # 5. Pulse-to-pulse message with from_pulse_id to a paused pulse
            p_paused = store.create_pulse(
                title="paused-p2p-target",
                model="claude-opus-5-5",
                vessel="claude-code",
                state="stopped",
            )
            sender = store.create_pulse(
                title="sender", model="claude-opus-5-5", vessel="claude-code"
            )
            mp = await client.post(
                f"/pulses/{p_paused.id}/messages",
                json={"text": "ping", "from_pulse_id": sender.id},
            )
            assert mp.status_code == 202
            assert store.get_pulse(p_paused.id).state == "running"
            assert len(list(paths.inbox_new(p_paused.id).glob("*.json"))) == 1
    finally:
        store.close()


def test_sweep_spares_a_normal_stop_inside_the_grace_window(
    data_root, fake_spawn  # noqa: F811
):
    """A fresh ``stopped`` beat is what reset, handoff and pause leave behind.
    Only a recorded failure (``cli_dead``) skips the grace window."""
    store = Store(str(data_root / "pulsar.db"))
    try:
        pulse = store.create_pulse(
            title="mid reset", model="m", vessel="claude-code", state="running"
        )
        _write_beat(pulse.id, state="stopped", offset_s=1.0)
        assert reconcile_mod.is_fresh_dead_beat(pulse, paths.read_heartbeat(pulse.id)) is False
        swept = reconcile_mod.sweep_dead_runners(store, grace_s=20.0)
        assert swept["stopped"] == 0
        assert store.get_pulse(pulse.id).state == "running"
    finally:
        store.close()


def test_locked_sweep_does_not_reap_a_pulse_that_a_reset_restarted(
    data_root, monkeypatch  # noqa: F811
):
    """The sweep sees the old runner's last beat, then waits for the pulse lock
    that a reset holds. The reset starts a new runner and touches the row. The
    sweep must then leave the pulse running and must not stop its unit."""
    import threading

    from pulsar.daemon import lifecycle
    from pulsar.launch import units as units_mod

    stops: list[str] = []
    unit = {"active": False}
    monkeypatch.setattr(units_mod, "stop_unit", lambda pid: stops.append(pid) or True)
    monkeypatch.setattr(units_mod, "fence_report", lambda pid: {"result": "success"})
    monkeypatch.setattr(units_mod, "unit_active", lambda pid: unit["active"])

    store = Store(str(data_root / "pulsar.db"))
    try:
        pulse = store.create_pulse(
            title="reset race", model="m", vessel="claude-code", state="running"
        )
        _write_beat(pulse.id, state="stopped", offset_s=0.0)

        result: dict = {}
        with lifecycle.pulse_lock(pulse.id):
            t = threading.Thread(
                target=lambda: result.update(
                    reconcile_mod.sweep_dead_runners(store, lock=True)
                )
            )
            t.start()
            t.join(timeout=0.5)
            assert t.is_alive(), "the sweep must wait for the pulse lock"
            # What a reset does before it lets the lock go: a new runner is
            # up, and its first heartbeat is not written yet.
            unit["active"] = True
            store.update_settings(pulse.id, {"stop_reason": None})
        t.join(timeout=10)
        assert not t.is_alive()
        assert result["stopped"] == 0
        assert result["kept"] == 1
        assert stops == []
        after = store.get_pulse(pulse.id)
        assert after.state == "running"
        assert not after.settings.get("stop_reason")

        # With no reset in the way, the same sweep does reap a dead runner.
        unit["active"] = False
        swept = reconcile_mod.sweep_dead_runners(store, lock=True)
        assert swept["stopped"] == 1
        assert stops == [pulse.id]
        assert store.get_pulse(pulse.id).state == "stopped"
    finally:
        store.close()
