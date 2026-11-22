"""Vessel tests for PiRpcVessel against the fake pi rpc CLI shim (no real pair).

PiRpcVessel drives ONE long-lived `pi --mode rpc` process per session and
adds a real `inject` (steer) path. These tests prove: prompt over stdin,
event mapping reuses classify_record (turn_end→usage, agent_end→done{result}),
process-exit → done{exited} (the inverted backstop), the turn_in_flight-gated
watchdog, and the steer command on inject. Driven with asyncio.run + wait_for
(no pytest-asyncio), mirroring test_vessel_pi.
"""
from __future__ import annotations
import asyncio
import json
import os
import stat
import sys
import uuid
from pathlib import Path
import pytest

from pulsar.vessel.pi import PiRpcVessel
from pulsar.vessel.events import VesselEvent

_HERE = Path(__file__).resolve().parent
_FAKE = _HERE / "fake_pi_rpc.py"


def _write_shim(bindir: Path) -> Path:
    bindir.mkdir(exist_ok=True)
    shim = bindir / "pi"
    shim.write_text(
        f"#!{sys.executable}\n"
        f"import runpy, sys\n"
        f"sys.argv[0] = {str(_FAKE)!r}\n"
        f"runpy.run_path({str(_FAKE)!r}, run_name='__main__')\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    return shim


def _shim_env(monkeypatch, tmp_path) -> Path:
    bindir = tmp_path / "bin"
    _write_shim(bindir)
    monkeypatch.setenv("PULSAR_PI_BIN", str(bindir / "pi"))
    return bindir / "pi"


async def _collect_turn(it) -> list[VesselEvent]:
    out = []
    async for ev in it:
        out.append(ev)
        if ev.kind == "done":
            break
    return out


def _kinds(evs: list[VesselEvent]) -> list[str]:
    return [e.kind for e in evs]


def test_text_turn(tmp_path, monkeypatch):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
            cid = v.cli_session_id(sid)
        finally:
            await v.stop(sid)
        return evs, cid

    evs, cid = asyncio.run(run())
    assert json.loads((tmp_path / "ws" / ".pi" / "settings.json").read_text())["compaction"]["enabled"] is False
    assert _kinds(evs) == ["assistant_text", "usage", "done"], _kinds(evs)
    assert evs[-1].payload["reason"] == "result"
    u = next(e for e in evs if e.kind == "usage").payload
    assert u == {"in": 10, "out": 2, "ctx": 12}
    uuid.UUID(cid)  # session id is a uuid


def test_tool_turn(tmp_path, monkeypatch):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="TOOL x")
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    kinds = _kinds(evs)
    assert "tool_call" in kinds and "tool_result" in kinds
    tc = next(e for e in evs if e.kind == "tool_call")
    tr = next(e for e in evs if e.kind == "tool_result")
    assert tc.payload["id"] == "t1" and tr.payload["id"] == "t1"
    assert tr.payload["ok"] is True


def test_multi_step_turn_one_done(tmp_path, monkeypatch):
    """The 3b17920 fix ports to rpc: 4 turn_end steps → 4 usage, ONE done."""
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="MULTI")
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    assert sum(1 for e in evs if e.kind == "done") == 1
    assert sum(1 for e in evs if e.kind == "usage") == 4
    assert evs[-1].payload["reason"] == "result"


def test_two_turns_one_process(tmp_path, monkeypatch):
    """rpc process is long-lived: two prompts on the SAME subscribe stream."""
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="first")
        it = v.subscribe(sid)
        try:
            t1 = await asyncio.wait_for(_collect_turn(it), timeout=15)
            # No respawn: send() writes a new prompt over stdin.
            await v.send(sid, "TOOL second")
            t2 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return t1, t2

    t1, t2 = asyncio.run(run())
    assert _kinds(t1)[-1] == "done"
    assert "tool_call" in _kinds(t2)


