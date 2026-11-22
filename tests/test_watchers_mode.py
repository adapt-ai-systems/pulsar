"""Watchers (2026-09-07): pulse mode flag, archive-with-monitoring choice,
condition-watch delivery tracking, watching-mode handoff addendum, and the
sleeping-watcher rotation guard.

    venv/bin/python -m pytest tests/test_watchers_mode.py -q
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import condwatch, handoff as handoff_mod, lifecycle, paths, scheduler, watch_sleep
from pulsar.store import Store
# Daemon fixtures (ASGI client + fake spawn/socket) — reused, not copied.
from tests.test_daemon import client, store, fake_spawn, fake_sock, data_root  # noqa: F401

NOW = datetime(2026, 9, 7, 12, 0, 0, tzinfo=timezone.utc)
T2 = NOW + timedelta(seconds=61)
T3 = NOW + timedelta(seconds=122)


def _iso(dt):
    return dt.isoformat(timespec="seconds")


def _inbox(pulse_id):
    d = paths.inbox_new(pulse_id)
    if not d.exists():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.glob("*.json"))]


# --------------------------------------------------------------------------- #
# PATCH /pulses/{id}/mode
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_mode_set_and_clear(client, store):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/mode", json={"mode": "watching"})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["settings"]["mode"] == "watching"
    assert store.get_pulse(pid).state == "running"  # orthogonal to lifecycle
    r = await client.patch(f"/pulses/{pid}/mode", json={"mode": "paused"})
    assert r.json()["value"]["settings"]["mode"] == "paused"
    r = await client.patch(f"/pulses/{pid}/mode", json={"mode": None})
    assert r.status_code == 200
    assert r.json()["value"]["settings"]["mode"] is None


@pytest.mark.anyio
async def test_mode_rejects_unknown_value_and_extra_fields(client):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/mode", json={"mode": "sleeping"})
    assert r.status_code == 422
    r = await client.patch(f"/pulses/{pid}/mode", json={"mode": "watching", "x": 1})
    assert r.status_code == 422


# --------------------------------------------------------------------------- #
# Archive vs live monitoring
# --------------------------------------------------------------------------- #
async def _pulse_with_monitoring(client, store):
    r = await client.post("/pulses", json={"title": "Campaign monitor", "model": "m"})
    pid = r.json()["value"]["id"]
    store.create_condition_watch(pid, "cmd", {"cmd": "true"},
                                 next_check_at=_iso(NOW))
    store.create_schedule(pid, "loop", "check the account", interval_minutes=60)
    return pid


@pytest.mark.anyio
async def test_archive_with_monitoring_requires_choice(client, store):
    pid = await _pulse_with_monitoring(client, store)
    r = await client.post(f"/pulses/{pid}/archive")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "monitoring_choice_required"
    assert store.get_pulse(pid).state != "archived"


@pytest.mark.anyio
async def test_archive_keep_switches_to_watching_mode(client, store, fake_spawn, fake_sock):
    pid = await _pulse_with_monitoring(client, store)
    r = await client.post(f"/pulses/{pid}/archive", json={"monitoring": "keep"})
    assert r.status_code == 200, r.text
    p = store.get_pulse(pid)
    assert p.state == "stopped"  # was running → put to sleep, NOT archived
    assert p.settings["mode"] == "watching"
    assert len(store.list_condition_watches(state="watching", pulse_id=pid)) == 1
    assert len(store.list_schedules(pulse_id=pid, enabled_only=True)) == 1


@pytest.mark.anyio
async def test_archive_stop_closes_watches_disables_schedules(client, store):
    pid = await _pulse_with_monitoring(client, store)
    r = await client.post(f"/pulses/{pid}/archive", json={"monitoring": "stop"})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).state == "archived"
    assert store.list_condition_watches(state="watching", pulse_id=pid) == []
    closed = store.list_condition_watches(state="closed", pulse_id=pid)
    assert [w.outcome for w in closed] == ["owner_archived"]
    assert store.list_schedules(pulse_id=pid, enabled_only=True) == []


@pytest.mark.anyio
async def test_archive_without_monitoring_unchanged(client, store):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r = await client.post(f"/pulses/{pid}/archive")
    assert r.status_code == 200
    assert r.json()["value"]["state"] == "archived"


@pytest.mark.anyio
async def test_archive_idle_never_sweeps_a_watcher(client, store):
    pid = await _pulse_with_monitoring(client, store)
    r2 = await client.post("/pulses", json={"title": "plain", "model": "m"})
    plain = r2.json()["value"]["id"]
    # Backdate both so they look idle.
    for p in (pid, plain):
        store._conn.execute("UPDATE pulses SET updated_at='2020-01-01T00:00:00+00:00' WHERE id=?", (p,))
    r = await client.post("/pulses/archive-idle", json={"idle_hours": 1})
    body = r.json()["value"]
    assert [a["id"] for a in body["archived"]] == [plain]
    assert [s["id"] for s in body["skipped"]] == [pid]
    assert "still monitoring" in body["skipped"][0]["error"]
    assert store.get_pulse(pid).state != "archived"


# --------------------------------------------------------------------------- #
# Condition-watch delivery tracking
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cw_store(tmp_path):
    s = Store(str(tmp_path / "cw.db"))
    yield s
    s.close()


def test_once_watch_closes_only_after_delivery(cw_store, tmp_path):
    owner = cw_store.create_pulse("Owner", "m", "v").id
    flag = tmp_path / "done.flag"
    w = cw_store.create_condition_watch(owner, "file", {"path": str(flag)},
                                        next_check_at=_iso(NOW), mode="once")
    flag.write_text("x")
    assert condwatch.tick(cw_store, NOW)["fired"] == 1
    got = cw_store.get_condition_watch(w.id)
    assert got.state == "closed" and got.outcome == "fired"
    assert got.last_delivery == "delivered"
    assert got.last_fired_at is not None
    assert len(_inbox(owner)) == 1


def test_delivery_failure_keeps_watch_and_retries(cw_store, tmp_path, monkeypatch):
    owner = cw_store.create_pulse("Owner", "m", "v").id
    flag = tmp_path / "done.flag"
    w = cw_store.create_condition_watch(owner, "file", {"path": str(flag)},
                                        next_check_at=_iso(NOW), mode="once")
    flag.write_text("x")
    calls = {"n": 0}
    real = condwatch._notify_owner

    def flaky(store, watch, text):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("spawn exploded")
        return real(store, watch, text)

    monkeypatch.setattr(condwatch, "_notify_owner", flaky)
    summary = condwatch.tick(cw_store, NOW)
    assert summary.get("fired", 0) == 0
    got = cw_store.get_condition_watch(w.id)
    assert got.state == "watching"           # NOT closed — message was lost
    assert got.last_delivery.startswith("failed: spawn exploded")
    assert not got.bad                       # re-armed so it retries
    assert _inbox(owner) == []
    # Next tick: condition still met → fires again, this time delivered.
    assert condwatch.tick(cw_store, T2)["fired"] == 1
    got = cw_store.get_condition_watch(w.id)
    assert got.state == "closed" and got.last_delivery == "delivered"
    assert len(_inbox(owner)) == 1


def test_edge_watch_retries_after_failed_delivery(cw_store, tmp_path, monkeypatch):
    owner = cw_store.create_pulse("Owner", "m", "v").id
    flag = tmp_path / "done.flag"
    w = cw_store.create_condition_watch(owner, "file", {"path": str(flag)},
                                        next_check_at=_iso(NOW), mode="edge")
    flag.write_text("x")
    monkeypatch.setattr(condwatch, "_notify_owner",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("down")))
    condwatch.tick(cw_store, NOW)
    assert cw_store.get_condition_watch(w.id).last_delivery == "failed: down"
    monkeypatch.undo()
    assert condwatch.tick(cw_store, T2)["fired"] == 1
    assert cw_store.get_condition_watch(w.id).last_delivery == "delivered"
    assert len(_inbox(owner)) == 1


# --------------------------------------------------------------------------- #
# Handoff addendum + sleeping-watcher rotation
# --------------------------------------------------------------------------- #
def test_handoff_prompt_adds_watch_brief_only_in_watching_mode(cw_store):
    p = cw_store.create_pulse("t", "claude-opus-5", "claude-code")
    assert "WATCHING mode" not in handoff_mod.handoff_prompt_for(p)
    p = cw_store.update_settings(p.id, {"mode": "watching"})
    text = handoff_mod.handoff_prompt_for(p)
    assert text.startswith(handoff_mod.HANDOFF_REQUEST)
    assert "Standing watch brief" in text and "RESUME WAITING" in text
    p = cw_store.update_settings(p.id, {"mode": "paused"})
    assert "WATCHING mode" not in handoff_mod.handoff_prompt_for(p)


def test_over_threshold(cw_store):
    p = cw_store.create_pulse("t", "claude-fable-5", "claude-code")  # 200k default
    assert not handoff_mod.over_threshold(p, None)
    assert not handoff_mod.over_threshold(p, {"last_ctx_tokens": 100})
    assert handoff_mod.over_threshold(p, {"last_ctx_tokens": 250_000})
    p = cw_store.update_settings(p.id, {"last_handoff_at":
                                        datetime.now(timezone.utc).isoformat()})
    assert not handoff_mod.over_threshold(p, {"last_ctx_tokens": 250_000})


def test_woken_watcher_over_threshold_stays_up_for_rotation(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    st = Store(str(tmp_path / "t.db"))
    pulse = st.create_pulse("Watcher", "claude-fable-5", "claude-code", state="stopped")
    watch_sleep.remember(st, pulse, "wake.json")
    st.set_state(pulse.id, "running")
    home = paths.pulse_home(pulse.id)
    for folder in ("new", "feeding", "acked"):
        (home / "inbox" / folder).mkdir(parents=True)
    (home / "inbox/acked/wake.json").write_text("{}")
    monkeypatch.setattr(paths, "heartbeat_fresh", lambda beat: True)
    stopped = []
    monkeypatch.setattr(units_mod, "stop_unit", lambda pid: stopped.append(pid) or True)
    pulse = st.get_pulse(pulse.id)
    # Over threshold → must NOT be put to sleep (handoff monitor needs it running).
    assert watch_sleep.finish(st, pulse, {"state": "idle", "last_ctx_tokens": 250_000}) is False
    assert stopped == [] and st.get_pulse(pulse.id).state == "running"
    # Under threshold → sleeps as before.
    assert watch_sleep.finish(st, pulse, {"state": "idle", "last_ctx_tokens": 1_000}) is True
    assert st.get_pulse(pulse.id).state == "stopped"
    st.close()


def test_schedule_wake_puts_watching_pulse_back_to_sleep(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    st = Store(str(tmp_path / "t.db"))
    woken = []
    monkeypatch.setattr(lifecycle, "wake_if_stopped", lambda s, p: woken.append(p.id))
    plain = st.create_pulse("plain", "m", "v", state="stopped")
    watcher = st.create_pulse("watcher", "m", "v", state="stopped")
    st.update_settings(watcher.id, {"mode": "watching"})
    s1 = st.create_schedule(plain.id, "loop", "go", interval_minutes=5)
    s2 = st.create_schedule(watcher.id, "loop", "check", interval_minutes=5)
    assert scheduler._fire(st, s1, NOW) == "fired"
    assert scheduler._fire(st, s2, NOW) == "fired"
    assert woken == [plain.id, watcher.id]
    assert watch_sleep.KEY not in st.get_pulse(plain.id).settings   # unchanged behaviour
    saved = st.get_pulse(watcher.id).settings[watch_sleep.KEY]
    assert saved["state"] == "stopped" and saved["message"].startswith("sched-")
    st.close()


# --------------------------------------------------------------------------- #
# Delete closes the pulse's own watchers instead of refusing
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_delete_closes_own_watchers_first(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "lead", "model": "m"})
    lead = r.json()["value"]["id"]
    r = await client.post("/pulses", json={"title": "helper", "model": "m"})
    helper = r.json()["value"]["id"]
    cw = store.create_condition_watch(lead, "cmd", {"cmd": "true"}, next_check_at=_iso(NOW))
    dw = store.create_watch(lead, helper, "spawn", _iso(NOW))
    await client.post(f"/pulses/{lead}/pause")
    assert store.has_pulse_watchers(lead)
    # The pane previews what would be closed, then confirms.
    r = await client.get(f"/pulses/{lead}/watchers")
    assert r.status_code == 200
    body = r.json()["value"]
    assert [w["id"] for w in body["condition"]] == [cw.id]
    assert [w["id"] for w in body["delegation"]] == [dw.id]
    assert body["delegation"][0]["target_title"] == "helper"
    r = await client.delete(f"/pulses/{lead}")
    assert r.status_code == 409 and r.json()["error"]["code"] == "has_watchers"
    assert store.get_pulse(lead).title == "lead"
    r = await client.delete(f"/pulses/{lead}?close_watchers=true")
    assert r.status_code == 200, r.text
    with pytest.raises(Exception):
        store.get_pulse(lead)
    # Rows cascade-deleted with the pulse; the helper is untouched.
    assert store.get_pulse(helper).title == "helper"
    assert store.list_condition_watches(pulse_id=lead) == []
    assert all(w.id != dw.id for w in store.list_watches())
