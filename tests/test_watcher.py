"""Delegation watch tests — store CRUD, the watcher engine driven
with a frozen ``now``, and the daemon routes that create watches.

    venv/bin/python -m pytest tests -k watcher -q

Born from the 2026-08-06 incident: a lead spawned helpers with briefs in an
unsupported field, the API dropped them silently, and the helpers idled for an
hour. These tests pin the three defenses: unknown spawn fields 422, the
one-call spawn-with-brief lands the brief, and an idle delegate gets reported
to its spawner loudly — never silently.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

import httpx
import pytest

import pulsar.launch.units as units_mod
from pulsar.store import Store, UnknownWatch
from pulsar.daemon import paths
from pulsar.daemon import scheduler as sched_mod
from pulsar.daemon import watcher as watcher_mod
from pulsar.daemon.app import create_app

UTC = timezone.utc


# --------------------------------------------------------------------------- #
# Fakes / fixtures (test_daemon.py pattern)
# --------------------------------------------------------------------------- #
class FakeSpawn:
    def __init__(self) -> None:
        self.spawned: list[dict] = []

    def spawn_runner(self, pulse_id, home, model, vessel, resume=None,
                     memory_max="6G", tasks_max=512, diet=None, title=None,
                     effort=None, fast=False):
        self.spawned.append({"pulse_id": pulse_id, "home": str(home)})
        _write_heartbeat(pulse_id, state="idle", age_s=0.0)

    def stop_unit(self, pulse_id):
        return True

    def list_pulse_units(self):
        return []

    def fence_report(self, pulse_id):
        return None


def _write_heartbeat(pulse_id: str, state: str = "idle", age_s: float = 0.0,
                     ctx=None, busy_since: float | None = None) -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.fromtimestamp(time.time() - age_s, tz=UTC).strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    beat = {"ts": ts, "pid": 1, "cli_pid": 2, "state": state,
            "cli_session_id": f"sess-{pulse_id}", "last_ctx_tokens": ctx,
            "busy_since": busy_since, "protocol": 1}
    (home / "heartbeat.json").write_text(json.dumps(beat))


def _inbox_texts(pulse_id: str) -> list[str]:
    new = paths.inbox_new(pulse_id)
    if not new.exists():
        return []
    return [json.loads(p.read_text())["text"]
            for p in sorted(new.iterdir()) if not p.name.endswith(".tmp")]


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
def fake_spawn(monkeypatch):
    fs = FakeSpawn()
    monkeypatch.setattr(units_mod, "spawn_runner", fs.spawn_runner)
    monkeypatch.setattr(units_mod, "stop_unit", fs.stop_unit)
    monkeypatch.setattr(units_mod, "list_pulse_units", fs.list_pulse_units)
    monkeypatch.setattr(units_mod, "fence_report", fs.fence_report)
    return fs


@pytest.fixture()
async def client(store, fake_spawn):
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c:
        async with app.router.lifespan_context(app):
            yield c


def _mk_pulse(store, title="p", state="running"):
    p = store.create_pulse(title=title, model="claude-opus-5",
                           vessel="claude-code", state=state)
    _write_heartbeat(p.id)
    return p


def _iso_in(minutes: float) -> str:
    return (datetime.now(UTC) + timedelta(minutes=minutes)).isoformat(
        timespec="seconds")


# --------------------------------------------------------------------------- #
# Store CRUD
# --------------------------------------------------------------------------- #
def test_store_watch_crud(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(5))
    assert w.state == "watching" and w.confirm and w.checks_done == 0
    assert store.has_active_watch(a.id, b.id)
    assert not store.has_active_watch(b.id, a.id)

    w2 = store.bump_watch(w.id, _iso_in(15))
    assert w2.checks_done == 1

    w3 = store.close_watch(w.id, "working")
    assert w3.state == "closed" and w3.outcome == "working"
    assert w3.closed_at is not None
    assert not store.has_active_watch(a.id, b.id)
    assert [x.id for x in store.list_watches(state="closed")] == [w.id]


def test_prune_closed_watches_keeps_watching(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    live = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(5))
    dead = store.create_watch(a.id, b.id, "message", next_check_at=_iso_in(5))
    store.close_watch(dead.id, "working")
    assert len(store.list_watches()) == 2
    n = store.prune_closed_watches()
    assert n == 1
    left = store.list_watches()
    assert [w.id for w in left] == [live.id]
    assert store.list_watches(state="closed") == []


def test_store_watch_bad_kind_and_unknown(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    with pytest.raises(ValueError):
        store.create_watch(a.id, b.id, "telepathy", next_check_at=_iso_in(5))
    with pytest.raises(UnknownWatch):
        store.get_watch("nope")


def test_delete_pulse_removes_watches(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(5))
    store.set_state(b.id, "stopped")
    store.delete_pulse(b.id)
    assert store.list_watches() == []


# --------------------------------------------------------------------------- #
# Watcher engine (frozen now, no sleeping)
# --------------------------------------------------------------------------- #
def test_watch_not_due_untouched(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(5))
    summary = watcher_mod.tick(store)
    assert summary["due"] == 0
    assert _inbox_texts(a.id) == []


def test_working_helper_confirms_and_arms_follow(store):
    """v12: a working delegate no longer closes the watch — it arms the
    follow phase (the stall watcher) and keeps watching."""
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    _write_heartbeat(b.id, state="busy", ctx=42_000)
    summary = watcher_mod.tick(store)
    assert summary == {**summary, "due": 1, "working": 1}
    fw = store.get_watch(w.id)
    assert fw.state == "watching" and fw.phase == "follow"
    assert fw.last_ctx == 42_000 and fw.checks_done == 0
    texts = _inbox_texts(a.id)
    assert len(texts) == 1 and "✅ work is happening" in texts[0]
    assert "stall watch" in texts[0]  # no longer claims the watch is closed
    assert "'helper'" in texts[0] and b.id in texts[0]


def test_ctx_growth_counts_as_working(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    store.create_watch(a.id, b.id, "message", next_check_at=_iso_in(-1),
                       confirm=False, baseline_ctx=10_000)
    _write_heartbeat(b.id, state="idle", ctx=25_000)  # fast turn, already done
    summary = watcher_mod.tick(store)
    assert summary["working"] == 1
    # alert-only watch: success is silent — no chatter back to the sender
    assert _inbox_texts(a.id) == []


# --------------------------------------------------------------------------- #
# Follow phase (v12 stall watcher)
# --------------------------------------------------------------------------- #
def _arm_follow(store, lead, helper, ctx=10_000):
    """Drive a watch through its initial working check into the follow phase,
    then force the first follow check due."""
    w = store.create_watch(lead.id, helper.id, "spawn",
                           next_check_at=_iso_in(-1))
    _write_heartbeat(helper.id, state="busy", ctx=ctx)
    assert watcher_mod.tick(store)["working"] == 1
    fw = store.get_watch(w.id)
    assert fw.phase == "follow"
    _force_due(store, w.id)
    return w


def _force_due(store, watch_id):
    store._conn.execute(
        "UPDATE delegation_watches SET next_check_at = ? WHERE id = ?;",
        (_iso_in(-1), watch_id))


def test_follow_ctx_growth_stays_quiet(store):
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b, ctx=10_000)
    n0 = len(_inbox_texts(a.id))  # the ✅ arming message is already there
    _write_heartbeat(b.id, state="idle", ctx=15_000)  # did another turn
    summary = watcher_mod.tick(store)
    assert summary["clean"] == 1
    fw = store.get_watch(w.id)
    assert fw.state == "watching" and fw.last_ctx == 15_000
    assert fw.follow_checks == 0 and fw.checks_done == 0
    assert len(_inbox_texts(a.id)) == n0  # quiet while working — zero chatter


def test_follow_frozen_ctx_zombie_alerts_then_final(store):
    """The common failure: helper finishes its turn, never replies, spawner
    waits forever. Frozen ctx across ZOMBIE_CHECKS → nudge, recheck, final."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b, ctx=10_000)
    _write_heartbeat(b.id, state="idle", ctx=10_000)  # busy never zombie-counts
    for _ in range(watcher_mod.ZOMBIE_CHECKS - 1):  # quiet checks first
        _force_due(store, w.id)
        assert watcher_mod.tick(store)["clean"] == 1
    _force_due(store, w.id)
    assert watcher_mod.tick(store)["idle_alerted"] == 1
    texts = _inbox_texts(a.id)
    assert "finished without replying" in texts[-1]
    # mid-ladder progress clears the streak and re-arms
    _write_heartbeat(b.id, state="idle", ctx=11_000)
    _force_due(store, w.id)
    assert watcher_mod.tick(store)["clean"] == 1
    fw = store.get_watch(w.id)
    assert fw.checks_done == 0 and fw.follow_checks == 0
    # back to frozen → the ladder runs its full course again
    _write_heartbeat(b.id, state="idle", ctx=11_000)
    for _ in range(watcher_mod.ZOMBIE_CHECKS + 1):
        _force_due(store, w.id)
        summary = watcher_mod.tick(store)
    assert summary["stalled"] == 1
    assert store.get_watch(w.id).outcome == "stalled"
    assert "DROPPED" in _inbox_texts(a.id)[-1]


