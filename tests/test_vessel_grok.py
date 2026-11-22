"""Regression tests for the Grok ACP vessel."""

from __future__ import annotations

import asyncio

from pulsar.vessel.grok import (
    GrokVessel, _Session, usage_from_turn, tool_fields, tool_snippet,
)


class _FakeProcess:
    returncode = 0
    pid = 12345


def test_spawn_does_not_advertise_unimplemented_client_tools(monkeypatch, tmp_path):
    """Grok must execute tools locally instead of RPCing them back to Pulsar."""

    calls: list[tuple[str, dict]] = []
    exec_argv: list[str] = []

    async def run():
        vessel = GrokVessel()

        async def fake_exec(*args, **kwargs):
            exec_argv.extend(args)
            return _FakeProcess()

        async def fake_pump(session):
            await asyncio.Future()

        async def fake_wait(session):
            await asyncio.Future()

        async def fake_request(session, method, params, timeout=180):
            calls.append((method, params))
            if method == "initialize":
                return {"authMethods": [{"id": "cached_token"}]}
            if method == "session/new":
                return {"sessionId": "grok-session"}
            return {}

        # A stand-in binary: the test must not depend on a grok install on
        # the machine it runs on. The process itself is faked below.
        fake_bin = tmp_path / "grok"
        fake_bin.write_text("")
        monkeypatch.setenv("PULSAR_GROK_BIN", str(fake_bin))
        monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)
        monkeypatch.setattr(vessel, "_pump", fake_pump)
        monkeypatch.setattr(vessel, "_wait", fake_wait)
        monkeypatch.setattr(vessel, "_request", fake_request)

        sid = await vessel.spawn(cwd=str(tmp_path), model="grok-4.6")
        await vessel.stop(sid)

    asyncio.run(run())

    initialize = next(params for method, params in calls if method == "initialize")
    assert initialize["clientCapabilities"] == {}
    assert exec_argv[1:4] == ["agent", "--always-approve", "stdio"]


def test_send_returns_before_long_prompt_rpc_completes(tmp_path):
    """The bridge must be able to drain live updates while Grok is working."""

    async def run():
        vessel = GrokVessel()
        session = _Session("sid", str(tmp_path), _FakeProcess())
        vessel._sessions["sid"] = session
        release = asyncio.Event()
        started = asyncio.Event()

        async def slow_request(session, method, params, timeout=180):
            assert method == "session/prompt"
            assert timeout == 3600
            started.set()
            await release.wait()
            return {"stopReason": "end_turn"}

        vessel._request = slow_request
        await asyncio.wait_for(vessel.send("sid", "do work"), timeout=0.1)
        await asyncio.wait_for(started.wait(), timeout=0.1)
        assert session.prompt_task is not None
        assert not session.prompt_task.done()
        release.set()
        await asyncio.wait_for(session.prompt_task, timeout=0.1)

    asyncio.run(run())


def test_queued_send_waits_for_completed_prompt_rpc_cleanup(tmp_path):
    """A follow-up queued at turn end must not kill the Grok runner."""

    async def run():
        vessel = GrokVessel()
        session = _Session("sid", str(tmp_path), _FakeProcess())
        vessel._sessions["sid"] = session
        first_release = asyncio.Event()
        calls: list[str] = []

        async def fake_request(session, method, params, timeout=180):
            calls.append(params["prompt"][0]["text"])
            if len(calls) == 1:
                await first_release.wait()
            return {"stopReason": "end_turn"}

        vessel._request = fake_request
        await vessel.send("sid", "first")
        await asyncio.sleep(0)
        vessel._update(session, {"update": {
            "sessionUpdate": "turn_completed",
            "stop_reason": "end_turn",
        }})

        follow_up = asyncio.create_task(vessel.send("sid", "second"))
        await asyncio.sleep(0)
        assert not follow_up.done()
        first_release.set()
        await asyncio.wait_for(follow_up, timeout=0.1)
        await asyncio.sleep(0)
        assert calls == ["first", "second"]

    asyncio.run(run())


