"""End-to-end vessel tests against the fake codex CLI (no real tokens).

We install a `codex` shim on a temp PATH that execs tests/fake_codex.py, so the
vessel's binary lookup resolves to the fake. Also exercises PULSAR_CODEX_BIN
(the env override that must win over PATH). The vessel is async; each test drives
it with ``asyncio.run`` (no pytest-asyncio dependency).
"""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import sys
from pathlib import Path

import pytest

from pulsar.vessel import CodexVessel, VesselEvent

_HERE = Path(__file__).resolve().parent
_FAKE = _HERE / "fake_codex.py"


def _write_shim(bindir: Path) -> Path:
    """Write a `codex` shim that execs fake_codex.py directly via the interpreter.

    A python shebang (not `#!/usr/bin/env bash`) so the shim still runs under an
    EMPTY PATH — the env-override test blanks PATH, where `env bash` can't resolve.
    """
    bindir.mkdir(exist_ok=True)
    shim = bindir / "codex"
    shim.write_text(f"#!{sys.executable}\n" f"import runpy, sys\n" f"sys.argv[0] = {str(_FAKE)!r}\n" f"runpy.run_path({str(_FAKE)!r}, run_name='__main__')\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


@pytest.fixture()
def fake_codex_on_path(tmp_path, monkeypatch):
    """Put a `codex` shim (execs fake_codex.py) first on PATH."""
    bindir = tmp_path / "bin"
    shim = _write_shim(bindir)
    monkeypatch.setenv("PATH", f"{bindir}:{os.environ.get('PATH', '')}")
    monkeypatch.delenv("PULSAR_CODEX_BIN", raising=False)
    return shim


async def _collect(vessel: CodexVessel, sid: str) -> list[VesselEvent]:
    out: list[VesselEvent] = []
    async for ev in vessel.subscribe(sid):
        out.append(ev)
        if ev.kind == "done" and ev.payload.get("reason") == "result":
            break
    return out


async def _collect_turn(it) -> list[VesselEvent]:
    """Collect from an ALREADY-OPEN subscribe iterator until one turn boundary.

    Reuses a single subscription across turns — two concurrent subscribe
    generators would race on the shared event queue.
    """
    out: list[VesselEvent] = []
    async for ev in it:
        out.append(ev)
        if ev.kind == "done" and ev.payload.get("reason") == "result":
            break
    return out


def _kinds(events: list[VesselEvent]) -> list[str]:
    return [e.kind for e in events]


def test_spawn_yields_normalized_sequence(fake_codex_on_path, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = CodexVessel()
        sid = await v.spawn(cwd=str(cwd), initial_prompt="what files are here?")
        try:
            events = await asyncio.wait_for(_collect(v, sid), timeout=15)
            cli_id = v.cli_session_id(sid)
        finally:
            await v.stop(sid)
        return events, cli_id

    events, cli_id = asyncio.run(run())
    kinds = _kinds(events)

    # tool_call -> tool_result -> assistant_text -> usage -> done.
    assert kinds == [
        "tool_call",
        "tool_result",
        "assistant_text",
        "usage",
        "done",
    ], kinds

    call = next(e for e in events if e.kind == "tool_call")
    assert call.payload["name"] == "command_execution"
    assert call.payload["args"] == {"command": "ls scraper/"}
    assert call.payload["id"] == "item_cmd_1"

    res = next(e for e in events if e.kind == "tool_result")
    assert res.payload["ok"] is True
    assert "reviews.py" in res.payload["snippet"]
    assert res.payload["exit_code"] == 0
    assert res.payload["id"] == "item_cmd_1"

    text = next(e for e in events if e.kind == "assistant_text")
    assert text.payload["text"] == "Two files: reviews.py and scrape.py."

    # codex usage: input_tokens already includes the cached portion; reasoning
    # folds into output. in=123, out=14+6=20, ctx=143.
    usage = next(e for e in events if e.kind == "usage")
    assert usage.payload["in"] == 123
    assert usage.payload["out"] == 20
    assert usage.payload["ctx"] == 143

    done = events[-1]
    assert done.payload["reason"] == "result"

    # cli_session_id is the codex thread id, captured for resume.
    assert cli_id == "fake-thread-0001"


def test_send_after_first_turn(fake_codex_on_path, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = CodexVessel()
        sid = await v.spawn(cwd=str(cwd), initial_prompt="hello")
        it = v.subscribe(sid)  # one subscription across both turns
        await asyncio.wait_for(_collect_turn(it), timeout=15)
        # A second turn on the SAME warm app-server.
        await v.send(sid, "and again")
        events2 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.stop(sid)
        return events2

    events2 = asyncio.run(run())
    kinds = _kinds(events2)
    assert kinds == ["assistant_text", "usage", "done"], kinds
    assert events2[0].payload["text"] == "Resumed. Understood."


def test_resume_relaunches_with_thread_id(fake_codex_on_path, tmp_path):
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = CodexVessel()
        sid = await v.spawn(cwd=str(cwd), initial_prompt="hello")
        await asyncio.wait_for(_collect(v, sid), timeout=15)
        cli_id = v.cli_session_id(sid)
        await v.stop(sid)

        sid2 = await v.spawn(
            cwd=str(cwd),
            resume_cli_session_id=cli_id,
            initial_prompt="you were resumed — ack",
        )
        try:
            events2 = await asyncio.wait_for(_collect(v, sid2), timeout=15)
            resumed_cli_id = v.cli_session_id(sid2)
        finally:
            await v.stop(sid2)
        return cli_id, resumed_cli_id, events2

    cli_id, resumed_cli_id, events2 = asyncio.run(run())
    assert cli_id == "fake-thread-0001"
    assert resumed_cli_id == cli_id

    kinds = _kinds(events2)
    assert kinds == ["assistant_text", "usage", "done"], kinds
    assert events2[0].payload["text"] == "Resumed. Understood."


def test_pulsar_codex_bin_env_wins(tmp_path, monkeypatch):
    """PULSAR_CODEX_BIN overrides PATH lookup (mirrors PULSAR_CLAUDE_BIN)."""
    bindir = tmp_path / "override"
    shim = _write_shim(bindir)
    # Empty PATH: only the env override can find the binary.
    monkeypatch.setenv("PATH", "")
    monkeypatch.setenv("PULSAR_CODEX_BIN", str(shim))
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = CodexVessel()
        sid = await v.spawn(cwd=str(cwd), initial_prompt="hi")
        try:
            events = await asyncio.wait_for(_collect(v, sid), timeout=15)
        finally:
            await v.stop(sid)
        return events

    events = asyncio.run(run())
    assert events[-1].payload["reason"] == "result"


def test_argv_asks_for_readable_reasoning_summaries():
    # Without this the rollout keeps {"summary": [], "encrypted_content": …}:
    # a reasoning record we can never read back (probed 2026-07-29). The
    # tokens are billed either way, so the record should be legible.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    argv = v._build_argv("codex", _Session(sid="s", cwd="/tmp", model="gpt-6-sol",
                                           effort="high"))
    assert 'model_reasoning_summary="auto"' in argv
    # each -c carries exactly one TOML assignment, and they stay paired:
    # model, effort, sandbox, summary, features.plugins + the provider-table
    # defs from the local route (ds4vllm ×4) so ex-local sessions can resume.
    assert argv.count("-c") == 9
    assert argv[argv.index('model_reasoning_summary="auto"') - 1] == "-c"


def test_argv_fast_mode_sets_service_tier():
    # Fast mode = the "fast" service tier (verified live 2026-07-31 on
    # gpt-6-sol). Off by default: no service_tier override at all.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    argv = v._build_argv("codex", _Session(sid="s", cwd="/tmp",
                                           model="gpt-6-sol", fast=True))
    assert 'service_tier="fast"' in argv
    assert argv[argv.index('service_tier="fast"') - 1] == "-c"

    argv = v._build_argv("codex", _Session(sid="s", cwd="/tmp",
                                           model="gpt-6-sol"))
    assert not any("service_tier" in a for a in argv)