def test_inject_writes_steer_command(tmp_path, monkeypatch):
    """inject() must write a `steer` command, not a `prompt` (new turn)."""
    dump = tmp_path / "stdin.log"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_RPC_STDIN_DUMP", str(dump))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        try:
            await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
            await v.inject(sid, "do this instead")
            # let the fake read the steer line before stop() kills the process
            await asyncio.sleep(0.2)
        finally:
            await v.stop(sid)

    asyncio.run(run())
    cmds = [json.loads(l) for l in dump.read_text().splitlines() if l.strip()]
    types = [c.get("type") for c in cmds]
    assert "steer" in types
    steer = next(c for c in cmds if c.get("type") == "steer")
    assert steer["message"] == "do this instead"


def test_glm52_rpc_thinking_flag(tmp_path, monkeypatch):
    dump = tmp_path / "argv.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_RPC_ARGV_DUMP", str(dump))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), model="pi-glm-52",
                            effort="max", initial_prompt="hi")
        try:
            await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        finally:
            await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert d[d.index("--thinking") + 1] == "xhigh"
    assert d[d.index("--model") + 1] == "glmlocal/glm-5.2"


def test_can_inject_flag(tmp_path, monkeypatch):
    """BridgedVessel whitelists PiRpcVessel for mid-turn inject."""
    _shim_env(monkeypatch, tmp_path)
    from pulsar.runner.vessel_bridge import BridgedVessel
    bv = BridgedVessel("pi-glm-52", tmp_path, None, vessel_cls=PiRpcVessel)
    assert bv.can_inject is True
    assert bv.can_interrupt is True


def test_pi_rpc_flag_selects_rpc_vessel():
    """models.json ``pi_rpc`` is the only switch; pi-qwen38-flash is on
    rpc so a busy pulse can steer a handoff between tool calls (Claude
    shape) instead of parking it until the json process-per-message turn
    ends."""
    from pulsar.runner.vessel_bridge import _pi_vessel_cls
    from pulsar.vessel.pi import PiRpcVessel, PiVessel
    from pulsar import models as reg
    for m in reg.MODELS.values():
        if m.vessel != "pi":
            continue
        want = PiRpcVessel if m.pi_rpc else PiVessel
        assert _pi_vessel_cls(m.id) is want, m.id
    assert _pi_vessel_cls("pi-qwen38-flash") is PiRpcVessel
    assert _pi_vessel_cls("pi-glm-flash") is PiRpcVessel
    assert _pi_vessel_cls("pi-qwen27-interface") is PiRpcVessel
    assert _pi_vessel_cls("pi-v4-flash-b") is PiVessel


def test_process_exit_emits_exited_not_result(tmp_path, monkeypatch):
    """MUST-FIX 2: process death ends the stream with done{exited}, never
    done{result}. Kill the process mid-turn; the reader must emit exited."""
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        it = v.subscribe(sid)
        # Let the turn start, then kill the underlying process directly.
        await asyncio.sleep(0.3)
        proc = v._sessions[sid].process
        proc.kill()
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    assert evs[-1].kind == "done"
    assert evs[-1].payload["reason"] == "exited"


def test_watchdog_skips_idle_session(tmp_path, monkeypatch):
    """MUST-FIX 1: an idle (no turn in flight) rpc session must NOT be killed
    by the watchdog. Short timeout + idle wait → process still alive."""
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setattr("pulsar.vessel.pi._STALL_TIMEOUT", 2.0)
    monkeypatch.setattr("pulsar.vessel.pi._STALL_POLL", 0.5)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        try:
            await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
            # Turn done → turn_in_flight False. Wait past the stall timeout.
            await asyncio.sleep(4)
            proc = v._sessions[sid].process
            return proc.returncode
        finally:
            await v.stop(sid)

    rc = asyncio.run(run())
    assert rc is None, "watchdog killed an idle session"


def test_watchdog_kills_wedged_turn(tmp_path, monkeypatch):
    """With a turn in flight and silent past the timeout, the watchdog kills it."""
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setattr("pulsar.vessel.pi._STALL_TIMEOUT", 1.0)
    monkeypatch.setattr("pulsar.vessel.pi._STALL_POLL", 0.2)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=20)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    assert evs[-1].kind == "done"


