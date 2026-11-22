"""pi conformance probes.

Scripted turns every pi change must keep green (1-10, 3b, 11-12, 13). Probes 2-10 run
the REAL pinned pi CLI through PiRpcVessel against tests/fake_pi_model.py,
a scripted OpenAI-compatible server, so they test pi itself with no live
model. Probe 1 loads guard.ts under node. All skip when the pinned pi or
node is missing. The meter gate (0 null in_tok over a 50-call session with
parallel batches) runs on tests/fake_pi_rpc.py.
"""
from __future__ import annotations

import asyncio
import json
import os
import shutil
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from tests.fake_pi_model import FakeModel
from pulsar.cli import guard
from pulsar.launch.units import DEFAULT_PI_BIN
from pulsar.runner.meter import CallMeter
from pulsar.vessel import pi
from pulsar.vessel.pi import PiRpcVessel

_HERE = Path(__file__).resolve().parent
PI_BIN = os.environ.get("PULSAR_PI_BIN") or DEFAULT_PI_BIN
needs_pi = pytest.mark.skipif(
    not (Path(PI_BIN).exists() and shutil.which("node")),
    reason="pinned pi CLI or node not installed")


def _pulse(tmp_path: Path, fm: FakeModel, diet: dict | None) -> tuple[Path, dict]:
    """A pulse home the way the spawner lays it out: <home>/cli is the cwd,
    <home>/pi-home/agent/models.json the private provider map."""
    home = tmp_path / "pulse"
    cwd = home / "cli"
    cwd.mkdir(parents=True)
    agent = home / "pi-home" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(json.dumps(fm.provider_map()))
    if diet is not None:
        (home / "diet.json").write_text(json.dumps(diet))
    env = {"PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1",
           "PULSE_CLI_DIR": str(cwd)}
    return cwd, env


def _turn(tmp_path, monkeypatch, script, *, diet=None, on_tool_call=None,
          before=None, timeout=40):
    """One user turn on the real pi (rpc mode). Returns (events, fake)."""
    monkeypatch.setenv("PULSAR_PI_BIN", PI_BIN)
    fm = FakeModel(script).start()
    try:
        cwd, env = _pulse(tmp_path, fm, diet)
        if before:
            before(cwd)

        async def go():
            v = PiRpcVessel()
            sid = await v.spawn(cwd=str(cwd), model="fake/fake-model", env=env)
            it = v.subscribe(sid)
            evs = []
            try:
                await v.send(sid, "go")

                async def collect():
                    async for ev in it:
                        evs.append(ev)
                        if ev.kind == "tool_call" and on_tool_call:
                            await on_tool_call(v, sid, ev)
                        if ev.kind == "done":
                            return
                await asyncio.wait_for(collect(), timeout)
            finally:
                await v.stop(sid)
            return evs
        return asyncio.run(go()), fm
    finally:
        fm.stop()


def _results(evs):
    return [e.payload for e in evs if e.kind == "tool_result"]


def _tool_msgs(req: dict) -> list[str]:
    out = []
    for m in req.get("messages") or []:
        if m.get("role") == "tool":
            c = m.get("content")
            out.append(c if isinstance(c, str) else json.dumps(c))
    return out


def _system(req: dict) -> str:
    m = (req.get("messages") or [{}])[0]
    c = m.get("content")
    return c if isinstance(c, str) else json.dumps(c)


# -- 1. bash call without timeout gets 120 s ------------------------------- #
@needs_pi
def test_probe1_bash_without_timeout_gets_120s(tmp_path):
    harness = tmp_path / "g.mjs"
    harness.write_text(
        "const mod = await import(process.argv[2]);\n"
        "let h; mod.default({ on: (ev, fn) => { if (ev === 'tool_call') h = fn; } });\n"
        "const a = { toolName: 'bash', input: { command: 'true' } };\n"
        "const b = { toolName: 'bash', input: { command: 'true', timeout: 7 } };\n"
        "await h(a); await h(b);\n"
        "console.log(JSON.stringify([a.input.timeout, b.input.timeout]));\n")
    out = subprocess.run(["node", str(harness), str(pi.PI_GUARD_EXTENSION)],
                         capture_output=True, text=True, timeout=30,
                         env={**os.environ, "PULSAR_GUARD_BIN": "/bin/true"})
    assert json.loads(out.stdout.strip().splitlines()[-1]) == [120, 7], out.stderr