def test_follow_hung_busy_alerts(store):
    """A hung helper keeps a FRESH heartbeat (the heartbeat task is
    independent) — the signal is busy_since age + frozen ctx."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b, ctx=10_000)
    _write_heartbeat(b.id, state="busy", ctx=10_000,
                     busy_since=time.time() - watcher_mod.BUSY_STALL_S - 60)
    assert watcher_mod.tick(store)["idle_alerted"] == 1
    assert "HUNG" in _inbox_texts(a.id)[-1]


def test_follow_busy_but_growing_ctx_is_not_hung(store):
    """A legitimately long turn grows ctx between checks — never alarm on it."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b, ctx=10_000)
    n0 = len(_inbox_texts(a.id))
    _write_heartbeat(b.id, state="busy", ctx=12_000,
                     busy_since=time.time() - watcher_mod.BUSY_STALL_S - 60)
    assert watcher_mod.tick(store)["clean"] == 1
    assert len(_inbox_texts(a.id)) == n0


def test_follow_stopped_closes_with_fyi(store):
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b)
    store.set_state(b.id, "stopped")
    store.update_settings(b.id, {"stop_reason": "cli died"})
    summary = watcher_mod.tick(store)
    assert summary["stopped"] == 1
    fw = store.get_watch(w.id)
    assert fw.state == "closed" and fw.outcome == "stopped"
    texts = _inbox_texts(a.id)
    assert len(texts) == 2 and "STOPPED (cli died)" in texts[1]


