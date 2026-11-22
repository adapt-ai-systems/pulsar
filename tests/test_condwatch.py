"""Condition-watch tests — store CRUD + evaluator tick + routes.

    venv/bin/python -m pytest tests/test_condwatch.py -q

No real runners: pulses are created through the store directly, heartbeats are
written by hand into a tmp data dir, http checks run against a live local
http.server on an ephemeral port. Frozen `now` throughout — no sleeping.
"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, HTTPServer

import httpx
import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import condwatch
from pulsar.daemon.app import create_app
from pulsar.store import Store

NOW = datetime(2026, 8, 29, 12, 0, 0, tzinfo=timezone.utc)
T2 = NOW + timedelta(seconds=61)   # after one default-interval bump
T3 = NOW + timedelta(seconds=122)
T4 = NOW + timedelta(seconds=183)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


@pytest.fixture()
def store(tmp_path):
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def owner(store):
    """The pulse that owns the watches under test."""
    return store.create_pulse("Owner", "m", "v").id


def _advance(store, watch_id, minutes=1):
    """Re-due a watch NOW + minutes after a tick (bumps move next_check_at)."""
    w = store.get_condition_watch(watch_id)
    return w


def _run_tick(store, now=NOW):
    return condwatch.tick(store, now)


def _inbox(pulse_id, tmp_path):
    from pulsar.daemon import paths
    d = paths.inbox_new(pulse_id)
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


# --------------------------------------------------------------------------- #
# Store CRUD
# --------------------------------------------------------------------------- #
def test_create_and_roundtrip(store, owner):
    w = store.create_condition_watch(
        owner, "log", {"path": "/tmp/x.log", "pattern": "ERROR"},
        next_check_at=_iso(NOW))
    got = store.get_condition_watch(w.id)
    assert got.kind == "log"
    assert got.spec == {"path": "/tmp/x.log", "pattern": "ERROR"}
    assert got.state == "watching"
    assert got.mode == "edge"
    assert got.interval_s == 60
    assert got.bad is None
    assert got.detail is None


def test_create_rejects_bad_kind_and_mode(store, owner):
    with pytest.raises(ValueError):
        store.create_condition_watch(owner, "bogus", {}, next_check_at=_iso(NOW))
    with pytest.raises(ValueError):
        store.create_condition_watch(owner, "cmd", {"cmd": "true"},
                                     next_check_at=_iso(NOW), mode="bogus")
    with pytest.raises(Exception):
        store.create_condition_watch("nope", "cmd", {"cmd": "true"},
                                     next_check_at=_iso(NOW))


def test_bump_and_close(store, owner):
    w = store.create_condition_watch(owner, "cmd", {"cmd": "true"},
                                     next_check_at=_iso(NOW))
    store.bump_condition_watch(w.id, _iso(NOW), True, {"returncode": 1})
    got = store.get_condition_watch(w.id)
    assert got.checks_done == 1
    assert got.bad  # stored as SQLite int 1/0
    assert got.detail == {"returncode": 1}
    store.close_condition_watch(w.id, "fired")
    got = store.get_condition_watch(w.id)
    assert got.state == "closed"
    assert got.outcome == "fired"
    assert store.list_condition_watches(state="watching") == []


def test_delete_pulse_requires_removing_active_watch(store, owner):
    w = store.create_condition_watch(owner, "cmd", {"cmd": "true"},
                                     next_check_at=_iso(NOW))
    from pulsar.store.db import InvalidStateTransition
    with pytest.raises(InvalidStateTransition, match="active watchers"):
        store.delete_pulse(owner)
    assert store.get_condition_watch(w.id).state == "watching"
    store.delete_condition_watch(w.id)
    store.delete_pulse(owner)


# --------------------------------------------------------------------------- #
# Spec validation
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_route_rejects_bad_specs(store, owner):
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
        async with app.router.lifespan_context(app):
            r = await c.post("/watches/conditions", json={
                "kind": "log", "pulse_id": owner, "spec": {"path": "/tmp/x"}})
            assert r.status_code == 400
            r = await c.post("/watches/conditions", json={
                "kind": "bogus", "pulse_id": owner, "spec": {"a": 1}})
            assert r.status_code == 400
            r = await c.post("/watches/conditions", json={
                "kind": "cmd", "pulse_id": owner, "spec": {"cmd": "true"},
                "interval_seconds": 5})
            assert r.status_code == 400


# --------------------------------------------------------------------------- #
# Evaluator: each kind
# --------------------------------------------------------------------------- #
def test_file_watch_fires_once_mode(store, owner, tmp_path):
    flag = tmp_path / "done.flag"
    w = store.create_condition_watch(
        owner, "file", {"path": str(flag)}, next_check_at=_iso(NOW),
        mode="once")
    # Not there yet → clean.
    summary = _run_tick(store)
    assert summary["clean"] == 1
    # Appears → fires once and CLOSES (mode once).
    flag.write_text("done")
    summary = _run_tick(store, T2)
    assert summary["fired"] == 1
    assert store.get_condition_watch(w.id).state == "closed"
    msgs = _inbox(owner, tmp_path)
    assert len(msgs) == 1
    assert "condition met" in msgs[0]["text"]
    assert msgs[0]["source"] == "watch"


def test_file_watch_edge_rearms(store, owner, tmp_path):
    flag = tmp_path / "ping.flag"
    w = store.create_condition_watch(
        owner, "file", {"path": str(flag)}, next_check_at=_iso(NOW),
        mode="edge")
    flag.write_text("x")
    assert _run_tick(store)["fired"] == 1
    # Still exists → bad but quiet (edge: no refire while it stays met).
    assert _run_tick(store, T2)["bad_quiet"] == 1
    assert len(_inbox(owner, tmp_path)) == 1
    # Gone (cleared) → re-arms; back again → fires again.
    flag.unlink()
    assert _run_tick(store, T3)["clean"] == 1
    flag.write_text("x")
    assert _run_tick(store, T4)["fired"] == 1
    assert len(_inbox(owner, tmp_path)) == 2


def test_log_watch_only_new_bytes(store, owner, tmp_path):
    log = tmp_path / "app.log"
    log.write_text("line1\n")
    w = store.create_condition_watch(
        owner, "log", {"path": str(log), "pattern": "Traceback"},
        next_check_at=_iso(NOW))
    # Old bytes with no match → clean, offset advances.
    assert _run_tick(store)["clean"] == 1    # Append a match → fires.
    with log.open("a") as fh:
        fh.write("boom\nTraceback (most recent call last)\n")
    assert _run_tick(store, T2)["fired"] == 1
    # Offset semantics: the same match is never re-fired (it was consumed),
    # and the next check with no NEW bytes is clean — the watch re-arms.
    assert _run_tick(store, T3)["clean"] == 1
    assert len(_inbox(owner, tmp_path)) == 1
    # Rotated (truncated) → offset resets, no false fire without a match.
    log.write_text("fresh start\n")
    assert _run_tick(store, T4)["clean"] == 1


def test_cmd_watch_fires_on_nonzero(store, owner, tmp_path):
    w = store.create_condition_watch(
        owner, "cmd", {"cmd": "exit 3"}, next_check_at=_iso(NOW), mode="once")
    summary = _run_tick(store, T2)
    assert summary["fired"] == 1
    msgs = _inbox(owner, tmp_path)
    assert len(msgs) == 1
    assert "returncode: 3" in msgs[0]["text"]
    assert store.get_condition_watch(w.id).state == "closed"


def test_cmd_watch_clean_on_zero(store, owner):
    store.create_condition_watch(
        owner, "cmd", {"cmd": "true"}, next_check_at=_iso(NOW))
    assert _run_tick(store)["clean"] == 1


def _http_server():
    class H(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/ok":
                self.send_response(200)
                self.end_headers()
                self.wfile.write(b"all good")
            elif self.path == "/500":
                self.send_response(500)
                self.end_headers()
            else:
                self.send_response(404)
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), H)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv


def test_http_watch(store, owner):
    srv = _http_server()
    try:
        base = f"http://127.0.0.1:{srv.server_port}"
        w_ok = store.create_condition_watch(
            owner, "http", {"url": f"{base}/ok"}, next_check_at=_iso(NOW))
        w_bad = store.create_condition_watch(
            owner, "http", {"url": f"{base}/500"}, next_check_at=_iso(NOW),
            mode="once")
        w_body = store.create_condition_watch(
            owner, "http", {"url": f"{base}/ok", "contains": "all good"},
            next_check_at=_iso(NOW))
        w_miss = store.create_condition_watch(
            owner, "http", {"url": f"{base}/ok", "contains": "not there"},
            next_check_at=_iso(NOW), mode="once")
        w_dead = store.create_condition_watch(
            owner, "http", {"url": "http://127.0.0.1:1/nope"},
            next_check_at=_iso(NOW))
        summary = _run_tick(store)
        assert summary["clean"] == 2          # /ok, and /ok with body present
        assert summary["fired"] == 3          # /500, missing body text, dead
        assert store.get_condition_watch(w_ok.id).state == "watching"
        assert store.get_condition_watch(w_bad.id).state == "closed"
        assert store.get_condition_watch(w_body.id).state == "watching"
        assert store.get_condition_watch(w_miss.id).state == "closed"
        # Edge-mode fire does NOT close — stays watching, quiet while bad.
        assert store.get_condition_watch(w_dead.id).state == "watching"
    finally:
        srv.shutdown()


def test_pulse_state_watch(store, owner, tmp_path):
    from pulsar.daemon import paths
    helper = store.create_pulse("Helper", "m", "v").id
    w = store.create_condition_watch(
        owner, "pulse_state",
        {"target_id": helper, "stopped": True, "max_ctx": 100000},
        next_check_at=_iso(NOW))
    # Healthy helper → clean.
    assert _run_tick(store)["clean"] == 1
    # Helper stops → fires.
    store.set_state(helper, "stopped")
    store.update_settings(helper, {"stop_reason": "oom"})
    assert _run_tick(store, T2)["fired"] == 1
    msgs = _inbox(owner, tmp_path)
    assert len(msgs) == 1 and "oom" in msgs[0]["text"]
    # Target deleted → watch closes target_gone.
    store.delete_pulse(helper)
    assert _run_tick(store, T3)["target_gone"] == 1
    assert store.get_condition_watch(w.id).outcome == "target_gone"


def test_pulse_state_ctx_ceiling(store, owner, tmp_path):
    from pulsar.daemon import paths
    helper = store.create_pulse("CtxHelper", "m", "v").id
    store.create_condition_watch(
        owner, "pulse_state", {"target_id": helper, "max_ctx": 100000},
        next_check_at=_iso(NOW))
    beat = paths.heartbeat_path(helper)
    beat.parent.mkdir(parents=True, exist_ok=True)
    beat.write_text(json.dumps({
        "state": "busy", "last_ctx_tokens": 220000,
        "ts": _iso(datetime.now(timezone.utc))}))
    assert _run_tick(store)["fired"] == 1


@pytest.mark.parametrize("state", ["archived", "stopped", "running"])
@pytest.mark.parametrize("mode", ["edge", "once"])
def test_trigger_restores_owner(store, owner, tmp_path, monkeypatch, state, mode):
    spawned = []
    monkeypatch.setattr(units_mod, "spawn_runner", lambda *args, **kwargs: spawned.append(args[0]))
    flag = tmp_path / "x.flag"
    watch = store.create_condition_watch(
        owner, "file", {"path": str(flag)}, next_check_at=_iso(NOW), mode=mode)
    if state != "running":
        store.set_state(owner, state)
    store.update_settings(owner, {"stop_reason": "user"})
    # Archiving/pausing does not cancel the watch or wake the owner early.
    assert _run_tick(store)["clean"] == 1
    assert store.get_pulse(owner).state == state
    assert store.get_condition_watch(watch.id).state == "watching"
    flag.write_text("ready")
    assert _run_tick(store, T2)["fired"] == 1
    restored = store.get_pulse(owner)
    assert restored.state == "running"
    assert restored.settings.get("mode") == ("watching" if state == "archived" else None)
    assert spawned == ([] if state == "running" else [owner])
    if state != "running":
        assert restored.settings["stop_reason"] is None
    assert len(_inbox(owner, tmp_path)) == 1


def test_closed_watch_does_not_block_delete(store, owner):
    watch = store.create_condition_watch(owner, "cmd", {"cmd": "true"},
                                          next_check_at=_iso(NOW))
    store.close_condition_watch(watch.id, "fired")
    store.delete_pulse(owner)


def test_not_due_until_interval(store, owner, tmp_path):
    flag = tmp_path / "y.flag"
    store.create_condition_watch(
        owner, "file", {"path": str(flag)}, next_check_at=_iso(NOW),
        interval_s=600)
    flag.write_text("x")
    # Fire at NOW; the next check is NOW+600s — NOW+30 must NOT refire.
    assert _run_tick(store)["fired"] == 1
    assert _run_tick(store, NOW + timedelta(seconds=30))["due"] == 0
    assert len(_inbox(owner, tmp_path)) == 1
    later = NOW + timedelta(seconds=601)
    w = store.list_condition_watches(state="watching")[0]
    store.bump_condition_watch(w.id, _iso(later), w.bad, w.detail)
    assert _run_tick(store, later)["bad_quiet"] == 1


def test_evaluator_surprise_retries_later_not_every_tick(store, owner,
                                                         tmp_path):
    """A raised check bumps next_check_at — no 30s retry storm."""
    w = store.create_condition_watch(
        owner, "http", {"url": "http://127.0.0.1:1/nope"},
        next_check_at=_iso(NOW))
    # (dead port fires clean via the evaluator; force a surprise instead)
    import pulsar.daemon.condwatch as cw
    orig = cw._eval_http
    cw._eval_http = lambda spec: (_ for _ in ()).throw(RuntimeError("boom"))
    try:
        summary = _run_tick(store)
    finally:
        cw._eval_http = orig
    assert summary["failed"] == 1
    got = store.get_condition_watch(w.id)
    assert got.next_check_at >= _iso(NOW + timedelta(seconds=59))
    assert got.checks_done == 1


def test_log_watch_baselines_ignores_preexisting_match(store, owner, tmp_path):
    log = tmp_path / "old.log"
    log.write_text("Traceback from yesterday\n")
    w = store.create_condition_watch(
        owner, "log", {"path": str(log), "pattern": "Traceback"},
        next_check_at=_iso(NOW))
    # First check baselines at EOF — stale history must not wake the owner.
    assert _run_tick(store)["clean"] == 1
    assert store.get_condition_watch(w.id).detail["offset"] == log.stat().st_size
    assert _inbox(owner, tmp_path) == []
    with log.open("a") as fh:
        fh.write("Traceback (fresh)\n")
    assert _run_tick(store, T2)["fired"] == 1
    assert len(_inbox(owner, tmp_path)) == 1


def test_invert_fires_when_condition_not_met(store, owner, tmp_path):
    # cmd + invert: wait for a command to START succeeding.
    marker = tmp_path / "up.flag"
    w = store.create_condition_watch(
        owner, "cmd", {"cmd": f"test -f {marker}", "invert": True},
        next_check_at=_iso(NOW), mode="once")
    assert _run_tick(store)["clean"] == 1          # failing cmd, inverted → clean
    marker.write_text("1")
    assert _run_tick(store, T2)["fired"] == 1      # succeeding cmd → fires
    msgs = _inbox(owner, tmp_path)
    assert len(msgs) == 1 and "inverted: True" in msgs[0]["text"]
    assert store.get_condition_watch(w.id).state == "closed"


def test_validate_invert_and_timeout_cap():
    assert condwatch.validate_spec("cmd", {"cmd": "true", "invert": "yes"})
    assert condwatch.validate_spec("cmd", {"cmd": "true", "timeout_s": 60})
    assert condwatch.validate_spec("http", {"url": "http://x", "timeout_s": "n"})
    assert condwatch.validate_spec("cmd", {"cmd": "true", "invert": True,
                                           "timeout_s": 20}) == []


def test_tests_never_touch_live_data_root(owner, tmp_path):
    from pulsar.daemon import paths
    assert str(paths.inbox_new(owner)).startswith(str(tmp_path))


def test_open_watch_cap_per_pulse(store, owner, tmp_path):
    from pulsar.store.db import COND_WATCH_MAX_OPEN
    for i in range(COND_WATCH_MAX_OPEN):
        store.create_condition_watch(
            owner, "file", {"path": str(tmp_path / f"f{i}")}, next_check_at=_iso(NOW))
    with pytest.raises(ValueError, match="open condition watches"):
        store.create_condition_watch(
            owner, "file", {"path": str(tmp_path / "one-more")}, next_check_at=_iso(NOW))
    # Closing one frees a slot.
    first = store.list_condition_watches(state="watching", pulse_id=owner)[0]
    store.close_condition_watch(first.id, "fired")
    store.create_condition_watch(
        owner, "file", {"path": str(tmp_path / "one-more")}, next_check_at=_iso(NOW))


@pytest.mark.parametrize("owner_state", ["running", "stopped", "archived"])
@pytest.mark.parametrize("mode", ["edge", "once"])
def test_archived_target_closes_silently(store, owner, tmp_path, monkeypatch,
                                        owner_state, mode):
    store.set_state(owner, owner_state)
    notifications = []
    monkeypatch.setattr(condwatch, "_notify_owner", lambda *a: notifications.append(a))
    target = store.create_pulse("Scout", "m", "v").id
    w = store.create_condition_watch(
        owner, "pulse_state", {"target_id": target, "stopped": True, "stale": True},
        next_check_at=_iso(NOW), mode=mode)
    store.archive_pulse(target)
    assert _run_tick(store)["target_gone"] == 1
    assert store.get_condition_watch(w.id).state == "closed"
    msgs = _inbox(owner, tmp_path)
    assert msgs == []
    assert notifications == []
    assert store.get_pulse(owner).state == owner_state
    assert _run_tick(store, T2)["due"] == 0   # closed: never checked again


def test_pulse_state_idle_edge(store, owner, tmp_path):
    """--idle: fires when the helper's runner reports idle (turn ended) while
    the pulse is still store-state running; edge mode refires per turn."""
    from pulsar.daemon import paths
    helper = store.create_pulse("IdleHelper", "m", "v").id
    store.create_condition_watch(
        owner, "pulse_state", {"target_id": helper, "idle": True},
        mode="edge", next_check_at=_iso(NOW))
    beat = paths.heartbeat_path(helper)
    beat.parent.mkdir(parents=True, exist_ok=True)

    def hb(state):   # heartbeat freshness is judged against the wall clock
        beat.write_text(json.dumps({"ts": _iso(datetime.now(timezone.utc)),
                                    "state": state, "last_ctx_tokens": 10}))
    hb("busy")
    assert _run_tick(store)["clean"] == 1
    hb("idle")
    assert _run_tick(store, T2)["fired"] == 1
    assert "turn ended" in _inbox(owner, tmp_path)[-1]["text"]
    hb("idle")
    assert _run_tick(store, T3)["fired"] == 0        # edge: no refire while idle
    hb("busy")
    _run_tick(store, T3 + timedelta(seconds=61))
    hb("idle")
    assert _run_tick(store, T3 + timedelta(seconds=122))["fired"] == 1  # next turn end refires
