"""Vessel tests for PiVessel against the fake pi CLI shim (no real pair).

A `pi` shim on a temp PATH exec'ing fake_pi.py resolves the vessel's binary
lookup; PULSAR_PI_BIN override is exercised too. The fake speaks the live
0.73.1 wire schema (toolCallId, usage at turn_end.message.usage). Async, driven
with asyncio.run + wait_for (no pytest-asyncio).
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

from pulsar.vessel.pi import PiVessel
from pulsar.vessel.claude import VesselError
from pulsar.vessel.events import VesselEvent

_HERE = Path(__file__).resolve().parent
_FAKE = _HERE / "fake_pi.py"


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


async def _collect_turn(it) -> list[VesselEvent]:
    out = []
    async for ev in it:
        out.append(ev)
        if ev.kind == "done":
            break
    return out


def _kinds(evs: list[VesselEvent]) -> list[str]:
    return [e.kind for e in evs]


def _shim_env(monkeypatch, tmp_path) -> Path:
    bindir = tmp_path / "bin"
    shim = _write_shim(bindir)
    monkeypatch.setenv("PULSAR_PI_BIN", str(shim))
    return shim


def test_text_turn(tmp_path, monkeypatch):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
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
    assert sum(1 for e in evs if e.kind == "done") == 1
    assert evs[-1].payload["reason"] == "result"
    u = next(e for e in evs if e.kind == "usage").payload
    assert u == {"in": 10, "out": 2, "ctx": 12}
    parsed = uuid.UUID(cid)
    assert parsed.version == 4


def test_disable_auto_compaction_preserves_other_settings(tmp_path):
    from pulsar.vessel.pi import disable_pi_auto_compaction

    path = tmp_path / ".pi" / "settings.json"
    path.parent.mkdir()
    path.write_text(json.dumps({"theme": "dark", "compaction": {
        "enabled": True, "reserveTokens": 12000, "keepRecentTokens": 8000}}))
    disable_pi_auto_compaction(tmp_path)
    disable_pi_auto_compaction(tmp_path)
    assert json.loads(path.read_text()) == {"theme": "dark", "compaction": {
        "enabled": False, "reserveTokens": 12000, "keepRecentTokens": 8000},
        "retry": {"provider": {"maxRetries": 0}}}


def test_tool_and_error_turn_pair_ids(tmp_path, monkeypatch):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
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
    assert tc.payload["name"] == "bash"
    assert tc.payload["id"] == "t1" and tr.payload["id"] == "t1"
    assert tr.payload["ok"] is True


def test_two_turns_one_subscribe_does_not_end(tmp_path, monkeypatch):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="first")
        it = v.subscribe(sid)
        try:
            t1 = await asyncio.wait_for(_collect_turn(it), timeout=15)
            await v.send(sid, "TOOL second")
            t2 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return t1, t2

    t1, t2 = asyncio.run(run())
    # subscribe must survive turn 1 (done{result}) and serve turn 2 on the SAME stream.
    assert _kinds(t1)[-1] == "done"
    assert "tool_call" in _kinds(t2)


def test_resume_passes_session_flag(tmp_path, monkeypatch):
    dump = tmp_path / "argv1.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v1 = PiVessel()
        sid = await v1.spawn(cwd=str(tmp_path / "ws1"), initial_prompt="hi")
        await asyncio.wait_for(_collect_turn(v1.subscribe(sid)), timeout=15)
        cid = v1.cli_session_id(sid)
        await v1.stop(sid)

        dump2 = tmp_path / "argv2.json"
        monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump2))
        # the session file must EXIST under the resume cwd, mirroring the
        # real binary (an unknown --session id now falls back to fresh)
        (tmp_path / "ws2" / "pi-sessions").mkdir(parents=True)
        (tmp_path / "ws2" / "pi-sessions" / f"2026-09-08T00-00-00-000Z_{cid}.jsonl").write_text("")
        v2 = PiVessel()
        sid2 = await v2.spawn(cwd=str(tmp_path / "ws2"),
                              resume_cli_session_id=cid, initial_prompt="again")
        await asyncio.wait_for(_collect_turn(v2.subscribe(sid2)), timeout=15)
        cid2 = v2.cli_session_id(sid2)
        await v2.stop(sid2)
        return cid, cid2, dump2

    cid, cid2, dump2 = asyncio.run(run())
    d = json.loads(dump2.read_text())
    assert "--session" in d and str(cid) in d
    assert cid2 == cid  # resume preserved the session id through the fake


def test_missing_binary_raises(monkeypatch, tmp_path):
    monkeypatch.delenv("PULSAR_PI_BIN", raising=False)
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    with pytest.raises(VesselError):
        PiVessel()


def test_model_flag_maps_picker_id_to_provider(monkeypatch, tmp_path):
    # REVIEW 9 #1: BridgedVessel passes the PICKER id (e.g. "pi-v4-flash") as
    # model=; PiVessel must translate it to pi\u2019s real provider model
    # (dspair/deepseek-v4-flash) in --model, never pass the picker id.
    dump = tmp_path / "argv.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v = PiVessel(model="pi-v4-flash")
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert "--model" in d
    assert d[d.index("--model") + 1] == "dspair/deepseek-v4-flash"


@pytest.mark.parametrize(("picker", "actual"), [
    ("pi-qwen27-interface", "localqwen/qwen3.8-27b"),
    ("pi-qwen35-interface", "localqwen/qwen3.6-35b-a3b-fast"),
])
def test_model_flag_maps_omp_interface_models(monkeypatch, tmp_path,
                                               picker, actual):
    dump = tmp_path / "argv.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v = PiVessel(model=picker)
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert d[d.index("--model") + 1] == actual


@pytest.mark.parametrize(("effort", "thinking"), [
    (None, "off"),
    ("off", "off"),
    ("high", "high"),
    ("max", "xhigh"),
    ("low", "off"),  # not a GLM-5.2 level — clamp to off, don't invent a flag
])
def test_glm52_thinking_flag(monkeypatch, tmp_path, effort, thinking):
    dump = tmp_path / "argv.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v = PiVessel(model="pi-glm-52")
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi",
                            effort=effort)
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert d[d.index("--thinking") + 1] == thinking
    assert d[d.index("--model") + 1] == "glmlocal/glm-5.2"


def test_v4_pi_stays_thinking_off_even_with_effort(monkeypatch, tmp_path):
    dump = tmp_path / "argv.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v = PiVessel(model="pi-v4-flash")
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi",
                            effort="max")
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert d[d.index("--thinking") + 1] == "off"


def test_model_flag_passthrough_unknown_id(monkeypatch, tmp_path):
    # REVIEW 9 #1 companion: an unknown/raw provider model that ISN’t a picker
    # id must pass through unmodified in --model (the map only remaps the
    # pi-v4-flash/pi-v4-flash-b picker ids; anything else is trusted verbatim).
    dump = tmp_path / "argv.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v = PiVessel(model="mock/mymodel")
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert "--model" in d
    assert d[d.index("--model") + 1] == "mock/mymodel"


def test_pulsar_context_append_when_pulsar_md_in_cwd(tmp_path, monkeypatch):
    # REVIEW 13b: a pulse context file in cwd must reach pi so a pulse is never
    # mute. pi's --append-system-prompt accepts a file path (resolvePromptInput
    # reads it), so PiVessel passes the PULSAR.md PATH rather than inlining.
    dump = tmp_path / "argv.json"
    ws = tmp_path / "ws"
    ws.mkdir(parents=True, exist_ok=True)
    pulsar_md = ws / "PULSAR.md"
    pulsar_md.write_text("<!-- pulsar: pulse id 123 + messaging recipe -->")
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ARGV_DUMP", str(dump))

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(ws), initial_prompt="hi")
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    d = json.loads(dump.read_text())
    assert "--append-system-prompt" in d
    idx = d.index("--append-system-prompt")
    assert d[idx + 1] == str(pulsar_md)  # the path pi will read for the body
    assert Path(d[idx + 1]).read_text() == pulsar_md.read_text()


def test_real_pi_cli_env_contract_pi_coding_agent_dir(tmp_path, monkeypatch):
    # Spot-check FAIL regression against the REAL pi 0.73.1 CLI contract
    # (probe 2026-08-19): pi resolves its agent dir from PI_CODING_AGENT_DIR
    # and IGNORES PI_HOME. Encode that here so the bridge env can't regress
    # to the wrong var. Skipped where no pi binary is installed.
    import shutil
    import subprocess
    from pulsar.launch.units import DEFAULT_PI_BIN
    bin_pi = (os.environ.get("PULSAR_PI_BIN")
              or shutil.which("pi")
              or DEFAULT_PI_BIN)
    if not bin_pi or not os.path.exists(bin_pi):
        pytest.skip("no pi binary — real-CLI env contract not exercised")
    agent = tmp_path / "private-agent"
    agent.mkdir(parents=True, exist_ok=True)
    unique = "probe-only-model-" + uuid.uuid4().hex[:8]
    (agent / "models.json").write_text(json.dumps({
        "providers": {"PROBE": {
            "baseUrl": "http://private.invalid/v1", "api": "openai-completions",
            "apiKey": "none",
            "models": [{"id": unique, "name": "PROBE"}]}}}))
    env = dict(os.environ)

    def models_with(extra):
        e = {**env, **extra}
        r = subprocess.run([bin_pi, "--list-models"], env=e,
                           capture_output=True, text=True)
        # pi prints the model table to STDERR (verified 2026-08-19), not stdout.
        return r.stdout + r.stderr

    # PI_CODING_AGENT_DIR IS honored -> unique model resolves.
    assert unique in models_with({"PI_CODING_AGENT_DIR": str(agent)})
    # PI_HOME is ignored -> unique model (only in the private dir) is absent,
    # proving pi read the shared ~/.pi, not our private dir.
    assert unique not in models_with({"PI_HOME": str(agent)})


def test_interrupt_emits_interrupted_keeps_stream(tmp_path, monkeypatch):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        it = v.subscribe(sid)
        t1 = await asyncio.wait_for(_collect_turn(it), timeout=15)  # drain t1 result
        await v.send(sid, "SLOW blocked turn")
        await asyncio.sleep(0.2)
        await v.interrupt(sid)
        out = await asyncio.wait_for(_collect_turn(it), timeout=15)  # same stream
        await v.send(sid, "TOOL third")                              # stream survived
        t3 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.stop(sid)
        return t1, out, t3

    t1, out, t3 = asyncio.run(run())
    assert t1[-1].payload["reason"] == "result"
    assert out[-1].kind == "done" and out[-1].payload["reason"] == "interrupted"
    assert "tool_call" in [e.kind for e in t3]  # subscription alive after interrupt


def test_interrupt_then_crash_emits_exited(tmp_path, monkeypatch):
    # REVIEW 9 #3a: killed_by must be cleared on the NEXT send(). Without that,
    # a stale "interrupt" from turn 2 would suppress the done{exited} for a
    # genuine abnormal exit of turn 3 (a real crash silently swallowed).
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        it = v.subscribe(sid)
        await asyncio.wait_for(_collect_turn(it), timeout=15)      # drain t1
        await v.send(sid, "SLOW blocked")
        await asyncio.sleep(0.2)
        await v.interrupt(sid)                                     # kill t2
        out = await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.send(sid, "CRASH boom")                            # t3 aborts
        t3 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.stop(sid)
        return out, t3

    out, t3 = asyncio.run(run())
    assert out[-1].kind == "done" and out[-1].payload["reason"] == "interrupted"
    assert t3[-1].kind == "done" and t3[-1].payload["reason"] == "exited"


def test_interrupt_serializes_next_send_and_stream_survives(tmp_path, monkeypatch):
    # REVIEW 9 #3b: interrupt() must WAIT for the killed process\u2019s reader to
    # finish before releasing, and the next send() must not create a second
    # reader racing the same event queue — the subscription stays a single
    # clean stream across kill-and-resume.
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        it = v.subscribe(sid)
        await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.send(sid, "SLOW blocked")
        await asyncio.sleep(0.2)
        await v.interrupt(sid)
        out = await asyncio.wait_for(_collect_turn(it), timeout=15)  # interrupted
        # Follow-ups on the SAME stream, no reader duplication.
        await v.send(sid, "TOOL after")
        t3 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.send(sid, "TOOL after2")
        t4 = await asyncio.wait_for(_collect_turn(it), timeout=15)
        await v.stop(sid)
        return out, t3, t4

    out, t3, t4 = asyncio.run(run())
    assert out[-1].payload["reason"] == "interrupted"
    assert "tool_call" in [e.kind for e in t3]
    assert "tool_call" in [e.kind for e in t4]
    # exactly one done per collected turn (no duplicate boundaries from a race)
    assert [e.kind for e in out].count("done") == 1
    assert [e.kind for e in t4].count("done") == 1


def test_env_merge_preserves_path(tmp_path, monkeypatch):
    edump = tmp_path / "env.json"
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_ENV_DUMP", str(edump))

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi",
                            env={"PI_HOME": "/private/pi"})
        await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        await v.stop(sid)

    asyncio.run(run())
    env = json.loads(edump.read_text())
    assert env.get("PI_HOME") == "/private/pi"
    assert "PATH" in env  # inherited, not clobbered by the merge fix


def test_multi_step_turn_emits_exactly_one_done(tmp_path, monkeypatch):
    """A pi ``turn_end`` is an agent STEP, not the user's turn.

    Regression (2026-08-23): the vessel mapped every turn_end to
    done{result}, so the runner acked the message and flipped the pulse idle on
    step 1 while pi kept working — half-thought replies in the UI, invisible
    tool calls, and a fresh orphan pi for every follow-up message.
    """
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="MULTI go")
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    kinds = _kinds(evs)
    # exactly one boundary, and it is LAST — every step landed before it
    assert kinds.count("done") == 1, kinds
    assert kinds[-1] == "done" and evs[-1].payload["reason"] == "result"
    # all four steps' work arrived inside the one turn
    assert kinds.count("tool_call") == 4, kinds
    assert kinds.count("tool_result") == 4, kinds
    assert kinds.count("assistant_text") == 4, kinds
    # every step's usage is still reported (the ctx meter reads the last one)
    usages = [e for e in evs if e.kind == "usage"]
    assert len(usages) == 4, kinds
    assert usages[-1].payload["in"] == 400


def test_clean_exit_without_agent_end_still_ends_the_turn(tmp_path, monkeypatch):
    """One short-lived process per message: the process exiting IS the boundary.

    Without this backstop a pi that never emits agent_end would hang send()
    forever.
    """
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"),
                            initial_prompt="NOAGENTEND go")
        it = v.subscribe(sid)
        try:
            evs = await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)
        return evs

    evs = asyncio.run(run())
    kinds = _kinds(evs)
    assert kinds.count("done") == 1, kinds
    assert evs[-1].payload["reason"] == "result"
    assert "assistant_text" in kinds


def test_watchdog_kills_a_silent_turn_and_keeps_the_session(tmp_path, monkeypatch):
    """A wedged tool call must end the TURN, not the pulse.

    pi's bash tool has no default timeout, so an unbounded command (`find /`,
    2026-08-23) leaves the process alive and mute forever. The
    watchdog kills the process group; the turn closes with done{result} so the
    session stays resumable and the next message just works.
    """
    _shim_env(monkeypatch, tmp_path)
    monkeypatch.setattr("pulsar.vessel.pi._STALL_TIMEOUT", 1.0)
    monkeypatch.setattr("pulsar.vessel.pi._STALL_POLL", 0.2)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="HANG now")
        it = v.subscribe(sid)
        try:
            t1 = await asyncio.wait_for(_collect_turn(it), timeout=20)
            proc = v._sessions[sid].process
            # the wedged process really is gone, not just abandoned
            assert proc.returncode is not None
            await v.send(sid, "TOOL after")
            t2 = await asyncio.wait_for(_collect_turn(it), timeout=20)
        finally:
            await v.stop(sid)
        return t1, t2

    t1, t2 = asyncio.run(run())
    assert t1[-1].kind == "done" and t1[-1].payload["reason"] == "result"
    assert [e.kind for e in t1].count("done") == 1
    # the stream survived: the next turn runs normally on the same subscribe
    assert "tool_call" in _kinds(t2)
    assert t2[-1].kind == "done" and t2[-1].payload["reason"] == "result"


def test_classify_skips_whitespace_only_assistant_text():
    """Qwen-in-pi emits "\\n\\n\\n" text blocks next to every tool call
    (2026-08-29): recording them made the UI render one empty REPLY bubble
    per tool call. A whitespace-only assistant message is never surfaced."""
    from pulsar.vessel.pi import classify_record

    rec = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "\n\n\n"},
                {"type": "toolCall", "id": "c1", "name": "bash"},
            ],
        },
    }
    assert classify_record(rec) == []

    # Real text still passes through, joined from multiple blocks.
    rec_text = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "content": [
                {"type": "text", "text": "hello\n"},
                {"type": "text", "text": "world"},
            ],
        },
    }
    events = classify_record(rec_text)
    assert len(events) == 1
    assert events[0].kind == "assistant_text"
    assert events[0].payload["text"] == "hello\n\nworld"


def test_qwen38_thinking_level_map():
    """R2.3: pi-qwen38-flash gets the Qwen3.8 effort dial. Template
    vocabulary is exactly low/medium/xhigh (chat_template.jinja verified);
    anything else -> off. GLM keeps its own off/high/max map."""
    from pulsar.vessel.pi import pi_thinking_level

    assert pi_thinking_level("pi-qwen38-flash", "low") == "low"
    assert pi_thinking_level("pi-qwen38-flash", "medium") == "medium"
    assert pi_thinking_level("pi-qwen38-flash", "xhigh") == "xhigh"
    assert pi_thinking_level("pi-qwen38-flash", None) == "low"  # registry default (2026-09-24)
    assert pi_thinking_level("pi-qwen38-flash", "off") == "off"
    # 'high' is NOT valid for this template — must not leak through
    assert pi_thinking_level("pi-qwen38-flash", "high") == "off"
    assert pi_thinking_level("glmlocal/Qwen3.8-Flash-Next-NVFP4", "low") == "low"
    # GLM map unchanged
    assert pi_thinking_level("pi-glm-flash", "max") == "xhigh"
    assert pi_thinking_level("pi-glm-flash", "high") == "high"
    # abliterated GLM on the TP2 pair shares the GLM dial
    assert pi_thinking_level("pi-glm-uncensored", "max") == "xhigh"
    assert pi_thinking_level("pi-glm-uncensored", None) == "off"
    assert pi_thinking_level("glm53pair/glm-5.3-flash-nvfp4", "high") == "high"
    # other pi models stay forced off
    assert pi_thinking_level("pi-v4-flash", "xhigh") == "off"


# ---- pi_extra_args (per-pulse pi-args.json) --------------------------------

def test_pi_extra_args_missing_file(tmp_path):
    from pulsar.vessel.pi import pi_extra_args
    cli = tmp_path / "cli"
    cli.mkdir()
    assert pi_extra_args(cli) == []


def test_pi_extra_args_list_of_strings(tmp_path):
    from pulsar.vessel.pi import pi_extra_args
    cli = tmp_path / "cli"
    cli.mkdir()
    (tmp_path / "pi-args.json").write_text(
        json.dumps(["-e", "/x/mcp-bridge.ts", "--tools", "read,mcp__a__b"]))
    assert pi_extra_args(cli) == ["-e", "/x/mcp-bridge.ts",
                                  "--tools", "read,mcp__a__b"]


def test_pi_extra_args_rejects_non_list_and_garbage(tmp_path):
    from pulsar.vessel.pi import pi_extra_args
    cli = tmp_path / "cli"
    cli.mkdir()
    (tmp_path / "pi-args.json").write_text('{"tools": "read"}')
    assert pi_extra_args(cli) == []
    (tmp_path / "pi-args.json").write_text('["ok", 42]')
    assert pi_extra_args(cli) == []
    (tmp_path / "pi-args.json").write_text('not json')
    assert pi_extra_args(cli) == []


# ---- default --tools -------------------------------------------------------

def test_pi_tools_default_lists_native_tools_and_todo(monkeypatch):
    from pulsar.vessel import pi as pi_mod
    monkeypatch.setattr(pi_mod.shutil, "which",
                        lambda name: "/usr/bin/fdfind" if name == "fdfind" else None)
    assert pi_mod.pi_tools_args() == [
        "--tools", "read,bash,edit,write,grep,find,ls,todo_write"]


def test_pi_tools_default_drops_find_without_fd(monkeypatch):
    # No fd on PATH: pi would download one from GitHub at first find call.
    from pulsar.vessel import pi as pi_mod
    monkeypatch.setattr(pi_mod.shutil, "which", lambda name: None)
    assert pi_mod.pi_tools_args() == [
        "--tools", "read,bash,edit,write,grep,ls,todo_write"]


def test_pulse_tools_allowlist_comes_after_the_default(tmp_path, monkeypatch):
    # pi keeps the LAST --tools, so pi-args.json must follow the default.
    from pulsar.vessel.pi import PiVessel, _Session, pi_tools_args
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    cli = tmp_path / "cli"
    cli.mkdir()
    (tmp_path / "pi-args.json").write_text(
        json.dumps(["-e", "/x/mcp-bridge.ts", "--tools", "read,mcp__x__y"]))
    sess = _Session(sid="s", cwd=str(cli), session_dir=tmp_path / "sd",
                    cli_session_id="c", model="pi-v4-flash", env={})
    cmd = PiVessel()._build_command(sess, "hi")
    idx = [i for i, a in enumerate(cmd) if a == "--tools"]
    assert [cmd[i + 1] for i in idx] == [pi_tools_args()[1], "read,mcp__x__y"]


# ---- Pulsar base prompt ---------------------------------------------------

def test_pi_base_prompt_replaces_pi_text_and_follows_tools():
    from pulsar.vessel.pi import pi_base_prompt
    text = pi_base_prompt(["read", "bash", "edit", "write", "grep", "ls",
                           "todo_write"])
    assert text.startswith("You are a Pulsar pulse")
    assert "expert coding assistant" not in text and "Pi documentation" not in text
    assert "- grep: " in text and "- ls: " in text and "- find: " not in text
    assert "Prefer grep/find/ls tools over bash" in text
    assert "Use bash for file operations" not in text
    assert "2,000 lines or 50 KB" in text
    assert "edits[].oldText must match exactly" in text
    assert "other tools" not in text
    bare = pi_base_prompt(["read", "bash", "mcp__x__y"])
    assert "- grep: " not in bare and "Prefer grep/find/ls" not in bare
    assert "edits[]" not in bare and "other tools" in bare


def test_pi_base_args_follow_a_pulse_tools_override():
    from pulsar.vessel.pi import pi_base_args
    args = pi_base_args(["-e", "/x/bridge.ts", "-t", "read,bash"])
    assert args[0] == "--tools" and args[2] == "--system-prompt"
    assert "- read: " in args[3] and "- grep: " not in args[3]


def test_length_stop_emits_loud_output_cap_error():
    """A turn that burns max_tokens inside its thinking block must not end
    as a silent 'turn ended (no reply)'."""
    from pulsar.vessel.pi import classify_record

    rec = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "length",
            "usage": {"input": 22983, "output": 8192},
            "content": [{"type": "thinking", "thinking": "x" * 23777}],
        },
    }
    events = classify_record(rec)
    assert [e.kind for e in events] == ["runner_error"]
    p = events[0].payload
    assert p["where"] == "output_cap"
    assert "stop_reason=length" in p["reason"]
    assert "8192 output tokens" in p["reason"]
    assert "mid-reasoning" in p["reason"] and "no reply" in p["reason"]

    # A truncated visible reply keeps the text AND flags the cap.
    rec_partial = dict(rec, message=dict(rec["message"], content=[
        {"type": "text", "text": "Summary so far: the build is"}]))
    events = classify_record(rec_partial)
    assert [e.kind for e in events] == ["assistant_text", "runner_error"]
    assert "mid-reply" in events[1].payload["reason"]

    # A normal end_turn message is unchanged.
    rec_ok = dict(rec, message=dict(rec["message"], stopReason="stop",
                                    content=[{"type": "text", "text": "done"}]))
    assert [e.kind for e in classify_record(rec_ok)] == ["assistant_text"]


def test_error_stop_emits_loud_provider_error():
    """When a provider returns HTTP 400 (e.g. prompt + max_tokens exceeds
    context window) and pi ends the turn with an empty stopReason=error
    message, the provider's error message must reach the pane."""
    from pulsar.vessel.pi import classify_record

    rec = {
        "type": "message_end",
        "message": {
            "role": "assistant",
            "stopReason": "error",
            "provider": "glm53local",
            "model": "glm-5.3",
            "usage": {"input": 0, "output": 0},
            "content": [],
            "errorMessage": ("400 This model's maximum context length is 200000 "
                             "tokens. However, you requested 131072 output tokens"),
        },
    }
    events = classify_record(rec)
    assert [e.kind for e in events] == ["runner_error"]
    p = events[0].payload
    assert p["where"] == "provider_error"
    assert "stop_reason=error" in p["reason"]
    assert "glm53local/glm-5.3" in p["reason"]
    assert "maximum context length is 200000" in p["reason"]