# -- 2. guard refuses rm -rf on a protected tree --------------------------- #
@needs_pi
def test_probe2_guard_refuses_protected_delete(tmp_path, monkeypatch):
    # A pulses-dir entry that does not exist: protected, so the guard must
    # refuse it, and harmless even if a broken guard let it run.
    target = f"{guard.data_dir()}/pulses/zz-conformance-probe-absent"
    assert not Path(target).exists()
    assert guard.check_command(f"rm -rf {target}", cwd="/tmp") is not None
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "bash", "args": {"command": f"rm -rf {target}"}}]},
        {"text": "ok"}])
    (res,) = _results(evs)
    assert res["ok"] is False and "pulsar-guard" in res["snippet"], res


# -- 3. a linked skill body is in the first request ------------------------ #
@needs_pi
def test_probe3_linked_skill_body_in_first_request(tmp_path, monkeypatch):
    def link(cwd):
        d = cwd / ".claude" / "skills" / "probe-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: probe-skill\n---\nPROBE-SKILL-BODY-7731\n")
    evs, fm = _turn(tmp_path, monkeypatch, [{"text": "ok"}],
                    diet={"clean": True, "skills": ["probe-skill"]}, before=link)
    assert "PROBE-SKILL-BODY-7731" in _system(fm.requests[0])


# -- 3b. arm B (pi-skill-bodies.json = []): read line, no body (step 5) ---- #
@needs_pi
def test_probe3b_catalog_arm_sends_read_line_not_body(tmp_path, monkeypatch):
    def link(cwd):
        d = cwd / ".claude" / "skills" / "probe-skill"
        d.mkdir(parents=True)
        (d / "SKILL.md").write_text("---\nname: probe-skill\n---\nPROBE-SKILL-BODY-7731\n")
        (cwd.parent / "pi-skill-bodies.json").write_text("[]")
    evs, fm = _turn(tmp_path, monkeypatch, [{"text": "ok"}],
                    diet={"clean": True, "skills": ["probe-skill"]}, before=link)
    system = _system(fm.requests[0])
    assert "PROBE-SKILL-BODY-7731" not in system
    path = tmp_path / "pulse" / "cli" / ".claude" / "skills" / "probe-skill" / "SKILL.md"
    assert f"NOT loaded: read {path} before working" in system


# -- 4. todo_write is callable --------------------------------------------- #
@needs_pi
def test_probe4_todo_write_is_callable(tmp_path, monkeypatch):
    todos = [{"id": "1", "content": "probe", "status": "in_progress"}]
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "todo_write", "args": {"todos": todos}}]},
        {"text": "ok"}])
    assert "todo_write" in [t["function"]["name"] for t in fm.requests[0]["tools"]]
    (res,) = _results(evs)
    assert res["ok"] is True, res


# -- 5. bash output over 2,000 lines is cut, rest in a temp file ----------- #
@needs_pi
def test_probe5_long_bash_output_truncated_to_temp_file(tmp_path, monkeypatch):
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "bash", "args": {"command": "seq 1 3000"}}]},
        {"text": "ok"}])
    (msg,) = _tool_msgs(fm.requests[1])
    assert "of 3001. Full output: /tmp/pi-bash-" in msg, msg[-300:]
    assert msg.count("\\n") <= 2001


# -- 6. two tool calls in one message run in parallel ---------------------- #
@needs_pi
def test_probe6_two_tool_calls_run_in_parallel(tmp_path, monkeypatch):
    flag = tmp_path / "b-ran"
    wait = (f"for i in $(seq 50); do [ -f {flag} ] && echo SAW-B && exit 0; "
            f"sleep 0.1; done; echo NO-B")
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "bash", "args": {"command": wait}},
                   {"name": "bash", "args": {"command": f"sleep 0.3; touch {flag}"}}]},
        {"text": "ok"}])
    assert any("SAW-B" in m for m in _tool_msgs(fm.requests[1])), _tool_msgs(fm.requests[1])


# -- 7. abort_bash ends a sleeping bash ------------------------------------ #
@needs_pi
def test_probe7_abort_bash_ends_a_sleeping_bash(tmp_path, monkeypatch):
    async def abort(v, sid, ev):
        await asyncio.sleep(0.5)
        await v.abort_bash(sid)
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "bash", "args": {"command": "sleep 60"}}]},
        {"text": "ok"}], on_tool_call=abort, timeout=15)  # sleep 60 must not run out
    (res,) = _results(evs)
    # killed on a signal: guard.ts turns pi's "(no output)" success into an error
    assert res["ok"] is False and "killed by abort_bash" in _tool_msgs(fm.requests[1])[0]
    assert evs[-1].kind == "done" and evs[-1].payload["reason"] == "result"
    assert len(fm.requests) == 2  # the turn went on after the abort