def test_argv_ds4_route_carries_provider():
    # codex-ds4-flash rides the codex CLI against a local vLLM endpoint —
    # the route must rewrite the model slug and carry the whole ds4vllm
    # provider via -c so it works under any CODEX_HOME.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    argv = v._build_argv("codex", _Session(sid="s", cwd="/tmp",
                                           model="codex-ds4-flash",
                                           effort="max"))
    assert 'model="deepseek-v4-flash"' in argv
    assert not any("codex-ds4-flash" in a for a in argv)  # picker id never leaks
    assert 'model_provider="ds4vllm"' in argv
    assert ('model_providers.ds4vllm.base_url='
            '"http://192.0.2.10:8000/v1"') in argv
    assert 'model_reasoning_effort="max"' in argv
    catalog = [a for a in argv if a.startswith("model_catalog_json=")]
    assert len(catalog) == 1
    path = catalog[0].split("=", 1)[1].strip('"')
    assert Path(path).is_file()  # the repo-owned ds4-models.json
    # non-routed models keep the provider TABLE defined (a session recorded
    # under ds4vllm must still resume after a switch to a cloud model —
    # codex validates the rollout's provider against spawn config) but never
    # the selection or catalog.
    argv2 = v._build_argv("codex", _Session(sid="s", cwd="/tmp",
                                            model="gpt-6-sol"))
    assert 'model="gpt-6-sol"' in argv2
    assert ('model_providers.ds4vllm.base_url='
            '"http://192.0.2.10:8000/v1"') in argv2
    assert 'model_provider="ds4vllm"' not in argv2
    assert not any(a.startswith("model_catalog_json=") for a in argv2)