def test_ds41_thinking_levels_and_handoff():
    from pulsar.vessel.pi import pi_thinking_level
    from pulsar.models import model_by_id

    model = model_by_id("pi-ds41-flash")
    assert model.default_handoff == 250000
    # 2026-11-02: numeric dial; old names still map (not yet migrated).
    assert list(model.efforts) == ["80", "70", "50", "30", "none"]
    assert model.default_effort == "none"
    for name in ("pi-ds41-flash", "ds41local/deepseek-v4.1-flash",
                 "deepseek-v4.1-flash"):
        for effort, slot in [(None, "off"), ("none", "off"), ("30", "minimal"),
                             ("50", "low"), ("70", "medium"), ("80", "high"),
                             ("off", "off"), ("low", "low"), ("high", "high"),
                             ("xhigh", "high"), ("max", "high"),
                             ("invalid", "off")]:
            assert pi_thinking_level(name, effort) == slot


@pytest.mark.parametrize("model", ["pi-qwen27-interface", "localqwen/qwen3.8-27b", "qwen3.8-27b"])
@pytest.mark.parametrize("effort,expected", [(None,"off"),("off","off"),("low","low"),("medium","medium"),("xhigh","xhigh"),("high","off")])
def test_qwen27_ninfer_thinking_levels(model, effort, expected):
    from pulsar.vessel.pi import pi_thinking_level
    assert pi_thinking_level(model, effort) == expected


