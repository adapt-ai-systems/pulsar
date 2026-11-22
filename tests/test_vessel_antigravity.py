"""AntigravityVessel — ACP protocol, reasoning traces, tool pairing, and interrupts."""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

from pulsar.runner.runner import Runner
from pulsar.runner.vessel_bridge import BridgedVessel
from pulsar.vessel.antigravity import (
    AntigravityVessel,
    Translator,
    _Session,
    resolve_model_and_effort,
    tool_fields,
    tool_result_fields,
    usage_from_prompt_result,
)
from pulsar.vessel.claude import VesselError
from pulsar.vessel.events import VesselEvent

_IGNORE_TERM = (
    "import signal, time\n"
    "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
    "print('ready', flush=True)\n"
    "time.sleep(60)\n"
)


async def _vessel_with_child(script: str, *, turn_done: bool = False):
    v = AntigravityVessel(agy_bin="/bin/true")
    sess = _Session(sid="agy1", cwd="/tmp", model_ref="m", env={}, turn_done=turn_done)
    proc = await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, start_new_session=True,
        stdout=asyncio.subprocess.PIPE)
    assert proc.stdout is not None
    await proc.stdout.readline()
    sess.process = proc
    v._sessions["agy1"] = sess
    return v, sess, proc


async def _collect(events):
    return [event async for event in events]


async def _collect_turn(it) -> list[VesselEvent]:
    out: list[VesselEvent] = []
    async for ev in it:
        out.append(ev)
        if ev.kind == "done":
            break
    return out


async def _wait_for_file(path: Path) -> None:
    for _ in range(300):
        if path.exists():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"agy child did not produce marker: {path}")


