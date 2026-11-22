"""End-to-end vessel tests against the fake claude CLI (no real tokens).

We install a `claude` shim on a temp PATH that execs tests/fake_claude.py, so
``shutil.which("claude")`` inside the vessel resolves to the fake. The vessel is
async; each test drives it with ``asyncio.run`` (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio
import os
import stat
import sys
from pathlib import Path

import pytest

from pulsar.vessel import ClaudeVessel, VesselEvent
from pulsar.vessel.parse import _bash_exit_code, classify_record

_HERE = Path(__file__).resolve().parent
_FAKE = _HERE / "fake_claude.py"


@pytest.fixture()
def fake_claude_on_path(tmp_path, monkeypatch):
    """Put a `claude` shim (execs fake_claude.py) first on PATH."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    shim = bindir / "claude"
    shim.write_text(
        "#!/usr/bin/env bash\n"
        f'exec {sys.executable!r} {str(_FAKE)!r} "$@"\n'
    )
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    return shim


async def _collect(vessel: ClaudeVessel, sid: str, *, stop_after_done: bool) -> list[VesselEvent]:
    """Collect events until the first turn-boundary ``done`` (reason=result)."""
    out: list[VesselEvent] = []
    async for ev in vessel.subscribe(sid):
        out.append(ev)
        if stop_after_done and ev.kind == "done" and ev.payload.get("reason") == "result":
            break
    return out


def _kinds(events: list[VesselEvent]) -> list[str]:
    return [e.kind for e in events]