# ---- _OWNED_PIDS bookkeeping -----------------------------------------------

def _wait_owned():
    from pulsar.vessel import pi as pi_mod
    return pi_mod._wait_owned


class _FakeProc:
    def __init__(self, pid, wait):
        self.pid = pid
        self._wait = wait
        self.returncode = None

    async def wait(self):
        return await self._wait()


def test_owned_pid_discarded_after_normal_exit():
    from pulsar.vessel import pi as pi_mod

    async def run():
        async def done():
            return 0
        pi_mod._OWNED_PIDS.add(777001)
        await _wait_owned()(_FakeProc(777001, done))
        return 777001 in pi_mod._OWNED_PIDS
    assert asyncio.run(run()) is False


def test_owned_pid_discarded_when_wait_raises():
    from pulsar.vessel import pi as pi_mod

    async def run():
        async def boom():
            raise RuntimeError("wait failed")
        pi_mod._OWNED_PIDS.add(777002)
        with pytest.raises(RuntimeError):
            await _wait_owned()(_FakeProc(777002, boom))
        return 777002 in pi_mod._OWNED_PIDS
    assert asyncio.run(run()) is False


def test_owned_pid_discarded_when_waiter_task_cancelled():
    from pulsar.vessel import pi as pi_mod

    async def run():
        async def forever():
            await asyncio.sleep(60)
        pi_mod._OWNED_PIDS.add(777003)
        t = asyncio.create_task(_wait_owned()(_FakeProc(777003, forever)))
        await asyncio.sleep(0.05)
        t.cancel()
        with pytest.raises(asyncio.CancelledError):
            await t
        return 777003 in pi_mod._OWNED_PIDS
    assert asyncio.run(run()) is False