def _fake_agy(tmp_path: Path) -> Path:
    """Fake ``agy acp --dangerously-skip-permissions`` JSON-RPC 2.0 server."""
    script = tmp_path / "agy"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, signal, sys, threading, time\n"
        "\n"
        "assert sys.argv[1:] == ['acp', '--dangerously-skip-permissions'], sys.argv\n"
        "test_dir = pathlib.Path(os.environ['AGY_TEST_DIR'])\n"
        "log_path = test_dir / 'rpc_log.jsonl'\n"
        "out_lock = threading.Lock()\n"
        "active_prompt_id = None\n"
        "cancel_event = threading.Event()\n"
        "ignore_cancel = os.environ.get('AGY_IGNORE_CANCEL') == '1'\n"
        "reject_resume = os.environ.get('AGY_REJECT_RESUME') == '1'\n"
        "\n"
        "if ignore_cancel:\n"
        "    signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "\n"
        "def send(obj):\n"
        "    with out_lock:\n"
        "        sys.stdout.write(json.dumps(obj) + '\\n')\n"
        "        sys.stdout.flush()\n"
        "\n"
        "def record(msg):\n"
        "    with log_path.open('a', encoding='utf-8') as fh:\n"
        "        fh.write(json.dumps(msg) + '\\n')\n"
        "\n"
        "def notify(sid, update):\n"
        "    send({'jsonrpc': '2.0', 'method': 'session/update',\n"
        "          'params': {'sessionId': sid, 'update': update}})\n"
        "\n"
        "def handle_prompt(rid, sid, text):\n"
        "    global active_prompt_id\n"
        "    active_prompt_id = rid\n"
        "    cancel_event.clear()\n"
        "    tag = text if len(text) < 80 and '/' not in text else 'long_prompt'\n"
        "    (test_dir / tag).write_text(str(os.getpid()))\n"
        "    if tag == 'long_prompt':\n"
        "        (test_dir / 'long_prompt_len').write_text(str(len(text)))\n"
        "\n"
        "    if text == 'hang_cancelable':\n"
        "        notify(sid, {'sessionUpdate': 'agent_thought_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'thinking before cancel...'}})\n"
        "        notify(sid, {'sessionUpdate': 'agent_message_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'partial reply'}})\n"
        "        cancel_event.wait(timeout=30)\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {\n"
        "            'stopReason': 'cancelled',\n"
        "            'usage': {'inputTokens': 50, 'outputTokens': 10, 'thoughtTokens': 5, 'totalTokens': 65}\n"
        "        }})\n"
        "        active_prompt_id = None\n"
        "        return\n"
        "\n"
        "    if text == 'hang_wedged':\n"
        "        time.sleep(60)\n"
        "        return\n"
        "\n"
        "    if text == 'reason_and_tool':\n"
        "        notify(sid, {'sessionUpdate': 'agent_thought_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'First I inspect the workspace. '}})\n"
        "        notify(sid, {'sessionUpdate': 'agent_thought_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'Now running command.'}})\n"
        "        notify(sid, {'sessionUpdate': 'agent_message_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'Checking status.'}})\n"
        "        notify(sid, {\n"
        "            'sessionUpdate': 'tool_call',\n"
        "            'toolCallId': 'call-1',\n"
        "            'name': 'run_command',\n"
        "            'kind': 'execute',\n"
        "            'title': 'run_command',\n"
        "            'status': 'in_progress',\n"
        "            'args': {'CommandLine': 'git status', 'toolSummary': 'Check git status'},\n"
        "            'locations': [{'path': '/repo'}],\n"
        "        })\n"
        "        notify(sid, {\n"
        "            'sessionUpdate': 'tool_call_update',\n"
        "            'toolCallId': 'call-1',\n"
        "            'name': 'run_command',\n"
        "            'status': 'completed',\n"
        "            'rawOutput': {'output': 'clean tree\\n', 'exit_code': 0},\n"
        "        })\n"
        "        notify(sid, {'sessionUpdate': 'agent_thought_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'Tree is clean; summarizing.'}})\n"
        "        notify(sid, {'sessionUpdate': 'agent_message_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'All '}})\n"
        "        notify(sid, {'sessionUpdate': 'agent_message_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'good!'}})\n"
        "        notify(sid, {'sessionUpdate': 'usage_update', 'used': 14200, 'size': 1000000})\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {\n"
        "            'stopReason': 'end_turn',\n"
        "            'usage': {\n"
        "                'inputTokens': 22000,\n"
        "                'cachedReadTokens': 8000,\n"
        "                'outputTokens': 200,\n"
        "                'thoughtTokens': 345,\n"
        "                'totalTokens': 30545,\n"
        "            },\n"
        "        }})\n"
        "        active_prompt_id = None\n"
        "        return\n"
        "\n"
        "    if text == 'ask_client_rpc':\n"
        "        send({'jsonrpc': '2.0', 'id': 9001, 'method': 'fs/read_text_file',\n"
        "              'params': {'path': '/etc/hostname'}})\n"
        "        time.sleep(0.05)\n"
        "        notify(sid, {'sessionUpdate': 'agent_message_chunk',\n"
        "                     'content': {'type': 'text', 'text': 'handled gracefully'}})\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {'stopReason': 'end_turn'}})\n"
        "        active_prompt_id = None\n"
        "        return\n"
        "\n"
        "    if text == 'provider_fail':\n"
        "        sys.stderr.write('upstream 503 overloaded\\n')\n"
        "        sys.stderr.flush()\n"
        "        time.sleep(0.02)\n"
        "        send({'jsonrpc': '2.0', 'id': rid,\n"
        "              'error': {'code': -32000, 'message': 'model overloaded'}})\n"
        "        active_prompt_id = None\n"
        "        return\n"
        "\n"
        "    notify(sid, {'sessionUpdate': 'agent_message_chunk',\n"
        "                 'content': {'type': 'text', 'text': f'echo:{tag}'}})\n"
        "    send({'jsonrpc': '2.0', 'id': rid, 'result': {\n"
        "        'stopReason': 'end_turn',\n"
        "        'usage': {'inputTokens': 100, 'outputTokens': 10, 'totalTokens': 110},\n"
        "    }})\n"
        "    active_prompt_id = None\n"
        "\n"
        "for raw in sys.stdin:\n"
        "    line = raw.strip()\n"
        "    if not line:\n"
        "        continue\n"
        "    msg = json.loads(line)\n"
        "    record({k: v for k, v in msg.items() if k != 'params'} | {\n"
        "        'params_summary': (\n"
        "            {'sessionId': msg['params'].get('sessionId'), 'prompt_len': len(msg['params']['prompt'][0]['text'])}\n"
        "            if isinstance(msg.get('params'), dict) and 'prompt' in msg['params']\n"
        "            else msg.get('params')\n"
        "        )\n"
        "    })\n"
        "    rid = msg.get('id')\n"
        "    method = msg.get('method')\n"
        "    params = msg.get('params') or {}\n"
        "    if method == 'initialize':\n"
        "        assert params.get('clientCapabilities') == {}, params\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {'protocolVersion': 1}})\n"
        "    elif method == 'session/new':\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {'sessionId': 'conv-fresh-123'}})\n"
        "    elif method == 'session/resume':\n"
        "        if reject_resume:\n"
        "            send({'jsonrpc': '2.0', 'id': rid, 'error': {'code': -32602, 'message': 'session not found'}})\n"
        "        else:\n"
        "            send({'jsonrpc': '2.0', 'id': rid, 'result': {'sessionId': params['sessionId']}})\n"
        "    elif method == 'session/set_model':\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})\n"
        "    elif method == 'session/set_config_option':\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})\n"
        "    elif method == 'session/prompt':\n"
        "        text = params['prompt'][0]['text']\n"
        "        threading.Thread(target=handle_prompt, args=(rid, params['sessionId'], text), daemon=True).start()\n"
        "    elif method == 'session/cancel':\n"
        "        if not ignore_cancel:\n"
        "            cancel_event.set()\n"
        "        send({'jsonrpc': '2.0', 'id': rid, 'result': {}})\n"
    )
    script.chmod(0o755)
    return script