def test_follow_ttl_expires(store):
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b)
    store._conn.execute(
        "UPDATE delegation_watches SET created_at = ? WHERE id = ?;",
        ((datetime.now(UTC) - timedelta(hours=25)).isoformat(
            timespec="seconds"), w.id))
    _write_heartbeat(b.id, state="busy", ctx=50_000)  # still working
    summary = watcher_mod.tick(store)
    assert summary["expired"] == 1
    fw = store.get_watch(w.id)
    assert fw.state == "closed" and fw.outcome == "expired"
    assert "expired after 24h" in _inbox_texts(a.id)[-1]


def test_follow_busy_short_turn_with_frozen_ctx_never_zombies(store):
    """Regression: a legit long tool call is
    busy with fresh busy_since and frozen ctx — it must NOT trip the zombie
    alert; only the hang path (busy past BUSY_STALL_S) owns busy stalls."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b, ctx=10_000)
    n0 = len(_inbox_texts(a.id))
    for _ in range(watcher_mod.ZOMBIE_CHECKS + 1):
        _force_due(store, w.id)
        _write_heartbeat(b.id, state="busy", ctx=10_000,
                         busy_since=time.time() - 60)  # fresh turn, frozen ctx
        summary = watcher_mod.tick(store)
        fw = store.get_watch(w.id)
        assert fw.state == "watching" and fw.follow_checks == 0
        assert fw.checks_done == 0
    assert len(_inbox_texts(a.id)) == n0  # silent the whole time


def test_follow_hang_alert_then_idle_frozen_still_reaches_final(store):
    """Regression: after a hang first-alert,
    a delegate that flips to idle with ctx still frozen must NOT silently
    drop the promised escalation — the zombie ladder inherits the count and
    goes straight to final."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b, ctx=10_000)
    _write_heartbeat(b.id, state="busy", ctx=10_000,
                     busy_since=time.time() - watcher_mod.BUSY_STALL_S - 60)
    assert watcher_mod.tick(store)["idle_alerted"] == 1
    assert store.get_watch(w.id).checks_done == 1
    # flip to idle, ctx frozen — the clean path must NOT reset the ladder
    _write_heartbeat(b.id, state="idle", ctx=10_000)
    for _ in range(watcher_mod.ZOMBIE_CHECKS - 1):
        _force_due(store, w.id)
        assert watcher_mod.tick(store)["clean"] == 1
        assert store.get_watch(w.id).checks_done == 1  # ladder kept alive
    _force_due(store, w.id)
    assert watcher_mod.tick(store)["stalled"] == 1
    assert store.get_watch(w.id).outcome == "stalled"
    assert "DROPPED" in _inbox_texts(a.id)[-1]


