"""Contract tests for HTTP and /live read models.

Pins:
- Roster: ``live._roster_snapshot(store)`` equals ``GET /pulses`` and
  ``GET /grids`` for a store with grids and pulses.
- Events: for each case (append, unchanged, shrink, over-cap gap, missing file,
  tail expansion, malformed line, ``event_id``, ``full``), pins the exact HTTP
  output and the exact ``/live`` output separately, asserting equality on
  shared content (parsed events of a tail window, offset, count) and asserting
  the documented differences (full tail vs delta, ``delta``/``unchanged`` keys).
- Partial and reasoning: pins the exact HTTP output (includes ``ts``) and the
  exact ``/live`` output (omits ``ts``).
- Schedule view: pins ``GET /pulses/{id}/schedules`` dict shape.

Once ``pulsar.daemon.views`` exists, runs the same assertions against its
functions directly.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest
from starlette.testclient import TestClient

from pulsar.daemon import events_io
from pulsar.daemon import live as live_mod
from pulsar.daemon import paths
from pulsar.daemon.app import create_app
from pulsar.daemon.routes.events import EVENTS_TAIL_MAX
from pulsar.store import Store

from tests.test_daemon import data_root, fake_sock, fake_spawn  # noqa: F401


def _has_views() -> bool:
    return importlib.util.find_spec("pulsar.daemon.views") is not None


@pytest.fixture()
def sync_client(data_root, fake_spawn, fake_sock):  # noqa: F811
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    with TestClient(app) as c:
        c.store = store
        yield c
    store.close()


def test_roster_http_equals_live_snapshot(sync_client):
    """live._roster_snapshot equals the pulse and grid parts of GET /pulses + GET /grids."""
    st: Store = sync_client.store

    g1 = st.create_grid("Alpha")
    g2 = st.create_grid("Beta", parent_id=g1.id)
    p1 = st.create_pulse(
        title="Leader", model="claude-opus-5", vessel="claude-code",
        grid_id=g1.id, state="running",
    )
    p2 = st.create_pulse(
        title="Worker", model="pi-qwen38", vessel="pi",
        grid_id=g2.id, state="stopped",
    )
    st.create_pulse(
        title="Archived", model="claude-opus-5", vessel="claude-code",
        grid_id=g1.id, state="archived",
    )

    r_pulses = sync_client.get("/pulses")
    assert r_pulses.status_code == 200
    http_pulses = r_pulses.json()["value"]

    r_grids = sync_client.get("/grids")
    assert r_grids.status_code == 200
    http_grids = r_grids.json()["value"]

    snap = live_mod._roster_snapshot(st)
    assert snap == {
        "type": "roster",
        "pulses": http_pulses,
        "grids": http_grids,
    }
    by_id = {p["id"]: p for p in http_pulses}
    assert set(by_id) == {p1.id, p2.id}
    # Every agent tool: stream_partials + stream_reasoning default to True in view
    assert by_id[p2.id]["settings"]["stream_partials"] is True
    assert by_id[p2.id]["settings"]["stream_reasoning"] is True
    assert by_id[p1.id]["settings"]["stream_partials"] is True
    assert by_id[p1.id]["settings"]["stream_reasoning"] is True

    if _has_views():
        from pulsar.daemon import views

        assert views.roster(st) == snap
        assert views.grid_dict(g2) == {
            "id": g2.id, "name": "Beta", "position": g2.position,
            "parent_id": g1.id,
        }
        assert views.pulse_view(p1) == by_id[p1.id]


def test_events_contracts_http_vs_live(sync_client, monkeypatch):
    """Pin exact HTTP and /live event payloads across all documented cases."""
    st: Store = sync_client.store
    p = st.create_pulse(
        title="ev", model="claude-opus-5", vessel="claude-code", state="stopped"
    )
    ev_path = paths.events_path(p.id)

    # 1. Missing file
    r_missing = sync_client.get(f"/pulses/{p.id}/events?tail=10")
    assert r_missing.status_code == 200
    assert r_missing.json()["value"] == {"events": [], "count": 0, "offset": 0}
    assert live_mod._events_tail(p.id, 10) == {
        "type": "events", "pulse_id": p.id, "offset": 0,
        "events": [], "count": 0, "delta": False,
    }
    assert live_mod._events_since(p.id, 0, 10) == {
        "type": "events", "pulse_id": p.id, "offset": 0,
        "events": [], "count": 0, "delta": False,
    }

    # Write initial events: e1, e2
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    e1 = {"type": "user_message", "id": "e1", "text": "one"}
    e2 = {"type": "assistant_text", "id": "e2", "text": "two"}
    ev_path.write_text(json.dumps(e1) + "\n" + json.dumps(e2) + "\n")
    size1 = ev_path.stat().st_size

    # Initial tail read (no after): HTTP and live share events, count, offset
    http_init = sync_client.get(f"/pulses/{p.id}/events?tail=10").json()["value"]
    live_init = live_mod._events_tail(p.id, 10)
    assert http_init == {"events": [e1, e2], "count": 2, "offset": size1}
    assert live_init == {
        "type": "events", "pulse_id": p.id, "offset": size1,
        "events": [e1, e2], "count": 2, "delta": False,
    }
    assert http_init["events"] == live_init["events"]
    assert http_init["offset"] == live_init["offset"]

    # 2. Unchanged (after == size1)
    http_unch = sync_client.get(
        f"/pulses/{p.id}/events?tail=10&after={size1}"
    ).json()["value"]
    live_unch = live_mod._events_since(p.id, size1, 10)
    assert http_unch == {
        "events": [], "count": 0, "offset": size1, "unchanged": True,
    }
    assert live_unch == {
        "type": "events", "pulse_id": p.id, "offset": size1,
        "events": [], "count": 0, "delta": True, "unchanged": True,
    }

    # 3. Append (e3 appended after size1): HTTP returns full tail window;
    #    live returns only the appended delta with delta=True.
    e3 = {"type": "assistant_text", "id": "e3", "text": "three"}
    with ev_path.open("a") as fh:
        fh.write(json.dumps(e3) + "\n")
    size2 = ev_path.stat().st_size

    http_app = sync_client.get(
        f"/pulses/{p.id}/events?tail=10&after={size1}"
    ).json()["value"]
    live_app = live_mod._events_since(p.id, size1, 10)
    assert http_app == {"events": [e1, e2, e3], "count": 3, "offset": size2}
    assert live_app == {
        "type": "events", "pulse_id": p.id, "offset": size2,
        "events": [e3], "count": 1, "delta": True,
    }
    # Explicit difference: HTTP has full tail [e1, e2, e3]; live has delta [e3]
    assert http_app["events"] != live_app["events"]
    assert http_app["events"][-1:] == live_app["events"]

    # 4. Shrink (after > size): both fall back to replacement tail
    e_fresh = {"type": "user_message", "id": "ef", "text": "fresh"}
    ev_path.write_text(json.dumps(e_fresh) + "\n")
    size_fresh = ev_path.stat().st_size

    http_shrink = sync_client.get(
        f"/pulses/{p.id}/events?tail=10&after={size2}"
    ).json()["value"]
    live_shrink = live_mod._events_since(p.id, size2, 10)
    assert http_shrink == {
        "events": [e_fresh], "count": 1, "offset": size_fresh,
    }
    assert live_shrink == {
        "type": "events", "pulse_id": p.id, "offset": size_fresh,
        "events": [e_fresh], "count": 1, "delta": False,
    }
    assert http_shrink["events"] == live_shrink["events"]

    # 5. Over-cap gap (gap > TAIL_MAX_BYTES): live falls back to full tail
    #    with delta=False and untrusted-tail width max(tail, TAIL_MAX).
    e_second = {"type": "assistant_text", "id": "es", "text": "second"}
    with ev_path.open("a") as fh:
        fh.write(json.dumps(e_second) + "\n")
    size_two = ev_path.stat().st_size

    orig_lines_after = events_io.lines_after
    monkeypatch.setattr(
        events_io, "lines_after",
        lambda path, offset, max_bytes=1: orig_lines_after(path, offset, max_bytes=1),
    )
    live_gap = live_mod._events_since(p.id, size_fresh, 10)
    assert live_gap == {
        "type": "events", "pulse_id": p.id, "offset": size_two,
        "events": [e_fresh, e_second], "count": 2, "delta": False,
    }
    monkeypatch.setattr(events_io, "lines_after", orig_lines_after)

    # 6. Malformed line + tool_result trimming + full + event_id + tail expansion
    fat_body = "x" * (events_io.TOOL_RESULT_BODY_MAX + 50)
    tr_ev = {"type": "tool_result", "id": "tr1", "ok": True, "snippet": fat_body}
    ev_path.write_text(
        json.dumps(e1) + "\n"
        + "{malformed json\n"
        + json.dumps(tr_ev) + "\n"
    )
    size_mal = ev_path.stat().st_size
    trimmed_tr = {
        "type": "tool_result", "id": "tr1", "ok": True,
        "snippet": fat_body[:events_io.TOOL_RESULT_BODY_MAX],
        "truncated": True,
    }

    http_mal = sync_client.get(f"/pulses/{p.id}/events?tail=10").json()["value"]
    live_mal = live_mod._events_tail(p.id, 10)
    expected_wire_events = [e1, {"_raw": "{malformed json"}, trimmed_tr]
    assert http_mal == {
        "events": expected_wire_events, "count": 3, "offset": size_mal,
    }
    assert live_mal == {
        "type": "events", "pulse_id": p.id, "offset": size_mal,
        "events": expected_wire_events, "count": 3, "delta": False,
    }

    # full=true keeps untrimmed tool_result body on HTTP
    http_full = sync_client.get(
        f"/pulses/{p.id}/events?tail=10&full=true"
    ).json()["value"]
    assert http_full == {
        "events": [e1, {"_raw": "{malformed json"}, tr_ev],
        "count": 3, "offset": size_mal,
    }

    # event_id filters to matching id untrimmed and honors tail beyond EVENTS_TAIL_MAX
    http_by_id = sync_client.get(
        f"/pulses/{p.id}/events?tail=1&event_id=e1"
    ).json()["value"]
    assert http_by_id == {
        "events": [e1], "count": 1, "offset": size_mal,
    }
    http_tr_id = sync_client.get(
        f"/pulses/{p.id}/events?tail=1&event_id=tr1"
    ).json()["value"]
    assert http_tr_id == {
        "events": [tr_ev], "count": 1, "offset": size_mal,
    }

    # Tail expansion: when file exceeds TAIL_WINDOW_BYTES (64 KiB), last_lines
    # widens the seek window in 4x steps to return the requested line count.
    n_big = EVENTS_TAIL_MAX + 20
    pad_line = "p" * 60
    ev_path.write_text(
        json.dumps({"type": "user_message", "id": "head-ev", "text": "very old"}) + "\n"
        + "".join(
            json.dumps({"type": "assistant_text", "id": f"m{i}", "pad": pad_line}) + "\n"
            for i in range(n_big)
        )
    )
    assert ev_path.stat().st_size > events_io.TAIL_WINDOW_BYTES * 2
    # Normal HTTP GET clamps tail to EVENTS_TAIL_MAX (and expands window past 64 KiB)
    http_clamped = sync_client.get(
        f"/pulses/{p.id}/events?tail={n_big + 10}"
    ).json()["value"]
    assert http_clamped["count"] == EVENTS_TAIL_MAX
    assert http_clamped["events"][0]["id"] == "m20"
    assert http_clamped["events"][-1]["id"] == f"m{n_big - 1}"
    # event_id searches max(clamped_tail, EVENTS_TAIL_MAX) = EVENTS_TAIL_MAX lines
    http_expanded = sync_client.get(
        "/pulses/" + p.id + "/events?tail=1&event_id=m20"
    ).json()["value"]
    assert http_expanded["events"] == [
        {"type": "assistant_text", "id": "m20", "pad": pad_line}
    ]
    assert http_expanded["count"] == 1

    # /live shrink fallback honors tail > TAIL_MAX (no route-level clamp)
    live_expanded = live_mod._events_since(p.id, 10**9, n_big + 10)
    assert live_expanded["delta"] is False
    assert live_expanded["count"] == n_big + 1
    assert live_expanded["events"][0]["id"] == "head-ev"

    if _has_views():
        from pulsar.daemon import views

        assert views.events_http(p.id, EVENTS_TAIL_MAX, None) == http_clamped
        assert views.events_http(
            p.id, 1, None, event_id="m20"
        ) == http_expanded
        # Direct call to events_http with tail > EVENTS_TAIL_MAX and event_id
        # reaches past EVENTS_TAIL_MAX (lines_read = max(tail, EVENTS_TAIL_MAX)).
        assert views.events_http(
            p.id, n_big + 10, None, event_id="head-ev"
        )["events"] == [
            {"type": "user_message", "id": "head-ev", "text": "very old"}
        ]
        assert views.events_live(p.id, n_big + 10, 10**9) == live_expanded
        assert views.events_live(p.id, 10, None) == live_mod._events_tail(p.id, 10)


def test_partial_and_reasoning_contracts_http_vs_live(sync_client):
    """Pin exact HTTP (with ts) and /live (without ts) partial and reasoning shapes."""
    st: Store = sync_client.store
    p = st.create_pulse(
        title="pr", model="claude-opus-5", vessel="claude-code", state="stopped"
    )
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    partial_file = paths.partial_path(p.id)
    reasoning_file = paths.reasoning_path(p.id)

    # 1. Missing files
    assert sync_client.get(f"/pulses/{p.id}/partial").json()["value"] == {
        "partial": None
    }
    assert live_mod._partial_payload(p.id, None) == {
        "type": "partial", "pulse_id": p.id, "text": None,
    }
    assert sync_client.get(f"/pulses/{p.id}/reasoning").json()["value"] == {
        "reasoning": None
    }
    assert live_mod._reasoning_payload(p.id, None) == {
        "type": "reasoning", "pulse_id": p.id, "text": None, "block": None,
    }

    # 2. Present with ts (and block for reasoning)
    partial_file.write_text(json.dumps({"ts": "2026-10-13T12:00:00Z", "text": "hello"}))
    st_p = live_mod._partial_stat(p.id)
    http_p = sync_client.get(f"/pulses/{p.id}/partial").json()["value"]
    live_p = live_mod._partial_payload(p.id, st_p)
    assert http_p == {
        "partial": {"ts": "2026-10-13T12:00:00Z", "text": "hello"},
    }
    assert live_p == {
        "type": "partial", "pulse_id": p.id, "text": "hello",
    }
    assert "ts" in http_p["partial"]
    assert "ts" not in live_p

    reasoning_file.write_text(json.dumps({
        "ts": "2026-10-13T12:00:01Z", "text": "full trace", "block": "tail block",
    }))
    st_r = live_mod._reasoning_stat(p.id)
    http_r = sync_client.get(f"/pulses/{p.id}/reasoning").json()["value"]
    live_r = live_mod._reasoning_payload(p.id, st_r)
    assert http_r == {
        "reasoning": {
            "ts": "2026-10-13T12:00:01Z",
            "text": "full trace",
            "block": "tail block",
        },
    }
    assert live_r == {
        "type": "reasoning",
        "pulse_id": p.id,
        "text": "full trace",
        "block": "tail block",
    }
    assert "ts" in http_r["reasoning"]
    assert "ts" not in live_r

    # 3. Reasoning without block (or non-string block) -> block is None
    reasoning_file.write_text(json.dumps({
        "ts": "2026-10-13T12:00:02Z", "text": "trace only", "block": 123,
    }))
    st_r2 = live_mod._reasoning_stat(p.id)
    assert sync_client.get(f"/pulses/{p.id}/reasoning").json()["value"] == {
        "reasoning": {
            "ts": "2026-10-13T12:00:02Z", "text": "trace only", "block": None,
        },
    }
    assert live_mod._reasoning_payload(p.id, st_r2) == {
        "type": "reasoning", "pulse_id": p.id, "text": "trace only", "block": None,
    }

    # 4. Malformed JSON or non-string text -> None
    partial_file.write_text("{corrupt")
    reasoning_file.write_text(json.dumps({"text": 999}))
    assert sync_client.get(f"/pulses/{p.id}/partial").json()["value"] == {
        "partial": None
    }
    assert live_mod._partial_payload(p.id, live_mod._partial_stat(p.id)) == {
        "type": "partial", "pulse_id": p.id, "text": None,
    }
    assert sync_client.get(f"/pulses/{p.id}/reasoning").json()["value"] == {
        "reasoning": None
    }
    assert live_mod._reasoning_payload(p.id, live_mod._reasoning_stat(p.id)) == {
        "type": "reasoning", "pulse_id": p.id, "text": None, "block": None,
    }

    if _has_views():
        from pulsar.daemon import views

        partial_file.write_text(json.dumps({"ts": "t1", "text": "hi"}))
        reasoning_file.write_text(json.dumps({"ts": "t2", "text": "think", "block": "b"}))
        assert views.read_partial(p.id) == ("hi", "t1")
        assert views.partial_http(p.id) == {"partial": {"ts": "t1", "text": "hi"}}
        assert views.partial_live(p.id, live_mod._partial_stat(p.id)) == {
            "type": "partial", "pulse_id": p.id, "text": "hi",
        }
        assert views.read_reasoning(p.id) == ("think", "b", "t2")
        assert views.reasoning_http(p.id) == {
            "reasoning": {"ts": "t2", "text": "think", "block": "b"},
        }
        assert views.reasoning_live(p.id, live_mod._reasoning_stat(p.id)) == {
            "type": "reasoning", "pulse_id": p.id, "text": "think", "block": "b",
        }


def test_schedule_dict_contract(sync_client):
    """Pin schedule serialization shape."""
    st: Store = sync_client.store
    p = st.create_pulse(
        title="sched", model="claude-opus-5", vessel="claude-code", state="stopped"
    )
    sched = st.create_schedule(
        pulse_id=p.id, kind="loop", prompt="ping", interval_minutes=15,
    )
    r = sync_client.get(f"/pulses/{p.id}/schedules")
    assert r.status_code == 200
    items = r.json()["value"]
    assert len(items) == 1
    assert items[0] == {
        "id": sched.id,
        "pulse_id": p.id,
        "kind": "loop",
        "prompt": "ping",
        "interval_minutes": 15,
        "at_iso": None,
        "cron_hhmm": None,
        "days": None,
        "enabled": True,
        "last_fired_at": None,
        "created_at": sched.created_at,
        "target_pulse_id": None,
        "triggers": ["timer"],
        "trigger_mode": "any",
    }
    if _has_views():
        from pulsar.daemon import views

        assert views.schedule_dict(sched) == items[0]
