"""Characterization of every inbox/new/*.json producer.

    TZ=UTC venv/bin/python -m pytest tests/test_mailbox_format.py -q

Pins, for each of the 8 daemon inbox writers:
  - the filename regex (`<ts>-<8hex>.json` or `<prefix>-<ts>-<8hex>.json`)
  - the exact JSON key set and `source` value
  - the `queued_at` ISO timestamp format (seconds resolution)
  - producer-level supersede for `handoff`, `handoff-seed`, `handoff-warn`, and
    `loopguard`, including cross-prefix removal (`handoff` removes queued
    `handoff-seed-*` and `handoff-warn-*` files)
  - end-to-end compatibility with `runner.requeue_message`
"""

from __future__ import annotations

import asyncio
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pulsar.daemon import condwatch as condwatch_mod
from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import inbox as inbox_mod
from pulsar.daemon import loopguard as loopguard_mod
from pulsar.daemon import messages as messages_mod
from pulsar.daemon import paths
from pulsar.daemon import questions as questions_mod
from pulsar.daemon import scheduler as scheduler_mod
from pulsar.daemon import watcher as watcher_mod
from pulsar.runner import runner as runner_mod
from tests.test_lifecycle_sites import (  # noqa: F401
    client, data_root, spy, store, _create, _write_heartbeat)

pytestmark = pytest.mark.anyio

PLAIN_NAME_RE = re.compile(r"^\d{8}T\d{12}Z-[0-9a-f]{8}\.json$")
QUEUED_AT_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\+00:00|Z)$"
)


def _prefix_re(prefix: str) -> re.Pattern[str]:
    return re.compile(rf"^{re.escape(prefix)}-\d{{8}}T\d{{12}}Z-[0-9a-f]{{8}}\.json$")


def _inbox_files(pulse_id: str) -> list[Path]:
    d = paths.inbox_new(pulse_id)
    if not d.exists():
        return []
    assert list(d.glob("*.tmp")) == []
    return sorted(d.glob("*.json"))


def _clear_inbox(pulse_id: str) -> None:
    d = paths.inbox_new(pulse_id)
    if d.exists():
        for p in d.glob("*"):
            p.unlink()


# --------------------------------------------------------------------------- #
# 1. app._write_message (via POST /pulses and POST /pulses/{id}/messages)
# --------------------------------------------------------------------------- #
async def test_app_write_message_format(client, store, spy):
    # create_pulse first_message (no spawner)
    r = await client.post("/pulses", json={
        "title": "p1", "model": "claude-opus-5", "first_message": "boot brief"
    })
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    [f] = _inbox_files(pid)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "origin", "via"}
    assert "source" not in body
    assert (body["origin"], body["via"]) == ("user", "first_message")
    assert body["text"] == "boot brief"
    assert QUEUED_AT_RE.match(body["queued_at"])
    _clear_inbox(pid)

    # POST /pulses/{id}/messages plain text
    r = await client.post(f"/pulses/{pid}/messages", json={"text": "hello pane"})
    assert r.status_code == 202, r.text
    [f] = _inbox_files(pid)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "origin", "via"}
    assert (body["origin"], body["via"]) == ("user", "pane")
    assert body["text"] == "hello pane"
    assert QUEUED_AT_RE.match(body["queued_at"])
    _clear_inbox(pid)

    # POST /pulses/{id}/messages with reply_to (adds display_text and reply_to)
    r = await client.post(f"/pulses/{pid}/messages", json={
        "text": "my reply",
        "reply_to": {"text": "earlier msg", "ts": "2026-10-13T00:00:00Z"},
    })
    assert r.status_code == 202, r.text
    [f] = _inbox_files(pid)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "display_text", "reply_to", "origin", "via"}
    assert "source" not in body
    assert body["display_text"] == "my reply"
    assert body["reply_to"] == {"text": "earlier msg", "ts": "2026-10-13T00:00:00Z"}
    assert QUEUED_AT_RE.match(body["queued_at"])
    _clear_inbox(pid)

    # Multipart with attachment (adds display_text and attachments)
    r = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "see file"},
        files=[("file", ("note.txt", b"hello", "text/plain"))],
    )
    assert r.status_code == 202, r.text
    [f] = _inbox_files(pid)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "display_text", "attachments", "origin", "via"}
    assert "source" not in body
    assert body["display_text"] == "see file"
    assert len(body["attachments"]) == 1