def test_follow_stale_heartbeat_ladder(store):
    """Backstop: a dead runner that reconcile hasn't swept yet still ladders."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    w = _arm_follow(store, a, b)
    _write_heartbeat(b.id, state="busy", ctx=10_000, age_s=600)  # stale
    assert watcher_mod.tick(store)["idle_alerted"] == 1
    assert "STALE" in _inbox_texts(a.id)[-1]
    _force_due(store, w.id)
    _write_heartbeat(b.id, state="busy", ctx=10_000, age_s=1200)
    assert watcher_mod.tick(store)["stalled"] == 1
    assert store.get_watch(w.id).outcome == "stalled"


def test_idle_helper_alerts_then_final_stall(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    _write_heartbeat(b.id, state="idle", ctx=None)  # never did a turn

    summary = watcher_mod.tick(store)
    assert summary["idle_alerted"] == 1
    w1 = store.get_watch(w.id)
    assert w1.state == "watching" and w1.checks_done == 1
    texts = _inbox_texts(a.id)
    assert len(texts) == 1 and "⚠️ nothing is happening" in texts[0]

    # not due again until the recheck window
    assert watcher_mod.tick(store)["due"] == 0

    # force the recheck due and keep the helper idle → final alert + close
    store._conn.execute(
        "UPDATE delegation_watches SET next_check_at = ? WHERE id = ?;",
        (_iso_in(-1), w.id))
    summary = watcher_mod.tick(store)
    assert summary["stalled"] == 1
    assert store.get_watch(w.id).state == "closed"
    assert store.get_watch(w.id).outcome == "stalled"
    texts = _inbox_texts(a.id)
    assert len(texts) == 2 and "STILL nothing happening" in texts[1]


def test_dead_runner_is_not_working(store):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    _write_heartbeat(b.id, state="busy", ctx=90_000, age_s=600)  # stale
    summary = watcher_mod.tick(store)
    assert summary["idle_alerted"] == 1
    texts = _inbox_texts(a.id)
    assert "STALE" in texts[0]


def test_stall_alert_wakes_stopped_spawner(store, fake_spawn):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    _write_heartbeat(b.id, state="idle", ctx=None)
    store.set_state(a.id, "stopped")
    watcher_mod.tick(store)
    assert [c["pulse_id"] for c in fake_spawn.spawned] == [a.id]
    assert store.get_pulse(a.id).state == "running"


def test_archived_target_closes_silently(store, fake_spawn):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    store.set_state(b.id, "stopped")
    store.set_state(b.id, "archived")
    summary = watcher_mod.tick(store)
    assert summary["due"] == 0
    assert store.get_watch(w.id).outcome == "target_gone"
    assert _inbox_texts(a.id) == []
    assert fake_spawn.spawned == []


def test_scheduler_tick_runs_watcher_pass(store, fake_spawn):
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    _write_heartbeat(b.id, state="busy", ctx=1_000)
    sched_mod.tick(store)
    fw = store.get_watch(w.id)
    assert fw.state == "watching" and fw.phase == "follow"


# --------------------------------------------------------------------------- #
# Daemon routes
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_create_unknown_field_422(client):
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-opus-5",
        "frist_message": "typo'd brief"})
    assert r.status_code == 422
    assert r.json()["ok"] is False


@pytest.mark.anyio
async def test_spawn_with_brief_and_watch(client, store):
    lead = _mk_pulse(store, "lead")
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-opus-5",
        "from_pulse_id": lead.id, "first_message": "clone the repo and go"})
    assert r.status_code == 201, r.text
    val = r.json()["value"]
    assert val["link"] == f"[helper](/?pulse-focus={val['id']})"
    helper_id = val["id"]

    # the brief landed in the helper's inbox, named from the lead
    texts = _inbox_texts(helper_id)
    assert len(texts) == 1
    assert "clone the repo and go" in texts[0]
    assert f"[message from 'lead' (pulse {lead.id})]" in texts[0]

    # the watch exists and is reported in the response + settings
    assert val["delegation"]["watch_id"]
    assert store.get_pulse(helper_id).settings["spawned_by"] == lead.id
    watches = store.list_watches(state="watching")
    assert [(w.spawner_id, w.target_id, w.kind, w.confirm)
            for w in watches] == [(lead.id, helper_id, "spawn", True)]

    # the lead's own pane gets the link marker, whatever its model replies
    lines = paths.events_path(lead.id).read_text().splitlines()
    marker = json.loads(lines[-1])
    assert marker["type"] == "pulse_spawned"
    assert (marker["pulse_id"], marker["title"], marker["model"]) == (
        helper_id, "helper", "claude-opus-5")


@pytest.mark.anyio
async def test_spawn_from_ui_writes_no_marker(client):
    # No from_pulse_id = the user made it in the app: no pane gets a marker.
    r = await client.post("/pulses", json={
        "title": "solo", "model": "claude-opus-5"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    ev = paths.events_path(pid)
    assert not ev.exists() or "pulse_spawned" not in ev.read_text()


@pytest.mark.anyio
async def test_spawn_with_unknown_spawner_404(client):
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-opus-5",
        "from_pulse_id": "ghost"})
    assert r.status_code == 404


@pytest.mark.anyio
async def test_first_message_without_sender_plain(client, store):
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-opus-5",
        "first_message": "hello from user"})
    assert r.status_code == 201, r.text
    texts = _inbox_texts(r.json()["value"]["id"])
    assert texts == ["hello from user"]
    assert store.list_watches() == []  # no spawner → nothing to report to


@pytest.mark.anyio
async def test_pulse_message_creates_alert_only_watch(client, store):
    lead = _mk_pulse(store, "lead")
    helper = _mk_pulse(store, "helper")
    _write_heartbeat(helper.id, ctx=7_000)
    r = await client.post(f"/pulses/{helper.id}/messages", json={
        "text": "brief", "from_pulse_id": lead.id})
    assert r.status_code == 202, r.text
    watches = store.list_watches(state="watching")
    assert len(watches) == 1
    w = watches[0]
    assert (w.kind, w.confirm, w.baseline_ctx) == ("message", False, 7_000)

    # second message while the watch is active does NOT stack another
    r = await client.post(f"/pulses/{helper.id}/messages", json={
        "text": "more", "from_pulse_id": lead.id})
    assert r.status_code == 202
    assert len(store.list_watches(state="watching")) == 1

    # v12: a new message on a FOLLOW-phase pair re-baselines it — the stall
    # counters measure progress against the LATEST message, not the first.
    store.arm_follow(watches[0].id, _iso_in(5), last_ctx=7_000)
    _write_heartbeat(helper.id, ctx=9_000)
    r = await client.post(f"/pulses/{helper.id}/messages", json={
        "text": "nudge", "from_pulse_id": lead.id})
    assert r.status_code == 202
    fw = store.get_watch(watches[0].id)
    assert fw.phase == "follow" and fw.baseline_ctx == 9_000
    assert fw.last_ctx == 9_000 and fw.follow_checks == 0 and fw.checks_done == 0


@pytest.mark.anyio
async def test_watches_route_lists(client, store):
    lead = _mk_pulse(store, "lead")
    helper = _mk_pulse(store, "helper")
    store.create_watch(lead.id, helper.id, "spawn", next_check_at=_iso_in(5))
    r = await client.get("/watches", params={"state": "watching"})
    assert r.status_code == 200
    rows = r.json()["value"]
    assert len(rows) == 1 and rows[0]["target_id"] == helper.id


@pytest.mark.anyio
async def test_watches_route_defaults_to_watching(client, store):
    lead = _mk_pulse(store, "lead")
    helper = _mk_pulse(store, "helper")
    live = store.create_watch(lead.id, helper.id, "spawn", next_check_at=_iso_in(5))
    dead = store.create_watch(lead.id, helper.id, "message", next_check_at=_iso_in(5))
    store.close_watch(dead.id, "expired")
    r = await client.get("/watches")
    assert r.status_code == 200
    ids = [row["id"] for row in r.json()["value"]]
    assert ids == [live.id]
    all_rows = (await client.get("/watches", params={"state": "all"})).json()["value"]
    assert {row["id"] for row in all_rows} == {live.id, dead.id}


def test_active_delegation_watch_blocks_owner_deletion(store):
    from pulsar.store.db import InvalidStateTransition
    a = _mk_pulse(store, "lead")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "spawn", next_check_at=_iso_in(-1))
    with pytest.raises(InvalidStateTransition, match="active watchers"):
        store.delete_pulse(a.id)
    store.close_watch(w.id, "done")
    store.delete_pulse(a.id)


def test_follow_idle_on_a_schedule_is_not_a_zombie(store):
    """A delegate that has an enabled schedule (loop/daily/once) is idle
    BETWEEN wakes by design — frozen idle ctx there must never climb the
    zombie ladder (2026-09-09: the false alarm pushed a Keeper into
    watching mode to silence it)."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "keeper")
    store.create_schedule(b.id, "loop", "[keeper check]", interval_minutes=30)
    w = _arm_follow(store, a, b, ctx=10_000)
    _write_heartbeat(b.id, state="idle", ctx=10_000)
    n0 = len(_inbox_texts(a.id))
    for _ in range(watcher_mod.ZOMBIE_CHECKS + 2):
        _force_due(store, w.id)
        assert watcher_mod.tick(store)["clean"] == 1
    assert store.get_watch(w.id).follow_checks == 0
    assert len(_inbox_texts(a.id)) == n0
    # disabling the schedule puts the normal rule back
    sid = store.list_schedules(pulse_id=b.id)[0].id
    store.set_schedule_enabled(sid, False)
    for _ in range(watcher_mod.ZOMBIE_CHECKS):
        _force_due(store, w.id)
        summary = watcher_mod.tick(store)
    assert summary["idle_alerted"] == 1