def test_owned_pid_discarded_after_real_turn_exit(tmp_path, monkeypatch):
    from pulsar.vessel import pi as pi_mod
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="hi")
        pid = v._sessions[sid].process.pid
        it = v.subscribe(sid)
        try:
            await asyncio.wait_for(_collect_turn(it), timeout=15)
            await asyncio.wait_for(v._sessions[sid].process.wait(), timeout=15)
            await asyncio.sleep(0.2)
            return pid in pi_mod._OWNED_PIDS
        finally:
            await v.stop(sid)
    assert asyncio.run(run()) is False


def test_stop_signals_through_killpg_and_discards_the_pid(tmp_path, monkeypatch):
    from pulsar.vessel import pi as pi_mod
    _shim_env(monkeypatch, tmp_path)
    sent = []
    real = pi_mod.safe_killpg
    # Stub the signal only for the vessel's own pid-bookkeeping wrapper.
    monkeypatch.setattr(pi_mod, "safe_killpg",
                        lambda pid, sig: sent.append((pid, sig)))

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="SLOW hold")
        proc = v._sessions[sid].process
        pid = proc.pid
        await asyncio.sleep(0.2)
        assert pid in pi_mod._OWNED_PIDS
        try:
            await v.stop(sid)
            owned = pid in pi_mod._OWNED_PIDS
        finally:
            real(pid, 9)  # the stub signalled nothing; reap the real child
            await proc.wait()
        return pid, owned
    pid, owned = asyncio.run(run())
    import signal as _sig
    assert sent == [(pid, _sig.SIGTERM)]
    assert owned is False


# ---- harness overrides (an experiment, per-pulse knobs) --------------------

def _harness_home(tmp_path, overrides=None, **home_files):
    """A pulse home with a cli cwd; harness-overrides.json goes in the home
    (parent of cwd, beside diet.json). Extra home files via kwargs."""
    home = tmp_path / "pulse"
    cli = home / "cli"
    cli.mkdir(parents=True, exist_ok=True)
    if overrides is not None:
        (home / "harness-overrides.json").write_text(json.dumps(overrides))
    for name, text in home_files.items():
        (home / name).write_text(text)
    return cli


def _harness_command(tmp_path, monkeypatch, overrides=None, **home_files):
    from pulsar.vessel.pi import PiVessel, _Session
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    cli = _harness_home(tmp_path, overrides, **home_files)
    sess = _Session(sid="s", cwd=str(cli), session_dir=tmp_path / "sd",
                    cli_session_id="c", model="pi-v4-flash", env={})
    return cli, PiVessel()._build_command(sess, "hi")


def _harness_prompt(cmd):
    return cmd[cmd.index("--system-prompt") + 1]


def test_harness_base_prompt_replaces_default_and_wins_over_guidelines(
        tmp_path, monkeypatch):
    _, cmd = _harness_command(
        tmp_path, monkeypatch,
        {"base_prompt": "CUSTOM BASE", "base_guidelines": ["SHOULD NOT SHOW"]})
    assert [_harness_prompt(cmd)] == ["CUSTOM BASE"]
    assert not any("You are a Pulsar pulse" in a for a in cmd)
    assert not any("SHOULD NOT SHOW" in a for a in cmd)


def test_harness_base_guidelines_replace_computed_list(tmp_path, monkeypatch):
    _, cmd = _harness_command(tmp_path, monkeypatch,
                              {"base_guidelines": ["ONE RULE"]})
    prompt = _harness_prompt(cmd)
    assert prompt.startswith("You are a Pulsar pulse")
    assert "Guidelines:\n- ONE RULE" in prompt
    assert "Prefer grep/find/ls tools over bash" not in prompt
    assert "2,000 lines or 50 KB" not in prompt  # the computed list is gone


def test_harness_base_guidelines_empty_removes_the_section(
        tmp_path, monkeypatch):
    _, cmd = _harness_command(tmp_path, monkeypatch, {"base_guidelines": []})
    prompt = _harness_prompt(cmd)
    assert prompt.startswith("You are a Pulsar pulse")
    assert "Guidelines:" not in prompt


def test_harness_todo_prompt_replaces_default_and_empty_keeps_the_tool(
        tmp_path, monkeypatch):
    from pulsar.vessel.pi import PI_TODO_EXTENSION, PI_TODO_PROMPT
    _, cmd = _harness_command(tmp_path, monkeypatch,
                              {"todo_prompt": "USE THE LIST TOOL"})
    assert str(PI_TODO_EXTENSION) in cmd  # the extension still loads
    assert "USE THE LIST TOOL" in cmd
    assert PI_TODO_PROMPT not in cmd
    # "" = no todo append line ONLY — the extension/tool stay (instructions
    # and tool availability are separate experiment variables).
    _, cmd = _harness_command(tmp_path, monkeypatch, {"todo_prompt": ""})
    assert str(PI_TODO_EXTENSION) in cmd
    assert PI_TODO_PROMPT not in cmd
    assert not any(a == "--append-system-prompt"
                   and cmd[i + 1] == PI_TODO_PROMPT
                   for i, a in enumerate(cmd))


def test_harness_base_prompt_empty_is_an_empty_replacement(
        tmp_path, monkeypatch):
    # present != truthy: "" replaces the base
    # prompt with empty text; base_guidelines is suppressed whenever
    # base_prompt is present.
    _, cmd = _harness_command(
        tmp_path, monkeypatch,
        {"base_prompt": "", "base_guidelines": ["MUST BE IGNORED"]})
    assert _harness_prompt(cmd) == ""
    assert not any("MUST BE IGNORED" in a for a in cmd)
    assert not any("You are a Pulsar pulse" in a for a in cmd)