def test_interrupt_sends_rpc_and_failed_turn_closes_clean():
    # interrupt() must fire turn/interrupt for the active turn, and a
    # turn/failed caused by our own abort must close as a NORMAL boundary
    # (done reason=result, subtype=interrupted) — not a fatal cli error.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id="turn9")
    v._sessions["t1"] = sess
    sent = []

    async def fake_request(s, method, params=None, **kw):
        sent.append((method, params))
        return {}

    v._send_request = fake_request

    async def run():
        await v.interrupt("t1")
        assert sess.interrupt_requested
        await v._handle_notification(
            sess, {"jsonrpc": "2.0", "method": "turn/failed",
                   "params": {"turn_id": "turn9", "error": {"message": "aborted"}}})
        return await sess.event_queue.get()

    done = asyncio.run(run())
    assert sent == [("turn/interrupt", {"threadId": "t1", "turnId": "turn9"})]
    assert done.kind == "done"
    assert done.payload["reason"] == "result"
    assert done.payload["subtype"] == "interrupted"
    assert not sess.interrupt_requested
    assert sess.active_turn_id is None  # input gate reopened


def test_interrupt_noop_when_idle():
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id=None)
    v._sessions["t1"] = sess

    async def boom(*a, **kw):  # pragma: no cover - must not be called
        raise AssertionError("no RPC expected when idle")

    v._send_request = boom
    asyncio.run(v.interrupt("t1"))
    assert not sess.interrupt_requested


def test_unrequested_turn_failed_still_fatal():
    # A turn/failed WITHOUT an interrupt in flight keeps its meaning: a real
    # failure the runner must surface, not something to paper over.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id="turn9")
    v._sessions["t1"] = sess

    async def run():
        await v._handle_notification(
            sess, {"jsonrpc": "2.0", "method": "turn/failed",
                   "params": {"error": {"message": "boom"}}})
        return await sess.event_queue.get()

    done = asyncio.run(run())
    assert done.payload["reason"] == "error"
    assert done.payload["subtype"] == "turn_failed"


def test_reconnect_survives_runner_bridge_and_accepts_next_turn(
    fake_codex_on_path, tmp_path, monkeypatch,
):
    from pulsar.runner.vessel_bridge import BridgedVessel

    monkeypatch.setenv("FAKE_CODEX_RECONNECT", "1")

    async def run():
        bridge = BridgedVessel("gpt-6-astra", tmp_path, None, vessel_cls=CodexVessel)
        await bridge.start()
        pid = bridge.pid
        try:
            first = [ev async for ev in bridge.send("hello")]
            assert not bridge._exited.done()
            second = [ev async for ev in bridge.send("and again")]
            assert bridge.pid == pid
            assert not bridge._exited.done()
            return first, second
        finally:
            await bridge.stop()

    first, second = asyncio.run(asyncio.wait_for(run(), timeout=15))
    for events in (first, second):
        done = [ev for ev in events if ev["type"] == "done"]
        assert len(done) == 1
        assert done[0]["reason"] == "result"
    assert any(ev.get("text") == "Resumed. Understood." for ev in second)


