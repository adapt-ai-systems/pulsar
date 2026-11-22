#!/usr/bin/env python3
"""Fake `pi --mode rpc` CLI for PiRpcVessel tests.

Speaks the pi rpc wire protocol: ONE long-lived process that
reads JSONL commands on stdin and writes JSONL events + `response` acks on
stdout. Stays alive across multiple prompt/steer/abort commands (the whole
point of rpc mode vs the one-process-per-message json vessel).

Command dispatch (keyed on the prompt message body, same convention as
fake_pi.py):
    "TOOL"   -> a tool turn          ("ERROR" -> isError: true)
    "MULTI"  -> a 4-step turn (4x turn_end, one agent_end) — the shape that
               broke the json vessel's turn_end/done mapping
    "HANG"   -> turn_start then silence forever (watchdog bait; gated on
               turn_in_flight so an IDLE-then-HANG proves the guard)
    "PARALLEL" -> 49 steps of two parallel tool calls in REAL pi order
               (assistant message_end, both starts, results out of order,
               then turn_end usage), then a text step: 50 model calls
    "STEER"  -> a turn that sleeps after turn_start so an injected `steer`
               lands mid-turn; emits a queue_update when the steer arrives,
               then completes
    default  -> a text turn

Session id is minted on first run and reused on `--session <id>` resume.

FAKE_PI_RPC_STDIN_DUMP (path): append every stdin command line there, so tests
can assert exactly what the vessel wrote (prompt vs steer vs abort).
"""
from __future__ import annotations
import json
import os
import sys
import time
import uuid

_SID = None
_DUMP = os.environ.get("FAKE_PI_RPC_STDIN_DUMP")
# WEDGE prompt state: the toolCallId of the bash tool that never ends on
# its own — only an `abort_bash` command frees it (the hung-ssh shape).
_WEDGED = None


def _emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def _resp(req_id, command: str, success: bool = True, error: str | None = None) -> None:
    o = {"type": "response", "command": command, "success": success}
    if req_id is not None:
        o["id"] = req_id
    if error:
        o["error"] = error
    _emit(o)


def _usage(i: int = 1) -> dict:
    return {"input": 10 * i, "output": 2, "cacheRead": 0, "cacheWrite": 0,
            "totalTokens": 12, "cost": {"input": 0, "output": 0, "total": 0}}


def _text_turn(msg: str) -> None:
    _emit({"type": "turn_start"})
    _emit({"type": "message_end",
           "message": {"role": "assistant",
                       "content": [{"type": "text", "text": "PIRPC-OK"}]}})
    _emit({"type": "turn_end", "message": {"role": "assistant", "usage": _usage()}})
    _emit({"type": "agent_end"})


def _tool_turn(msg: str) -> None:
    err = "ERROR" in msg
    _emit({"type": "turn_start"})
    _emit({"type": "tool_execution_start", "toolCallId": "t1",
           "toolName": "bash", "args": {"command": "echo hi"}})
    _emit({"type": "tool_execution_end", "toolCallId": "t1",
           "toolName": "bash", "isError": err,
           "result": {"content": [{"type": "text",
                                   "text": "boom" if err else "ran"}],
                      "details": {}}})
    _emit({"type": "message_end",
           "message": {"role": "assistant",
                       "content": [{"type": "text",
                                    "text": "nope" if err else "did it"}]}})
    _emit({"type": "turn_end", "message": {"role": "assistant", "usage": _usage()}})
    _emit({"type": "agent_end"})


def _multi_turn() -> None:
    for i in range(4):
        _emit({"type": "turn_start"})
        _emit({"type": "tool_execution_start", "toolCallId": f"m{i}",
               "toolName": "bash", "args": {"command": f"step {i}"}})
        _emit({"type": "tool_execution_end", "toolCallId": f"m{i}",
               "toolName": "bash", "isError": False,
               "result": {"content": [{"type": "text", "text": f"out {i}"}],
                          "details": {}}})
        _emit({"type": "message_end",
               "message": {"role": "assistant",
                           "content": [{"type": "text",
                                        "text": f"step {i} narration"}]}})
        _emit({"type": "turn_end",
               "message": {"role": "assistant", "usage": _usage(i + 1)}})
    _emit({"type": "agent_end"})