def test_harness_malformed_fields_are_dropped_loudly(tmp_path, monkeypatch,
                                                    caplog):
    # A malformed disk value must NEVER prevent a
    # launch — it is dropped with a loud log and the default behaviour
    # applies. Good values in the same object still apply.
    import logging
    from pulsar.vessel.pi import PiVessel, _Session, pi_harness_overrides
    home = tmp_path / "pulse"
    cli = home / "cli"
    cli.mkdir(parents=True)
    (home / "harness-overrides.json").write_text(json.dumps({
        "skill_bodies": [{}],          # unhashable — used to crash set()
        "thinking": 5,                 # not a str
        "base_prompt": "bad\x00base",  # NUL cannot reach argv
        "base_guidelines": [1, "ok"],  # not all str
        "todo_prompt": "\ud800bad",    # lone surrogate
        "bogus_knob": True,             # unknown key
    }))
    with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):
        ov = pi_harness_overrides(cli)
    assert ov == {}
    for key in ("skill_bodies", "thinking", "base_prompt",
                "base_guidelines", "todo_prompt", "bogus_knob"):
        assert key in caplog.text
    # the good-value case: only the bad fields drop, the good ones apply
    (home / "harness-overrides.json").write_text(json.dumps({
        "skill_bodies": ["selected"],
        "thinking": "high",
        "todo_prompt": "ok line",
    }))
    assert pi_harness_overrides(cli) == {"skill_bodies": ["selected"],
                                         "thinking": "high",
                                         "todo_prompt": "ok line"}
    # and a spawn still works end to end with the malformed file present
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    (home / "harness-overrides.json").write_text(
        json.dumps({"skill_bodies": [{}]}))
    sess = _Session(sid="s", cwd=str(cli), session_dir=tmp_path / "sd",
                    cli_session_id="c", model="pi-v4-flash", env={})
    cmd = PiVessel()._build_command(sess, "hi")
    assert cmd[0] == "/bin/true" and cmd[-1] == "hi"


def _skill_case(root, overrides=None):
    """A full skill workspace: two selected skills, a pi-skill-bodies.json
    arm C (selected only), and an optional harness-overrides.json."""
    home = root / "pulse"
    cli = home / "cli"
    cli.mkdir(parents=True)
    for name in ("selected", "other"):
        skill = cli / ".claude" / "skills" / name
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(f"{name.upper()}_SENTINEL\n")
    (home / "diet.json").write_text(
        json.dumps({"clean": True, "skills": ["selected", "other"]}))
    (home / "pi-skill-bodies.json").write_text(json.dumps(["selected"]))
    if overrides is not None:
        (home / "harness-overrides.json").write_text(json.dumps(overrides))
    return cli


def test_harness_skill_bodies_wins_over_pi_skill_bodies_file(
        tmp_path, monkeypatch):
    from pulsar.vessel.pi import PiVessel, _Session
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")

    def cmd_for(root, overrides=None):
        cli = _skill_case(root, overrides)
        sess = _Session(sid="s", cwd=str(cli), session_dir=root / "sd",
                        cli_session_id="c", model="pi-v4-flash", env={})
        return cli, PiVessel()._build_command(sess, "hi")

    # baseline: the file wins when the override key is absent (arm C)
    cli, cmd = cmd_for(tmp_path / "base")
    assert str(cli / ".claude" / "skills" / "selected" / "SKILL.md") in cmd
    assert str(cli / ".claude" / "skills" / "other" / "SKILL.md") not in cmd
    assert any("other. Its SKILL.md is NOT loaded" in a for a in cmd)

    # skill_bodies present WINS over the file: null = arm A (all bodies),
    # [] = arm B (no bodies), a list = arm C (those bodies)
    for i, (overrides, sel_body, other_body) in enumerate((
            ({"skill_bodies": None}, True, True),     # arm A
            ({"skill_bodies": []}, False, False),     # arm B
            ({"skill_bodies": ["other"]}, False, True))):  # arm C
        cli, cmd = cmd_for(tmp_path / f"case{i}", overrides)
        assert (str(cli / ".claude" / "skills" / "selected" / "SKILL.md")
                in cmd) == sel_body
        assert (str(cli / ".claude" / "skills" / "other" / "SKILL.md")
                in cmd) == other_body


def test_harness_thinking_replaces_computed_level(tmp_path, monkeypatch):
    from pulsar.vessel.pi import _wire_model_for, pi_thinking_level
    _, cmd = _harness_command(tmp_path / "ov", monkeypatch,
                              {"thinking": "xhigh"})
    assert cmd[cmd.index("--thinking") + 1] == "xhigh"
    # no file: the computed level stays, byte-for-byte with today
    cli, cmd = _harness_command(tmp_path / "plain", monkeypatch)
    assert cmd[cmd.index("--thinking") + 1] == pi_thinking_level(
        _wire_model_for("pi-v4-flash", str(cli)), None)


def test_harness_overrides_loader_ignores_bad_files(tmp_path, caplog):
    import logging
    from pulsar.vessel.pi import pi_harness_overrides
    cli = _harness_home(tmp_path)  # no file
    assert pi_harness_overrides(cli) == {}
    (cli.parent / "harness-overrides.json").write_text("{not json")
    with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):
        assert pi_harness_overrides(cli) == {}
    (cli.parent / "harness-overrides.json").write_text("[1, 2]")
    with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):
        assert pi_harness_overrides(cli) == {}
    (cli.parent / "harness-overrides.json").write_text(
        json.dumps({"thinking": "high"}))
    assert pi_harness_overrides(cli) == {"thinking": "high"}


# ---- harness overrides in PiRpcVessel (the GLM path) ----------------------

def _rpc_argv(tmp_path, monkeypatch, cli, tag):
    """Spawn PiRpcVessel against ``cli`` via the fake rpc shim; return the
    argv the real pi binary would have received (FAKE_PI_RPC_ARGV_DUMP
    pattern from tests/test_pi_skill_loading.py — PiRpcVessel builds its
    command inline in spawn(), so a dumped argv is the only handle)."""
    from pulsar.vessel.pi import PiRpcVessel
    from tests.test_vessel_pi_rpc import _shim_env as rpc_shim
    dump = tmp_path / f"argv-{tag}.json"
    rpc_shim(monkeypatch, tmp_path)
    monkeypatch.setenv("FAKE_PI_RPC_ARGV_DUMP", str(dump))

    async def run():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(cli), initial_prompt="hi")
        try:
            await asyncio.wait_for(_collect_turn(v.subscribe(sid)), timeout=15)
        finally:
            await v.stop(sid)
    asyncio.run(run())
    return json.loads(dump.read_text())


def test_rpc_harness_base_prompt_replaces_default_and_empty_is_empty(
        tmp_path, monkeypatch):
    cli = _harness_home(tmp_path, {"base_prompt": "RPC BASE",
                                   "base_guidelines": ["NO SHOW"]})
    args = _rpc_argv(tmp_path, monkeypatch, cli, "bp")
    assert args[args.index("--system-prompt") + 1] == "RPC BASE"
    assert not any("NO SHOW" in a for a in args)
    assert not any("You are a Pulsar pulse" in a for a in args)
    # "" is an EMPTY replacement, not absent.
    cli = _harness_home(tmp_path / "e", {"base_prompt": ""})
    args = _rpc_argv(tmp_path / "e", monkeypatch, cli, "bpe")
    assert args[args.index("--system-prompt") + 1] == ""


def test_rpc_harness_base_guidelines_replace_computed_list(
        tmp_path, monkeypatch):
    cli = _harness_home(tmp_path, {"base_guidelines": ["ONE RULE"]})
    args = _rpc_argv(tmp_path, monkeypatch, cli, "bg")
    prompt = args[args.index("--system-prompt") + 1]
    assert prompt.startswith("You are a Pulsar pulse")
    assert "Guidelines:\n- ONE RULE" in prompt
    assert "Prefer grep/find/ls tools over bash" not in prompt


def test_rpc_harness_todo_prompt_replaces_default_and_empty_keeps_the_tool(
        tmp_path, monkeypatch):
    from pulsar.vessel.pi import PI_TODO_EXTENSION, PI_TODO_PROMPT
    cli = _harness_home(tmp_path, {"todo_prompt": "USE THE LIST TOOL"})
    args = _rpc_argv(tmp_path, monkeypatch, cli, "tp")
    assert str(PI_TODO_EXTENSION) in args  # the extension still loads
    assert "USE THE LIST TOOL" in args
    assert PI_TODO_PROMPT not in args
    # "" = no todo append line ONLY — the extension/tool stay (instructions
    # and availability are separate knobs).
    cli = _harness_home(tmp_path / "e", {"todo_prompt": ""})
    args = _rpc_argv(tmp_path / "e", monkeypatch, cli, "tpe")
    assert str(PI_TODO_EXTENSION) in args
    assert not any(a == "--append-system-prompt"
                   and args[i + 1] == PI_TODO_PROMPT
                   for i, a in enumerate(args))


def test_rpc_harness_skill_bodies_wins_over_pi_skill_bodies_file(
        tmp_path, monkeypatch):
    for i, (overrides, sel_body, other_body) in enumerate((
            ({"skill_bodies": None}, True, True),     # arm A
            ({"skill_bodies": []}, False, False),     # arm B
            ({"skill_bodies": ["other"]}, False, True))):  # arm C
        root = tmp_path / f"case{i}"
        cli = _skill_case(root, overrides)
        args = _rpc_argv(root, monkeypatch, cli, f"sb{i}")
        assert (str(cli / ".claude" / "skills" / "selected" / "SKILL.md")
                in args) == sel_body
        assert (str(cli / ".claude" / "skills" / "other" / "SKILL.md")
                in args) == other_body