@pytest.mark.parametrize("retry", [True, False, None, "true", 1])
def test_error_only_explicit_retry_keeps_turn_alive(retry):
    from pulsar.vessel.codex import _Session

    async def run():
        v = CodexVessel()
        sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id="turn9")
        params = {"threadId": "t1", "turnId": "turn9",
                  "error": {"message": "Reconnecting... 2/5"}}
        if retry is not None:
            params["willRetry"] = retry
        await v._handle_notification(sess, {"method": "error", "params": params})
        assert sess.active_turn_id == "turn9"
        if retry is True:
            assert sess.event_queue.empty()
        else:
            done = sess.event_queue.get_nowait()
            assert done.kind == "done"
            assert done.payload["reason"] == "error"
            assert done.payload["message"] == "Reconnecting... 2/5"

    asyncio.run(run())


@pytest.mark.parametrize("terminal", ["error", "turn/failed", "turn/completed"])
def test_reconnect_exhaustion_preserves_real_failure(terminal):
    from pulsar.vessel.codex import _Session

    async def run():
        v = CodexVessel()
        sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id="turn9")
        await v._handle_notification(sess, {"method": "error", "params": {
            "threadId": "t1", "turnId": "turn9", "willRetry": True,
            "error": {"message": "Reconnecting... 2/5"},
        }})
        assert sess.event_queue.empty()
        error = {"message": "401 Unauthorized: token refresh failed"}
        params = {"threadId": "t1", "turnId": "turn9", "willRetry": False,
                  "error": error}
        if terminal == "turn/completed":
            params = {"threadId": "t1", "turn": {
                "id": "turn9", "status": "failed", "error": error,
            }}
        await v._handle_notification(sess, {"method": terminal, "params": params})
        done = sess.event_queue.get_nowait()
        assert done.kind == "done"
        assert done.payload["reason"] == "error"
        assert done.payload["message"] == error["message"]

    asyncio.run(run())


def test_in_turn_usage_snapshots_all_surface():
    # Long tool-heavy Codex turns emit MANY thread/tokenUsage/updated
    # notifications under one turnId. Every new snapshot must reach the
    # runner (live ctx growth); only an exact repeat of the last-emitted
    # fingerprint is suppressed. Regression for the frozen last_ctx_tokens
    # bug where the adapter boolean-deduped by turn_id and dropped all
    # snapshots after the first.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id="turn9")
    v._sessions["t1"] = sess

    def usage_note(tin, tout):
        return {
            "jsonrpc": "2.0",
            "method": "thread/tokenUsage/updated",
            "params": {
                "turnId": "turn9",
                "tokenUsage": {"last": {"inputTokens": tin, "outputTokens": tout}},
            },
        }

    async def run():
        for tin, tout in [(100, 10), (200, 20), (200, 20), (300, 30)]:
            await v._handle_notification(sess, usage_note(tin, tout))
        events = []
        while not sess.event_queue.empty():
            events.append(sess.event_queue.get_nowait())
        return events

    events = asyncio.run(run())
    assert [e.kind for e in events] == ["usage", "usage", "usage"]
    assert [e.payload["ctx"] for e in events] == [110, 220, 330]


