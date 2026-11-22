"""pulsar.vessel.base — the helpers every adapter shares, and the capability
flags the runner bridge reads instead of matching class names."""
import os
import signal

import pytest

from pulsar.vessel import base
from pulsar.vessel.claude import ClaudeVessel
from pulsar.vessel.codex import CodexVessel
from pulsar.vessel.grok import GrokVessel
from pulsar.vessel.pi import PiRpcVessel, PiVessel
from pulsar.runner.vessel_bridge import BridgedVessel


def test_as_int_rejects_bools_junk_and_negatives():
    assert base.as_int(7) == 7
    assert base.as_int(True) == 0
    assert base.as_int("7") == 0
    assert base.as_int(-3) == 0
    assert base.as_int(None) == 0


def test_snippet_strips_and_truncates_with_marker():
    assert base.snippet(None, 10) == ""
    assert base.snippet("  hi  ", 10) == "hi"
    out = base.snippet("x" * 50, 10)
    assert out.startswith("x" * 10) and out.endswith(base.TRUNCATED_MARK)
    assert base.snippet({"a": 1}, 100) == "{'a': 1}"


def test_safe_killpg_refuses_non_group_leaders(monkeypatch):
    sent = []
    monkeypatch.setattr(os, "killpg", lambda pid, sig: sent.append((pid, sig)))
    monkeypatch.setattr(os, "getpgid", lambda pid: 1)  # not its own leader
    base.safe_killpg(4242, signal.SIGTERM)
    assert sent == []
    base.safe_killpg(0, signal.SIGTERM)
    base.safe_killpg(None, signal.SIGTERM)
    assert sent == []
    monkeypatch.setattr(os, "getpgid", lambda pid: pid)  # a group we own
    base.safe_killpg(4242, signal.SIGTERM)
    assert sent == [(4242, signal.SIGTERM)]


@pytest.mark.parametrize("cls,inject,interrupt", [
    (ClaudeVessel, True, False),
    (CodexVessel, True, True),
    (PiVessel, False, True),
    (PiRpcVessel, True, True),
    (GrokVessel, False, False),
])
def test_capability_flags_drive_the_bridge(tmp_path, monkeypatch, cls, inject, interrupt):
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")  # pi vessels probe the binary on construction
    assert issubclass(cls, base.BaseVessel)
    assert cls.supports_inject is inject
    assert cls.supports_interrupt is interrupt
    bv = BridgedVessel("m", tmp_path, None, vessel_cls=cls)
    assert bv.can_inject is inject
    assert bv.can_interrupt is interrupt
    if interrupt:
        assert callable(getattr(cls, "interrupt", None)), cls


def test_only_claude_takes_a_diet():
    assert ClaudeVessel.takes_diet is True
    for cls in (CodexVessel, PiVessel, PiRpcVessel,
                GrokVessel):
        assert cls.takes_diet is False, cls


# ---- shared lifecycle helpers -----------------------------------------------

import asyncio
import logging
import sys
import types

from pulsar.vessel.events import VesselEvent


def _run(coro):
    return asyncio.run(coro)


def test_drain_until_exited_stops_after_done_exited():
    async def go():
        q = asyncio.Queue()
        for ev in (VesselEvent(kind="assistant_text", payload={"text": "a"}),
                   VesselEvent(kind="done", payload={"reason": "result"}),
                   VesselEvent(kind="done", payload={"reason": "exited"}),
                   VesselEvent(kind="assistant_text", payload={"text": "late"})):
            q.put_nowait(ev)
        return [e async for e in base.drain_until_exited(q)], q.qsize()
    got, left = _run(go())
    assert [(e.kind, e.payload.get("reason")) for e in got] == [
        ("assistant_text", None), ("done", "result"), ("done", "exited")]
    assert left == 1  # the event after done{exited} is not consumed


async def _spawn_group(script):
    return await asyncio.create_subprocess_exec(
        sys.executable, "-c", script, start_new_session=True)


_IGNORE_TERM = ("import signal, time\n"
                "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
                "print('ready', flush=True)\n"
                "time.sleep(60)\n")


def test_terminate_group_stops_a_real_child(caplog):
    async def go():
        proc = await _spawn_group("import time; time.sleep(60)")
        await asyncio.sleep(0.2)  # let it exec so getpgid(pid) == pid
        await base.terminate_group(proc, sid="s1")
        return proc
    with caplog.at_level(logging.INFO, logger="pulsar.vessel"):
        proc = _run(go())
    assert proc.returncode == -signal.SIGTERM
    names = [r.getMessage().split()[0] for r in caplog.records]
    assert names == ["vessel_stop"]