def test_abort_command_on_interrupt(tmp_path, monkeypatch):
    """interrupt() writes an `abort` command (not a signal)."""
    dump = tmp_path / "stdin.log"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_RPC_STDIN_DUMP", str(dump))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        try:
            await asyncio.sleep(0.3)
            await v.interrupt(sid)
            await asyncio.sleep(0.3)
        finally:
            await v.stop(sid)

    asyncio.run(run())
    cmds = [json.loads(l) for l in dump.read_text().splitlines() if l.strip()]
    types = [c.get("type") for c in cmds]
    assert "abort" in types


def test_interrupt_does_not_kill_process(tmp_path, monkeypatch):
    """interrupt() ends the TURN (abort -> agent_end -> done{result}), NOT the
    process. A long-lived rpc process must survive interrupt so the next turn
    runs on the same warm-cache session.

    The runner's socket `interrupt` op (runner.py:202, the STOP button) calls
    this. The old code waited 5s for process exit (a long-lived rpc process
    does NOT exit on abort), then SIGTERMed -- destroying the session and
    reintroducing the kill-and-resume 4c exists to eliminate, through the stop
    button. Verified against real pi: abort emits agent_end (terminal) but the
    process stays alive waiting for the next prompt.
    """
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        try:
            await asyncio.sleep(0.3)
            await v.interrupt(sid)
            # Give the reader time to process agent_end -> done{result}.
            await asyncio.sleep(0.5)
            proc = v._sessions[sid].process
            alive = proc.returncode is None
        finally:
            await v.stop(sid)
        return alive

    alive = asyncio.run(run())
    assert alive, ("interrupt() killed the long-lived process -- the stop "
                   "button would destroy the session (kill-and-resume via "
                   "the back door)")


def test_inject_raises_on_rejected_steer(tmp_path, monkeypatch):
    """A rejected steer (response success:false) must raise VesselError, not
    silently return. Without the raise, _inject_loop claims the message and
    acks it as delivered, but the model never saw it -- silent message loss.
    """
    from pulsar.vessel.claude import VesselError
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        try:
            await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
            await v.inject(sid, "REJECT this steer")
        finally:
            await v.stop(sid)

    with pytest.raises(VesselError, match="rejected"):
        asyncio.run(run())


def test_broken_abort_process_exit_emits_done_exited(tmp_path, monkeypatch):
    """interrupt() + process death WITHOUT agent_end must emit done{exited}.

    The hang a613c13 opened: interrupt() now sets killed_by="interrupt" and
    writes abort but does NOT kill the process (correct — the process should
    live). If pi then dies WITHOUT emitting agent_end (broken abort, crash,
    OOM, external kill), _read_stdout's finally must emit done{exited} or the
    runner hangs forever inside `async for ev in vessel.send(...)` waiting for
    a terminal done.

    The old guard `killed_by != "interrupt"` suppressed this very emission —
    a matched pair with the done{interrupted} line that was removed, leaving
    the guard orphaned. Same class as PiVessel REVIEW 9 #3 (killed_by residue
    swallows a real crash), reintroduced in a new shape.
    """
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_RPC_ABORT_BROKEN", "1")

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        it = v.subscribe(sid)
        try:
            # Let the turn start (turn_start emitted, no agent_end).
            await asyncio.sleep(0.3)
            await v.interrupt(sid)
            # The fake exits on abort without agent_end; the reader's finally
            # must emit done{exited}. Bounded wait — if the bug is present,
            # _collect_turn hangs until the timeout.
            evs = await asyncio.wait_for(_collect_turn(it), timeout=10)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    assert evs[-1].kind == "done"
    assert evs[-1].payload["reason"] == "exited", evs[-1].payload


def _argv_of(tmp_path, monkeypatch, *, cwd, resume=None):
    """Spawn once and return the argv the vessel actually passed to pi."""
    _shim_env(monkeypatch, tmp_path)
    dump = tmp_path / "argv.json"
    monkeypatch.setenv("FAKE_PI_RPC_ARGV_DUMP", str(dump))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(cwd), resume_cli_session_id=resume)
        await asyncio.sleep(0.4)
        await v.stop(sid)

    asyncio.run(run())
    return json.loads(dump.read_text())


