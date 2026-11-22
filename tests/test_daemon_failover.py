"""Model failover tests (daemon/failover.py).

    venv/bin/python -m pytest tests/test_daemon_failover.py -q

No real runners or models: spawn/stop go through test_daemon's FakeSpawn and
FakeSock, and the end-to-end cases run the REAL handoff engine on top of them
so the re-sent message is proven to survive the handoff's inbox retirement.
"""

from __future__ import annotations

import json
import time

import pytest

from pulsar.daemon import failover as failover_mod
from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import lifespan as lifespan_mod
from pulsar.daemon import paths
from tests.test_daemon import (  # noqa: F401
    client, data_root, fake_sock, fake_spawn, store)

pytestmark = pytest.mark.anyio

PRIMARY = "gpt-6-luna"          # a codex pulse
BACKUP = {"model": "claude-opus-5-5", "effort": "low"}


@pytest.fixture(autouse=True)
def quick_handoff(monkeypatch):
    """The fake runner never writes a stopped heartbeat — skip the settle."""
    monkeypatch.setattr(handoff_mod.engine, "_wait_stopped",
                        lambda *a, **k: None)


def _mk(store, *, state="stopped", stop_reason="vessel error: boom",
        backup=BACKUP, model=PRIMARY, vessel="codex", **extra):
    settings = {"stop_reason": stop_reason, **extra}
    if backup is not None:
        settings["backup"] = dict(backup)
    return store.create_pulse("fo", model, vessel, state=state,
                              settings=settings)


def _monitor(store):
    # Created BEFORE the failures under test happen — nothing is "old".
    return failover_mod.Monitor(store, now=time.time() - 3600)


def _events(pid):
    path = paths.events_path(pid)
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


def _lines(pid):
    """The failover pane lines: the success marker and the red failures."""
    return [e for e in _events(pid)
            if e.get("type") == "failover"
            or (e.get("type") == "runner_error"
                and e.get("where") == "failover")]


def _msg(pid, folder, name, text):
    d = paths.pulse_home(pid) / "inbox" / folder
    d.mkdir(parents=True, exist_ok=True)
    (d / name).write_text(json.dumps({"text": text}))


def _heartbeat(pid, **beat):
    home = paths.pulse_home(pid)
    home.mkdir(parents=True, exist_ok=True)
    (home / "heartbeat.json").write_text(json.dumps(
        {"ts": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "state": "idle",
         "protocol": 1, **beat}))


def _spy_handoff(monkeypatch, ok=True):
    calls = []

    async def fake(store_, pulse_, **kwargs):
        calls.append(kwargs)
        return ok

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake)
    return calls


# --------------------------------------------------------------------------- #
# (a) death with a reason -> failover + re-send + one pane line
# --------------------------------------------------------------------------- #
async def test_death_fails_over_resends_in_flight_and_posts_one_line(
        store, fake_spawn, fake_sock):
    p = _mk(store, stop_reason="cli died (rc=1)", effort="high")
    # The dead runner was mid-turn on this message: claimed AND fed (its
    # user_message is in the transcript, so a plain forced handoff would
    # retire it as already received).
    _msg(p.id, "feeding", "20261014T000000-a.json", "do the work")
    paths.events_path(p.id).write_text(json.dumps(
        {"type": "user_message", "text": "do the work",
         "msg_file": "20261014T000000-a.json"}) + "\n")
    _msg(p.id, "new", "20261014T000001-b.json", "queued after")

    m = _monitor(store)
    assert await m.tick() == 1

    after = store.get_pulse(p.id)
    assert after.state == "running"
    assert (after.model, after.vessel) == ("claude-opus-5-5", "claude-code")
    assert after.settings.get("stop_reason") is None
    assert after.settings["effort"] == "low"          # the backup's effort
    assert after.settings["failover"]["from"] == PRIMARY
    assert after.settings["failover"]["to"] == "claude-opus-5-5"
    spawn = fake_spawn.spawned[-1]
    assert (spawn["model"], spawn["vessel"], spawn["resume"]) == (
        "claude-opus-5-5", "claude-code", None)
    new = sorted(f.name for f in
                 (paths.pulse_home(p.id) / "inbox" / "new").glob("*.json"))
    assert "20261014T000000-a.json" in new            # re-sent
    assert "20261014T000001-b.json" in new            # still queued
    assert any(n.startswith("handoff-seed") for n in new)
    [line] = _lines(p.id)
    assert line == {**line, "type": "failover", "from": PRIMARY,
                    "to": "claude-opus-5-5", "reason": "cli died (rc=1)",
                    "resent": ["20261014T000000-a.json"]}
    assert store.list_questions(pulse_id=p.id) == []   # nothing pinned
    # Running on the backup now: the next tick does nothing.
    assert await m.tick() == 0
    assert len(_lines(p.id)) == 1