async def test_first_message_with_spawner_format(client, store, spy):
    spawner = store.create_pulse("lead", "claude-opus-5", "claude-code", state="running")
    r = await client.post("/pulses", json={
        "title": "child", "model": "claude-opus-5", "first_message": "do it",
        "from_pulse_id": spawner.id,
    })
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    [f] = _inbox_files(pid)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "origin", "via",
                                "from_pulse_id", "from_title"}
    assert "source" not in body
    assert (body["origin"], body["via"]) == ("peer", "first_message")
    assert (body["from_pulse_id"], body["from_title"]) == (spawner.id, "lead")
    assert body["text"] == f"[message from 'lead' (pulse {spawner.id})] do it"


async def test_cross_pulse_schedule_format(store, spy):
    a = store.create_pulse("a", "claude-opus-5", "claude-code", state="running")
    b = store.create_pulse("b", "claude-opus-5", "claude-code", state="stopped")
    _write_heartbeat(b.id, "stopped", "sess-b")
    store.create_schedule(a.id, "loop", "ping b", interval_minutes=1,
                          target_pulse_id=b.id)
    now = datetime.now(timezone.utc) + timedelta(minutes=2)
    summary = await asyncio.to_thread(scheduler_mod.tick, store, now=now)
    assert summary["fired"] == 1
    [f] = _inbox_files(b.id)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via",
                                "from_pulse_id", "from_title"}
    assert (body["origin"], body["via"]) == ("peer", "schedule")
    assert (body["from_pulse_id"], body["from_title"]) == (a.id, "a")


# --------------------------------------------------------------------------- #
# 2. messages.send
# --------------------------------------------------------------------------- #
async def test_messages_send_format(store, spy):
    sender = store.create_pulse("sender", "claude-opus-5", "claude-code", state="running")
    target = store.create_pulse("target", "claude-opus-5", "claude-code", state="stopped")
    _write_heartbeat(target.id, "stopped", "sess-t")

    name = await asyncio.to_thread(
        messages_mod.send, store, sender.id, target.id, "peer body"
    )
    [f] = _inbox_files(target.id)
    assert f.name == name
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via",
                                "from_pulse_id", "from_title"}
    assert body["source"] == "pulse"
    assert (body["origin"], body["via"]) == ("peer", "pulse")
    assert (body["from_pulse_id"], body["from_title"]) == (sender.id, "sender")
    assert body["text"] == f"[message from 'sender' (pulse {sender.id})] peer body"
    assert QUEUED_AT_RE.match(body["queued_at"])


# --------------------------------------------------------------------------- #
# 3. questions.answer / dismiss
# --------------------------------------------------------------------------- #
async def test_questions_deliver_format(store, spy):
    asker = store.create_pulse("asker", "claude-opus-5", "claude-code", state="stopped")
    _write_heartbeat(asker.id, "stopped", "sess-q")

    q1 = questions_mod.ask(store, asker.id, "Option A or B?")
    await asyncio.to_thread(questions_mod.answer, store, q1["id"], "Option A")
    [f] = _inbox_files(asker.id)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "pulse"
    assert (body["origin"], body["via"]) == ("user", "question")
    assert f"[The user answered your question {q1['id']}]" in body["text"]
    assert QUEUED_AT_RE.match(body["queued_at"])
    _clear_inbox(asker.id)

    q2 = questions_mod.ask(store, asker.id, "Another question?")
    await asyncio.to_thread(questions_mod.dismiss, store, q2["id"])
    [f] = _inbox_files(asker.id)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "pulse"
    assert (body["origin"], body["via"]) == ("user", "question")
    assert f"[The user dismissed your question {q2['id']}" in body["text"]
    assert QUEUED_AT_RE.match(body["queued_at"])