def test_rpc_harness_thinking_replaces_computed_level(tmp_path, monkeypatch):
    from pulsar.vessel.pi import (PI_MODEL, _wire_model_for,
                                  pi_thinking_level)
    cli = _harness_home(tmp_path, {"thinking": "xhigh"})
    args = _rpc_argv(tmp_path, monkeypatch, cli, "th")
    assert args[args.index("--thinking") + 1] == "xhigh"
    # no file: the computed level for the RPC default model stays
    cli = _harness_home(tmp_path / "plain")
    args = _rpc_argv(tmp_path / "plain", monkeypatch, cli, "thp")
    assert args[args.index("--thinking") + 1] == pi_thinking_level(
        _wire_model_for(PI_MODEL, str(cli)), None)


def test_rpc_harness_malformed_file_does_not_block_spawn(
        tmp_path, monkeypatch):
    # A malformed disk object (e.g. {"skill_bodies": [{}]}, which previously
    # crashed both launch modes with TypeError) is dropped loudly and the spawn
    # uses today's defaults.
    from pulsar.vessel.pi import PI_TODO_EXTENSION
    cli = _harness_home(tmp_path, {"skill_bodies": [{}], "thinking": 5,
                                   "base_prompt": "bad\x00base",
                                   "base_guidelines": [1, "x"],
                                   "todo_prompt": "\ud800bad",
                                   "bogus_knob": True})
    args = _rpc_argv(tmp_path, monkeypatch, cli, "bad")  # must not raise
    prompt = args[args.index("--system-prompt") + 1]
    assert prompt.startswith("You are a Pulsar pulse")
    assert not any("bad\x00base" in a or "\ud800bad" in a for a in args)
    assert str(PI_TODO_EXTENSION) in args


# ---- harness overrides: brief C knobs (tools, bash_prompt, snippet_max,
# compaction — an experiment PR 3) -------------------------------------------

def test_harness_tools_replace_default_allowlist(tmp_path, monkeypatch):
    from pulsar.vessel import pi as pi_mod
    monkeypatch.setattr(pi_mod.shutil, "which", lambda name: None)
    # with the key: the override list replaces PI_DEFAULT_TOOLS, the fd
    # rule still applies to it ("find" drops without fd/fdfind), and the
    # base prompt lists EXACTLY the tools that remain
    _, cmd = _harness_command(tmp_path, monkeypatch,
                              {"tools": ["read", "bash", "find", "ls"]})
    assert cmd[cmd.index("--tools") + 1] == "read,bash,ls"
    prompt = _harness_prompt(cmd)
    assert "- read: " in prompt and "- bash: " in prompt and "- ls: " in prompt
    assert "- grep: " not in prompt and "- find: " not in prompt
    assert "- todo_write: " not in prompt
    # without the key: today's allowlist and prompt, byte-for-byte
    _, cmd = _harness_command(tmp_path / "plain", monkeypatch)
    assert cmd[cmd.index("--tools") + 1] == \
        "read,bash,edit,write,grep,ls,todo_write"
    prompt = _harness_prompt(cmd)
    assert "- todo_write: " in prompt and "- grep: " in prompt


def test_harness_tools_padded_find_drops_without_fd(tmp_path, monkeypatch):
    # 2026-10-23: a padded " find " passes the loader's
    # utf-8/no-comma check, and pi's argument parser trims whitespace — so
    # the fd rule must normalize each name BEFORE filtering and joining,
    # not compare the raw string against "find".
    from pulsar.vessel import pi as pi_mod
    monkeypatch.setattr(pi_mod.shutil, "which", lambda name: None)
    _, cmd = _harness_command(tmp_path, monkeypatch,
                              {"tools": ["read", " find "]})
    assert cmd[cmd.index("--tools") + 1] == "read"
    prompt = _harness_prompt(cmd)
    assert "- read: " in prompt and "- find: " not in prompt
    # no override: today's default join is unchanged (find drops without fd)
    _, cmd = _harness_command(tmp_path / "plain", monkeypatch)
    assert cmd[cmd.index("--tools") + 1] == \
        "read,bash,edit,write,grep,ls,todo_write"


def test_harness_bash_prompt_replaces_default_and_empty_keeps_guard(
        tmp_path, monkeypatch):
    from pulsar.vessel.pi import (PI_BASH_PROMPT, PI_GUARD_EXTENSION,
                                  PI_TODO_EXTENSION)
    # with the key: the line after the guard -e is the replacement
    _, cmd = _harness_command(tmp_path, monkeypatch,
                              {"bash_prompt": "CUSTOM BASH LINE"})
    i = cmd.index(str(PI_GUARD_EXTENSION))
    assert cmd[i - 1] == "-e"
    assert cmd[i + 1] == "--append-system-prompt"
    assert cmd[i + 2] == "CUSTOM BASH LINE"
    assert PI_BASH_PROMPT not in cmd
    # "" = NO bash prompt line, the guard extension itself always stays
    _, cmd = _harness_command(tmp_path / "e", monkeypatch,
                              {"bash_prompt": ""})
    assert str(PI_GUARD_EXTENSION) in cmd
    assert str(PI_TODO_EXTENSION) in cmd
    assert PI_BASH_PROMPT not in cmd
    assert not any(a == "--append-system-prompt"
                   and cmd[j + 1] == PI_BASH_PROMPT
                   for j, a in enumerate(cmd))
    # without the key: today's PI_BASH_PROMPT line rides with the guard
    _, cmd = _harness_command(tmp_path / "plain", monkeypatch)
    assert PI_BASH_PROMPT in cmd


def test_harness_snippet_max_caps_tool_result_snippets(tmp_path):
    from pulsar.vessel.base import TRUNCATED_MARK
    from pulsar.vessel.pi import (_SNIPPET_MAX, _snippet_max_from,
                                  classify_record, pi_harness_overrides)
    rec = {"type": "tool_execution_end", "toolCallId": "t1",
           "result": {"content": [{"type": "text", "text": "x" * 9000}]}}
    # with the knob: the tool_execution_end result trims to N chars
    ev = classify_record(rec, 250)[0]
    assert ev.kind == "tool_result"
    assert ev.payload["snippet"] == "x" * 250 + TRUNCATED_MARK
    # without an override arg: today's default cap
    ev = classify_record(rec)[0]
    assert ev.payload["snippet"] == "x" * _SNIPPET_MAX + TRUNCATED_MARK
    # the reader wiring: the cap comes from the loaded sidecar
    cli = _harness_home(tmp_path, {"snippet_max": 250})
    assert _snippet_max_from(pi_harness_overrides(cli)) == 250
    cli = _harness_home(tmp_path / "plain")
    assert _snippet_max_from(pi_harness_overrides(cli)) == _SNIPPET_MAX


def test_harness_compaction_override(tmp_path, monkeypatch, caplog):
    import logging
    from pulsar.vessel.pi import PiVessel
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")

    def settings_at(root, overrides):
        cli = _harness_home(root, overrides)
        caplog.clear()
        with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):

            async def run():
                await PiVessel().spawn(cwd=str(cli))
            asyncio.run(run())
        return cli, json.loads((cli / ".pi" / "settings.json").read_text())

    # compaction=true is REFUSED in print mode (2026-10-23): `pi -p --mode json`
    # unsubscribes as soon as prompt()
    # returns — on overflow that is the ERROR agent_end, before the
    # compaction continuation runs — so the recovered answer never reaches
    # stdout. The setting stays disabled and the refusal is loud; the RPC
    # path keeps the knob (test_rpc_harness_brief_c_knobs).
    _, s = settings_at(tmp_path / "on", {"compaction": True})
    assert s["compaction"]["enabled"] is False
    assert "refused in print mode" in caplog.text
    # the 429 stall-kill retry fix still applies whatever the knob says
    assert s["retry"]["provider"]["maxRetries"] == 0
    # false and absent: today's behaviour (disabled), no refusal warning
    _, s = settings_at(tmp_path / "off", {"compaction": False})
    assert s["compaction"]["enabled"] is False
    assert "refused in print mode" not in caplog.text
    _, s = settings_at(tmp_path / "none", None)
    assert s["compaction"]["enabled"] is False
    assert "refused in print mode" not in caplog.text
    # the refusal holds on EVERY print-mode spawn: a second spawn with the
    # knob still writes enabled=false (no stale enable can survive)
    cli = _harness_home(tmp_path / "stale", {"compaction": True})

    async def spawn_here():
        await PiVessel().spawn(cwd=str(cli))
    asyncio.run(spawn_here())
    s = json.loads((cli / ".pi" / "settings.json").read_text())
    assert s["compaction"]["enabled"] is False
    (cli / ".pi" / "settings.json").write_text(
        json.dumps({"compaction": {"enabled": True}}))  # a stale enable
    asyncio.run(spawn_here())
    s = json.loads((cli / ".pi" / "settings.json").read_text())
    assert s["compaction"]["enabled"] is False