@pytest.mark.parametrize("phase", ["initial", "follow"])
@pytest.mark.parametrize("archived_end", ["spawner", "target"])
def test_legacy_archive_watch_closes_without_delivery(
        store, fake_spawn, monkeypatch, phase, archived_end):
    """Pre-fix rows must not revive archived owners or notify old helpers."""
    a = _mk_pulse(store, "lead", state="stopped")
    b = _mk_pulse(store, "helper")
    w = store.create_watch(a.id, b.id, "message", next_check_at=_iso_in(-1))
    store._conn.execute("UPDATE delegation_watches SET phase = ? WHERE id = ?",
                        (phase, w.id))
    archived_id = a.id if archived_end == "spawner" else b.id
    store._conn.execute("UPDATE pulses SET state = 'archived' WHERE id = ?",
                        (archived_id,))
    deliveries = []
    monkeypatch.setattr(watcher_mod, "_notify_spawner", lambda *a: deliveries.append(a))
    monkeypatch.setattr(watcher_mod, "_file_inbox", lambda *a: deliveries.append(a))
    watcher_mod.tick(store)
    assert store.get_watch(w.id).state == "closed"
    assert store.get_pulse(archived_id).state == "archived"
    assert deliveries == []
    assert fake_spawn.spawned == []


def test_archive_closes_both_directions_and_ignores_stale_snapshot(store, fake_spawn):
    lead = _mk_pulse(store, "lead")
    helper = _mk_pulse(store, "helper", state="archived")
    other = _mk_pulse(store, "other")
    out = store.create_watch(lead.id, helper.id, "spawn", next_check_at=_iso_in(-1))
    back = store.create_watch(helper.id, lead.id, "message", next_check_at=_iso_in(-1))
    unrelated = store.create_watch(other.id, helper.id, "message", next_check_at=_iso_in(5))
    store.set_state(lead.id, "stopped")
    store.archive_pulse(lead.id)
    assert store.get_watch(out.id).outcome == "owner_archived"
    assert store.get_watch(back.id).outcome == "target_gone"
    assert store.get_watch(unrelated.id).state == "watching"
    assert watcher_mod.check_watch(store, back) == "closed"
    store.unarchive_pulse(lead.id)
    assert store.get_watch(out.id).state == "closed"
    assert store.get_watch(back.id).state == "closed"
    assert _inbox_texts(helper.id) == []
    assert fake_spawn.spawned == []


