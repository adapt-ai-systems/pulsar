"""/live — the push channel that replaces roster + events polling."""
import json

import pytest
from starlette.testclient import TestClient

from pulsar.daemon import paths
from pulsar.daemon.app import create_app
from pulsar.store import Store

from tests.test_daemon import data_root, fake_spawn, fake_sock  # noqa: F401


@pytest.fixture()
def sync_client(data_root, fake_spawn, fake_sock):  # noqa: F811
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    with TestClient(app) as c:
        c.store = store
        yield c
    store.close()


def _recv_type(ws, kind, tries=8):
    for _ in range(tries):
        msg = ws.receive_json()
        if msg.get("type") == kind:
            return msg
    raise AssertionError(f"no {kind} message")


def test_live_pushes_roster_then_events_on_growth(sync_client):
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    ev_path = paths.events_path(p.id)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    ev_path.write_text(json.dumps({"type": "user_message", "text": "hi"}) + "\n")

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        roster = _recv_type(ws, "roster")
        assert [x["id"] for x in roster["pulses"]] == [p.id]
        assert isinstance(roster["grids"], list)

        first = _recv_type(ws, "events")
        assert first["pulse_id"] == p.id
        assert first["count"] == 1 and first["offset"] == ev_path.stat().st_size
        assert first.get("delta") is False

        with ev_path.open("a") as fh:
            fh.write(json.dumps({"type": "assistant_text", "text": "yo"}) + "\n")
        second = _recv_type(ws, "events")
        assert second["delta"] is True
        assert second["count"] == 1
        assert second["offset"] > first["offset"]
        assert [e["type"] for e in second["events"]] == ["assistant_text"]


def test_live_ignores_junk_and_caps_tail(sync_client):
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "nonsense"})
        ws.send_json({"type": "subscribe", "pulses": [p.id, 7, ""], "tail": 10 ** 9})
        roster = _recv_type(ws, "roster")
        assert roster["pulses"][0]["id"] == p.id
        first = _recv_type(ws, "events")   # no events.jsonl yet → empty push
        assert first["events"] == [] and first["offset"] == 0


def test_live_pushes_partial_on_change_and_clear(sync_client):
    """partial.json (mtime, size) changes become {type: partial} pushes.

    Missing-on-subscribe is silent (no null spam); a later create, edit, and
    unlink each produce one push. The pane uses this instead of GET /partial.
    """
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    partial = paths.partial_path(p.id)

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        _recv_type(ws, "roster")
        _recv_type(ws, "events")

        partial.write_text(json.dumps({"ts": "t0", "text": "hello"}))
        msg = _recv_type(ws, "partial")
        assert msg == {"type": "partial", "pulse_id": p.id, "text": "hello"}

        partial.write_text(json.dumps({"ts": "t1", "text": "hello world"}))
        msg = _recv_type(ws, "partial")
        assert msg["text"] == "hello world"

        partial.unlink()
        msg = _recv_type(ws, "partial")
        assert msg["text"] is None


def test_live_pushes_existing_partial_on_subscribe(sync_client):
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    paths.partial_path(p.id).write_text(
        json.dumps({"ts": "t0", "text": "already streaming"}))

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        msg = _recv_type(ws, "partial")
        assert msg["pulse_id"] == p.id
        assert msg["text"] == "already streaming"


def test_live_tail_change_does_not_resend_snapshot(sync_client):
    """Scrollback widens `tail` but the pane HTTP-fetches history; /live
    must not re-push the full window."""
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    ev_path = paths.events_path(p.id)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    ev_path.write_text(json.dumps({"type": "user_message", "text": "hi"}) + "\n")

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        _recv_type(ws, "roster")
        first = _recv_type(ws, "events")
        assert first["count"] == 1 and first.get("delta") is False

        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 400})
        # Next events push must be a growth delta, not a replay of the snapshot.
        with ev_path.open("a") as fh:
            fh.write(json.dumps({"type": "assistant_text", "text": "yo"}) + "\n")
        nxt = _recv_type(ws, "events")
        assert nxt["delta"] is True
        assert [e["type"] for e in nxt["events"]] == ["assistant_text"]


def test_live_file_shrink_resends_full_tail(sync_client):
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    ev_path = paths.events_path(p.id)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    ev_path.write_text(json.dumps({"n": 1}) + "\n" + json.dumps({"n": 2}) + "\n")

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        _recv_type(ws, "events")
        ev_path.write_text(json.dumps({"n": "fresh"}) + "\n")
        nxt = _recv_type(ws, "events")
        assert nxt.get("delta") is False
        assert [e["n"] for e in nxt["events"]] == ["fresh"]