def test_trailing_usage_after_completed_emits_once():
    # ~50% of turns send tokenUsage/updated AFTER turn/completed. The first
    # trailing snapshot must surface (it is the turn's final ctx figure on the
    # app-server route), and an exact repeat of it must be suppressed.
    from pulsar.vessel.codex import _Session

    v = CodexVessel()
    sess = _Session(sid="t1", cwd="/tmp", thread_id="t1", active_turn_id="turn9")
    v._sessions["t1"] = sess

    trailing = {
        "jsonrpc": "2.0", "method": "thread/tokenUsage/updated",
        "params": {"turnId": "turn9",
                   "tokenUsage": {"last": {"inputTokens": 500,
                                           "outputTokens": 50}}},
    }

    async def run():
        await v._handle_notification(
            sess,
            {"jsonrpc": "2.0", "method": "turn/completed",
             "params": {"turn": {"id": "turn9", "status": "completed"}}})
        await v._handle_notification(sess, trailing)
        await v._handle_notification(sess, trailing)
        events = []
        while not sess.event_queue.empty():
            events.append(sess.event_queue.get_nowait())
        return events

    events = asyncio.run(run())
    assert [e.kind for e in events] == ["done", "usage"]
    assert events[1].payload == {"in": 500, "out": 50, "ctx": 550}


def test_missing_binary_raises(monkeypatch, tmp_path):
    from pulsar.vessel import VesselError

    monkeypatch.setenv("PATH", "")
    monkeypatch.delenv("PULSAR_CODEX_BIN", raising=False)

    async def run():
        v = CodexVessel()
        await v.spawn(cwd=str(tmp_path), initial_prompt="x")

    with pytest.raises(VesselError):
        asyncio.run(run())


@pytest.mark.skipif(
    shutil.which("codex") is None and not os.environ.get("PULSAR_CODEX_BIN"),
    reason="real `codex` binary not on PATH",
)
def test_real_codex_smoke(tmp_path):
    """One cheap real turn if the codex binary exists. Skipped otherwise."""
    cwd = tmp_path / "work"
    cwd.mkdir()

    async def run():
        v = CodexVessel()
        sid = await v.spawn(
            cwd=str(cwd), initial_prompt="Reply with exactly: CODEX-OK"
        )
        try:
            events = await asyncio.wait_for(_collect(v, sid), timeout=120)
        finally:
            await v.stop(sid)
        return events

    events = asyncio.run(run())
    kinds = _kinds(events)
    assert "done" in kinds
    text = " ".join(
        e.payload.get("text", "") for e in events if e.kind == "assistant_text"
    )
    assert "CODEX-OK" in text, text


def test_tool_call_graceful_with_realistic_command_item():
    """A real codex command item (command/aggregated_output/exit_code — no
    model-authored summary, per the 0.144.0 protocol) still yields a tool_call,
    with description None so the UI falls back to its derived line."""
    from pulsar.vessel.codex import classify_record

    ev = classify_record({
        "type": "item.started",
        "item": {
            "id": "i1",
            "type": "command_execution",
            "command": "ls scraper/",
        },
    })
    call = next(e for e in ev if e.kind == "tool_call")
    assert call.payload["name"] == "command_execution"
    assert call.payload["description"] is None


def test_tool_call_forwards_summary_if_protocol_ever_sends_one():
    """Best-effort passthrough: IF a command item carries a model-authored
    summary/description, forward it as the display line. Absent otherwise."""
    from pulsar.vessel.codex import classify_record

    ev = classify_record({
        "type": "item.started",
        "item": {
            "id": "i1",
            "type": "command_execution",
            "command": "ls scraper/",
            "description": "Listed the directory to find the scraper.",
        },
    })
    call = next(e for e in ev if e.kind == "tool_call")
    assert call.payload["description"] == "Listed the directory to find the scraper."


def test_tool_desc_ignores_command_echo_and_absent():
    """Never surface a 'summary' that just echoes the command, and emit None
    when the item carries no human line (graceful fallback)."""
    from pulsar.vessel.codex import _tool_desc

    assert _tool_desc(
        {"type": "command_execution", "command": "ls", "summary": "ls"}
    ) is None
    assert _tool_desc(
        {"type": "command_execution", "command": "ls", "summary": "   "}
    ) is None
    assert _tool_desc({"type": "command_execution", "command": "ls"}) is None
    assert _tool_desc(
        {"type": "command_execution", "command": "ls",
         "summary": "List the directory contents"}
    ) == "List the directory contents"


class _FakeHttp:
    def __init__(self, body_bytes): self._b = body_bytes
    def read(self): return self._b
    def __enter__(self): return self
    def __exit__(self, *a): return False