def test_fresh_spawn_omits_session_flag(tmp_path, monkeypatch):
    """`--session <unknown-id>` is FATAL in pi, not a create.

    Real pi prints "No session found matching '<id>'" and exit(1)s
    (dist/main.js:196), verified against the binary 2026-08-23. Passing it on a
    fresh spawn killed every NEW pi-glm pulse, plus every reset and every
    handoff (handoff respawns with resume=None).
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    argv = _argv_of(tmp_path, monkeypatch, cwd=ws)
    assert "--session" not in argv, argv
    assert "--mode" in argv and argv[argv.index("--mode") + 1] == "rpc"
    assert "--session-dir" in argv


def test_resume_spawn_passes_session_flag(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    (ws / "pi-sessions").mkdir(parents=True)
    # the session must EXIST for a resume, mirroring the real binary
    (ws / "pi-sessions" / "2026-09-08T00-00-00-000Z_abc123.jsonl").write_text("")
    argv = _argv_of(tmp_path, monkeypatch, cwd=ws, resume="abc123")
    assert "--session" in argv, argv
    assert argv[argv.index("--session") + 1] == "abc123"


def test_pulsar_md_is_appended_as_system_prompt(tmp_path, monkeypatch):
    """Without PULSAR.md the pulse loses its identity entirely.

    -nc disables AGENTS.md/CLAUDE.md discovery, so PULSAR.md — the pulse's own
    id and the pulse-messaging recipe — only reaches the model through
    --append-system-prompt. PiVessel does this; PiRpcVessel shipped without it.
    """
    ws = tmp_path / "ws"
    ws.mkdir()
    md = ws / "PULSAR.md"
    md.write_text("# pulse identity\n")
    argv = _argv_of(tmp_path, monkeypatch, cwd=ws)
    assert "--append-system-prompt" in argv, argv
    assert argv[argv.index("--append-system-prompt") + 1] == str(md)


def test_no_pulsar_md_no_flag(tmp_path, monkeypatch):
    ws = tmp_path / "ws"
    ws.mkdir()
    argv = _argv_of(tmp_path, monkeypatch, cwd=ws)
    # The todo_write prompt line is always appended (pi_todo_args); only the
    # PULSAR.md identity append must be absent when the file is.
    appends = [argv[i + 1] for i, a in enumerate(argv) if a == "--append-system-prompt"]
    assert not any(a.endswith("PULSAR.md") for a in appends), argv


def test_resolve_resume_session_falls_back_to_newest(tmp_path):
    """A stale/made-up resume id must not kill the spawn (pi exit(1)s on an
    unknown --session): use the id's file if present, else the newest
    session file, else None (fresh)."""
    import time as _t
    from pulsar.vessel.pi import resolve_resume_session
    d = tmp_path / "pi-sessions"
    d.mkdir()
    assert resolve_resume_session(d, None) is None
    assert resolve_resume_session(d, "nope") is None
    old = d / "2026-09-08T00-00-00-000Z_aaaa.jsonl"
    old.write_text("{}")
    _t.sleep(0.01)
    new = d / "2026-09-08T01-00-00-000Z_bbbb.jsonl"
    new.write_text("{}")
    assert resolve_resume_session(d, "aaaa") == "aaaa"
    assert resolve_resume_session(d, "made-up-uuid") == "bbbb"


# --------------------------------------------------------------------------- #
# abort_bash — the surgical steer-delivery fix (2026-09-18: a hung
# ssh tool wedged the turn; three accepted steers sat undelivered because pi
# only delivers a steer before the NEXT LLM call, which never came).
# --------------------------------------------------------------------------- #

def test_abort_bash_frees_wedged_tool_and_steers_flow(tmp_path, monkeypatch):
    """WEDGE prompt: tool starts, then silence. A steer is accepted (chatter
    only). abort_bash kills the tool — error result, the agent loop keeps
    going, one reply, agent_end — on the SAME session."""
    _shim_env(monkeypatch, tmp_path)
    dump = tmp_path / "stdin.log"
    monkeypatch.setenv("FAKE_PI_RPC_STDIN_DUMP", str(dump))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="WEDGE")
        it = v.subscribe(sid)
        try:
            # wait for the tool to wedge (tool_call, then silence)
            first = []
            async for ev in it:
                first.append(ev)
                if ev.kind == "tool_call":
                    break
            await v.inject(sid, "status update?")
            await asyncio.sleep(0.3)
            await v.abort_bash(sid)
            rest = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return first, rest

    first, rest = asyncio.run(run())
    assert _kinds(first) == ["tool_call"]
    kinds = _kinds(rest)
    assert "tool_result" in kinds
    tr = next(e for e in rest if e.kind == "tool_result")
    assert tr.payload["ok"] is False and tr.payload["id"] == "w1"
    assert any(e.kind == "assistant_text" and
               e.payload["text"] == "PIRPC-WEDGED-FREED" for e in rest)
    assert kinds[-1] == "done" and rest[-1].payload["reason"] == "result"
    cmds = [json.loads(l) for l in dump.read_text().splitlines()]
    assert any(c.get("type") == "abort_bash" for c in cmds)
    steer = next(c for c in cmds if c.get("type") == "steer")
    assert steer["message"] == "status update?"


def test_abort_bash_rejected_raises(tmp_path, monkeypatch):
    """success:false (nothing running / unsupported build) raises VesselError
    so the runner escalates to a full turn interrupt."""
    from pulsar.vessel.claude import VesselError
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_RPC_ABORT_BASH_BROKEN", "1")

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="TOOL")
        it = v.subscribe(sid)
        try:
            await asyncio.wait_for(_collect_turn(it), timeout=15)
            await v.abort_bash(sid)
        finally:
            await v.stop(sid)

    with pytest.raises(VesselError, match="abort_bash"):
        asyncio.run(run())


def test_steer_chatter_does_not_pet_the_stall_watchdog(tmp_path, monkeypatch):
    """queue_update/response pairs are protocol chatter, not progress: an
    accepted steer on a silent HANG turn must NOT reset last_progress (each
    steer was buying a wedged tool another full _STALL_TIMEOUT — the exact
    reason the 2026-09-18 wedge survived three steers)."""
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        try:
            await asyncio.sleep(0.3)          # turn_start streams, then silence
            before = v.last_progress_ts(sid)
            await asyncio.sleep(0.3)
            assert v.last_progress_ts(sid) == before, \
                "no chatter expected while silent"
            await v.inject(sid, "steer one")
            await v.inject(sid, "steer two")
            await asyncio.sleep(0.3)          # queue_update + response pairs land
            return before, v.last_progress_ts(sid)
        finally:
            await v.stop(sid)

    before, after = asyncio.run(run())
    assert after == before, \
        "steer chatter (queue_update/response) must not count as progress"


# --------------------------------------------------------------------------- #
# Provider errors + pi auto-retry (2026-10-10: Z.ai 429 usage limit). pi ends
# EVERY failed attempt with agent_end, then retries. The turn must end ONCE,
# after the last attempt, with the provider's text visible as runner_error.
# --------------------------------------------------------------------------- #

def _retry_run(tmp_path, monkeypatch, prompt):
    _shim_env(monkeypatch, tmp_path)
    import pulsar.vessel.pi as pimod
    monkeypatch.setattr(pimod, "_RETRY_GRACE_S", 0.3)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt=prompt)
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
            await asyncio.sleep(0.6)     # past the grace: nothing may trail
            stray = []
            q = v._sessions[sid].event_queue
            while not q.empty():
                stray.append(q.get_nowait())
        finally:
            await v.stop(sid)
        return evs, stray

    return asyncio.run(run())


def test_pi_settings_turn_off_provider_sdk_retries(tmp_path, monkeypatch):
    """The Anthropic SDK sleeps the whole retry-after (Z.ai: 6843 s)."""
    evs, _ = _retry_run(tmp_path, monkeypatch, "hi")
    settings = json.loads((tmp_path / "ws" / ".pi" / "settings.json").read_text())
    assert settings["retry"]["provider"]["maxRetries"] == 0
    assert settings["compaction"]["enabled"] is False


def test_429_with_exhausted_retries_ends_the_turn_once(tmp_path, monkeypatch):
    evs, stray = _retry_run(tmp_path, monkeypatch, "RETRY429")
    kinds = _kinds(evs)
    assert kinds.count("done") == 1 and kinds[-1] == "done", kinds
    errs = [e for e in evs if e.kind == "runner_error"]
    # A provider error is PROVISIONAL — every attempt's error
    # message_end is held, so only the LAST attempt's error reaches the
    # runner (an exhausted-retry turn must still fail loudly, but the
    # earlier attempts' duplicates are gone).
    assert len(errs) == 1
    assert errs[-1].payload["where"] == "provider_error"
    assert "Usage limit reached for 5 hour" in errs[-1].payload["reason"]
    # delivered just before the turn's single done, so the runner records
    # the provider error for THIS turn and failover can act on it
    assert kinds[-2] == "runner_error", kinds
    assert stray == [], _kinds(stray)


def test_429_then_successful_retry_is_one_turn(tmp_path, monkeypatch):
    evs, stray = _retry_run(tmp_path, monkeypatch, "RETRYOK")
    kinds = _kinds(evs)
    assert kinds.count("done") == 1, kinds
    assert "assistant_text" in kinds, "the retried reply belongs to the turn"
    assert stray == [], _kinds(stray)


def test_non_retryable_error_ends_the_turn_after_the_grace(tmp_path, monkeypatch):
    evs, stray = _retry_run(tmp_path, monkeypatch, "ERR400")
    # The error is held provisionally and delivered just before
    # the done (after the grace), not inline at its message_end — the kinds
    # order reflects the hold.
    assert _kinds(evs) == ["usage", "runner_error", "done"], _kinds(evs)
    assert "400 prompt too long" in evs[1].payload["reason"]
    assert stray == []


def test_interrupt_keeps_the_process_owned(tmp_path, monkeypatch):
    """RPC interrupt is abort-only: no signal, process alive, pid still in
    _OWNED_PIDS (the atexit reaper still covers it)."""
    from pulsar.vessel import pi as pi_mod
    _shim_env(monkeypatch, tmp_path)
    sent = []
    monkeypatch.setattr(pi_mod, "safe_killpg",
                        lambda pid, sig: sent.append((pid, sig)))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        proc = v._sessions[sid].process
        pid = proc.pid
        try:
            await asyncio.sleep(0.3)
            await v.interrupt(sid)
            await asyncio.sleep(0.3)
            return proc.returncode is None, pid in pi_mod._OWNED_PIDS, list(sent)
        finally:
            proc.kill()  # the stubbed killpg signals nothing
            await v.stop(sid)
    alive, owned, signals = asyncio.run(run())
    assert alive and owned and signals == []


def test_owned_pid_discarded_after_process_exit_and_stop(tmp_path, monkeypatch):
    from pulsar.vessel import pi as pi_mod
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG")
        proc = v._sessions[sid].process
        pid = proc.pid
        await asyncio.sleep(0.3)
        assert pid in pi_mod._OWNED_PIDS
        await v.stop(sid)
        await asyncio.wait_for(proc.wait(), timeout=10)
        await asyncio.sleep(0.2)
        return pid in pi_mod._OWNED_PIDS
    assert asyncio.run(run()) is False


def test_rpc_provider_error_keeps_streamed_assistant_text(
        tmp_path, monkeypatch):
    # ERRPARTIAL (final review 0d64351c finding 1), RPC transport: text
    # streamed before the provider error must reach the durable log — only
    # the runner_error is held and flushed just before the done.
    evs, stray = _retry_run(tmp_path, monkeypatch, "ERRPARTIAL")
    assert _kinds(evs) == ["assistant_delta", "assistant_text", "usage",
                           "runner_error", "done"], _kinds(evs)
    assert evs[1].payload["text"] == \
        "PARTIAL ANSWER BEFORE PROVIDER ERROR"
    errs = [e for e in evs if e.kind == "runner_error"]
    assert len(errs) == 1
    assert errs[0].payload["where"] == "provider_error"
    assert "content_filter" in errs[0].payload["error"]
    assert stray == [], _kinds(stray)