def test_live_trims_tool_result_bodies_not_the_file(sync_client):
    from pulsar.daemon.events_io import TOOL_RESULT_BODY_MAX
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    ev_path = paths.events_path(p.id)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    fat = "y" * 8000
    ev_path.write_text(json.dumps({
        "type": "tool_result", "id": "c1", "ok": True, "snippet": fat,
    }) + "\n")
    on_disk = ev_path.read_text()

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        msg = _recv_type(ws, "events")
        assert msg["count"] == 1
        tr = msg["events"][0]
        assert tr["truncated"] is True
        assert tr["snippet"] == fat[:TOOL_RESULT_BODY_MAX]
        assert ev_path.read_text() == on_disk
        # Busy push is the new line only, already trimmed.
        with ev_path.open("a") as fh:
            fh.write(json.dumps({
                "type": "tool_result", "id": "c2", "ok": True,
                "snippet": "z" * 4000,
            }) + "\n")
        nxt = _recv_type(ws, "events")
        assert nxt["delta"] is True and nxt["count"] == 1
        assert nxt["events"][0]["truncated"] is True
        assert len(nxt["events"][0]["snippet"]) == TOOL_RESULT_BODY_MAX


def test_live_shrink_fallback_honors_tail_beyond_cap(sync_client):
    """The shrink/gap fallback is a REPLACEMENT snapshot — its window must be
    as wide as the caller's requested tail, even beyond TAIL_MAX. Scrollback
    clients run at 400/800/1200…; an internally-clamped fallback would
    silently drop events the client already loaded. (The first-snapshot path
    clamps at subscribe time and is a different, documented path.)"""
    from pulsar.daemon import live as live_mod
    from pulsar.daemon.live import TAIL_MAX
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    ev_path = paths.events_path(p.id)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    n = TAIL_MAX + 50
    ev_path.write_text("".join(
        json.dumps({"n": i}) + "\n" for i in range(n)))
    cursor = ev_path.stat().st_size

    # Direct unit of the fallback: an old (now-too-large) cursor forces a
    # full replacement at the caller's width.
    payload = live_mod._events_since(p.id, 10**9, n)
    assert payload["delta"] is False
    assert payload["count"] == n  # pre-fix: TAIL_MAX, silently truncating


# --- roster cache invalidation (new-pulse /live race, 2026-09-06) ---------
#
# The cache serves one snapshot per ROSTER_INTERVAL_S to every connection, so
# a frame SENT after POST /pulses returned could still carry a snapshot
# COMPUTED before the create committed. The web client adds the new pulse
# locally from the create response, so that frame briefly deleted it again —
# and on the mobile deck the pane then adopted a neighbour and never came
# back, so a newly created pulse failed to open on mobile.


def test_roster_cache_invalidate_serves_new_pulse(sync_client):
    """invalidate() must retire a still-fresh snapshot the moment a pulse is
    created — the next get() recomputes and includes it."""
    import asyncio
    from pulsar.daemon import live as live_mod
    st = sync_client.store
    cache = live_mod._RosterCache()
    first = asyncio.run(cache.get(st))
    assert first["pulses"] == []
    p = st.create_pulse(title="new", model="claude-opus-5",
                        vessel="claude-code", state="stopped")
    cache.invalidate()  # what POST /pulses now does
    second = asyncio.run(cache.get(st))
    assert [x["id"] for x in second["pulses"]] == [p.id]


def test_roster_cache_discards_compute_overtaken_by_invalidate(
        sync_client, monkeypatch):
    """A snapshot computed while a create lands must be thrown away, not
    cached — otherwise the invalidation is undone by the in-flight compute
    and the stale frame goes out anyway."""
    import asyncio
    import threading
    from pulsar.daemon import live as live_mod
    st = sync_client.store
    cache = live_mod._RosterCache()
    entered, release = threading.Event(), threading.Event()
    state = {"n": 0}
    real_snapshot = live_mod._roster_snapshot

    def fake_snapshot(store):
        state["n"] += 1
        if state["n"] == 1:
            # Simulate: compute starts, THEN the create commits + invalidates.
            entered.set()
            assert release.wait(5)
            return {"type": "roster", "pulses": [], "grids": []}
        return real_snapshot(store)

    monkeypatch.setattr(live_mod, "_roster_snapshot", fake_snapshot)

    result = {}

    def runner():
        result["value"] = asyncio.run(cache.get(st))

    t = threading.Thread(target=runner)
    t.start()
    assert entered.wait(5)
    p = st.create_pulse(title="new", model="claude-opus-5",
                        vessel="claude-code", state="stopped")
    cache.invalidate()
    release.set()
    t.join(10)
    assert not t.is_alive()
    ids = [x["id"] for x in result["value"]["pulses"]]
    assert p.id in ids
    assert state["n"] >= 2  # the pre-create compute was discarded, not cached