@pytest.mark.anyio
async def test_archive_route_closes_delegation_both_directions(client, store, fake_spawn):
    lead = _mk_pulse(store, "lead")
    helper = _mk_pulse(store, "helper", state="archived")
    out = store.create_watch(lead.id, helper.id, "spawn", next_check_at=_iso_in(-1))
    back = store.create_watch(helper.id, lead.id, "message", next_check_at=_iso_in(-1))
    response = await client.post(f"/pulses/{lead.id}/archive")
    assert response.status_code == 200, response.text
    assert store.get_watch(out.id).state == "closed"
    assert store.get_watch(back.id).state == "closed"
    watcher_mod.tick(store)
    assert store.get_pulse(lead.id).state == "archived"
    assert store.get_pulse(helper.id).state == "archived"
    assert _inbox_texts(helper.id) == []
    assert fake_spawn.spawned == []


def test_follow_idle_in_watching_mode_is_not_a_zombie(store):
    """A delegate that flipped itself to watching mode is idle between wakes
    by design."""
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "keeper")
    store.update_settings(b.id, {"mode": "watching"})
    w = _arm_follow(store, a, b, ctx=10_000)
    _write_heartbeat(b.id, state="idle", ctx=10_000)
    n0 = len(_inbox_texts(a.id))
    for _ in range(watcher_mod.ZOMBIE_CHECKS + 2):
        _force_due(store, w.id)
        assert watcher_mod.tick(store)["clean"] == 1
    assert store.get_watch(w.id).follow_checks == 0
    assert len(_inbox_texts(a.id)) == n0