# -- 8. steer reaches a running turn --------------------------------------- #
@needs_pi
def test_probe8_steer_reaches_a_running_turn(tmp_path, monkeypatch):
    async def steer(v, sid, ev):
        await v.inject(sid, "STEER-MARKER-5512")
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "bash", "args": {"command": "sleep 1.5"}}]},
        {"text": "ok"}], on_tool_call=steer)
    assert "STEER-MARKER-5512" in json.dumps(fm.requests[1]["messages"])


# -- 9. a 404 model id is a runner_error, not silence ---------------------- #
@needs_pi
def test_probe9_404_model_is_runner_error(tmp_path, monkeypatch):
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"status": 404, "error": "The model `fake-model` does not exist."}])
    errs = [e.payload for e in evs if e.kind == "runner_error"]
    assert errs and errs[0]["where"] == "provider_error", [e.kind for e in evs]
    assert "404" in errs[0]["reason"]
    assert evs[-1].kind == "done"


# -- 10. a `length` stop is a runner_error --------------------------------- #
@needs_pi
def test_probe10_length_stop_is_runner_error(tmp_path, monkeypatch):
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"text": "partial answer", "finish": "length"}])
    errs = [e.payload for e in evs if e.kind == "runner_error"]
    assert errs and errs[0]["where"] == "output_cap", [e.kind for e in evs]


# -- meter gate: 0 null in_tok over 50 calls with parallel batches --------- #
# -- 11. native grep/find/ls are offered and grep runs (step 3) ----------- #
@needs_pi
def test_probe11_native_grep_ls_offered_and_grep_runs(tmp_path, monkeypatch):
    def seed(cwd):
        (cwd / "notes.txt").write_text("alpha\nNEEDLE-5521\n")
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "grep", "args": {"pattern": "NEEDLE-5521"}}]},
        {"text": "ok"}], before=seed)
    names = [t["function"]["name"] for t in fm.requests[0]["tools"]]
    want = pi.pi_tools_args()[1].split(",")
    assert sorted(names) == sorted(want), names
    system = _system(fm.requests[0])
    assert "Prefer grep/find/ls tools over bash" in system
    assert "Use bash for file operations like ls, rg, find" not in system
    (res,) = _results(evs)
    assert res["ok"] is True and "NEEDLE-5521" in res["snippet"], res


# -- 11b. native find runs on the system fd/fdfind, nothing downloaded ---- #
@needs_pi
@pytest.mark.skipif(not (shutil.which("fd") or shutil.which("fdfind")),
                    reason="no fd/fdfind on PATH: find is not in the default")
def test_probe11b_native_find_uses_system_fd(tmp_path, monkeypatch):
    def seed(cwd):
        (cwd / "sub").mkdir()
        (cwd / "sub" / "probe-target-6630.txt").write_text("x")
    evs, fm = _turn(tmp_path, monkeypatch, [
        {"tools": [{"name": "find", "args": {"pattern": "*6630*"}}]},
        {"text": "ok"}], before=seed)
    assert "find" in [t["function"]["name"] for t in fm.requests[0]["tools"]]
    (res,) = _results(evs)
    assert res["ok"] is True and "probe-target-6630.txt" in res["snippet"], res
    # PI_OFFLINE=1 forbids a download, and pi's tools dir stays empty: the
    # result came from the system fd/fdfind.
    assert not (tmp_path / "pulse" / "pi-home" / "agent" / "bin").exists()


# -- 12. a pulse's own pi-args.json --tools still wins (step 3) ------------- #
@needs_pi
def test_probe12_pulse_tools_allowlist_wins(tmp_path, monkeypatch):
    def args(cwd):
        (cwd.parent / "pi-args.json").write_text(json.dumps(["--tools", "read,bash"]))
    evs, fm = _turn(tmp_path, monkeypatch, [{"text": "ok"}], before=args)
    names = [t["function"]["name"] for t in fm.requests[0]["tools"]]
    assert sorted(names) == ["bash", "read"], names
    # step 4: the base prompt lists the tools the pulse really has.
    system = _system(fm.requests[0])
    assert "- read: " in system and "- bash: " in system
    assert "- grep: " not in system and "Prefer grep/find/ls" not in system