def test_create_invalidates_live_roster_cache(sync_client):
    """POST /pulses must drop the cached roster so no /live frame served after
    its response can predate the new pulse."""
    import asyncio
    from pulsar.daemon import live as live_mod
    st = sync_client.store
    cache = sync_client.app.state.roster_cache
    assert isinstance(cache, live_mod._RosterCache)
    asyncio.run(cache.get(st))  # prime the cache
    assert cache.payload is not None
    r = sync_client.post("/pulses", json={"title": "t", "model": "claude-opus-5"})
    assert r.status_code == 201, r.text
    assert cache.payload is None  # invalidated — next frame recomputes


def test_live_pushes_reasoning_on_change_and_clear(sync_client):
    """reasoning.json (mtime, size) changes become {type: reasoning} pushes —
    the reasoning panel's live feed, mirroring partial.json."""
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    reasoning = paths.reasoning_path(p.id)

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        _recv_type(ws, "roster")
        _recv_type(ws, "events")

        reasoning.write_text(json.dumps({"ts": "t0", "text": "thinking"}))
        msg = _recv_type(ws, "reasoning")
        # No "block" from an old runner: null, the pane falls back to "text".
        assert msg == {"type": "reasoning", "pulse_id": p.id, "text": "thinking",
                       "block": None}

        # "block" (2026-09-14) rides along: the open, not-yet-carded block.
        reasoning.write_text(json.dumps(
            {"ts": "t1", "text": "thinking harder", "block": "harder"}))
        msg = _recv_type(ws, "reasoning")
        assert msg["text"] == "thinking harder" and msg["block"] == "harder"

        reasoning.unlink()
        msg = _recv_type(ws, "reasoning")
        assert msg["text"] is None


def test_live_pushes_code_on_change_and_clear(sync_client):
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    code = paths.code_path(p.id)

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        _recv_type(ws, "roster")
        _recv_type(ws, "events")

        code.write_text(json.dumps({"ts": "t0", "id": "t1", "name": "Write",
                                    "text": '{"content": "x', "truncated": False}))
        msg = _recv_type(ws, "code")
        assert msg == {"type": "code", "pulse_id": p.id, "code": {
            "id": "t1", "name": "Write", "text": '{"content": "x',
            "truncated": False}}

        code.unlink()
        msg = _recv_type(ws, "code")
        assert msg["code"] is None


def test_live_pushes_existing_reasoning_on_subscribe(sync_client):
    st = sync_client.store
    p = st.create_pulse(title="w", model="claude-opus-5", vessel="claude-code",
                        state="stopped")
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    paths.reasoning_path(p.id).write_text(
        json.dumps({"ts": "t0", "text": "already reasoned"}))

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({"type": "subscribe", "pulses": [p.id], "tail": 50})
        msg = _recv_type(ws, "reasoning")
        assert msg["pulse_id"] == p.id
        assert msg["text"] == "already reasoned"


def test_live_subscribe_ignores_non_store_and_traversal_ids(sync_client, tmp_path):
    st = sync_client.store
    real = st.create_pulse(title="real", model="claude-opus-5", vessel="claude-code",
                           state="stopped")
    real_ev = paths.events_path(real.id)
    real_ev.parent.mkdir(parents=True, exist_ok=True)
    real_ev.write_text(json.dumps({"type": "user_message", "text": "real-ok"}) + "\n")

    trav_dir = tmp_path / "trav"
    trav_dir.mkdir(parents=True, exist_ok=True)
    (trav_dir / "events.jsonl").write_text(
        json.dumps({"type": "user_message", "text": "SECRET-OUTSIDE"}) + "\n"
    )
    sibling = paths.data_root() / "sibling"
    sibling.mkdir(parents=True, exist_ok=True)
    (sibling / "events.jsonl").write_text(
        json.dumps({"type": "user_message", "text": "SECRET-SIBLING"}) + "\n"
    )

    with sync_client.websocket_connect("/live") as ws:
        ws.send_json({
            "type": "subscribe",
            "pulses": ["/tmp/x", "../x", str(trav_dir), "../sibling", "unknown-id", real.id],
            "tail": 50,
        })
        first = _recv_type(ws, "events")
        assert first["pulse_id"] == real.id
        assert [e["text"] for e in first["events"]] == ["real-ok"]