def _parallel_turn() -> None:
    """Real pi 0.73.1 order for a parallel batch (live R1 transcripts
    2026-10-22): the assistant message ends, both tools start, the faster
    one ends first, and the step's usage comes on turn_end after ALL."""
    for i in range(49):
        a, b = f"p{i}a", f"p{i}b"
        _emit({"type": "turn_start"})
        _emit({"type": "message_update",
               "assistantMessageEvent": {"type": "text_delta", "delta": f"step {i} "}})
        _emit({"type": "message_end",
               "message": {"role": "assistant",
                           "content": [{"type": "text", "text": f"step {i}"},
                                       {"type": "toolCall", "id": a, "name": "bash"},
                                       {"type": "toolCall", "id": b, "name": "bash"}]}})
        for tid in (a, b):
            _emit({"type": "tool_execution_start", "toolCallId": tid,
                   "toolName": "bash", "args": {"command": f"echo {tid}"}})
        for tid in (b, a):
            _emit({"type": "tool_execution_end", "toolCallId": tid,
                   "toolName": "bash", "isError": False,
                   "result": {"content": [{"type": "text", "text": tid}],
                              "details": {}}})
        _emit({"type": "turn_end",
               "message": {"role": "assistant", "usage": _usage(i + 1)}})
    _emit({"type": "turn_start"})
    _emit({"type": "message_update",
           "assistantMessageEvent": {"type": "text_delta", "delta": "PIRPC-OK"}})
    _emit({"type": "message_end",
           "message": {"role": "assistant",
                       "content": [{"type": "text", "text": "PIRPC-OK"}]}})
    _emit({"type": "turn_end", "message": {"role": "assistant", "usage": _usage(50)}})
    _emit({"type": "agent_end"})


_ERR429 = ('429 {"type":"error","error":{"type":"rate_limit_error","code":"1308",'
           '"message":"[1308][Usage limit reached for 5 hour. Your limit will '
           'reset at 2026-10-10 17:27:45][fake]"}}')


def _error_attempt(err: str) -> None:
    """One failed LLM call, real pi 0.73.1 shape: empty assistant message
    with stopReason=error, then agent_end."""
    msg = {"role": "assistant", "content": [], "provider": "zai",
           "model": "glm-5.3", "usage": _usage(0), "stopReason": "error",
           "errorMessage": err}
    _emit({"type": "turn_start"})
    _emit({"type": "message_end", "message": msg})
    _emit({"type": "turn_end", "message": msg})
    _emit({"type": "agent_end", "messages": [msg]})


def _retry_turn(msg: str) -> None:
    """RETRY429: every attempt 429s, pi auto-retries 3x then gives up.
    RETRYOK: the first attempt 429s, the retry succeeds.
    ERR400: one non-retryable error, no auto_retry records at all."""
    if "ERR400" in msg:
        _error_attempt("400 prompt too long")
        return
    attempts = 1 if "RETRYOK" in msg else 4
    for i in range(attempts):
        _error_attempt(_ERR429)
        if i < 3:
            _emit({"type": "auto_retry_start", "attempt": i + 1,
                   "maxAttempts": 3, "delayMs": 10, "errorMessage": _ERR429})
            time.sleep(0.01)
            _emit({"type": "agent_start"})
    if "RETRYOK" in msg:
        _text_turn(msg)
        _emit({"type": "auto_retry_end", "success": True, "attempt": 1})
    else:
        _emit({"type": "auto_retry_end", "success": False, "attempt": 3,
               "finalError": _ERR429})


def _errpartial_turn() -> None:
    """ERRPARTIAL: a provider error AFTER text streamed — the message_end
    carries BOTH content text and stopReason=error (final review 0d64351c
    finding 1). The text must reach the durable log; only the runner_error
    is held provisionally."""
    msg = {"role": "assistant",
           "content": [{"type": "text",
                        "text": "PARTIAL ANSWER BEFORE PROVIDER ERROR"}],
           "provider": "zai", "model": "glm-5.3", "usage": _usage(12),
           "stopReason": "error", "errorMessage": "content_filter"}
    _emit({"type": "turn_start"})
    _emit({"type": "message_update",
           "assistantMessageEvent": {"type": "text_delta",
                                     "delta": "PARTIAL ANSWER BEFORE "}})
    _emit({"type": "message_end", "message": msg})
    _emit({"type": "turn_end", "message": msg})
    _emit({"type": "agent_end", "messages": [msg]})


def _run_prompt(msg: str) -> None:
    global _WEDGED
    _WEDGED = None
    if "RETRY" in msg or "ERR400" in msg:
        _retry_turn(msg)
    elif "MULTI" in msg:
        _multi_turn()
    elif "PARALLEL" in msg:
        _parallel_turn()
    elif "TOOL" in msg:
        _tool_turn(msg)
    elif "HANG" in msg:
        # Emit turn_start then return to the stdin loop WITHOUT agent_end —
        # the turn stays in flight, the process stays alive reading commands
        # (so an `abort` can be read and dumped). Only the watchdog ends this.
        _emit({"type": "turn_start"})
    elif "ERRPARTIAL" in msg:
        _errpartial_turn()
    elif "WEDGE" in msg:
        # The hung-tool shape: tool_execution_start, then silence forever.
        # Steers are accepted (queue_update + ack) but never DELIVERED — pi
        # only delivers them before the next LLM call, which never comes.
        # An `abort_bash` frees the tool: error result, then the agent loop
        # continues (steers delivered), one reply, agent_end.
        _WEDGED = "w1"
        _emit({"type": "turn_start"})
        _emit({"type": "tool_execution_start", "toolCallId": "w1",
               "toolName": "bash",
               "args": {"command": "ssh box 'nohup start-server &'"}})
    else:
        _text_turn(msg)