# -- 13. Pulsar base prompt replaces pi's; appended parts still arrive ---- #
@needs_pi
def test_probe13_pulsar_base_prompt_and_appends(tmp_path, monkeypatch):
    def seed(cwd):
        (cwd / "PULSAR.md").write_text("# Pulsar pulse: probe\nPULSAR-MD-MARK-4417\n")
        cage = cwd.parent / "probe-cage.ts"
        cage.write_text(
            "export default function (pi: any) {\n"
            "  pi.on('before_agent_start', (e: any) => "
            "({ systemPrompt: e.systemPrompt + '\\n\\nCAGE-MARK-9902' }));\n}\n")
        (cwd.parent / "pi-args.json").write_text(json.dumps(["-e", str(cage)]))
    evs, fm = _turn(tmp_path, monkeypatch, [{"text": "ok"}], before=seed)
    system = _system(fm.requests[0])
    assert system.startswith("You are a Pulsar pulse"), system[:200]
    assert "expert coding assistant" not in system
    assert "Pi documentation" not in system
    order = ["You are a Pulsar pulse", "PULSAR-MD-MARK-4417", pi.PI_TODO_PROMPT,
             pi.PI_BASH_PROMPT, "Current date:", "Current working directory:",
             "CAGE-MARK-9902"]
    at = [system.find(m) for m in order]
    assert -1 not in at and at == sorted(at), dict(zip(order, at))


# -- 14. RPC overflow recovery stays inside ONE turn --------------------- #
@needs_pi
def test_probe14_rpc_compaction_overflow_recovers_in_one_turn(
        tmp_path, monkeypatch):
    # With compaction=true the RPC vessel must HOLD the turn boundary
    # across compaction_start/end: overflow -> summary -> continuation is
    # ONE turn that ends with exactly one done AFTER the recovered answer,
    # with turn_in_flight still true while recovery streams.
    monkeypatch.setenv("PULSAR_PI_BIN", PI_BIN)
    fm = FakeModel([
        {"text": "Initial response. " * 600},
        {"status": 400,
         "error": "maximum context length is 1000 tokens. "
                  "Your messages resulted in 2000 tokens."},
        {"text": "SUMMARY OF EARLIER MESSAGES"},
        {"text": "RECOVERED_AFTER_COMPACTION"},
    ]).start()
    try:
        cwd, env = _pulse(tmp_path, fm, {"clean": True, "skills": []})
        (cwd.parent / "harness-overrides.json").write_text(
            json.dumps({"compaction": True}))
        (cwd / ".pi").mkdir()
        (cwd / ".pi" / "settings.json").write_text(json.dumps(
            {"compaction": {"keepRecentTokens": 10, "reserveTokens": 100}}))

        async def go():
            v = PiRpcVessel()
            sid = await v.spawn(cwd=str(cwd), model="fake/fake-model",
                                env=env)
            try:
                await v.send(sid, "First prompt. " * 600)

                async def first():
                    async for ev in v.subscribe(sid):
                        if ev.kind == "done":
                            return
                await asyncio.wait_for(first(), 30)
                # Capture turn_in_flight AT DELIVERY TIME: reading
                # v._sessions[sid] from the consumer races the reader, which
                # may deliver the final done (and clear the flag) before the
                # consumer dequeues the recovered text.
                orig_deliver = v._deliver
                in_flight_at = {}

                def traced(sess_, ev):
                    if (ev.kind == "assistant_text"
                            and "RECOVERED_AFTER_COMPACTION"
                            in ev.payload.get("text", "")):
                        in_flight_at["recovered"] = sess_.turn_in_flight
                    return orig_deliver(sess_, ev)
                v._deliver = traced
                await v.send(sid, "Now continue the task")
                evs = []

                async def second():
                    async for ev in v.subscribe(sid):
                        evs.append(ev)
                        if ev.kind == "done":
                            return
                await asyncio.wait_for(second(), 30)
                return evs, in_flight_at
            finally:
                await v.stop(sid)
        evs, in_flight_at = asyncio.run(go())
        dones = [i for i, e in enumerate(evs) if e.kind == "done"]
        assert len(dones) == 1, [e.kind for e in evs]
        recovered = [
            i for i, e in enumerate(evs)
            if e.kind == "assistant_text"
            and "RECOVERED_AFTER_COMPACTION" in e.payload.get("text", "")]
        assert recovered, [e.payload for e in evs if e.kind == "assistant_text"]
        assert recovered[0] < dones[0]  # no done before the recovery lands
        # The provisional hold dropped the overflow
        # error on verified recovery — the recovered RPC turn must carry no
        # runner_error at all.
        assert not [e for e in evs if e.kind == "runner_error"], \
            [e.payload for e in evs if e.kind == "runner_error"]
        assert in_flight_at.get("recovered") is True
        # turn 1 reply, overflow, compaction summary, continuation
        assert len(fm.requests) == 4, len(fm.requests)
    finally:
        fm.stop()


