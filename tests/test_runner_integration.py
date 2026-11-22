"""End-to-end runner test against the fake CLI: inbox -> events.jsonl -> ack.

Runs the real `python -m pulsar.runner` as a subprocess (no systemd fence here
— that's launch/units.py's job, proven separately), drives it over the real unix
socket, and asserts the durable surfaces the daemon relies on.
"""

import json
import asyncio
import os
import socket
import subprocess
import sys
import time
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
PY = str(REPO / "venv" / "bin" / "python")
FAKE = str(REPO / "tests" / "fake_claude.py")


def sock_op(sock_path: Path, op: str, v: int = 1) -> dict:
    s = socket.socket(socket.AF_UNIX)
    s.settimeout(5)
    s.connect(str(sock_path))
    s.sendall((json.dumps({"v": v, "op": op}) + "\n").encode())
    buf = b""
    while not buf.endswith(b"\n"):
        chunk = s.recv(65536)
        if not chunk:
            break
        buf += chunk
    s.close()
    return json.loads(buf)


def wait_for(cond, timeout=15.0, what="condition"):
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        if cond():
            return
        time.sleep(0.2)
    raise AssertionError(f"timed out waiting for {what}")


@pytest.fixture
def runner_proc(tmp_path):
    home = tmp_path / "pulse-home"
    (home / "inbox" / "new").mkdir(parents=True)
    env = dict(os.environ, PULSAR_CLAUDE_BIN=f"{PY}")
    # PULSAR_CLAUDE_BIN must be a single executable; use a shim that execs the fake
    shim = tmp_path / "claude-shim"
    shim.write_text(f"#!/usr/bin/env bash\nexec {PY} {FAKE} \"$@\"\n")
    shim.chmod(0o755)
    env["PULSAR_CLAUDE_BIN"] = str(shim)
    proc = subprocess.Popen(
        [PY, "-m", "pulsar.runner", "--pulse-id", "itest", "--home", str(home),
         "--model", "claude-opus-5", "--vessel", "claude-code"],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    yield proc, home
    if proc.poll() is None:
        proc.terminate()
        proc.wait(timeout=10)


def test_runner_end_to_end(runner_proc):
    proc, home = runner_proc
    sock = home / "runner.sock"
    wait_for(sock.exists, what="runner.sock")

    hello = sock_op(sock, "hello")
    assert hello["ok"] and hello["pulse_id"] == "itest"

    # protocol mismatch is refused loudly
    bad = sock_op(sock, "status", v=99)
    assert not bad["ok"] and bad["error"] == "protocol_version_mismatch"

    # drop a message in the inbox -> it gets fed, events land, file is acked
    msg = home / "inbox" / "new" / "20260726T000000_t1.json"
    msg.write_text(json.dumps({"text": "hello fake model"}))
    wait_for(lambda: (home / "inbox" / "acked" / msg.name).exists(),
             what="message ack")

    events = [json.loads(l) for l in
              (home / "events.jsonl").read_text().splitlines()]
    kinds = [e["type"] for e in events]
    assert "user_message" in kinds
    assert "assistant_text" in kinds
    assert "done" in kinds
    assert "session" in kinds, "cli session id must be captured for resume"

    hb = json.loads((home / "heartbeat.json").read_text())
    assert hb["state"] == "idle"
    assert hb["cli_session_id"]

    # stop over the socket -> clean exit, final heartbeat says stopped
    stop = sock_op(sock, "stop")
    assert stop["ok"]
    wait_for(lambda: proc.poll() is not None, what="runner exit")
    assert proc.returncode == 0
    hb = json.loads((home / "heartbeat.json").read_text())
    assert hb["state"] == "stopped"


def test_busy_since_tracks_state(tmp_path):
    """Status honesty: busy_since set on BUSY, cleared otherwise,
    and surfaced in heartbeat.json."""
    from pulsar.runner.runner import (
        Runner, STATE_BUSY, STATE_IDLE)

    r = Runner("p-busy", tmp_path, "m", "claude-code", None)
    assert r.busy_since is None
    r.set_state(STATE_BUSY)
    assert r.busy_since is not None
    beat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert beat["busy_since"] == r.busy_since
    r.set_state(STATE_IDLE)
    assert r.busy_since is None
    beat = json.loads((tmp_path / "heartbeat.json").read_text())
    assert beat["busy_since"] is None


def test_feed_task_crash_is_reported_as_dead_runner(tmp_path, monkeypatch):
    """A feed failure must stop the child and leave a non-clean verdict."""
    from pulsar.runner.runner import Runner

    class BlockingVessel:
        can_inject = False
        pid = 123

        def __init__(self):
            self.stopped = asyncio.Event()

        async def start(self):
            return None

        async def wait(self):
            await self.stopped.wait()
            return 0

        async def stop(self):
            self.stopped.set()

    vessel = BlockingVessel()
    monkeypatch.setattr(
        "pulsar.runner.vessel_bridge.make_vessel",
        lambda *args, **kwargs: vessel,
    )
    home = tmp_path / "pulse-home"
    (home / "inbox" / "new").mkdir(parents=True)
    (home / "inbox" / "acked").mkdir()
    (home / "inbox" / "new" / "bad.json").write_text("not json")
    runner = Runner("feed-crash", home, "m", "claude-code", None)

    rc = asyncio.run(runner.run())

    assert rc == 1
    heartbeat = json.loads((home / "heartbeat.json").read_text())
    assert heartbeat["state"] == "cli_dead"
    assert heartbeat["reason"].startswith("feed task failed:")
    events = [json.loads(line) for line in (home / "events.jsonl").read_text().splitlines()]
    assert any(event["type"] == "runner_error" and
               event["where"] == "feed_loop" for event in events)
    assert vessel.stopped.is_set()


def test_streaming_partials_end_to_end(tmp_path):
    """Streaming mode: assistant_delta events fold into an ephemeral
    partial.json (visible mid-turn), never touch events.jsonl, and the partial
    is cleared before the final assistant_text lands (a poller must never see
    both)."""
    home = tmp_path / "pulse-home"
    (home / "inbox" / "new").mkdir(parents=True)
    shim = tmp_path / "claude-shim"
    shim.write_text(f"#!/usr/bin/env bash\nexec {PY} {FAKE} \"$@\"\n")
    shim.chmod(0o755)
    env = dict(os.environ, PULSAR_CLAUDE_BIN=str(shim), FAKE_CLAUDE_DELTAS="1")
    proc = subprocess.Popen(
        [PY, "-m", "pulsar.runner", "--pulse-id", "stream", "--home", str(home),
         "--model", "claude-opus-5", "--vessel", "claude-code"],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        sock = home / "runner.sock"
        wait_for(sock.exists, what="runner.sock")

        msg = home / "inbox" / "new" / "20260808T000000_s1.json"
        msg.write_text(json.dumps({"text": "stream me"}))

        # Mid-turn (the fake holds the turn open ~1.2s after the deltas): the
        # partial must be on disk with the accumulated text.
        partial = home / "partial.json"
        wait_for(partial.exists, what="partial.json mid-turn")
        data = json.loads(partial.read_text())
        assert data["text"].startswith("Two files: ")

        wait_for(lambda: (home / "inbox" / "acked" / msg.name).exists(),
                 what="message ack")
        # Turn over: the finished block superseded the partial.
        wait_for(lambda: not partial.exists(), what="partial cleared")

        events = [json.loads(l) for l in
                  (home / "events.jsonl").read_text().splitlines()]
        kinds = [e["type"] for e in events]
        assert "assistant_delta" not in kinds, "deltas must never be durable"
        assert "assistant_text" in kinds
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_crashed_turn_message_requeued_on_start(tmp_path):
    """At-least-once on crash: a message a dead runner left claimed in
    inbox/feeding/ is swept back to new/ at startup and processed. (Only a
    deliberate daemon reset retires feeding/ instead — that path is
    test_daemon's test_reset_retires_in_flight_messages.)"""
    home = tmp_path / "pulse-home"
    (home / "inbox" / "feeding").mkdir(parents=True)
    msg = home / "inbox" / "feeding" / "20260815T000000_t1.json"
    msg.write_text(json.dumps({"text": "hello fake model"}))
    shim = tmp_path / "claude-shim"
    shim.write_text(f"#!/usr/bin/env bash\nexec {PY} {FAKE} \"$@\"\n")
    shim.chmod(0o755)
    env = dict(os.environ, PULSAR_CLAUDE_BIN=str(shim))
    proc = subprocess.Popen(
        [PY, "-m", "pulsar.runner", "--pulse-id", "itest-requeue",
         "--home", str(home), "--model", "claude-opus-5",
         "--vessel", "claude-code"],
        cwd=REPO, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        wait_for(lambda: (home / "inbox" / "acked" / msg.name).exists(),
                 what="crashed-turn message redelivered and acked")
    finally:
        if proc.poll() is None:
            proc.terminate()
            proc.wait(timeout=10)


def test_reasoning_buffer_writes_trace_and_clears(tmp_path):
    """The reasoning trace buffer: reasoning_delta accumulates into the
    ephemeral reasoning.json (display-only), bounds itself, and clears. It is
    never emitted as a durable event — this is the file the reasoning panel
    and GET /pulses/{id}/reasoning read."""
    from pulsar.runner.runner import Runner

    home = tmp_path / "pulse-home"
    home.mkdir()
    r = Runner("reasoning-unit", home, "m", "pi", None)
    path = home / "reasoning.json"

    r._on_reasoning_delta("think ")
    r._on_reasoning_delta("hard")          # inside the throttle: no write yet
    r._reasoning_written = 0.0             # force the throttled write
    r._on_reasoning_delta("er")

    data = json.loads(path.read_text())
    assert data["text"] == "think harder"
    assert data["ts"]

    # Bounded: only the newest chars survive, and the marker says so.
    r.REASONING_MAX_CHARS = 5
    r._reasoning_written = 0.0
    r._on_reasoning_delta("0123456789")
    assert "…[earlier reasoning truncated]" in r._reasoning_text
    assert r._reasoning_text.endswith("56789")
    assert len(r._reasoning_text) <= 5 + len("…[earlier reasoning truncated]\n") + 1

    r._clear_reasoning()
    assert not path.exists()
    assert r._reasoning_text == ""


def test_code_buffer_writes_live_code_and_clears_on_tool_call(tmp_path):
    """Live code: code_delta accumulates the tool call's argument JSON into
    the ephemeral code.json; a new id starts a new call; the durable
    tool_call clears it. Never a durable event itself."""
    from pulsar.runner.runner import Runner

    home = tmp_path / "pulse-home"
    home.mkdir()
    r = Runner("code-unit", home, "m", "claude-code", None)
    emitted = []
    r.emit = emitted.append
    path = home / "code.json"

    r._handle_event({"type": "code_delta", "text": "", "name": "Write",
                     "id": "t1"})
    r._handle_event({"type": "code_delta", "text": '{"file_path": "a.py"',
                     "name": None, "id": None})
    r._code_written = 0.0
    r._handle_event({"type": "code_delta", "text": ', "content": "x=1',
                     "name": None, "id": None})
    data = json.loads(path.read_text())
    assert data["name"] == "Write" and data["id"] == "t1"
    assert data["text"] == '{"file_path": "a.py", "content": "x=1'
    assert data["truncated"] is False

    # A new id is a new call: the buffer restarts.
    r._code_written = 0.0
    r._handle_event({"type": "code_delta", "text": "{", "name": "Bash",
                     "id": "t2"})
    data = json.loads(path.read_text())
    assert data["name"] == "Bash" and data["text"] == "{"

    # Capped, and the card is told.
    r.CODE_MAX_CHARS = 4
    r._code_written = 0.0
    r._handle_event({"type": "code_delta", "text": "abcdef", "id": None})
    data = json.loads(path.read_text())
    assert data["text"] == "{abc" and data["truncated"] is True

    r._handle_event({"type": "tool_call", "name": "Bash", "args": {},
                     "id": "t2"})
    assert not path.exists()
    assert not any(e.get("type") == "code_delta" for e in emitted)


def test_reasoning_block_flushes_to_a_durable_event(tmp_path):
    """Thinking cards in the chat: each finished thinking
    block is ONE durable `reasoning` event, written just before the vessel
    event that closed it (reply / tool call / done), so the card sits above
    what it led to. reasoning.json keeps the whole-turn trace for the panel
    and exposes the not-yet-carded tail as "block" for the live ghost."""
    from pulsar.runner.runner import Runner

    home = tmp_path / "pulse-home"
    home.mkdir()
    r = Runner("reasoning-block", home, "m", "pi", None)
    events_path = home / "events.jsonl"
    r._events_fh = events_path.open("a")
    path = home / "reasoning.json"

    r._on_reasoning_delta("plan the ")
    r._reasoning_written = 0.0
    r._on_reasoning_delta("edit")
    assert json.loads(path.read_text())["block"] == "plan the edit"

    # The tool call closes the block: reasoning event first, then the call.
    r._emit_vessel({"type": "tool_call", "name": "Read", "args": {}})
    lines = [json.loads(l) for l in events_path.read_text().splitlines()]
    kinds = [e["type"] for e in lines]
    assert kinds == ["reasoning", "tool_call"]
    assert lines[0]["text"] == "plan the edit" and lines[0]["ts"]
    # Ghost cleared immediately (forced write), panel trace untouched.
    data = json.loads(path.read_text())
    assert data["block"] == "" and data["text"] == "plan the edit"

    # Nothing new to say: a closer with an empty block emits no card.
    r._emit_vessel({"type": "tool_result", "ok": True, "snippet": ""})
    r._emit_vessel({"type": "assistant_text", "text": "done"})
    kinds = [json.loads(l)["type"] for l in events_path.read_text().splitlines()]
    assert kinds.count("reasoning") == 1
    # Whitespace-only thinking is not a card either.
    r._on_reasoning_delta("\n\n")
    r._emit_vessel({"type": "done", "reason": "result"})
    kinds = [json.loads(l)["type"] for l in events_path.read_text().splitlines()]
    assert kinds.count("reasoning") == 1

    # A second block after the reply is its own card, and the whole-turn
    # trace in reasoning.json spans both.
    r._on_reasoning_delta("now verify")
    r._emit_vessel({"type": "assistant_text", "text": "ok"})
    lines = [json.loads(l) for l in events_path.read_text().splitlines()]
    cards = [e["text"] for e in lines if e["type"] == "reasoning"]
    assert cards == ["plan the edit", "now verify"]
    assert json.loads(path.read_text())["text"] == "plan the edit\n\nnow verify"

    # Per-event cap: the card keeps the tail and says so; the trace cap is
    # separate.
    r.REASONING_EVENT_MAX_CHARS = 5
    r._on_reasoning_delta("0123456789")
    r._emit_vessel({"type": "done", "reason": "result"})
    lines = [json.loads(l) for l in events_path.read_text().splitlines()]
    last = [e for e in lines if e["type"] == "reasoning"][-1]
    assert lines[-1]["type"] == "done"
    assert last["text"].startswith("…[earlier reasoning truncated]")
    assert last["text"].endswith("56789")
    r._events_fh.close()


def test_reasoning_block_archives_untruncated_trace(tmp_path):
    """Reasoning-trace archive: every finished thinking
    block also lands in traces.jsonl, whole (the chat card is tail-capped at
    REASONING_EVENT_MAX_CHARS), stamped with model and vessel."""
    from pulsar.runner.runner import Runner

    home = tmp_path / "pulse-home"
    home.mkdir()
    r = Runner("trace-archive", home, "pi-qwen38-flash", "pi", None)
    r._events_fh = (home / "events.jsonl").open("a")

    long = "x" * (Runner.REASONING_EVENT_MAX_CHARS + 500)
    r._on_reasoning_delta(long)
    r._emit_vessel({"type": "tool_call", "name": "Read", "args": {}})

    card = [json.loads(l) for l in (home / "events.jsonl").read_text().splitlines()][0]
    assert card["type"] == "reasoning" and len(card["text"]) < len(long)

    traces = [json.loads(l) for l in (home / "traces.jsonl").read_text().splitlines()]
    assert len(traces) == 1
    assert traces[0]["text"] == long and traces[0]["chars"] == len(long)
    assert traces[0]["model"] == "pi-qwen38-flash" and traces[0]["vessel"] == "pi"

    # Whitespace-only blocks are neither carded nor archived.
    r._on_reasoning_delta("\n\n")
    r._emit_vessel({"type": "assistant_text", "text": "done"})
    assert len((home / "traces.jsonl").read_text().splitlines()) == 1