# --------------------------------------------------------------------------- #
# 4. inbox.reply
# --------------------------------------------------------------------------- #
async def test_inbox_reply_format(store, spy):
    src = store.create_pulse("worker", "claude-opus-5", "claude-code", state="stopped")
    _write_heartbeat(src.id, "stopped", "sess-i")
    item = inbox_mod.produce(
        store, title="Status update", body="All good", channel="projects",
        source_kind="pulse", source_label="worker", source_pulse_id=src.id,
        needs_response=True,
    )
    await asyncio.to_thread(inbox_mod.reply, store, item.id, "Ship it")
    [f] = _inbox_files(src.id)
    assert PLAIN_NAME_RE.match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "pulse"
    assert (body["origin"], body["via"]) == ("user", "inbox_reply")
    assert body["text"].startswith("[The user replied via Inbox re: ")
    assert "Ship it" in body["text"]
    assert QUEUED_AT_RE.match(body["queued_at"])


# --------------------------------------------------------------------------- #
# 5. scheduler._fire (normal and watching mode)
# --------------------------------------------------------------------------- #
async def test_scheduler_format(store, spy):
    p = store.create_pulse("sched-p", "claude-opus-5", "claude-code", state="stopped")
    _write_heartbeat(p.id, "stopped", "sess-s")
    s = store.create_schedule(p.id, "loop", "run check", interval_minutes=5)
    now = datetime.now(timezone.utc) + timedelta(minutes=6)

    summary = await asyncio.to_thread(scheduler_mod.tick, store, now=now)
    assert summary["fired"] == 1
    [f] = _inbox_files(p.id)
    assert _prefix_re("sched").match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "scheduler"
    assert (body["origin"], body["via"]) == ("daemon", "schedule")
    assert body["text"] == "run check"
    assert QUEUED_AT_RE.match(body["queued_at"])


# --------------------------------------------------------------------------- #
# 6. watcher._notify_spawner (via watcher.tick)
# --------------------------------------------------------------------------- #
async def test_watcher_format(store, spy):
    spawner = store.create_pulse("lead", "claude-opus-5", "claude-code", state="stopped")
    target = store.create_pulse("helper", "claude-opus-5", "claude-code", state="running")
    _write_heartbeat(spawner.id, "stopped", "sess-l")
    _write_heartbeat(target.id, "busy", "sess-h")
    now = datetime.now(timezone.utc)
    store.create_watch(
        spawner.id, target.id, "spawn",
        next_check_at=(now - timedelta(seconds=1)).isoformat(timespec="seconds"),
    )

    summary = await asyncio.to_thread(watcher_mod.tick, store, now=now)
    assert summary["working"] == 1
    [f] = _inbox_files(spawner.id)
    assert _prefix_re("watch").match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "watch"
    assert (body["origin"], body["via"]) == ("daemon", "watch")
    assert body["text"].startswith(f"[delegation watch: 'helper' (pulse {target.id})]")
    assert QUEUED_AT_RE.match(body["queued_at"])