def test_resolve_model_and_effort_maps_picker_and_dials():
    assert resolve_model_and_effort("agy-gemini-4-neon", None) == ("gemini-4-neon-high", None)
    assert resolve_model_and_effort("agy-gemini-4-neon", "low") == ("gemini-4-neon-low", None)
    assert resolve_model_and_effort("agy-gemini-4-neon", "medium") == ("gemini-4-neon-medium", None)
    assert resolve_model_and_effort("agy-gemini-4-neon", "xhigh") == ("gemini-4-neon-xhigh", None)
    assert resolve_model_and_effort("agy-gemini-4-neon", "max") == ("gemini-4-neon-xhigh", None)
    assert resolve_model_and_effort("agy-gemini-4-carbon", None) == ("gemini-4-carbon-high", None)
    assert resolve_model_and_effort("agy-gemini-4-carbon", "low") == ("gemini-4-carbon-low", None)
    assert resolve_model_and_effort("agy-gemini-4-carbon", "medium") == ("gemini-4-carbon-medium", None)
    assert resolve_model_and_effort("agy-gemini-4-carbon", "high") == ("gemini-4-carbon-high", None)
    assert resolve_model_and_effort("agy-gemini-4-carbon", "max") == ("gemini-4-carbon-high", None)
    assert resolve_model_and_effort("gemini-4-hydrogen-high", "max") == ("gemini-4-hydrogen-xhigh", None)
    assert resolve_model_and_effort("custom-model", "medium") == ("custom-model", "medium")
    with pytest.raises(VesselError):
        resolve_model_and_effort("", None)


def test_tool_and_usage_helpers():
    name, args, desc = tool_fields({
        "name": "run_command",
        "title": "run_command",
        "args": {"CommandLine": "pytest -q", "toolSummary": "Run unit tests"},
        "locations": [{"path": "/tmp/work"}],
    })
    assert name == "run_command"
    assert desc == "Run unit tests"
    assert args["path"] == "/tmp/work"

    snip, ec = tool_result_fields({"rawOutput": {"output": "ok\n", "exit_code": 0}})
    assert (snip, ec) == ("ok", 0)
    snip2, ec2 = tool_result_fields({"content": [{"type": "diff", "path": "foo.py"}]})
    assert (snip2, ec2) == ("edited foo.py", None)

    # window_tokens (`usage_update.used`) overrides multi-step billed sum for ctx.
    mapped = usage_from_prompt_result(
        {"inputTokens": 40000, "cachedReadTokens": 10000, "outputTokens": 300,
         "thoughtTokens": 120, "totalTokens": 50420},
        window_tokens=15300,
    )
    assert mapped == (15000, 300, 15300, 120)


def test_interrupt_kills_a_term_ignoring_child_and_emits_interrupted(monkeypatch):
    monkeypatch.setattr("pulsar.vessel.antigravity._KILL_WAIT", 0.3)

    async def go():
        v, sess, proc = await _vessel_with_child(_IGNORE_TERM)
        await v.interrupt("agy1")
        return sess, proc

    sess, proc = asyncio.run(go())
    assert proc.returncode == -9  # SIGTERM ignored -> SIGKILL
    assert sess.killed_by == "interrupt" and sess.turn_done
    ev = sess.events.get_nowait()
    assert ev.kind == "done" and ev.payload["reason"] == "interrupted"