def test_send_still_rejects_a_real_overlapping_prompt(tmp_path):
    async def run():
        vessel = GrokVessel()
        session = _Session("sid", str(tmp_path), _FakeProcess())
        vessel._sessions["sid"] = session
        release = asyncio.Event()

        async def fake_request(session, method, params, timeout=180):
            await release.wait()
            return {}

        vessel._request = fake_request
        await vessel.send("sid", "first")
        await asyncio.sleep(0)
        try:
            await vessel.send("sid", "overlap")
        except Exception as exc:
            assert "already has an active prompt" in str(exc)
        else:
            raise AssertionError("overlapping prompt was accepted")
        release.set()
        await asyncio.wait_for(session.prompt_task, timeout=0.1)

    asyncio.run(run())


def _drain(session: _Session):
    events = []
    while not session.queue.empty():
        events.append(session.queue.get_nowait())
    return events


def test_usage_from_turn_prefers_live_window_over_billed_sum():
    # Live find 2026-08-13: a 161k-window turn billed 4.98M input across 44
    # inner calls; treating in+out as ctx fired the 300k Grok handoff.
    mapped = usage_from_turn(
        {"inputTokens": 4_983_969, "outputTokens": 30_230,
         "totalTokens": 5_014_199, "modelCalls": 44, "numTurns": 44},
        window_tokens=161_285,
    )
    assert mapped == (4_983_969, 30_230, 161_285)


def test_usage_from_turn_single_call_falls_back_to_in_plus_out():
    assert usage_from_turn(
        {"inputTokens": 161_413, "outputTokens": 696, "modelCalls": 1},
    ) == (161_413, 696, 162_109)


def test_usage_from_turn_drops_multi_call_bill_without_a_window():
    assert usage_from_turn(
        {"inputTokens": 4_983_969, "outputTokens": 30_230, "modelCalls": 44},
    ) is None


def test_update_uses_meta_window_not_turn_completed_bill(tmp_path):
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    vessel._update(session, {
        "_meta": {"totalTokens": 161_285},
        "update": {
            "sessionUpdate": "agent_message_chunk",
            "content": {"type": "text", "text": "done"},
        },
    })
    vessel._update(session, {
        "_meta": {"eventId": "turn-end"},
        "update": {
            "sessionUpdate": "turn_completed",
            "stop_reason": "end_turn",
            "usage": {
                "inputTokens": 4_983_969,
                "outputTokens": 30_230,
                "totalTokens": 5_014_199,
                "modelCalls": 44,
            },
        },
    })
    usage = [e for e in _drain(session) if e.kind == "usage"]
    assert len(usage) == 1
    assert usage[0].payload["ctx"] == 161_285
    assert usage[0].payload["in"] == 4_983_969
    assert usage[0].payload["out"] == 30_230


def test_tool_fields_prefer_function_name_and_path():
    name, args, desc = tool_fields({
        "title": "read_file",
        "name": "read_file",
        "rawInput": {"target_file": "/tmp/a.md"},
    })
    assert name == "read_file"
    assert args["target_file"] == "/tmp/a.md"
    assert desc is None

    name, args, desc = tool_fields({
        "title": "Read `/tmp/a.md`",
        "rawInput": {"variant": "ReadFile", "target_file": "/tmp/a.md"},
    })
    assert name == "tool"
    assert desc == "Read `/tmp/a.md`"
    assert args["target_file"] == "/tmp/a.md"


def test_tool_snippet_from_content_and_raw_output():
    assert "hello" in tool_snippet({
        "content": [{"type": "content", "content": {"type": "text", "text": "hello"}}],
    })
    assert "world" in tool_snippet({"rawOutput": {"text": "world\n"}})


def test_tool_snippet_decodes_bash_byte_output():
    raw = {"type": "Bash", "output": list(b"211 pages indexed\n225 points")}
    assert tool_snippet({"rawOutput": raw}) == "211 pages indexed\n225 points"
    assert tool_snippet({"rawOutput": list(b"hello")}) == "hello"
    assert tool_snippet({"rawOutput": {"output": "ok\n"}}) == "ok"