# --------------------------------------------------------------------------- #
# 7. condwatch._notify_owner (via condwatch.tick)
# --------------------------------------------------------------------------- #
async def test_condwatch_format(store, spy, tmp_path):
    owner = store.create_pulse("owner", "claude-opus-5", "claude-code", state="archived")
    _write_heartbeat(owner.id, "stopped", "sess-o")
    flag = tmp_path / "ready.flag"
    flag.write_text("1")
    now = datetime.now(timezone.utc)
    store.create_condition_watch(
        owner.id, "file", {"path": str(flag)},
        next_check_at=(now - timedelta(seconds=1)).isoformat(timespec="seconds"),
        mode="once", note="flag ready",
    )

    summary = await asyncio.to_thread(condwatch_mod.tick, store, now=now)
    assert summary["fired"] == 1
    assert store.get_pulse(owner.id).state == "running"
    [f] = _inbox_files(owner.id)
    assert _prefix_re("condwatch").match(f.name)
    body = json.loads(f.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "watch"
    assert (body["origin"], body["via"]) == ("daemon", "condwatch")
    assert body["text"].startswith("[watch: flag ready] 🔔 condition met")
    assert QUEUED_AT_RE.match(body["queued_at"])


# --------------------------------------------------------------------------- #
# 8. handoff & loopguard format + producer-level supersede
# --------------------------------------------------------------------------- #
async def test_handoff_and_loopguard_format_and_supersede(store, spy, monkeypatch):
    p = store.create_pulse("ho-pulse", "claude-fable-5", "claude-code", state="running")

    # 8a. handoff-warn via warn_pulses (twice across two sessions -> supersedes)
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    (home / "heartbeat.json").write_text(json.dumps({
        "ts": ts, "pid": 1, "cli_pid": 2, "state": "idle",
        "cli_session_id": "sess-1", "last_ctx_tokens": 170_000, "protocol": 1,
    }))
    assert handoff_mod.warn_pulses(store) == 1
    [w1] = _inbox_files(p.id)
    assert _prefix_re("handoff-warn").match(w1.name)
    body = json.loads(w1.read_text())
    assert set(body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert body["source"] == "handoff"
    assert (body["origin"], body["via"]) == ("daemon", "handoff")
    assert QUEUED_AT_RE.match(body["queued_at"])

    # Second session warning supersedes the first handoff-warn-* file
    (home / "heartbeat.json").write_text(json.dumps({
        "ts": ts, "pid": 1, "cli_pid": 2, "state": "idle",
        "cli_session_id": "sess-2", "last_ctx_tokens": 175_000, "protocol": 1,
    }))
    assert handoff_mod.warn_pulses(store) == 1
    [w2] = _inbox_files(p.id)
    assert _prefix_re("handoff-warn").match(w2.name)
    assert w2.name != w1.name

    # 8b. loopguard steer via loopguard._strike (twice -> supersedes)
    now_iso = datetime.now(timezone.utc).isoformat()
    await loopguard_mod._strike(
        store, p, {"ts": now_iso, "line": "Go.", "repeats": 100}
    )
    lg_files_1 = [f for f in _inbox_files(p.id) if f.name.startswith("loopguard-")]
    assert len(lg_files_1) == 1
    assert _prefix_re("loopguard").match(lg_files_1[0].name)
    lg_body = json.loads(lg_files_1[0].read_text())
    assert set(lg_body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert lg_body["source"] == "loopguard"
    assert (lg_body["origin"], lg_body["via"]) == ("daemon", "loopguard")
    assert lg_body["text"] == loopguard_mod.STEER_TEXT
    assert QUEUED_AT_RE.match(lg_body["queued_at"])

    loopguard_mod._strikes.clear()
    await loopguard_mod._strike(
        store, p, {"ts": now_iso + "x", "line": "Go.", "repeats": 100}
    )
    lg_files_2 = [f for f in _inbox_files(p.id) if f.name.startswith("loopguard-")]
    assert len(lg_files_2) == 1
    assert lg_files_2[0].name != lg_files_1[0].name

    # 8c. handoff-seed via run_handoff(force=True) (twice -> supersedes)
    assert await handoff_mod.engine.run_handoff(store, p, force=True, reason="manual") is True
    seeds_1 = [f for f in _inbox_files(p.id) if f.name.startswith("handoff-seed-")]
    assert len(seeds_1) == 1
    assert _prefix_re("handoff-seed").match(seeds_1[0].name)
    seed_body = json.loads(seeds_1[0].read_text())
    assert set(seed_body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert seed_body["source"] == "handoff"
    assert (seed_body["origin"], seed_body["via"]) == ("daemon", "handoff")
    assert QUEUED_AT_RE.match(seed_body["queued_at"])

    p = store.get_pulse(p.id)
    assert await handoff_mod.engine.run_handoff(store, p, force=True, reason="manual") is True
    seeds_2 = [f for f in _inbox_files(p.id) if f.name.startswith("handoff-seed-")]
    assert len(seeds_2) == 1
    assert seeds_2[0].name != seeds_1[0].name

    # 8d. handoff request via _ask_for_note:
    # Also verify cross-prefix supersede: a queued handoff-seed-* and
    # handoff-warn-* are removed when a `handoff` request is written!
    (home / "heartbeat.json").write_text(json.dumps({
        "ts": ts, "pid": 1, "cli_pid": 2, "state": "idle",
        "cli_session_id": "sess-3", "last_ctx_tokens": 175_000, "protocol": 1,
    }))
    p = store.get_pulse(p.id)
    assert handoff_mod.warn_pulses(store) == 1
    assert any(f.name.startswith("handoff-seed-") for f in _inbox_files(p.id))
    assert any(f.name.startswith("handoff-warn-") for f in _inbox_files(p.id))

    seen_requests: list[Path] = []

    async def fake_await_reply(pulse_id, msg_file, _timeout=None, *, timing=None,
                               **_kw):
        if timing is not None:
            timing["reply_end_offset"] = 0
        f = paths.inbox_new(pulse_id) / msg_file
        assert f.is_file()
        seen_requests.append(f)
        # First call returns un-sectioned note so _ask_for_note re-asks once,
        # exercising repeated `handoff` write supersede.
        return "short note without sections" if len(seen_requests) == 1 else (
            "## 1. What we're working on\nx\n## 2. Current state\ny\n"
            "## 3. What the user wants next\nz\n## 4. Anything the successor\nw"
        )

    monkeypatch.setattr(handoff_mod.engine, "_await_reply", fake_await_reply)
    await handoff_mod.engine._ask_for_note(p)
    assert len(seen_requests) == 2
    assert seen_requests[0].name != seen_requests[1].name
    # Cross-prefix check: handoff-seed-* and handoff-warn-* were unlinked by
    # the `handoff` write, and the first `handoff-*` request was superseded by
    # the second.
    remaining_handoff = [
        f for f in _inbox_files(p.id) if f.name.startswith("handoff-")
    ]
    assert [f.name for f in remaining_handoff] == [seen_requests[1].name]
    assert _prefix_re("handoff").match(seen_requests[1].name)
    req_body = json.loads(seen_requests[1].read_text())
    assert set(req_body.keys()) == {"text", "queued_at", "source", "origin", "via"}
    assert req_body["source"] == "handoff"
    assert (req_body["origin"], req_body["via"]) == ("daemon", "handoff")
    assert QUEUED_AT_RE.match(req_body["queued_at"])
    # loopguard-* was NOT touched by `handoff` write
    assert len([f for f in _inbox_files(p.id) if f.name.startswith("loopguard-")]) == 1


# --------------------------------------------------------------------------- #
# 9. End-to-end runner.requeue_message preserves all producer keys
# --------------------------------------------------------------------------- #
async def test_requeue_message_preserves_producer_keys(client, store, spy):
    r = await client.post("/pulses", json={"title": "rq", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    r = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "hello", "reply_to": json.dumps({"text": "prior", "ts": "t1"})},
        files=[("file", ("a.txt", b"data", "text/plain"))],
    )
    assert r.status_code == 202, r.text
    [f] = _inbox_files(pid)
    original = json.loads(f.read_text())

    feeding = paths.pulse_home(pid) / "inbox" / "feeding"
    feeding.mkdir(parents=True, exist_ok=True)
    claimed = feeding / f.name
    f.replace(claimed)

    runner_mod.requeue_message(claimed, f)
    assert not claimed.exists()
    requeued = json.loads(f.read_text())
    assert requeued == original