def test_harness_brief_c_malformed_fields_drop_loudly(tmp_path, monkeypatch,
                                                      caplog):
    # Same contract as the PR A keys: a malformed disk value is dropped
    # with a loud log and today's behaviour applies — never a blocked
    # launch. Good values in the same object still apply.
    import logging
    from pulsar.vessel import pi as pi_mod
    from pulsar.vessel.pi import pi_harness_overrides
    sidecar = (_harness_home(tmp_path)).parent / "harness-overrides.json"
    sidecar.write_text(json.dumps({
        "tools": [],                    # empty allowlist
        "snippet_max": 100,             # < 200
        "compaction": "yes",            # not a bool
        "bash_prompt": "bad\x00bash",   # NUL cannot reach argv
    }))
    with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):
        assert pi_harness_overrides(sidecar.parent / "cli") == {}
    for key in ("tools", "snippet_max", "compaction", "bash_prompt"):
        assert key in caplog.text
    # more bad shapes: comma tool name (allowlist is comma-joined),
    # non-list tools, non-int snippet_max, bool-as-int, int-as-bool
    sidecar.write_text(json.dumps({
        "tools": ["ok", "a,b"], "snippet_max": "800",
        "compaction": 1, "bash_prompt": ["not", "a", "str"]}))
    with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):
        assert pi_harness_overrides(sidecar.parent / "cli") == {}
    # good values all apply
    sidecar.write_text(json.dumps({
        "tools": ["read", "bash"], "bash_prompt": "ok line",
        "snippet_max": 250, "compaction": True}))
    assert pi_harness_overrides(sidecar.parent / "cli") == {
        "tools": ["read", "bash"], "bash_prompt": "ok line",
        "snippet_max": 250, "compaction": True}
    # and a spawn with the malformed file still builds today's command —
    # from the SAME home that holds the malformed sidecar (not a separate
    # directory whose sidecar is absent).
    sidecar.write_text(json.dumps({"tools": [{}], "compaction": "x",
                                   "snippet_max": 1}))
    cli = sidecar.parent / "cli"
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    monkeypatch.setattr(pi_mod.shutil, "which", lambda name: None)
    from pulsar.vessel.pi import PiVessel, _Session
    sess = _Session(sid="s", cwd=str(cli), session_dir=tmp_path / "sd",
                    cli_session_id="c", model="pi-v4-flash", env={})
    cmd = PiVessel()._build_command(sess, "hi")
    assert cmd[cmd.index("--tools") + 1] == pi_mod.pi_tools_args()[1]
    assert cmd[cmd.index("--thinking") + 1]  # computed level, not a crash
    # RPC mode too, from the same home, without touching the file
    args = _rpc_argv(tmp_path, monkeypatch, cli, "mal")
    assert args[args.index("--tools") + 1] == pi_mod.pi_tools_args()[1]


def test_harness_brief_c_fixture_exact_command_line(tmp_path, monkeypatch):
    # Brief C hand check: the EXACT argv _build_command produces from a
    # harness-overrides.json carrying all four keys, and without the file.
    # Everything is literal except the tmp/session paths, the extension
    # paths and the model mapping (repo config), pinned by reference —
    # snippet_max and compaction are reader/spawn-side and leave no argv
    # trace, which this equality makes obvious.
    from pulsar.vessel import pi as pi_mod
    from pulsar.vessel.pi import (PI_BASH_PROMPT, PI_GUARD_EXTENSION,
                                  PI_TODO_EXTENSION, PI_TODO_PROMPT,
                                  PiVessel, _Session, _wire_model_for)
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    monkeypatch.setattr(pi_mod.shutil, "which", lambda name: None)
    monkeypatch.setattr(pi_mod, "pi_thinking_level", lambda m, e: "off")
    model = _wire_model_for("pi-v4-flash", "x")

    cli = _harness_home(tmp_path, {"tools": ["read", "bash", "edit"],
                                   "bash_prompt": "CUSTOM BASH LINE",
                                   "snippet_max": 250,
                                   "compaction": True})
    sd = tmp_path / "sd"
    sess = _Session(sid="s", cwd=str(cli), session_dir=sd,
                    cli_session_id="c", model="pi-v4-flash", env={})
    assert PiVessel()._build_command(sess, "hi") == [
        "/bin/true", "-p", "--mode", "json", "--session-dir", str(sd),
        "-nc", "--no-skills", "--no-extensions",
        "--thinking", "off", "--model", model,
        "--tools", "read,bash,edit",
        "--system-prompt", pi_mod.pi_base_prompt(["read", "bash", "edit"]),
        "-e", str(PI_TODO_EXTENSION),
        "--append-system-prompt", PI_TODO_PROMPT,
        "-e", str(PI_GUARD_EXTENSION),
        "--append-system-prompt", "CUSTOM BASH LINE",
        "-e", str(pi_mod.PI_EFFORT_EXTENSION),
        "hi",
    ]
    # without the file: today's command, byte-for-byte
    cli2 = _harness_home(tmp_path / "plain")
    sd2 = tmp_path / "sd2"
    sess2 = _Session(sid="s", cwd=str(cli2), session_dir=sd2,
                     cli_session_id="c", model="pi-v4-flash", env={})
    default_tools = ["read", "bash", "edit", "write", "grep", "ls",
                     "todo_write"]
    assert PiVessel()._build_command(sess2, "hi") == [
        "/bin/true", "-p", "--mode", "json", "--session-dir", str(sd2),
        "-nc", "--no-skills", "--no-extensions",
        "--thinking", "off", "--model", model,
        "--tools", ",".join(default_tools),
        "--system-prompt", pi_mod.pi_base_prompt(default_tools),
        "-e", str(PI_TODO_EXTENSION),
        "--append-system-prompt", PI_TODO_PROMPT,
        "-e", str(PI_GUARD_EXTENSION),
        "--append-system-prompt", PI_BASH_PROMPT,
        "-e", str(pi_mod.PI_EFFORT_EXTENSION),
        "hi",
    ]


def test_rpc_harness_brief_c_knobs(tmp_path, monkeypatch):
    # The GLM path: same four knobs through PiRpcVessel.spawn — the argv
    # for tools/bash_prompt, and the .pi/settings.json for compaction.
    from pulsar.vessel.pi import PI_BASH_PROMPT, PI_GUARD_EXTENSION
    cli = _harness_home(tmp_path, {"tools": ["read", "bash"],
                                   "bash_prompt": "RPC BASH LINE",
                                   "compaction": True})
    args = _rpc_argv(tmp_path, monkeypatch, cli, "bc")
    assert args[args.index("--tools") + 1] == "read,bash"
    i = args.index(str(PI_GUARD_EXTENSION))
    assert args[i + 1] == "--append-system-prompt"
    assert args[i + 2] == "RPC BASH LINE"
    assert PI_BASH_PROMPT not in args
    settings = json.loads((cli / ".pi" / "settings.json").read_text())
    assert settings["compaction"]["enabled"] is True
    assert settings["retry"]["provider"]["maxRetries"] == 0
    # without the keys: today's command (default allowlist, default bash
    # prompt) and compaction disabled
    cli = _harness_home(tmp_path / "plain")
    args = _rpc_argv(tmp_path / "plain", monkeypatch, cli, "bcp")
    # the real default allowlist (fd presence decides "find")
    from pulsar.vessel.pi import pi_tools_args
    assert args[args.index("--tools") + 1] == pi_tools_args()[1]
    assert PI_BASH_PROMPT in args
    settings = json.loads((cli / ".pi" / "settings.json").read_text())
    assert settings["compaction"]["enabled"] is False
def test_harness_deeply_nested_json_never_stops_launch(
        tmp_path, monkeypatch, caplog):
    # The pi-side parse used to raise RecursionError on
    # a deeply-nested file, killing the launch BEFORE the vessel could
    # fall back. Parsing now goes through the shared
    # pulsar.harness_overrides.read_overrides, which never raises and
    # warns once per path per process.
    import logging
    from pulsar.vessel.pi import PiVessel, _Session, pi_harness_overrides
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    cli = _harness_home(tmp_path)
    nested = "[" * 10000 + "0" + "]" * 10000  # RecursionError in a naive parse
    (tmp_path / "pulse" / "harness-overrides.json").write_text(nested)
    sess = _Session(sid="s", cwd=str(cli), session_dir=tmp_path / "sd",
                    cli_session_id="c", model="pi-v4-flash", env={})
    cmd = PiVessel()._build_command(sess, "hi")  # must not raise
    assert cmd[0] == "/bin/true" and cmd[-1] == "hi"
    # The reader itself: {} and one warning per path, not per launch.
    with caplog.at_level(logging.WARNING, logger="pulsar.harness_overrides"):
        assert pi_harness_overrides(cli) == {}
        assert pi_harness_overrides(cli) == {}
    warns = [r for r in caplog.records if "harness overrides" in r.message]
    assert len(warns) == 1, "one warning per path, not per read"