def test_tool_call_emits_once_and_ignores_empty_updates(tmp_path):
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    cid = "call-1"
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call",
        "toolCallId": cid,
        "name": "read_file",
        "title": "read_file",
        "rawInput": {"target_file": "/tmp/a.md"},
        "status": "pending",
    }})
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call_update",
        "toolCallId": cid,
        "title": "Read `/tmp/a.md`",
        "rawInput": {"variant": "ReadFile", "target_file": "/tmp/a.md"},
        "status": "in_progress",
    }})
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call_update",
        "toolCallId": cid,
        "status": "in_progress",
    }})
    events = _drain(session)
    calls = [e for e in events if e.kind == "tool_call"]
    assert len(calls) == 1
    assert calls[0].payload["name"] == "read_file"
    assert calls[0].payload["args"]["target_file"] == "/tmp/a.md"
    assert not any(e.kind == "tool_result" for e in events)


def test_tool_call_completed_emits_result(tmp_path):
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    cid = "call-2"
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call",
        "toolCallId": cid,
        "name": "read_file",
        "rawInput": {"target_file": "/tmp/a.md"},
    }})
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call_update",
        "toolCallId": cid,
        "status": "completed",
        "rawOutput": {"text": "file body"},
    }})
    # Duplicate completed must not double-emit.
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call_update",
        "toolCallId": cid,
        "status": "completed",
    }})
    events = _drain(session)
    assert [e.kind for e in events] == ["tool_call", "tool_result"]
    assert events[1].payload["ok"] is True
    assert "file body" in events[1].payload["snippet"]


def test_commentary_flushes_before_tools_and_on_complete_sentence(tmp_path):
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    vessel._update(session, {"update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "I'll open the file next."},
    }})
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-3",
        "name": "read_file",
        "rawInput": {"target_file": "/tmp/a.md"},
    }})
    events = _drain(session)
    kinds = [e.kind for e in events]
    assert kinds == ["assistant_delta", "assistant_text", "tool_call"]
    assert events[1].payload["text"] == "I'll open the file next."


def test_thought_chunks_become_reasoning_deltas(tmp_path):
    """The reasoning panel's feed: grok's agent_thought_chunk is preserved as
    reasoning_delta (it used to be dropped)."""
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    vessel._update(session, {"update": {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": "weighing the options"},
    }})
    events = _drain(session)
    assert [e.kind for e in events] == ["reasoning_delta"]
    assert events[0].payload["text"] == "weighing the options"

    # An empty thought emits nothing rather than an empty trace chunk.
    vessel._update(session, {"update": {
        "sessionUpdate": "agent_thought_chunk",
        "content": {"type": "text", "text": ""},
    }})
    assert _drain(session) == []


def test_partial_chunk_waits_for_tool_before_flush(tmp_path):
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    vessel._update(session, {"update": {
        "sessionUpdate": "agent_message_chunk",
        "content": {"type": "text", "text": "Looking"},
    }})
    events = _drain(session)
    assert [e.kind for e in events] == ["assistant_delta"]
    vessel._update(session, {"update": {
        "sessionUpdate": "tool_call",
        "toolCallId": "call-4",
        "name": "grep",
        "rawInput": {"pattern": "foo"},
    }})
    events = _drain(session)
    assert [e.kind for e in events] == ["assistant_text", "tool_call"]
    assert events[0].payload["text"] == "Looking"


def test_update_skips_usage_when_multi_call_bill_has_no_window(tmp_path):
    vessel = GrokVessel()
    session = _Session("sid", str(tmp_path), _FakeProcess())
    vessel._update(session, {
        "update": {
            "sessionUpdate": "turn_completed",
            "stop_reason": "end_turn",
            "usage": {
                "inputTokens": 4_983_969,
                "outputTokens": 30_230,
                "modelCalls": 44,
            },
        },
    })
    events = _drain(session)
    assert [e.kind for e in events] == ["done"]
    assert events[0].payload["reason"] == "result"


def test_stop_never_signals_a_foreign_process_group(monkeypatch, tmp_path):
    """stop() goes through safe_killpg, which refuses a pid that is not its own group leader."""
    import os
    import signal

    class _Live:
        returncode = None
        pid = 12345  # not a real group leader of ours

        async def wait(self):
            return 0

    killed = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: 999)

    async def run():
        vessel = GrokVessel()
        vessel._sessions["sid"] = _Session("sid", str(tmp_path), _Live())
        await vessel.stop("sid")

    asyncio.run(run())
    assert killed == []
