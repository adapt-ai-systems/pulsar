"""Inbox tests — daemon-side.

Covers: produce (cap/slug validation), list + filters (incl. LIKE ESCAPE and
before-cursor pagination), unread-count correctness across transitions,
read⇒archive + unread / star / dismiss-response, read-all before-cutoff race,
the async reply write path (thread_key backfill, 409s, wake-failure state),
channel auto-create + mode gating, and producer wiring (watcher ⚠/🛑 and
scheduler fire-FAILURES).

    venv/bin/python -m pytest tests -k inbox -q
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import httpx
import pytest

from pulsar.daemon import paths
import pulsar.launch.units as units_mod
from pulsar.daemon import lifecycle
from pulsar.daemon import scheduler as scheduler_mod
from pulsar.daemon import watcher as watcher_mod
from pulsar.daemon import inbox as inbox_mod
from pulsar.daemon.app import create_app
from pulsar.store import Store

UTC = timezone.utc


@pytest.fixture()
def anyio_backend():
    return "asyncio"


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
    """Fake every systemd-touching spawn function so no test reaches the REAL
    systemd user manager (conftest.no_real_systemd philosophy)."""
    spawned = []

    def spawn_runner(pulse_id, home, model, vessel, resume=None, **kw):
        spawned.append(pulse_id)
        paths.pulse_home(pulse_id).mkdir(parents=True, exist_ok=True)

    def list_pulse_units():
        return []

    def stop_unit(pulse_id):
        return True

    def fence_report(pulse_id):
        return None

    for name, fn in (("spawn_runner", spawn_runner),
                     ("list_pulse_units", list_pulse_units),
                     ("stop_unit", stop_unit),
                     ("fence_report", fence_report)):
        monkeypatch.setattr(units_mod, name, fn)
    return spawned


def _mk(store, title, state="running"):
    return store.create_pulse(title, "claude-haiku-4-5-20251001",
                              "claude-code", state=state)


def _now_iso(dt=None):
    return (dt or datetime.now(UTC)).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Produce via the route — caps + slug validation
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_produce_script_item_and_caps(store, fake_spawn):
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.post("/inbox/items", json={
            "title": "Build check", "body": "base is green",
            "channel": "Benchmarks", "source_label": "nightly"})
        assert r.status_code == 201, r.text
        v = r.json()["value"]
        assert v["channel"] == "benchmarks"          # lowercased server-side
        assert v["source_kind"] == "script"
        assert v["source_label"] == "nightly"
        assert "." in v["created_at"]  # high-resolution legacy cutoff safety
        # channel auto-created with default notify
        assert store.get_channel("benchmarks").mode == "notify"

        # 422 beyond caps
        r = await c.post("/inbox/items", json={"title": "x" * 201,
                                               "body": "b", "channel": "c"})
        assert r.status_code == 422
        r = await c.post("/inbox/items", json={"title": "ok",
                                               "body": "大" * 30000,
                                               "channel": "c"})
        assert r.status_code == 422                       # >64KB bytes
        r = await c.post("/inbox/items", json={"title": "ok", "body": "b",
                                               "channel": "UPPER!"})
        assert r.status_code == 422                       # bad slug
        # unknown field rejected
        r = await c.post("/inbox/items", json={"title": "ok", "body": "b",
                                               "channel": "c", "bogus": 1})
        assert r.status_code == 422


@pytest.mark.anyio
async def test_produce_pulse_item_resolves_sender_label(store, fake_spawn):
    src = _mk(store, "the sender")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.post("/inbox/items", json={
            "title": "report", "body": "done", "channel": "projects",
            "from_pulse_id": src.id})
        assert r.status_code == 201, r.text
        v = r.json()["value"]
        assert v["source_kind"] == "pulse"
        assert v["source_pulse_id"] == src.id
        assert v["source_label"] == "the sender"
        # unknown sender → 404
        r = await c.post("/inbox/items", json={
            "title": "report", "body": "done", "channel": "projects",
            "from_pulse_id": "nope"})
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# List + filters
# --------------------------------------------------------------------------- #
def test_list_filters_like_escape_and_cursor(store):
    a = _mk(store, "alpha")
    it1 = store.create_inbox_item("stocks", "pulse", "100% up", "body one",
                                  source_label="alpha", source_pulse_id=a.id,
                                  thread_key="t1")
    it2 = store.create_inbox_item("stocks", "pulse", "flat", "body two",
                                  source_label="alpha", source_pulse_id=a.id,
                                  thread_key="t1", needs_response=True)
    it3 = store.create_inbox_item("system", "script", "reboot", "body three",
                                  source_label="ops")

    # unread_only (all fresh here)
    items = store.list_inbox_items(unread_only=True)
    assert {i.id for i in items} == {it1.id, it2.id, it3.id}

    # channel filter
    assert [i.id for i in store.list_inbox_items(channel="stocks")] == \
        [it2.id, it1.id]                      # newest first
    # thread filter
    assert [i.id for i in store.list_inbox_items(thread_key="t1")] == \
        [it2.id, it1.id]
    # needs_response
    assert [i.id for i in store.list_inbox_items(needs_response=True)] == \
        [it2.id]
    # q with LIKE ESCAPE — "100%" must not be a wildcard
    assert [i.id for i in store.list_inbox_items(q="100%")] == [it1.id]
    assert store.list_inbox_items(q="100_") == []
    # Composite pagination is stable even when rows share a timestamp.
    same_time = "2026-08-18T12:00:00+00:00"
    store._conn.execute(
        "UPDATE inbox_items SET created_at = ? WHERE id IN (?, ?)",
        (same_time, it1.id, it2.id))
    first = store.list_inbox_items(channel="stocks", limit=1)
    assert [i.id for i in first] == [it2.id]
    assert [i.id for i in store.list_inbox_items(
        channel="stocks", before=first[0].cursor)] == [it1.id]


def test_composite_cursor_does_not_skip_201_same_timestamp_rows(store):
    items = [store.create_inbox_item("bulk", "script", str(n), "body")
             for n in range(201)]
    same_time = "2026-08-18T12:00:00+00:00"
    store._conn.execute("UPDATE inbox_items SET created_at = ?",
                        (same_time,))

    page_one = store.list_inbox_items(channel="bulk", limit=200)
    page_two = store.list_inbox_items(
        channel="bulk", limit=200, before=page_one[-1].cursor)

    assert len(page_one) == 200
    assert len(page_two) == 1
    assert {i.id for i in page_one + page_two} == {i.id for i in items}


# --------------------------------------------------------------------------- #
# Unread-count correctness across transitions
# --------------------------------------------------------------------------- #
def test_unread_count_tracks_read_star_reply(store):
    a = _mk(store, "alpha")
    i1 = store.create_inbox_item("stocks", "pulse", "t1", "b1",
                                 source_label="a", source_pulse_id=a.id,
                                 thread_key="tg")
    i2 = store.create_inbox_item("stocks", "pulse", "t2", "b2",
                                 source_label="a", source_pulse_id=a.id,
                                 thread_key="tg")
    i3 = store.create_inbox_item("system", "script", "t3", "b3",
                                 source_label="ops")

    pc, pt, total = store.inbox_unread_maps()
    assert total == 3
    assert pc == {"stocks": 2, "system": 1}
    assert pt == {"tg": 2, None: 1}

    store.set_inbox_read(i1.id, True)                    # archive one
    pc, pt, total = store.inbox_unread_maps()
    assert total == 2
    assert pc == {"stocks": 1, "system": 1}
    assert pt == {"tg": 1, None: 1}

    store.set_inbox_read(i1.id, False)                   # back to unread
    pc, pt, total = store.inbox_unread_maps()
    assert total == 3


# --------------------------------------------------------------------------- #
# Item actions via the API (read⇒archive, unread, star, dismiss-response)
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_item_actions_api(store, fake_spawn):
    it = store.create_inbox_item("stocks", "script", "act", "body",
                                 source_label="ops", needs_response=True)
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.post(f"/inbox/items/{it.id}/read")
        assert r.status_code == 200 and r.json()["value"]["read_at"]
        r = await c.post(f"/inbox/items/{it.id}/unread")
        assert r.status_code == 200 and not r.json()["value"]["read_at"]
        r = await c.post(f"/inbox/items/{it.id}/star")
        assert r.json()["value"]["starred"] is True
        r = await c.post(f"/inbox/items/{it.id}/unstar")
        assert r.json()["value"]["starred"] is False
        r = await c.post(f"/inbox/items/{it.id}/dismiss-response")
        assert r.json()["value"]["needs_response"] is False
        # unknown item → 404
        r = await c.post("/inbox/items/nope/read")
        assert r.status_code == 404


@pytest.mark.anyio
async def test_inbox_list_and_unread_count_api(store, fake_spawn):
    a = _mk(store, "alpha")
    it = store.create_inbox_item("stocks", "pulse", "t1", "b1",
                                 source_label="a", source_pulse_id=a.id)
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        # unread-count returns total + per-channel + per-thread
        r = await c.get("/inbox/unread-count")
        assert r.status_code == 200
        v = r.json()["value"]
        assert v["total"] == 1
        assert v["channels"] == {"stocks": 1}
        assert v["threads"] == {"": 1}                     # NULL → ""
        # Plain GET defaults to unread. Explicit false is the all/archive path.
        archived = store.create_inbox_item("stocks", "script", "old", "b",
                                           source_label="ops")
        store.set_inbox_read(archived.id, True)
        r = await c.get("/inbox")
        assert [i["id"] for i in r.json()["value"]] == [it.id]
        assert r.json()["value"][0]["cursor"] == it.cursor
        r = await c.get("/inbox", params={"unread_only": "false"})
        assert {i["id"] for i in r.json()["value"]} == {it.id, archived.id}


# --------------------------------------------------------------------------- #
# read-all requires `before` cutoff (prevents unread-arrival race)
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_read_all_requires_before_and_cutoff(store, fake_spawn):
    i_early = store.create_inbox_item("stocks", "script", "early", "b",
                                      source_label="ops")
    i_late = store.create_inbox_item("stocks", "script", "late", "b",
                                     source_label="ops")
    # Reproduce the original race exactly: the rendered item and the unseen
    # later arrival have the same timestamp. The monotonic cursor tie-breaker
    # must keep the later row outside the rendered cutoff.
    same_time = "2026-08-18T12:00:00+00:00"
    store._conn.execute(
        "UPDATE inbox_items SET created_at = ? WHERE id IN (?, ?)",
        (same_time, i_early.id, i_late.id))
    i_early = store.get_inbox_item(i_early.id)
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        # missing before → 422
        r = await c.post("/inbox/read-all", json={"channel": "stocks"})
        assert r.status_code == 422
        # cutoff only archives rows ≤ before; the later one stays unread
        r = await c.post("/inbox/read-all",
                         json={"before": i_early.cursor,
                               "channel": "stocks"})
        assert r.status_code == 200
        assert r.json()["value"]["archived"] == 1
        assert store.get_inbox_item(i_early.id).read_at is not None
        assert store.get_inbox_item(i_late.id).read_at is None


# --------------------------------------------------------------------------- #
# Reply write path
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_reply_delivers_backfills_thread_and_clears(store, fake_spawn):
    src = _mk(store, "worker", state="stopped")
    item = store.create_inbox_item("projects", "pulse", "check the build",
                                   "body", source_label="worker",
                                   source_pulse_id=src.id,
                                   needs_response=True)
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.post(f"/inbox/items/{item.id}/reply", json={"text": "yes"})
        assert r.status_code == 200, r.text
        v = r.json()["value"]
        assert v["replied_at"] and v["needs_response"] is False
        # thread_key backfilled
        assert v["thread_key"] == f"re-{item.id[:8]}"
        # the reply landed in the source's inbox/new (delivery, NOT messages.send)
        msgs = sorted(paths.inbox_new(src.id).glob("*.json"))
        assert len(msgs) == 1
        body = json.loads(msgs[0].read_text())
        assert "The user replied via Inbox" in body["text"]
        assert f"re-{item.id[:8]}" in body["text"]
        # stopped source was woken
        assert fake_spawn == [src.id]


@pytest.mark.anyio
async def test_reply_rejects_non_pulse_and_archived_gone(store, fake_spawn):
    script_item = store.create_inbox_item("system", "script", "t", "b",
                                          source_label="ops")
    src = _mk(store, "worker", state="stopped")
    pulse_item = store.create_inbox_item("projects", "pulse", "t", "b",
                                         source_label="w",
                                         source_pulse_id=src.id)
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        # non-pulse → 409
        r = await c.post(f"/inbox/items/{script_item.id}/reply",
                         json={"text": "hi"})
        assert r.status_code == 409

        # archived source → 409
        store.archive_pulse(src.id)
        r = await c.post(f"/inbox/items/{pulse_item.id}/reply",
                         json={"text": "hi"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "source_archived"

        # gone source → 409
        gone = _mk(store, "ghost", state="stopped")
        gone_item = store.create_inbox_item("projects", "pulse", "t", "b",
                                            source_label="g",
                                            source_pulse_id=gone.id)
        store.delete_pulse(gone.id)
        r = await c.post(f"/inbox/items/{gone_item.id}/reply",
                         json={"text": "hi"})
        assert r.status_code == 409
        assert r.json()["error"]["code"] == "source_gone"


@pytest.mark.anyio
async def test_reply_wake_failure_leaves_item_untouched(store, fake_spawn,
                                                        monkeypatch):
    src = _mk(store, "worker", state="stopped")
    item = store.create_inbox_item("projects", "pulse", "t", "b",
                                   source_label="w", source_pulse_id=src.id,
                                   needs_response=True)

    def _boom(store, pulse_id):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(lifecycle, "wake_locked", _boom)
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.post(f"/inbox/items/{item.id}/reply", json={"text": "hi"})
        assert r.status_code == 502
        # wake-failure state: replied_at NOT set, needs_response NOT cleared
        fresh = store.get_inbox_item(item.id)
        assert fresh.replied_at is None
        assert fresh.needs_response is True


# --------------------------------------------------------------------------- #
# Channels — PATCH mode + mute_until gating
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_channel_patch_and_effective_mode(store, fake_spawn):
    store.create_inbox_item("bench", "script", "t", "b", source_label="ops")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.get("/inbox/channels")
        chans = {c["channel"]: c for c in r.json()["value"]}
        assert chans["bench"]["mode"] == "notify"

        # bad mode rejected
        r = await c.patch("/inbox/channels/bench", json={"mode": "loud"})
        assert r.status_code == 422
        # quiet
        r = await c.patch("/inbox/channels/bench", json={"mode": "quiet"})
        assert r.json()["value"]["mode"] == "quiet"
        future = _now_iso(datetime.now(UTC) + timedelta(hours=1))
        r = await c.patch("/inbox/channels/bench",
                          json={"mute_until": future})
        assert r.json()["value"]["mute_until"] == future
        # Omitted means preserve the current mute.
        r = await c.patch("/inbox/channels/bench", json={"mode": "notify"})
        assert r.json()["value"]["mute_until"] == future
        # Explicit JSON null means clear it.
        r = await c.patch("/inbox/channels/bench",
                          json={"mute_until": None})
        assert r.json()["value"]["mute_until"] is None
        # mute_until in the past has NO effect (passive expiry)
        past = _now_iso(datetime.now(UTC) - timedelta(hours=1))
        assert inbox_mod.effective_channel_mode("notify", past) == "notify"

        # unknown channel → 404
        r = await c.patch("/inbox/channels/missing", json={"mode": "muted"})
        assert r.status_code == 404


# --------------------------------------------------------------------------- #
# Whole-archive search: matches BOTH states, ignores the
# 200-row view cap, escapes LIKE metacharacters
# --------------------------------------------------------------------------- #
def test_search_inbox_items_covers_archive_and_escapes(store):
    old = store.create_inbox_item("projects", "pulse", "acme landing advice",
                                  "body", source_label="lead")
    store.set_inbox_read(old.id, True)  # archived
    fresh = store.create_inbox_item("benchmarks", "pulse", "qwen sweep",
                                    "very ACME LANDING notes inside", source_label="tuner")
    noise = store.create_inbox_item("system", "script", "reboot", "b",
                                    source_label="ops")

    ids = {i.id for i in store.search_inbox_items("acme landing")}
    assert ids == {old.id, fresh.id}          # case-insensitive, both states
    ids = {i.id for i in store.search_inbox_items("ops")}  # sender match
    assert ids == {noise.id}
    # LIKE metacharacters are literal, not wildcards
    store.create_inbox_item("system", "script", "100% up", "b",
                            source_label="ops")
    assert store.search_inbox_items("p%w") == []
    # empty after trim → no matches (never "everything")
    assert store.search_inbox_items("   ") == []


@pytest.mark.anyio
async def test_search_api_route(store, fake_spawn):
    hit = store.create_inbox_item("projects", "pulse", "wind tunnel cell",
                                  "b", source_label="lead")
    store.set_inbox_read(hit.id, True)
    store.create_inbox_item("system", "script", "other", "b",
                            source_label="ops")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.get("/inbox/search", params={"q": "wind tunnel"})
        assert r.status_code == 200
        v = r.json()["value"]
        assert [i["id"] for i in v] == [hit.id]   # archived rows are returned
        r = await c.get("/inbox/search", params={"q": "  "})
        assert r.json()["value"] == []


# --------------------------------------------------------------------------- #
# Producer wiring — watcher ⚠/🛑 → item (NOT ✅); scheduler failure → quiet
# --------------------------------------------------------------------------- #
def test_watcher_stall_produces_inbox_item(store, fake_spawn):
    spawner = _mk(store, "spawner")
    target = _mk(store, "sleeper", state="stopped")
    store.create_watch(spawner.id, target.id, "message", confirm=False,
                       next_check_at=watcher_mod.next_check_iso(),
                       baseline_ctx=None)
    now = datetime.now(UTC) + timedelta(minutes=10)
    watch = store.list_watches(state="watching")[0]
    watcher_mod.check_watch(store, watch, now=now)
    stall_items = [i for i in store.list_inbox_items()
                   if i.channel == "system" and i.source_kind == "watch"]
    assert len(stall_items) == 1
    assert ("⚠" in stall_items[0].body or "🛑" in stall_items[0].body
            or "nothing is happening" in stall_items[0].body)


def test_score1_rule_drops_stall_inbox_item_but_not_spawner_message(
        store, fake_spawn):
    """Score 1 on the delegation-stall kind ⇒ the Inbox never
    stores the item; the spawner still gets its watch message."""
    spawner = _mk(store, "spawner")
    target = _mk(store, "sleeper", state="stopped")
    # Teach the rule exactly like the /score route does.
    item = inbox_mod.produce(
        store, title=f"Delegation stall: {target.title!r}", body="x",
        channel="system", source_kind="watch",
        source_label="delegation-watch")
    store.set_inbox_score(item.id, 1)
    store.upsert_inbox_rule("system|delegation-watch|delegation stall", 1,
                            "system", "delegation-watch", item.title)

    store.create_watch(spawner.id, target.id, "message", confirm=False,
                       next_check_at=watcher_mod.next_check_iso(),
                       baseline_ctx=None)
    now = datetime.now(UTC) + timedelta(minutes=10)
    watch = store.list_watches(state="watching")[0]
    watcher_mod.check_watch(store, watch, now=now)

    stall_items = [i for i in store.list_inbox_items(unread_only=False)
                   if i.channel == "system" and i.source_kind == "watch"
                   and i.id != item.id]
    assert stall_items == []
    # Direct produce() also drops score=1 items without storing them (D8)
    assert inbox_mod.produce(
        store, title="Delegation stall: 'another'", body="x",
        channel="system", source_kind="watch",
        source_label="delegation-watch") is None
    # the spawner's inbox file still lands — the alert itself is not silenced
    new_dir = paths.inbox_new(spawner.id)
    assert [p for p in new_dir.glob("watch-*.json")], \
        "spawner message must still be delivered"


def test_scheduler_failure_produces_quiet_item(store, fake_spawn, monkeypatch):
    a = _mk(store, "worker")
    store.create_schedule(a.id, "loop", "ping", interval_minutes=1)

    def _boom(store, pulse):
        raise RuntimeError("spawn failed")

    monkeypatch.setattr(lifecycle, "wake_if_stopped", _boom)
    now = datetime.now(UTC) + timedelta(minutes=2)
    summary = scheduler_mod.tick(store, now=now)
    assert summary["failed"] == 1
    sched_items = [i for i in store.list_inbox_items()
                   if i.channel == "system" and i.source_kind == "scheduler"]
    assert len(sched_items) == 1
    assert sched_items[0].priority == "quiet"
    assert "Schedule fire failed" in sched_items[0].title


# --------------------------------------------------------------------------- #
# Restart persistence
# --------------------------------------------------------------------------- #
def test_inbox_persists_across_reopen(data_root):
    db = str(data_root / "pulsar.db")
    s1 = Store(db)
    it = s1.create_inbox_item("stocks", "script", "t", "b", source_label="ops")
    s1.close()
    s2 = Store(db)
    got = s2.get_inbox_item(it.id)
    assert got.title == "t" and got.channel == "stocks"
    s2.close()


# --------------------------------------------------------------------------- #
# Scoring (v11): 1 = don't want, 2 = ok, 3 = notify — learned per kind
# --------------------------------------------------------------------------- #
def test_kind_key_uses_title_prefix_and_sender():
    from pulsar.store import inbox_kind_key as k
    assert k("system", "delegation-watch", "Delegation stall: 'A'") == \
        k("system", "delegation-watch", "Delegation stall: 'B thing'")
    assert k("projects", "Traffic Monitor", "Page visitor: 17s, bounced") \
        == "projects|traffic monitor|page visitor"
    # a bare "Re:" prefix is too short to be a kind → first 4 title tokens
    assert k("p", "x", "Re: watcher — lead alerts on") == \
        "p|x|re watcher lead alerts"
    assert k("p", "x", "Delegation stall: 'A'") != k("q", "x",
                                                     "Delegation stall: 'A'")


@pytest.mark.anyio
async def test_score_learns_rule_and_gates_future_items(store, fake_spawn):
    a = store.create_inbox_item("system", "watch", "Delegation stall: 'A'",
                                "b", source_label="delegation-watch")
    b = store.create_inbox_item("system", "watch", "Delegation stall: 'B'",
                                "b", source_label="delegation-watch")
    other = store.create_inbox_item("system", "watch", "Loop guard: x", "b",
                                    source_label="delegation-watch")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        # score 1 → rule + every existing item of that kind archived
        r = await c.post(f"/inbox/items/{a.id}/score", json={"score": 1})
        assert r.status_code == 200, r.text
        v = r.json()["value"]
        assert v["item"]["score"] == 1 and v["rule"]["score"] == 1
        assert v["affected"] == 2
        assert store.get_inbox_item(b.id).read_at
        assert store.get_inbox_item(b.id).score == 1
        assert store.get_inbox_item(other.id).read_at is None
        # a NEW item of that kind is dropped outright (not stored — D8)
        before_ids = {i.id for i in store.list_inbox_items(unread_only=False)}
        r = await c.post("/inbox/items", json={
            "title": "Delegation stall: 'C'", "body": "b",
            "channel": "system", "source_label": "delegation-watch"})
        assert r.status_code == 200
        new = r.json()["value"]
        assert new == {"dropped": True, "score": 1, "kind_key": a.kind_key}
        assert {i.id for i in store.list_inbox_items(unread_only=False)} == before_ids
        r = await c.get("/inbox/unread-count")
        assert r.json()["value"]["total"] == 1   # only `other`
        # score 3 on a quiet-declared kind → notify
        r = await c.post(f"/inbox/items/{other.id}/score", json={"score": 3})
        assert r.json()["value"]["rule"]["score"] == 3
        r = await c.post("/inbox/items", json={
            "title": "Loop guard: y", "body": "b", "channel": "system",
            "source_label": "delegation-watch", "priority": "quiet"})
        assert r.json()["value"]["priority"] == "notify"
        assert r.json()["value"]["score"] == 3
        # score 2 → quiet even when the sender asked for notify
        r = await c.post(f"/inbox/items/{other.id}/score", json={"score": 2})
        r = await c.post("/inbox/items", json={
            "title": "Loop guard: z", "body": "b", "channel": "system",
            "source_label": "delegation-watch", "priority": "notify"})
        assert r.json()["value"]["priority"] == "quiet"
        # rules list + delete (unlearn)
        r = await c.get("/inbox/rules")
        keys = {x["kind_key"] for x in r.json()["value"]}
        assert keys == {a.kind_key, other.kind_key}
        r = await c.post(f"/inbox/items/{other.id}/score", json={"score": None})
        assert r.json()["value"]["rule"] is None
        assert store.get_inbox_rule(other.kind_key) is None
        assert store.get_inbox_item(other.id).score is None
        r = await c.delete(f"/inbox/rules/{a.kind_key}")
        assert r.status_code == 200
        assert (await c.get("/inbox/rules")).json()["value"] == []
        # validation
        r = await c.post(f"/inbox/items/{a.id}/score", json={"score": 7})
        assert r.status_code == 422
        r = await c.post("/inbox/items/nope/score", json={"score": 1})
        assert r.status_code == 404


def test_reply_still_refuses_a_daemon_alert_naming_a_pulse(store, fake_spawn):
    """Finding 2 guard: source_pulse_id opens the pulse, never a reply path."""
    import pytest as _pytest
    from pulsar.daemon import inbox as inbox_mod
    p = store.create_pulse("Stalled worker", "m", "pi")
    it = store.create_inbox_item("system", "loopguard", "t", "b",
                                 source_label="loop-guard",
                                 source_pulse_id=p.id)
    with _pytest.raises(inbox_mod.InboxError) as exc:
        inbox_mod.reply(store, it.id, "hello")
    assert exc.value.code == "not_replyable"


@pytest.mark.anyio
async def test_conversation_keeps_both_sides_after_reopen(store, fake_spawn):
    src = _mk(store, "worker")
    item = store.create_inbox_item(
        "projects", "pulse", "question", "First question",
        source_pulse_id=src.id, source_label="worker", needs_response=True)
    unrelated = store.create_inbox_item("system", "script", "other", "Unrelated")
    app = create_app(store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://daemon") as c, app.router.lifespan_context(app):
        # Standalone mail doesn't collect other thread-less messages.
        r = await c.get(f"/inbox/items/{item.id}/conversation")
        assert [m["id"] for m in r.json()["value"]] == [item.id]
        assert (await c.get("/inbox/items/missing/conversation")).status_code == 404
        for text in ("Yes, go ahead", "And keep me posted"):
            r = await c.post(f"/inbox/items/{item.id}/reply", json={"text": text})
            assert r.status_code == 200, r.text
        key = r.json()["value"]["thread_key"]
        answer = store.create_inbox_item(
            "projects", "pulse", "answer", "Done", source_pulse_id=src.id,
            source_label="worker", thread_key=key)
        store.set_inbox_read(item.id, True)
        before_counts = store.inbox_unread_maps()
        # Detail doesn't change badges, read state, or create inbound rows.
        r = await c.get(f"/inbox/items/{answer.id}/conversation")
        messages = r.json()["value"]
        assert [m["body"] for m in messages] == [
            "First question", "Yes, go ahead", "And keep me posted", "Done"]
        assert [m["source_kind"] for m in messages] == ["pulse", "user", "user", "pulse"]
        assert len({m["id"] for m in messages}) == 4
        assert all(m["read_at"] and m["source_label"] == "You"
                   for m in messages if m["source_kind"] == "user")
        assert store.inbox_unread_maps() == before_counts
        assert len(store.list_inbox_items()) == 3
        # Data survives a fresh store/API instance (not browser-only state).
        reopened = Store(store._path)
        try:
            other_app = create_app(store=reopened)
            other_app.state.store = reopened
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=other_app),
                                         base_url="http://daemon") as other:
                again = await other.get(f"/inbox/items/{item.id}/conversation")
                assert again.json()["value"] == messages
        finally:
            reopened.close()
        r = await c.get(f"/inbox/items/{unrelated.id}/conversation")
        assert len(r.json()["value"]) == 1


@pytest.mark.parametrize("failure", ["wake", "write"])
def test_failed_reply_does_not_add_sent_message(store, fake_spawn, monkeypatch, failure):
    src = _mk(store, "worker", state="stopped")
    item = store.create_inbox_item("projects", "pulse", "t", "b",
                                   source_pulse_id=src.id, needs_response=True)
    def fail(*args, **kwargs):
        raise RuntimeError("fixture delivery failure")
    if failure == "wake":
        monkeypatch.setattr(lifecycle, "wake_locked", fail)
    else:
        from pulsar.daemon import mailbox
        monkeypatch.setattr(mailbox, "write", fail)
    with pytest.raises(inbox_mod.InboxError, match="could not"):
        inbox_mod.reply(store, item.id, "not sent")
    assert store.inbox_conversation(item.id)[1] == []
    assert store.get_inbox_item(item.id).replied_at is None
    assert store.get_inbox_item(item.id).needs_response


@pytest.mark.anyio
async def test_conversation_not_limited_to_inbox_page(store, fake_spawn):
    src = _mk(store, "worker")
    for n in range(205):
        item = store.create_inbox_item("projects", "pulse", str(n), str(n),
                                       source_pulse_id=src.id, thread_key="long-thread")
        store.set_inbox_read(item.id, True)
    await asyncio.to_thread(inbox_mod.reply, store, item.id, "Last reply")
    app = create_app(store=store)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://daemon") as c, app.router.lifespan_context(app):
        r = await c.get(f"/inbox/items/{item.id}/conversation")
        assert len(r.json()["value"]) == 206
        assert r.json()["value"][0]["body"] == "0"
        assert r.json()["value"][-1]["body"] == "Last reply"