def test_follow_idle_after_replying_to_spawner_is_not_a_zombie(store):
    """The ladder says "finished without replying"; a delegate that messaged
    its spawner since the watch began DID reply, so frozen idle ctx is the
    done state, not a stall. A message from someone else does not count."""
    from pulsar.daemon import mailbox
    a, b = _mk_pulse(store, "lead"), _mk_pulse(store, "helper")
    other = _mk_pulse(store, "other")
    w = _arm_follow(store, a, b, ctx=10_000)
    _write_heartbeat(b.id, state="idle", ctx=10_000)
    # a peer message from a third pulse: still climbs the ladder
    mailbox.deliver(store, a, "hi from other", on_archived="skip", wake=False,
                    extra=mailbox.origin_keys("peer", "pulse",
                                              from_pulse_id=other.id))
    _force_due(store, w.id)
    watcher_mod.tick(store)
    assert store.get_watch(w.id).follow_checks == 1
    # the delegate's own report resets and holds the streak at zero
    mailbox.deliver(store, a, "REPORT: done, evidence at /tmp/x",
                    on_archived="skip", wake=False,
                    extra=mailbox.origin_keys("peer", "pulse",
                                              from_pulse_id=b.id))
    n0 = len(_inbox_texts(a.id))
    for _ in range(watcher_mod.ZOMBIE_CHECKS + 1):
        _force_due(store, w.id)
        assert watcher_mod.tick(store)["clean"] == 1
    assert store.get_watch(w.id).follow_checks == 0
    assert len(_inbox_texts(a.id)) == n0