def test_rpc_harness_deeply_nested_json_never_stops_launch(
        tmp_path, monkeypatch):
    # Same probe through PiRpcVessel's spawn(): the command build must not
    # raise, and the rpc argv shows today's defaults.
    from pulsar.vessel.pi import pi_thinking_level, _wire_model_for, PI_MODEL
    cli = _harness_home(tmp_path)
    (tmp_path / "pulse" / "harness-overrides.json").write_text(
        "[" * 10000 + "0" + "]" * 10000)
    args = _rpc_argv(tmp_path, monkeypatch, cli, "nested")  # must not raise
    assert args[args.index("--thinking") + 1] == pi_thinking_level(
        _wire_model_for(PI_MODEL, str(cli)), None)
    assert args[args.index("--system-prompt") + 1].startswith(
        "You are a Pulsar pulse")


def test_harness_runner_and_handoff_keys_pass_through_silently(
        tmp_path, caplog):
    # A combined sidecar (pi knobs + runner/handoff knobs) must not warn on
    # every pi launch: pi does not consume steer_nudge_s / steer_prefix /
    # handoff_prompt / successor_prefix / handoff_warning, so they pass
    # through _validated_harness_overrides without a shape check.
    import logging
    from pulsar.vessel.pi import pi_harness_overrides
    cli = _harness_home(tmp_path, {
        "thinking": "high",
        "steer_nudge_s": 0.3, "steer_prefix": "[steer] ",
        "handoff_prompt": "OPENER {opener}", "successor_prefix": "SEED",
        "handoff_warning": "W {ctx}/{thr}",
    })
    with caplog.at_level(logging.WARNING, logger="pulsar.vessel.pi"):
        ov = pi_harness_overrides(cli)
    assert ov == {
        "thinking": "high",
        "steer_nudge_s": 0.3, "steer_prefix": "[steer] ",
        "handoff_prompt": "OPENER {opener}", "successor_prefix": "SEED",
        "handoff_warning": "W {ctx}/{thr}",
    }
    assert not [r for r in caplog.records if "harness override" in r.message]


def test_harness_reader_accepts_render_knobs(tmp_path):
    # The disk reader consumes prompt_parts
    # (validated: null or a list of usable strings) and passes the
    # daemon-side render knobs part_text / skill_catalog through untouched —
    # pi ignores those; a bad prompt_parts value drops loudly, never fatally.
    from pulsar.vessel.pi import pi_harness_overrides
    cli = _harness_home(tmp_path, {
        "prompt_parts": ["identity"],
        "part_text": {"time_zone": "## X\n"},
        "skill_catalog": "names"})
    assert pi_harness_overrides(cli) == {
        "prompt_parts": ["identity"],
        "part_text": {"time_zone": "## X\n"},
        "skill_catalog": "names"}
    # explicit null is a valid prompt_parts value (all parts)
    cli = _harness_home(tmp_path / "h2", {"prompt_parts": None})
    assert pi_harness_overrides(cli) == {"prompt_parts": None}
    # a non-list is dropped with a warning; a list with an unusable string too
    cli = _harness_home(tmp_path / "h3", {"prompt_parts": "nope"})
    assert pi_harness_overrides(cli) == {}
    cli = _harness_home(tmp_path / "h4", {"prompt_parts": ["a", "bad\x00"]})
    assert pi_harness_overrides(cli) == {}


def test_prompt_parts_todo_exclusion_and_re_enable(tmp_path, monkeypatch):
    # The todo extension follows the ONE
    # effective prompt-parts allowlist — the harness-overrides list wins
    # over the diet's, and explicit null means all parts.
    from pulsar.vessel.pi import PI_TODO_EXTENSION, PI_TODO_PROMPT
    lean_diet = json.dumps({"clean": True, "prompt_parts": ["identity"]})
    # the diet's list excludes "todo": no extension, no prompt line
    _, cmd = _harness_command(tmp_path, monkeypatch, None,
                              **{"diet.json": lean_diet})
    assert str(PI_TODO_EXTENSION) not in cmd
    assert PI_TODO_PROMPT not in cmd
    # an override list WITHOUT "todo" wins over a diet that includes it
    todo_diet = json.dumps({"clean": True, "prompt_parts": ["todo"]})
    _, cmd = _harness_command(tmp_path / "h2", monkeypatch,
                              {"prompt_parts": ["identity"]},
                              **{"diet.json": todo_diet})
    assert str(PI_TODO_EXTENSION) not in cmd
    # an override list WITH "todo" re-enables it on a lean diet
    _, cmd = _harness_command(tmp_path / "h3", monkeypatch,
                              {"prompt_parts": ["todo"]},
                              **{"diet.json": lean_diet})
    assert str(PI_TODO_EXTENSION) in cmd and PI_TODO_PROMPT in cmd
    # explicit null = all parts: todo is back even on the lean diet
    _, cmd = _harness_command(tmp_path / "h4", monkeypatch,
                              {"prompt_parts": None},
                              **{"diet.json": lean_diet})
    assert str(PI_TODO_EXTENSION) in cmd and PI_TODO_PROMPT in cmd


# -- Provisional provider-error hold (2026-10-23) ------------------------- #

def _pi_turn(tmp_path, monkeypatch, prompt):
    _shim_env(monkeypatch, tmp_path)

    async def run():
        v = PiVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt=prompt)
        it = v.subscribe(sid)
        try:
            return await asyncio.wait_for(_collect_turn(it), timeout=15)
        finally:
            await v.stop(sid)

    return asyncio.run(run())


def test_compaction_recovery_holds_the_provider_error(tmp_path, monkeypatch):
    # COMPACTRECOVER: overflow -> empty error message_end -> compaction
    # willRetry:true -> the retry answers. The provisional provider error
    # is dropped on verified recovery: no runner_error, one done AFTER the
    # recovered text.
    evs = _pi_turn(tmp_path, monkeypatch, "COMPACTRECOVER")
    assert _kinds(evs) == ["usage", "assistant_text", "usage", "done"], \
        _kinds(evs)
    assert "RECOVERED_AFTER_COMPACTION" in evs[1].payload["text"]
    assert evs[-1].payload["reason"] == "result"


def test_compaction_without_retry_flushes_the_provider_error(
        tmp_path, monkeypatch):
    # COMPACTFAIL: compaction willRetry:false closes the turn — the held
    # provider error surfaces as runner_error just BEFORE that done, and
    # no assistant text ever arrives.
    evs = _pi_turn(tmp_path, monkeypatch, "COMPACTFAIL")
    assert _kinds(evs) == ["usage", "runner_error", "done"], _kinds(evs)
    err = evs[1]
    assert err.payload["where"] == "provider_error"
    assert "maximum context length" in err.payload["error"]
    assert not any(e.kind == "assistant_text" for e in evs)


def test_plain_provider_error_flushes_at_turn_end(tmp_path, monkeypatch):
    # OVFERR: a provider error with no recovery at all — the error is
    # flushed at EOF as runner_error just before the clean-exit done.
    evs = _pi_turn(tmp_path, monkeypatch, "OVFERR")
    assert _kinds(evs) == ["usage", "runner_error", "done"], _kinds(evs)
    assert evs[1].payload["where"] == "provider_error"


def test_provider_error_keeps_streamed_assistant_text(tmp_path, monkeypatch):
    # ERRPARTIAL (final review 0d64351c finding 1): a provider error whose
    # message_end carries streamed text must deliver that assistant_text —
    # base 46bd3cc0 emitted it; the P1 gate dropped it by consuming the
    # whole record. Only the runner_error is held, flushed just before the
    # done. No overrides file.
    evs = _pi_turn(tmp_path, monkeypatch, "ERRPARTIAL")
    assert _kinds(evs) == ["assistant_delta", "assistant_text", "usage",
                           "runner_error", "done"], _kinds(evs)
    assert evs[1].payload["text"] == \
        "PARTIAL ANSWER BEFORE PROVIDER ERROR"
    errs = [e for e in evs if e.kind == "runner_error"]
    assert len(errs) == 1
    assert errs[0].payload["where"] == "provider_error"
    assert "content_filter" in errs[0].payload["error"]
    assert not (tmp_path / "harness-overrides.json").exists()
