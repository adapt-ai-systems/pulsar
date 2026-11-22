"""POST /pulses/{id}/bookmark — set, then summary-since-bookmark.

    venv/bin/python -m pytest tests/test_bookmark.py -q
"""

from __future__ import annotations

import json

import httpx
import pytest

from pulsar.daemon import bookmark as bm, paths
from pulsar.daemon.app import create_app
from pulsar.store import Store

pytestmark = pytest.mark.anyio


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
async def client(store):
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c:
        async with app.router.lifespan_context(app):
            yield c


def _append(pid, *events):
    f = paths.events_path(pid)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("a") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _inbox(pid):
    d = paths.inbox_new(pid)
    return sorted(d.glob("*.json")) if d.exists() else []


async def test_first_press_sets_bookmark_without_a_message(client, store):
    p = store.create_pulse("t", "claude-opus-5", "claude-code", state="running")
    _append(p.id, {"type": "assistant_text", "text": "before I left",
                   "ts": "2026-11-08T12:00:00+00:00"})
    size = paths.events_path(p.id).stat().st_size
    r = await client.post(f"/pulses/{p.id}/bookmark")
    assert r.status_code == 200, r.text
    body = r.json()["value"]
    assert body["summary"] is None
    assert body["bookmark"]["offset"] == size
    assert body["pulse"]["settings"]["bookmark"]["offset"] == size
    assert _inbox(p.id) == []
    last = json.loads(paths.events_path(p.id).read_text().splitlines()[-1])
    assert last["type"] == "bookmark"


async def test_second_press_digests_only_the_period_and_queues_summary(
        client, store):
    p = store.create_pulse("t", "claude-opus-5", "claude-code", state="running")
    _append(p.id, {"type": "assistant_text", "text": "OLD WORK",
                   "ts": "2026-11-08T12:00:00+00:00"})
    await client.post(f"/pulses/{p.id}/bookmark")
    _append(p.id,
            {"type": "user_message", "text": "hi from helper", "origin": "peer",
             "from_title": "Helper", "ts": "2026-11-08T13:00:00+00:00"},
            {"type": "tool_call", "name": "Bash", "description": "Run tests",
             "ts": "2026-11-08T13:01:00+00:00"},
            {"type": "tool_result", "ok": False, "snippet": "3 failed",
             "ts": "2026-11-08T13:02:00+00:00"},
            {"type": "usage", "in": 5, "ts": "2026-11-08T13:02:00+00:00"},
            {"type": "assistant_text", "text": "NEW WORK done, commit abc123",
             "ts": "2026-11-08T13:03:00+00:00"})
    r = await client.post(f"/pulses/{p.id}/bookmark")
    assert r.status_code == 200, r.text
    s = r.json()["value"]["summary"]
    assert s is not None
    digest = open(s["digest"]).read()
    assert "NEW WORK" in digest and "OLD WORK" not in digest
    assert "message from pulse 'Helper'" in digest
    assert "tool Bash: Run tests" in digest
    assert "✗ failed: 3 failed" in digest
    assert "usage" not in digest
    msgs = _inbox(p.id)
    assert len(msgs) == 1
    msg = json.loads(msgs[0].read_text())
    assert s["digest"] in msg["text"]
    assert msg["display_text"].startswith("🔖")
    assert msg["origin"] == "user" and msg["via"] == "bookmark"
    # New bookmark starts after this press.
    lines = paths.events_path(p.id).read_bytes().splitlines(keepends=True)
    st = store.get_pulse(p.id).settings["bookmark"]
    assert st["offset"] == sum(len(x) for x in lines[:-1])   # before the marker


async def test_archived_pulse_is_refused(client, store):
    p = store.create_pulse("t", "claude-opus-5", "claude-code", state="archived")
    r = await client.post(f"/pulses/{p.id}/bookmark")
    assert r.status_code == 409


def test_digest_is_capped(tmp_path, monkeypatch):
    f = tmp_path / "events.jsonl"
    with f.open("w") as fh:
        for i in range(400):
            fh.write(json.dumps({"type": "assistant_text",
                                 "text": f"reply {i} " + "x" * 2000}) + "\n")
    monkeypatch.setattr(bm, "DIGEST_MAX_CHARS", 20_000)
    text, count = bm.build_digest(f, 0, f.stat().st_size)
    assert count == 400
    assert len(text) < 21_000 and "omitted" in text
    assert "reply 0 " in text and "reply 399 " in text


def test_fmt_time_handles_both_stamp_styles():
    from zoneinfo import ZoneInfo

    ny = ZoneInfo("America/New_York")
    assert bm.fmt_time("2026-11-08T20:15:00+00:00", tz=ny) == "Sun Nov 8, 3:15 PM EST"
    assert bm.fmt_time("2026-11-08T04:27:41-0500", with_date=False, tz=ny) == "4:27 AM EST"


async def test_bookmark_honors_owner_timezone_setting(client, store):
    store.put_setting("owner.timezone", "Asia/Tokyo")
    p = store.create_pulse("t", "claude-opus-5", "claude-code", state="running")
    await client.post(f"/pulses/{p.id}/bookmark")
    _append(p.id, {"type": "assistant_text", "text": "work",
                   "ts": "2026-11-08T13:00:00+00:00"})
    r = await client.post(f"/pulses/{p.id}/bookmark")
    assert r.status_code == 200, r.text
    digest = open(r.json()["value"]["summary"]["digest"]).read()
    assert "Times are in Asia/Tokyo." in digest
    assert "[10:00 PM JST] agent:" in digest


async def test_delete_clears_without_a_message(client, store):
    p = store.create_pulse("t", "claude-opus-5", "claude-code", state="running")
    await client.post(f"/pulses/{p.id}/bookmark")
    r = await client.delete(f"/pulses/{p.id}/bookmark")
    assert r.status_code == 200, r.text
    assert store.get_pulse(p.id).settings.get("bookmark") is None
    assert _inbox(p.id) == []
    last = json.loads(paths.events_path(p.id).read_text().splitlines()[-1])
    assert last == {**last, "type": "bookmark", "cleared": True}
    # Next press after a clear is a fresh first press: no summary.
    r = await client.post(f"/pulses/{p.id}/bookmark")
    assert r.json()["value"]["summary"] is None