def _session_dir(args: list[str]) -> str | None:
    for i, a in enumerate(args):
        if a == "--session-dir" and i + 1 < len(args):
            return args[i + 1]
    return None


def main() -> int:
    global _SID
    args = sys.argv[1:]
    dump = os.environ.get("FAKE_PI_RPC_ARGV_DUMP")
    if dump:
        with open(dump, "w") as f:
            f.write(json.dumps(args))
    sid_in = None
    for i, a in enumerate(args):
        if a == "--session" and i + 1 < len(args):
            sid_in = args[i + 1]
    # Real pi treats `--session <unknown-id>` as FATAL, not as a create:
    # "No session found matching '<id>'" + exit(1) (dist/main.js:196). The fake
    # accepted any id, which is exactly why it green-lit a spawn command that
    # killed every fresh pi-glm pulse. Mirror the real behaviour.
    if sid_in and not any(
            f.startswith(sid_in) or sid_in in f
            for f in os.listdir(_session_dir(args) or ".")):
        sys.stderr.write(f"No session found matching '{sid_in}'\n")
        return 1
    _SID = sid_in or str(uuid.uuid4())
    _emit({"type": "session", "version": 3, "id": _SID, "cwd": "."})
    for raw in sys.stdin:
        line = raw.rstrip("\n")
        if not line:
            continue
        if _DUMP:
            with open(_DUMP, "a") as f:
                f.write(line + "\n")
        try:
            cmd = json.loads(line)
        except json.JSONDecodeError:
            continue
        ctype = cmd.get("type")
        req_id = cmd.get("id")
        if ctype == "prompt":
            _resp(req_id, "prompt")
            _run_prompt(cmd.get("message", ""))
        elif ctype == "steer":
            # Real pi emits queue_update FIRST, then the response ack AFTER
            # (verified live 2026-08-23: queue_update arrives while the tool
            # runs, response lands right after). Matching the ordering matters
            # because inject() awaits the response — if the fake acked before
            # the event, a test could pass while real pi behaves differently.
            msg = cmd.get("message", "")
            if "REJECT" in msg:
                # Simulate a rejected steer (e.g. invalid message). Real pi
                # returns success:false with an error string. No queue_update.
                _resp(req_id, "steer", success=False,
                      error="steer rejected: invalid message")
            else:
                _emit({"type": "queue_update", "steering": [msg],
                       "followUp": []})
                _resp(req_id, "steer")
        elif ctype == "abort_bash":
            # Real pi aborts the RUNNING bash command; the tool returns an
            # error result and the agent loop CONTINUES (queued steers are
            # delivered before the next LLM call). Broken mode simulates a
            # pi with nothing running (or no abort_bash support): rejected.
            global _WEDGED
            if os.environ.get("FAKE_PI_RPC_ABORT_BASH_BROKEN"):
                _resp(req_id, "abort_bash", success=False,
                      error="no bash command running")
            elif _WEDGED:
                tid = _WEDGED
                _WEDGED = None
                _emit({"type": "tool_execution_end", "toolCallId": tid,
                       "toolName": "bash", "isError": True,
                       "result": {"content": [{"type": "text",
                                               "text": "command aborted"}],
                                  "details": {}}})
                _emit({"type": "message_end",
                       "message": {"role": "assistant",
                                   "content": [{"type": "text",
                                                "text": "PIRPC-WEDGED-FREED"}]}})
                _emit({"type": "turn_end",
                       "message": {"role": "assistant", "usage": _usage()}})
                _emit({"type": "agent_end"})
                _resp(req_id, "abort_bash")
            else:
                _resp(req_id, "abort_bash", success=False,
                      error="no bash command running")
        elif ctype == "abort":
            # Real pi emits tool_execution_end{isError} + turn_end +
            # turn_start + message_end{stopReason:aborted} + turn_end +
            # agent_end, THEN the response ack (verified live 2026-08-23).
            # agent_end is the terminal record classify_record maps to
            # done{result}. The response comes LAST.
            if os.environ.get("FAKE_PI_RPC_ABORT_BROKEN"):
                # Broken-abort mode: the process dies WITHOUT emitting
                # agent_end (crash, OOM, external kill, or a pi build whose
                # abort is broken). The reader's finally MUST emit done{exited}
                # or the runner hangs forever waiting for a terminal done.
                sys.stdout.flush()
                sys.exit(1)
            _emit({"type": "agent_end"})
            _resp(req_id, "abort")
        elif ctype == "follow_up":
            _resp(req_id, "follow_up")
        else:
            _resp(req_id, "unknown", success=False, error=f"unknown {ctype!r}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