@pytest.mark.parametrize("reason", [
    "vessel error: 529 overloaded", "spawn failed: no such model",
    "runner died (signal)", "hit memory cap", "handoff respawn failed"])
async def test_every_failure_reason_triggers(store, monkeypatch, reason):
    calls = _spy_handoff(monkeypatch)
    _mk(store, stop_reason=reason)
    assert await _monitor(store).tick() == 1
    assert calls[0]["new_model"] == "claude-opus-5-5"
    assert calls[0]["new_vessel"] == "claude-code"
    assert calls[0]["force"] is True


# --------------------------------------------------------------------------- #
# (b) provider errors on 2 turns in a row
# --------------------------------------------------------------------------- #
async def test_two_provider_error_turns_fail_over_and_resend_the_turn(
        store, fake_spawn, fake_sock):
    p = _mk(store, state="running", stop_reason=None)
    fake_spawn.units.append(p.id)
    _msg(p.id, "acked", "20261014T000002-c.json", "second try")
    _heartbeat(p.id, provider_error_turns=2,
               provider_error_msg="20261014T000002-c.json",
               reason="provider error: 503")

    assert await _monitor(store).tick() == 1

    after = store.get_pulse(p.id)
    assert after.state == "running"
    assert after.model == "claude-opus-5-5"
    assert p.id in fake_sock.stopped                   # the old runner
    home = paths.pulse_home(p.id)
    assert (home / "inbox" / "new" / "20261014T000002-c.json").exists()
    assert not (home / "inbox" / "acked" / "20261014T000002-c.json").exists()
    [line] = _lines(p.id)
    assert line["reason"] == ("provider error on 2 turns in a row: "
                              "provider error: 503")
    assert line["resent"] == ["20261014T000002-c.json"]


async def test_one_provider_error_then_a_good_turn_does_not_fail_over(
        store, monkeypatch):
    calls = _spy_handoff(monkeypatch)
    p = _mk(store, state="running", stop_reason=None)
    m = _monitor(store)
    _heartbeat(p.id, provider_error_turns=1, provider_error_msg="x.json")
    assert await m.tick() == 0
    _heartbeat(p.id, provider_error_turns=0, provider_error_msg=None)
    assert await m.tick() == 0
    assert calls == []
    assert _lines(p.id) == []


async def test_provider_error_failure_is_acted_on_once(store, monkeypatch):
    calls = _spy_handoff(monkeypatch, ok=False)
    monkeypatch.setattr(failover_mod, "_stop_running",
                        lambda st, pid, reason: st.update_settings(
                            pid, {"stop_reason": reason}))
    p = _mk(store, state="running", stop_reason=None)
    _heartbeat(p.id, provider_error_turns=2, provider_error_msg="x.json")
    m = _monitor(store)
    assert await m.tick() == 0
    assert await m.tick() == 0
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Deliberate stops never fail over
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("reason", [None, "", "watch work complete"])
async def test_pause_stop_and_non_failures_never_fail_over(
        store, monkeypatch, reason):
    calls = _spy_handoff(monkeypatch)
    p = _mk(store, stop_reason=reason)
    assert await _monitor(store).tick() == 0
    assert calls == []
    assert _lines(p.id) == []


async def test_pause_route_does_not_fail_over(client, store, fake_spawn,
                                              fake_sock, monkeypatch):
    r = await client.post("/pulses", json={"title": "p", "model": PRIMARY})
    pid = r.json()["value"]["id"]
    store.update_settings(pid, {"backup": dict(BACKUP)})
    r = await client.post(f"/pulses/{pid}/pause")
    assert r.status_code == 200, r.text
    calls = _spy_handoff(monkeypatch)
    assert await _monitor(store).tick() == 0
    assert calls == []
    assert store.get_pulse(pid).model == PRIMARY


# --------------------------------------------------------------------------- #
# No ping-pong
# --------------------------------------------------------------------------- #
async def test_backup_also_fails_stays_stopped_even_after_restart(
        store, monkeypatch):
    calls = _spy_handoff(monkeypatch)
    p = _mk(store, model="claude-opus-5-5", vessel="claude-code",
            stop_reason="cli died (rc=1)",
            failover={"from": PRIMARY, "to": "claude-opus-5-5",
                      "reason": "vessel error", "at": "t"},
            backup={"model": PRIMARY})
    m = _monitor(store)
    assert await m.tick() == 0
    after = store.get_pulse(p.id)
    assert after.state == "stopped"
    assert after.model == "claude-opus-5-5"
    assert after.settings["stop_reason"].startswith(
        "failover: claude-opus-5-5 (backup) also failed: cli died (rc=1)")
    assert len(_lines(p.id)) == 1
    assert await m.tick() == 0
    # A daemon restart (fresh monitor, the row now looks old or new — both
    # ways) does not move it either, and posts nothing more.
    for now in (time.time() - 3600, time.time() + 3600):
        assert await failover_mod.Monitor(store, now=now).tick() == 0
    assert calls == []
    assert len(_lines(p.id)) == 1