def test_terminate_group_escalates_to_kill_and_logs(caplog):
    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _IGNORE_TERM, start_new_session=True,
            stdout=asyncio.subprocess.PIPE)
        await proc.stdout.readline()  # SIGTERM is ignored from here on
        await base.terminate_group(proc, sid="s2", term_timeout=0.3)
        return proc
    with caplog.at_level(logging.INFO, logger="pulsar.vessel"):
        proc = _run(go())
    assert proc.returncode == -signal.SIGKILL
    assert [r.getMessage().split()[0] for r in caplog.records] == [
        "vessel_stop", "vessel_stop_sigterm_timeout"]


def test_terminate_group_unbounded_final_wait_and_silent_log(caplog):
    async def go():
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", _IGNORE_TERM, start_new_session=True,
            stdout=asyncio.subprocess.PIPE)
        await proc.stdout.readline()
        await base.terminate_group(proc, sid="s3", term_timeout=0.3,
                                   kill_timeout=None, log=None)
        return proc
    with caplog.at_level(logging.DEBUG, logger="pulsar.vessel"):
        proc = _run(go())
    assert proc.returncode == -signal.SIGKILL
    assert caplog.records == []


def test_terminate_group_logs_sigkill_timeout_and_uses_signal_fn(caplog):
    sent = []

    class FakeProc:
        pid = 4242
        returncode = None

        async def wait(self):
            await asyncio.sleep(10)  # never exits

    async def go():
        await base.terminate_group(
            FakeProc(), sid="s4", term_timeout=0.05, kill_timeout=0.05,
            signal_fn=lambda pid, sig: sent.append((pid, sig)))
    with caplog.at_level(logging.INFO, logger="pulsar.vessel"):
        _run(go())
    assert sent == [(4242, signal.SIGTERM), (4242, signal.SIGKILL)]
    assert [(r.levelno, r.getMessage().split()[0]) for r in caplog.records] == [
        (logging.INFO, "vessel_stop"),
        (logging.WARNING, "vessel_stop_sigterm_timeout"),
        (logging.ERROR, "vessel_stop_sigkill_timeout")]


def test_terminate_group_is_a_noop_for_an_exited_process():
    sent = []
    proc = types.SimpleNamespace(pid=1234, returncode=0)
    _run(base.terminate_group(proc, sid="s5",
                              signal_fn=lambda pid, sig: sent.append(sig)))
    assert sent == []


class _Stream:
    def __init__(self, lines, boom=None):
        self._lines, self._boom = list(lines), boom

    async def readline(self):
        if self._lines:
            return self._lines.pop(0)
        if self._boom:
            raise self._boom
        return b""


def test_log_stderr_logs_non_empty_lines_at_level(caplog):
    stream = _Stream([b"hello\n", b"\n", b"  \n", b"bad \xff byte\n", b"last"])
    with caplog.at_level(logging.DEBUG, logger="pulsar.vessel"):
        _run(base.log_stderr(stream, "sidX"))
        _run(base.log_stderr(_Stream([b"dbg\n"]), "sidX", level=logging.DEBUG))
    msgs = [(r.levelno, r.getMessage()) for r in caplog.records]
    assert msgs[0] == (logging.WARNING, "vessel_stderr sid=sidX line=hello")
    assert msgs[1][1].startswith("vessel_stderr sid=sidX line=bad ")  # decode error survived
    assert msgs[2] == (logging.WARNING, "vessel_stderr sid=sidX line=last")
    assert msgs[3] == (logging.DEBUG, "vessel_stderr sid=sidX line=dbg")


def test_log_stderr_logs_pump_error_and_none_stream_is_a_noop(caplog):
    with caplog.at_level(logging.DEBUG, logger="pulsar.vessel"):
        _run(base.log_stderr(None, "s"))
        _run(base.log_stderr(_Stream([b"x\n"], boom=RuntimeError("boom")), "s"))
    assert [r.getMessage().split()[0] for r in caplog.records] == [
        "vessel_stderr", "vessel_stderr_pump_error"]


def test_pid_of_reads_the_session_process():
    v = base.BaseVessel()
    assert v.pid_of("nope") is None
    v._sessions = {"a": types.SimpleNamespace(process=types.SimpleNamespace(pid=77)),
                   "b": types.SimpleNamespace(process=None)}
    assert v.pid_of("a") == 77
    assert v.pid_of("b") is None


def test_bridge_pid_comes_from_the_vessels_session_process(tmp_path):
    class _V(base.BaseVessel):
        def __init__(self):
            self._sessions = {}

        async def spawn(self, **kw):
            self._sessions["s1"] = types.SimpleNamespace(
                process=types.SimpleNamespace(pid=4321))
            return "s1"

        def subscribe(self, sid):
            async def _empty():
                return
                yield
            return _empty()

    async def go():
        bv = BridgedVessel("m", tmp_path, None, vessel_cls=_V)
        await bv.start()
        pid = bv.pid
        bv._pump_task.cancel()
        return pid
    assert _run(go()) == 4321