def test_stop_signals_term_and_queues_done_exited():
    async def go():
        v, sess, proc = await _vessel_with_child(
            "import time; print('ready', flush=True); time.sleep(60)")
        await v.stop("agy1")
        await proc.wait()
        return v, sess, proc

    v, sess, proc = asyncio.run(go())
    assert proc.returncode == -15
    assert "agy1" not in v._sessions and sess.stopped and sess.killed_by == "stop"
    ev = sess.events.get_nowait()
    assert ev.kind == "done" and ev.payload["reason"] == "exited"


def test_subscribe_ends_after_done_exited():
    async def go():
        v, sess, proc = await _vessel_with_child("print('ready', flush=True)", turn_done=True)
        sess.events.put_nowait(VesselEvent(kind="done", payload={"reason": "exited"}))
        kinds = [e.kind async for e in v.subscribe("agy1")]
        await proc.wait()
        return kinds

    assert asyncio.run(go()) == ["done"]


def test_acp_streams_reasoning_tools_usage_and_feeds_runner(tmp_path, monkeypatch):
    """End-to-end ACP turn -> BridgedVessel -> Runner writes reasoning.json,
    durable `reasoning` cards in events.jsonl, traces.jsonl, tool pairs,
    accurate window `ctx`, and `thinking_tokens` on `done`."""
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("PULSAR_AGY_BIN", str(script))
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))

    pulse_home = tmp_path / "pulse"
    pulse_home.mkdir()
    ws = pulse_home / "cli"
    ws.mkdir()

    runner = Runner("p-carbon", pulse_home, "agy-gemini-4-carbon", "antigravity", None)
    events_path = pulse_home / "events.jsonl"
    runner._events_fh = events_path.open("a", encoding="utf-8")

    async def go():
        bridge = BridgedVessel(
            "agy-gemini-4-carbon", ws, None,
            vessel_cls=AntigravityVessel, effort="medium",
        )
        await bridge.start()
        try:
            async for ev in bridge.send("reason_and_tool"):
                # Force every reasoning delta write so we also exercise reasoning.json
                runner._reasoning_written = 0.0
                runner._handle_event(ev)
        finally:
            await bridge.stop()

    try:
        asyncio.run(go())
    finally:
        runner._events_fh.close()

    rpc_log = [json.loads(line) for line in (tmp_path / "rpc_log.jsonl").read_text().splitlines()]
    methods = [m.get("method") for m in rpc_log if "method" in m]
    assert methods[:3] == ["initialize", "session/new", "session/set_model"]
    set_model = next(m for m in rpc_log if m.get("method") == "session/set_model")
    assert set_model["params_summary"] == {
        "sessionId": "conv-fresh-123",
        "modelId": "gemini-4-carbon-medium",
    }

    durable = [json.loads(line) for line in events_path.read_text().splitlines()]
    dtypes = [e["type"] for e in durable if e["type"] not in ("metrics", "session")]
    # Block 1 reasoning -> assistant_text -> tool_call -> tool_result ->
    # Block 2 reasoning -> assistant_text -> usage -> done
    assert dtypes == [
        "reasoning",
        "assistant_text",
        "tool_call",
        "tool_result",
        "reasoning",
        "assistant_text",
        "usage",
        "done",
    ], dtypes
    assert runner.cli_session_id == "conv-fresh-123"
    assert runner.last_ctx_tokens == 14200  # usage_update.used won over 30545 multi-step sum

    r_cards = [e["text"] for e in durable if e["type"] == "reasoning"]
    assert r_cards == [
        "First I inspect the workspace. Now running command.",
        "Tree is clean; summarizing.",
    ]
    tc = next(e for e in durable if e["type"] == "tool_call")
    tr = next(e for e in durable if e["type"] == "tool_result")
    assert tc["name"] == "run_command"
    assert tc["description"] == "Check git status"
    assert tc["args"]["path"] == "/repo"
    assert tr["id"] == tc["id"] == "call-1"
    assert tr["ok"] is True and tr["exit_code"] == 0 and tr["snippet"] == "clean tree"

    done_ev = next(e for e in durable if e["type"] == "done")
    assert done_ev["reason"] == "result"
    assert done_ev["thinking_tokens"] == 345

    # Whole-turn reasoning.json has full trace and empty open block after flush.
    r_json = json.loads((pulse_home / "reasoning.json").read_text())
    assert "First I inspect the workspace." in r_json["text"]
    assert "Tree is clean; summarizing." in r_json["text"]
    assert r_json["block"] == ""

    traces = [json.loads(line) for line in (pulse_home / "traces.jsonl").read_text().splitlines()]
    assert len(traces) == 2
    assert traces[0]["vessel"] == "antigravity"
    assert traces[0]["model"] == "agy-gemini-4-carbon"