async def test_backup_also_fails_on_provider_errors_stops_it(store,
                                                             monkeypatch):
    calls = _spy_handoff(monkeypatch)
    stopped = []
    monkeypatch.setattr(failover_mod, "_stop_running",
                        lambda st, pid, reason: (
                            stopped.append(pid),
                            st.set_state(pid, "stopped"),
                            st.update_settings(pid, {"stop_reason": reason})))
    p = _mk(store, state="running", stop_reason=None,
            model="claude-opus-5-5", vessel="claude-code",
            failover={"from": PRIMARY, "to": "claude-opus-5-5"})
    _heartbeat(p.id, provider_error_turns=2, provider_error_msg="x.json")
    assert await _monitor(store).tick() == 0
    assert calls == [] and stopped == [p.id]
    assert store.get_pulse(p.id).settings["stop_reason"].startswith(
        "failover: claude-opus-5-5 (backup) also failed")


async def test_failed_move_leaves_it_stopped_with_one_line(store,
                                                           monkeypatch):
    calls = _spy_handoff(monkeypatch, ok=False)
    p = _mk(store, effort="high")
    m = _monitor(store)
    assert await m.tick() == 0
    after = store.get_pulse(p.id)
    assert after.state == "stopped"
    assert after.settings["stop_reason"].startswith(
        "failover to claude-opus-5-5 failed")
    assert after.settings["effort"] == "high"          # old effort back
    assert len(_lines(p.id)) == 1
    assert await m.tick() == 0                         # never retried
    assert len(calls) == 1
    assert len(_lines(p.id)) == 1


@pytest.mark.parametrize("backup", [None, {"model": PRIMARY}])
async def test_no_usable_backup_leaves_the_pulse_as_it_is(store,
                                                          monkeypatch,
                                                          backup):
    calls = _spy_handoff(monkeypatch)
    p = _mk(store, backup=backup)
    assert await _monitor(store).tick() == 0
    assert calls == []
    assert store.get_pulse(p.id).settings["stop_reason"] == \
        "vessel error: boom"
    assert _lines(p.id) == []


async def test_switching_the_model_clears_the_failover_marker(
        client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": PRIMARY})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    store.update_settings(pid, {"failover": {"from": "x", "to": PRIMARY}})
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "gpt-6-sol"})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("failover") is None


# --------------------------------------------------------------------------- #
# Boot guard
# --------------------------------------------------------------------------- #
async def test_old_failures_at_boot_are_left_alone_until_they_run_again(
        store, monkeypatch):
    calls = _spy_handoff(monkeypatch)
    old = _mk(store, stop_reason="runner died (signal)")
    m = failover_mod.Monitor(store, now=time.time()
                             + failover_mod.BOOT_GRACE_S + 60)
    assert await m.tick() == 0
    # Touching the row later (rename, settings edit) refreshes updated_at
    # but does not turn the old failure into a failover.
    store.update_settings(old.id, {"handoff_tokens_note": "x"})
    store.set_title(old.id, "renamed")
    assert await m.tick() == 0
    assert calls == []
    # It runs again and later fails anew: that IS a failover.
    store.set_state(old.id, "running")
    assert await m.tick() == 0
    store.update_settings(old.id, {"stop_reason": "cli died (rc=2)"})
    store.set_state(old.id, "stopped")
    assert await m.tick() == 1
    assert len(calls) == 1


async def test_unarchived_pulse_with_an_old_reason_does_not_fail_over(
        store, monkeypatch):
    """Unarchiving a previously failure-stopped pulse must not trigger
    automatic failover to its backup model."""
    calls = _spy_handoff(monkeypatch)
    p = _mk(store, state="archived", stop_reason="runner exited without "
            "recording a reason")
    m = _monitor(store)
    assert await m.tick() == 0
    store.set_state(p.id, "stopped")          # unarchive -> stopped
    assert await m.tick() == 0
    assert calls == []
    # It runs again and later fails anew: that IS a failover.
    store.set_state(p.id, "running")
    assert await m.tick() == 0
    store.update_settings(p.id, {"stop_reason": "cli died (rc=2)"})
    store.set_state(p.id, "stopped")
    assert await m.tick() == 1
    assert len(calls) == 1


async def test_pulse_archived_after_boot_is_also_guarded(store, monkeypatch):
    calls = _spy_handoff(monkeypatch)
    m = _monitor(store)
    assert await m.tick() == 0                 # baseline taken, no pulses
    p = _mk(store, state="archived", stop_reason="vessel error: old")
    assert await m.tick() == 0                 # seen while archived
    store.set_state(p.id, "stopped")
    assert await m.tick() == 0
    assert calls == []