# -- 15. print mode REFUSES the compaction knob -------------------------- #
@needs_pi
def test_probe15_print_mode_compaction_refused_still_one_done(
        tmp_path, monkeypatch, caplog):
    # `pi -p --mode json` unsubscribes as soon as prompt() returns — on
    # overflow that is the ERROR agent_end, before the compaction
    # continuation runs — so print mode must refuse the knob: settings stay
    # disabled, the refusal is loud, and a turn that hits overflow still
    # ends with exactly one done (held boundary flushes at stdout EOF) and
    # never hangs waiting for a recovery that cannot reach stdout.
    import logging
    from pulsar.vessel.pi import PiVessel
    monkeypatch.setenv("PULSAR_PI_BIN", PI_BIN)
    fm = FakeModel([
        {"text": "Initial response. " * 600},
        {"status": 400,
         "error": "maximum context length is 1000 tokens. "
                  "Your messages resulted in 2000 tokens."},
        {"text": "SUMMARY OF EARLIER MESSAGES"},
        {"text": "RECOVERED_AFTER_COMPACTION"},
    ]).start()
    try:
        cwd, env = _pulse(tmp_path, fm, {"clean": True, "skills": []})
        (cwd.parent / "harness-overrides.json").write_text(
            json.dumps({"compaction": True}))
        (cwd / ".pi").mkdir()
        (cwd / ".pi" / "settings.json").write_text(json.dumps(
            {"compaction": {"keepRecentTokens": 10, "reserveTokens": 100}}))

        async def go():
            v = PiVessel()
            with caplog.at_level(logging.WARNING,
                                  logger="pulsar.vessel.pi"):
                sid = await v.spawn(cwd=str(cwd), model="fake/fake-model",
                                    env=env)
            try:
                settings = json.loads(
                    (cwd / ".pi" / "settings.json").read_text())
                await v.send(sid, "First prompt. " * 600)

                async def first():
                    async for ev in v.subscribe(sid):
                        if ev.kind == "done":
                            return
                await asyncio.wait_for(first(), 30)
                await v.send(sid, "Now continue the task")
                evs = []

                async def second():
                    async for ev in v.subscribe(sid):
                        evs.append(ev)
                        if ev.kind == "done":
                            return
                await asyncio.wait_for(second(), 30)
                return settings, evs
            finally:
                await v.stop(sid)
        settings, evs = asyncio.run(go())
        assert settings["compaction"]["enabled"] is False
        assert "refused in print mode" in caplog.text
        dones = [e for e in evs if e.kind == "done"]
        assert len(dones) == 1, [e.kind for e in evs]
        texts = [e.payload.get("text", "") for e in evs
                 if e.kind == "assistant_text"]
        assert not any("RECOVERED_AFTER_COMPACTION" in t or
                       "SUMMARY OF EARLIER" in t for t in texts), texts
        # compaction never ran: turn 1 reply, overflow — no summary request
        assert len(fm.requests) == 2, len(fm.requests)
    finally:
        fm.stop()


def _write_fake_rpc_shim(bindir: Path) -> Path:
    bindir.mkdir(exist_ok=True)
    fake = _HERE / "fake_pi_rpc.py"
    shim = bindir / "pi"
    shim.write_text(f"#!{sys.executable}\nimport runpy, sys\nsys.argv[0] = {str(fake)!r}\n"
                    f"runpy.run_path({str(fake)!r}, run_name='__main__')\n")
    shim.chmod(shim.stat().st_mode | stat.S_IEXEC)
    return shim


def test_meter_gate_no_null_in_tok_over_50_parallel_calls(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_PI_BIN", str(_write_fake_rpc_shim(tmp_path / "bin")))

    async def go():
        v = PiRpcVessel()
        sid = await v.spawn(cwd=str(tmp_path / "ws"), initial_prompt="PARALLEL")
        m, metrics = CallMeter(), []
        m.start()
        try:
            async def collect():
                async for ev in v.subscribe(sid):
                    # the runner's routing: deltas to delta(), the rest to observe()
                    if ev.kind in ("assistant_delta", "reasoning_delta", "hidden_delta",
                                   "code_delta"):
                        r = m.delta(ev.payload.get("text") or "")
                    else:
                        r = m.observe({"type": ev.kind, **ev.payload})
                    if r:
                        metrics.append(r)
                    if ev.kind == "done":
                        return
            await asyncio.wait_for(collect(), 30)
        finally:
            await v.stop(sid)
        return metrics

    metrics = asyncio.run(go())
    assert len(metrics) == 50
    assert [x for x in metrics if x["in_tok"] is None] == []
    assert [x["in_tok"] for x in metrics] == [10 * i for i in range(1, 51)]