def test_session_resume_and_fallback_to_new(tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))

    async def run_ok():
        v = AntigravityVessel(agy_bin=str(script))
        sid = await v.spawn(
            cwd=str(tmp_path / "ws1"),
            model="agy-gemini-4-carbon",
            resume_cli_session_id="legacy-stream-json-uuid",
        )
        cid = v.cli_session_id(sid)
        await v.stop(sid)
        return cid

    assert asyncio.run(run_ok()) == "legacy-stream-json-uuid"

    monkeypatch.setenv("AGY_REJECT_RESUME", "1")

    async def run_fallback():
        v = AntigravityVessel(agy_bin=str(script))
        sid = await v.spawn(
            cwd=str(tmp_path / "ws2"),
            model="agy-gemini-4-carbon",
            resume_cli_session_id="missing-uuid",
        )
        cid = v.cli_session_id(sid)
        await v.stop(sid)
        return cid

    assert asyncio.run(run_fallback()) == "conv-fresh-123"


def test_session_cancel_interrupt_keeps_acp_warm_for_next_turn(tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("PULSAR_AGY_BIN", str(script))
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))

    async def go():
        bridge = BridgedVessel("agy-gemini-4-carbon", tmp_path / "ws", None,
                               vessel_cls=AntigravityVessel)
        await bridge.start()
        first = asyncio.create_task(_collect(bridge.send("hang_cancelable")))
        await _wait_for_file(tmp_path / "hang_cancelable")
        first_pid = int((tmp_path / "hang_cancelable").read_text())
        await asyncio.sleep(0.05)  # allow thought + partial delta to arrive
        await bridge.interrupt()
        first_events = await asyncio.wait_for(first, 3)
        second_events = await asyncio.wait_for(_collect(bridge.send("second")), 3)
        second_pid = int((tmp_path / "second").read_text())
        await bridge.stop()
        return first_events, second_events, first_pid, second_pid

    first, second, first_pid, second_pid = asyncio.run(go())
    assert [e["reason"] for e in first if e["type"] == "done"] == ["interrupted"]
    assert any(e["type"] == "reasoning_delta" for e in first)
    assert [e["reason"] for e in second if e["type"] == "done"] == ["result"]
    assert not any(e["type"] == "runner_error" for e in second)
    # Warm ACP server survived `session/cancel` without respawning!
    assert first_pid == second_pid


def test_interrupt_escalates_on_wedged_child_and_restarts_warm_on_next_send(
        tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("PULSAR_AGY_BIN", str(script))
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))
    monkeypatch.setenv("AGY_IGNORE_CANCEL", "1")
    monkeypatch.setattr("pulsar.vessel.antigravity._CANCEL_WAIT", 0.15)
    monkeypatch.setattr("pulsar.vessel.antigravity._KILL_WAIT", 0.15)

    async def go():
        bridge = BridgedVessel("agy-gemini-4-carbon", tmp_path / "ws", None,
                               vessel_cls=AntigravityVessel)
        await bridge.start()
        first = asyncio.create_task(_collect(bridge.send("hang_wedged")))
        await _wait_for_file(tmp_path / "hang_wedged")
        old_pid = int((tmp_path / "hang_wedged").read_text())
        await bridge.interrupt()
        first_events = await asyncio.wait_for(first, 3)

        # Next turn runs cleanly on a freshly restarted ACP process resumed on conv-fresh-123.
        monkeypatch.setenv("AGY_IGNORE_CANCEL", "0")
        second_events = await asyncio.wait_for(_collect(bridge.send("after_wedge")), 3)
        new_pid = int((tmp_path / "after_wedge").read_text())
        await bridge.stop()
        return first_events, second_events, old_pid, new_pid

    first, second, old_pid, new_pid = asyncio.run(go())
    assert [e["reason"] for e in first if e["type"] == "done"] == ["interrupted"]
    assert [e["reason"] for e in second if e["type"] == "done"] == ["result"]
    assert old_pid != new_pid