async def test_boot_baseline_is_taken_before_the_boot_migrations(
        store, monkeypatch):
    """lifespan order: take_baseline, THEN retire_stop_ask. Its settings
    writes bump updated_at; taken after it, the baseline would see every old
    failure as fresh."""
    calls = _spy_handoff(monkeypatch)
    _mk(store, stop_reason="runner died (signal)",
        stop_ask={"choice": "pending"})
    m = failover_mod.Monitor(store, now=time.time()
                             + failover_mod.BOOT_GRACE_S + 60)
    m.take_baseline()
    assert failover_mod.retire_stop_ask(store) == 1
    assert await m.tick() == 0
    assert calls == []
    src = open(lifespan_mod.__file__).read()
    assert (src.index("failover_monitor.take_baseline()")
            < src.index("failover_mod.retire_stop_ask(store_)"))


async def test_a_pulse_that_died_during_the_restart_fails_over(store,
                                                               monkeypatch):
    calls = _spy_handoff(monkeypatch)
    _mk(store, stop_reason="runner died (signal)")   # reconcile: just now
    assert await failover_mod.Monitor(store).tick() == 1
    assert len(calls) == 1


# --------------------------------------------------------------------------- #
# Boot: stop-and-ask leftovers
# --------------------------------------------------------------------------- #
def test_retire_stop_ask_dismisses_open_questions_and_clears_markers(store):
    p = _mk(store)
    q = store.create_question(p.id, "Move or wait?",
                              choices=["Move", "Wait"])
    store.update_settings(p.id, {"stop_ask": {"question_id": q.id,
                                              "choice": "pending"}})
    assert failover_mod.retire_stop_ask(store) == 1
    assert store.get_question(q.id).state == "dismissed"
    assert store.get_pulse(p.id).settings.get("stop_ask") is None
    assert failover_mod.retire_stop_ask(store) == 0


# --------------------------------------------------------------------------- #
# User-configured default backup setting
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("model", ["claude-opus-5-5", "gpt-6-luna"])
async def test_create_without_default_backup_setting_has_no_backup(
        client, store, fake_spawn, fake_sock, model):
    r = await client.post("/pulses", json={"title": "d", "model": model})
    assert r.status_code == 201, r.text
    pulse = store.get_pulse(r.json()["value"]["id"])
    assert "backup" not in pulse.settings
    assert failover_mod.backup_for(pulse) is None


async def test_create_with_default_backup_setting_copies_it(
        client, store, fake_spawn, fake_sock):
    want = {"model": "gpt-6-sol", "effort": "high"}
    put = await client.put("/settings/default_backup", json=want)
    assert put.status_code == 200, put.text
    assert put.json()["value"] == want

    r = await client.post("/pulses",
                          json={"title": "d", "model": "claude-opus-5-5"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings["backup"] == want

    # Clearing the setting leaves existing pulses alone; new pulses get none.
    delete = await client.delete("/settings/default_backup")
    assert delete.status_code == 200, delete.text
    assert store.get_pulse(pid).settings["backup"] == want

    r2 = await client.post("/pulses",
                           json={"title": "d2", "model": "claude-opus-5-5"})
    assert r2.status_code == 201, r2.text
    assert "backup" not in store.get_pulse(r2.json()["value"]["id"]).settings


@pytest.mark.parametrize("bad_value", [
    {"model": "no-such-model"},
    {"model": "grok-4.7", "effort": "max"},
    {"model": "gpt-6-sol", "vessel": "codex"},
    {"model": None},
    {"model": ""},
    {},
    "gpt-6-sol",
    None,
])
async def test_put_default_backup_rejects_bad_values(client, store, bad_value):
    r = await client.put(
        "/settings/default_backup",
        content=json.dumps(bad_value),
        headers={"content-type": "application/json"},
    )
    assert r.status_code == 422, r.text
    assert store.get_setting("default_backup") is None


@pytest.mark.parametrize("stored,want", [
    ("absent", None),
    (None, None),
    ({"model": "gpt-6-sol", "effort": "high"},
     {"model": "gpt-6-sol", "effort": "high"}),
])
async def test_switch_model_leaves_backup_unchanged(
        client, store, fake_spawn, fake_sock, stored, want):
    # Even when an app default_backup is configured, switching an existing
    # pulse's model must leave its backup as it is.
    put = await client.put("/settings/default_backup",
                           json={"model": "claude-opus-5-5", "effort": "low"})
    assert put.status_code == 200, put.text

    r = await client.post("/pulses", json={"title": "s", "model": PRIMARY})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    settings = dict(store.get_pulse(pid).settings)
    if stored == "absent":
        settings.pop("backup", None)
    else:
        settings["backup"] = stored
    store.update_settings(pid, settings, merge=False)

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5-5"})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("backup") == want