def test_spawn_yields_normalized_sequence(fake_claude_on_path, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = ClaudeVessel()
        sid = await v.spawn(cwd=str(cwd), initial_prompt="what files are here?")
        try:
            events = await asyncio.wait_for(
                _collect(v, sid, stop_after_done=True), timeout=15
            )
            cli_id = v.cli_session_id(sid)  # capture before stop() pops session
        finally:
            await v.stop(sid)
        return events, cli_id

    events, cli_id = asyncio.run(run())
    kinds = _kinds(events)

    # Normalized event sequence: text, tool_call, tool_result, text, usage, done.
    assert kinds == [
        "assistant_text",
        "usage",
        "tool_call",
        "usage",
        "tool_result",
        "assistant_text",
        "usage",
        "done",
    ], kinds

    # First assistant text.
    assert events[0].payload["text"] == "On it — I'll check the file."

    # tool_call preserves name, args, and the description field.
    call = next(e for e in events if e.kind == "tool_call")
    assert call.payload["name"] == "Bash"
    assert call.payload["args"] == {"command": "ls scraper/"}
    assert call.payload["description"] == "List the source directory"
    assert call.payload["id"] == "toolu_bash_1"

    # tool_result normalized shape.
    res = next(e for e in events if e.kind == "tool_result")
    assert res.payload["ok"] is True
    assert "reviews.py" in res.payload["snippet"]
    assert res.payload["exit_code"] is None
    assert res.payload["id"] == "toolu_bash_1"

    # usage folds cache tokens into `in`; ctx is in+out.
    final_usage = [e for e in events if e.kind == "usage"][-1]
    assert final_usage.payload["in"] == 20 + 100 + 3
    assert final_usage.payload["out"] == 14
    assert final_usage.payload["ctx"] == 20 + 100 + 3 + 14

    # done is a clean per-turn boundary.
    done = events[-1]
    assert done.payload["reason"] == "result"

    # CLI session id was captured from the stream for resume. The fake echoes
    # whatever --session-id the vessel passed (its generated handle), proving the
    # capture path reads session_id off the stream rather than guessing.
    assert cli_id is not None and cli_id != ""


def test_resume_relaunches_with_cli_session_id(fake_claude_on_path, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = ClaudeVessel()
        sid = await v.spawn(cwd=str(cwd), initial_prompt="hello")
        await asyncio.wait_for(_collect(v, sid, stop_after_done=True), timeout=15)
        cli_id = v.cli_session_id(sid)
        await v.stop(sid)

        # Resume: relaunch with the captured CLI session id and a new message.
        sid2 = await v.spawn(
            cwd=str(cwd),
            resume_cli_session_id=cli_id,
            initial_prompt="you were resumed — ack",
        )
        try:
            events2 = await asyncio.wait_for(
                _collect(v, sid2, stop_after_done=True), timeout=15
            )
            resumed_cli_id = v.cli_session_id(sid2)  # before stop() pops session
        finally:
            await v.stop(sid2)
        return cli_id, sid2, resumed_cli_id, events2

    cli_id, sid2, resumed_cli_id, events2 = asyncio.run(run())

    # The resume relaunch reused the CLI session id (fake echoes --resume value),
    # proving the vessel captured it on the first run and replayed it on resume.
    assert cli_id is not None and cli_id != ""
    assert resumed_cli_id == cli_id

    # The resumed turn produced its own normalized sequence.
    kinds = _kinds(events2)
    assert kinds == ["assistant_text", "usage", "done"], kinds
    assert events2[0].payload["text"] == "Resumed. Understood."
    assert events2[-1].payload["reason"] == "result"


def test_missing_binary_raises(monkeypatch, tmp_path):
    from pulsar.vessel import VesselError

    # Empty PATH and no override -> no claude binary.
    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("PULSAR_CLAUDE_BIN", raising=False)

    async def run():
        v = ClaudeVessel()
        await v.spawn(cwd=str(tmp_path), initial_prompt="x")

    with pytest.raises(VesselError):
        asyncio.run(run())


def test_malformed_claude_record_becomes_normalized_error():
    events = classify_record({"type": "assistant", "message": ["bad"]})

    assert len(events) == 1
    assert events[0].kind == "done"
    assert events[0].payload["reason"] == "error"
    assert events[0].payload["subtype"] == "malformed_record"


def test_bash_exit_code_scans_backwards_without_losing_trailing_marker():
    block = {"content": "output\n" * 100 + "Exit code: 7\n"}

    assert _bash_exit_code(block) == 7


def test_stream_event_text_delta_maps_to_assistant_delta():
    """--include-partial-messages deltas: visible text becomes assistant_delta,
    thinking becomes reasoning_delta; tool-arg deltas and non-delta stream
    events stay internal."""
    evs = classify_record({
        "type": "stream_event",
        "event": {"type": "content_block_delta", "index": 0,
                  "delta": {"type": "text_delta", "text": "Hel"}},
    })
    assert [e.kind for e in evs] == ["assistant_delta"]
    assert evs[0].payload["text"] == "Hel"

    evs = classify_record({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "thinking_delta", "thinking": "hmm"}},
    })
    assert [e.kind for e in evs] == ["reasoning_delta"]
    assert evs[0].payload["text"] == "hmm"

    assert classify_record({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "thinking_delta", "thinking": ""}},
    }) == []
    # Tool-arg JSON feeds the live code card: the tool_use block start names
    # the call, its input_json_delta chunks continue it.
    evs = classify_record({
        "type": "stream_event",
        "event": {"type": "content_block_start", "index": 1,
                  "content_block": {"type": "tool_use", "id": "toolu_1",
                                    "name": "Write", "input": {}}},
    })
    assert [e.kind for e in evs] == ["code_delta"]
    assert evs[0].payload == {"text": "", "name": "Write", "id": "toolu_1"}
    evs = classify_record({
        "type": "stream_event",
        "event": {"type": "content_block_delta",
                  "delta": {"type": "input_json_delta", "partial_json": "{"}},
    })
    assert [e.kind for e in evs] == ["code_delta"]
    assert evs[0].payload == {"text": "{", "name": None, "id": None}
    assert classify_record({
        "type": "stream_event",
        "event": {"type": "content_block_start", "index": 0,
                  "content_block": {"type": "text", "text": ""}},
    }) == []
    assert classify_record({
        "type": "stream_event", "event": {"type": "message_start"},
    }) == []
    # Total function: a malformed inner event must not raise.
    assert classify_record({"type": "stream_event", "event": "bad"}) == []


def test_command_requests_summarized_thinking():
    """Headless claude defaults thinking display to omitted; both flags are
    needed for the API to return summaries."""
    from pulsar.vessel.claude import _build_command
    cmd = _build_command("claude", model="claude-opus-5-5", effort=None,
                         session_id="s", resume_session_id=None)
    assert cmd[cmd.index("--thinking") + 1] == "adaptive"
    assert cmd[cmd.index("--thinking-display") + 1] == "summarized"


def test_result_done_carries_turn_thinking_tokens():
    evs = classify_record({
        "type": "result", "subtype": "success",
        "usage": {"output_tokens": 589,
                  "output_tokens_details": {"thinking_tokens": 368}},
    })
    assert [e.kind for e in evs] == ["done"]
    assert evs[0].payload["thinking_tokens"] == 368
    # No usage block: the field is absent, not zero.
    evs = classify_record({"type": "result", "subtype": "success"})
    assert "thinking_tokens" not in evs[0].payload


def test_usage_null_counter_counts_as_zero():
    # A null counter used to raise TypeError and surface as malformed_record.
    evs = classify_record({
        "type": "assistant",
        "message": {"content": [], "usage": {
            "input_tokens": 10, "cache_read_input_tokens": None,
            "cache_creation_input_tokens": 5, "output_tokens": None}},
    })
    usage = [e for e in evs if e.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].payload == {"in": 15, "out": 0, "ctx": 15}