def test_send_rejects_overlap_without_a_turn_boundary(tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))

    async def go():
        vessel = AntigravityVessel(agy_bin=str(script))
        sid = await vessel.spawn(cwd=str(tmp_path / "ws"), model="agy-gemini-4-carbon")
        await vessel.send(sid, "hang_cancelable")
        await _wait_for_file(tmp_path / "hang_cancelable")
        with pytest.raises(VesselError, match="previous agy turn is still active"):
            await vessel.send(sid, "second")
        assert not (tmp_path / "second").exists()
        await vessel.stop(sid)

    asyncio.run(go())


def test_unsolicited_server_rpc_gets_method_not_found_without_deadlock(tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))

    async def go():
        vessel = AntigravityVessel(agy_bin=str(script))
        sid = await vessel.spawn(cwd=str(tmp_path / "ws"), model="agy-gemini-4-carbon")
        it = vessel.subscribe(sid)
        await vessel.send(sid, "ask_client_rpc")
        evs = await asyncio.wait_for(_collect_turn(it), 3)
        await vessel.stop(sid)
        return evs

    evs = asyncio.run(go())
    assert [e.kind for e in evs if e.kind == "assistant_text"] == ["assistant_text"]
    assert evs[-1].payload["reason"] == "result"
    rpc_log = [json.loads(line) for line in (tmp_path / "rpc_log.jsonl").read_text().splitlines()]
    err_reply = next(m for m in rpc_log if m.get("id") == 9001)
    assert err_reply["error"]["code"] == -32601


def test_provider_rpc_error_emits_runner_error_and_done_error(tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))

    async def go():
        vessel = AntigravityVessel(agy_bin=str(script))
        sid = await vessel.spawn(cwd=str(tmp_path / "ws"), model="agy-gemini-4-carbon")
        it = vessel.subscribe(sid)
        await vessel.send(sid, "provider_fail")
        evs = await asyncio.wait_for(_collect_turn(it), 3)
        await vessel.stop(sid)
        return evs

    evs = asyncio.run(go())
    err = next(e for e in evs if e.kind == "runner_error")
    assert err.payload["where"] == "provider_error"
    assert "model overloaded" in err.payload["reason"]
    assert "upstream 503" in err.payload["reason"]
    assert evs[-1].kind == "done" and evs[-1].payload["reason"] == "error"


def test_large_prompt_uses_stdin_instead_of_argv(tmp_path, monkeypatch):
    script = _fake_agy(tmp_path)
    monkeypatch.setenv("AGY_TEST_DIR", str(tmp_path))
    prompt = "x" * (256 * 1024)  # Exceeds Linux MAX_ARG_STRLEN (128 KiB).

    async def go():
        vessel = AntigravityVessel(agy_bin=str(script))
        sid = await vessel.spawn(cwd=str(tmp_path / "ws"), model="agy-gemini-4-carbon")
        it = vessel.subscribe(sid)
        await vessel.send(sid, prompt)
        await _wait_for_file(tmp_path / "long_prompt_len")
        evs = await asyncio.wait_for(_collect_turn(it), 3)
        await vessel.stop(sid)
        return evs

    evs = asyncio.run(go())
    assert int((tmp_path / "long_prompt_len").read_text()) == len(prompt)
    assert evs[-1].kind == "done" and evs[-1].payload["reason"] == "result"


def test_usage_ctx_adds_cache_read_tokens():
    """Legacy stream-json Translator compatibility: input_tokens + cache_read_tokens."""
    tr = Translator()

    def step(idx, usage):
        return {
            "event": "step_update",
            "step_update": {
                "conversation_id": "c",
                "step_index": idx,
                "state": "DONE",
                "step_type": "agent_response",
                "usage": usage,
            },
        }

    first = [
        e for e in tr.feed(step(1, {"input_tokens": 8241, "output_tokens": 91,
                                    "thinking_tokens": 0, "cache_read_tokens": 0}))
        if e.kind == "usage"
    ]
    second = [
        e for e in tr.feed(step(5, {"input_tokens": 5684, "output_tokens": 5,
                                    "thinking_tokens": 0, "cache_read_tokens": 8242}))
        if e.kind == "usage"
    ]
    assert first[0].payload["ctx"] == 8332
    assert second[0].payload == {"in": 13926, "out": 5, "ctx": 13931}
    assert second[0].payload["ctx"] > first[0].payload["ctx"]