def test_summarize_blocking_disabled_returns_none(monkeypatch):
    import pulsar.vessel.codex as mod
    monkeypatch.setattr(mod, "SUMMARIZER_ENABLED", False)
    assert mod._summarize_blocking("ls -la") is None


def test_summarize_blocking_unset_url_returns_none(monkeypatch):
    import pulsar.vessel.codex as mod
    monkeypatch.setattr(mod, "SUMMARIZER_ENABLED", True)
    monkeypatch.setattr(mod, "SUMMARIZER_URL", "")
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("should not call urlopen when URL unset"))
    assert mod._summarize_blocking("ls -la") is None


def test_summarize_blocking_parses_response(monkeypatch):
    import pulsar.vessel.codex as mod
    monkeypatch.setattr(mod, "SUMMARIZER_ENABLED", True)
    monkeypatch.setattr(mod, "SUMMARIZER_URL", "http://192.0.2.10:8888/v1")
    body = b'{"choices":[{"message":{"content":"  Listed the folder contents.  "}}]}'
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: _FakeHttp(body))
    assert mod._summarize_blocking("ls -la") == "Listed the folder contents."


def test_summarize_blocking_rejects_prompt_or_scratch_leak(monkeypatch):
    import pulsar.vessel.codex as mod
    monkeypatch.setattr(mod, "SUMMARIZER_ENABLED", True)
    monkeypatch.setattr(mod, "SUMMARIZER_URL", "http://192.0.2.10:8888/v1")
    body = json.dumps({"choices": [{"message": {"content":
        "We need answer user's request. Need produce final only one short "
        "plain-English sentence at most two clauses. Command: ls -la"}}]}).encode()
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: _FakeHttp(body))
    assert mod._summarize_blocking("ls -la") is None
    body = json.dumps({"choices": [{"message": {"content":
        "You write terse display labels for an AI agent's tool calls."}}]}).encode()
    monkeypatch.setattr(mod.urllib.request, "urlopen",
                        lambda *a, **k: _FakeHttp(body))
    assert mod._summarize_blocking("ls -la") is None


def test_summarize_blocking_none_on_error(monkeypatch):
    import pulsar.vessel.codex as mod
    monkeypatch.setattr(mod, "SUMMARIZER_ENABLED", True)
    monkeypatch.setattr(mod, "SUMMARIZER_URL", "http://192.0.2.10:8888/v1")

    def boom(*a, **k):
        raise RuntimeError("unreachable")
    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    assert mod._summarize_blocking("ls -la") is None


def test_summarize_command_graceful_when_disabled(monkeypatch):
    import pulsar.vessel.codex as mod
    monkeypatch.setattr(mod, "SUMMARIZER_ENABLED", False)
    import asyncio
    assert asyncio.run(mod.summarize_command("ls -la")) is None


def test_reasoning_item_becomes_reasoning_delta():
    """codex's reasoning summary (model_reasoning_summary="auto") is preserved
    for the pane's reasoning panel instead of being dropped."""
    from pulsar.vessel.codex import classify_record

    item = {"id": "rs_1", "type": "reasoning",
            "summary": [{"type": "summary_text",
                         "text": "**Checking the file**"}]}
    events = classify_record({"type": "item.completed", "item": item})
    assert [e.kind for e in events] == ["reasoning_delta"]
    assert events[0].payload == {"text": "**Checking the file**"}

    # Started carries no text yet; a reasoning item with nothing readable is
    # silent rather than an empty trace chunk.
    assert classify_record({"type": "item.started", "item": item}) == []
    assert classify_record({"type": "item.completed",
                            "item": {"type": "reasoning"}}) == []


def test_reasoning_item_accepts_string_and_content_shapes():
    from pulsar.vessel.codex import classify_record

    for item in ({"type": "reasoning", "summary": "plain summary"},
                 {"type": "reasoning", "text": "plain text"},
                 {"type": "reasoning",
                  "content": [{"type": "text", "text": "block text"}]}):
        events = classify_record({"type": "item.completed", "item": item})
        assert [e.kind for e in events] == ["reasoning_delta"], item
        assert events[0].payload["text"]
