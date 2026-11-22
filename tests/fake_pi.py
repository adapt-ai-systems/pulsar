#!/usr/bin/env python3
"""Fake `pi` CLI for PiVessel tests.

Speaks the LIVE pi 0.73.1 `--mode json` wire schema (verified from bench
transcripts): toolCallId on tool_execution_start/end, usage on
turn_end.message.usage, agent_end at process end. One short-lived process per
invocation, keyed by the final positional (the message body):
    "TOOL"  -> a tool turn      ("ERROR" -> tool turn with isError: true)
    "MULTI" -> a realistic multi-STEP turn (4x turn_end, one agent_end)
    "NOAGENTEND" -> a clean exit with no agent_end at all
    "HANG"  -> session header then silence forever (watchdog bait)
    "COMPACTRECOVER" -> overflow error, compaction willRetry:true, retry
                         answer (P1: no runner_error, one done)
    "COMPACTFAIL" -> overflow error, compaction willRetry:false, exit
    "OVFERR" -> a plain provider error, no recovery
    "ERRPARTIAL" -> a provider error AFTER text streamed: message_end
                     carries BOTH content and stopReason=error (final review
                     0d64351c finding 1 — the text must survive)
    default -> a text turn
The session id is minted on first run and reused on `--session <id>` resume so
the vessel's cli_session_id stays stable across messages.

FAKE_PI_ARGV_DUMP (a path): dump the full argv there on startup, so tests can
assert exactly what the vessel passed (--session / --thread-id / --model).
"""
from __future__ import annotations
import json
import os
import sys
import uuid


def main() -> int:
    dump = os.environ.get("FAKE_PI_ARGV_DUMP")
    if dump:
        with open(dump, "w") as f:
            f.write(json.dumps(sys.argv[1:]))
    edump = os.environ.get("FAKE_PI_ENV_DUMP")
    if edump:
        with open(edump, "w") as f:
            f.write(json.dumps({k: v for k, v in os.environ.items()
                                if k in ("PI_HOME", "PATH")}))
    args = sys.argv[1:]
    sid_in = None
    text = ""
    for i, a in enumerate(args):
        if a == "--session" and i + 1 < len(args):
            sid_in = args[i + 1]
    for a in args:
        if not a.startswith("-"):
            text = a
    sid = sid_in or str(uuid.uuid4())
    if "HANG" in text:
        # Emit the session header, then go silent forever with the process
        # still alive — a wedged tool call (the `find /` shape). Only the
        # vessel watchdog can end this turn.
        print(json.dumps({"type": "session", "version": 3, "id": sid,
                          "cwd": "."}), flush=True)
        import time
        time.sleep(300)
        return 0
    if "SLOW" in text:
        import time
        time.sleep(5)
    if "CRASH" in text:
        # Abnormal exit: emit only the session record, then die nonzero. Used
        # to prove PiVessel clears killed_by between turns so a genuine crash
        # after a prior interrupt still emits done{exited}.
        print(json.dumps({"type": "session", "version": 3, "id": sid, "cwd": "."}))
        return 7
    out = [{"type": "session", "version": 3, "id": sid, "cwd": "."}]
    if "MULTI" in text:
        # What a REAL turn looks like: pi emits one turn_end per agent STEP
        # (it increments turnIndex on each), so a single user message produces
        # a whole run of them and only ONE agent_end at the very end. The
        # single-turn_end shape below is the degenerate case that let the
        # step/turn confusion ship (2026-08-23).
        for i in range(4):
            out += [
                {"type": "turn_start"},
                {"type": "tool_execution_start", "toolCallId": f"m{i}",
                 "toolName": "bash", "args": {"command": f"step {i}"}},
                {"type": "tool_execution_end", "toolCallId": f"m{i}",
                 "toolName": "bash", "isError": False,
                 "result": {"content": [{"type": "text", "text": f"out {i}"}],
                            "details": {}}},
                {"type": "message_end",
                 "message": {"role": "assistant",
                             "content": [{"type": "text",
                                          "text": f"step {i} narration"}]}},
                {"type": "turn_end",
                 "message": {"role": "assistant",
                             "usage": {"input": 100 * (i + 1), "output": 20,
                                       "cacheRead": 0, "cacheWrite": 0,
                                       "totalTokens": 120,
                                       "cost": {"input": 0, "output": 0,
                                                "total": 0}}}},
            ]
    elif "NOAGENTEND" in text:
        # Clean exit that never emits agent_end — the process going away is
        # still the turn boundary, so send() must not hang.
        out += [
            {"type": "turn_start"},
            {"type": "message_end",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "no agent_end"}]}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 10, "output": 2, "cacheRead": 0,
                                   "cacheWrite": 0, "totalTokens": 12,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
        ]
        for obj in out:
            sys.stdout.write(json.dumps(obj) + "\n")
        return 0
    elif "COMPACTRECOVER" in text:
        # Context overflow, recovered by compaction: empty assistant message
        # stops on error (the provider's 400), pi runs compaction with
        # willRetry:true, and the RETRY produces the answer. The P1 hold must
        # swallow the provisional provider error — no runner_error, one done.
        out += [
            {"type": "turn_start"},
            {"type": "message_end",
             "message": {"role": "assistant", "stopReason": "error",
                         "content": [],
                         "errorMessage": "400 maximum context length is 1000 "
                                         "tokens. Your messages resulted in "
                                         "2000 tokens.",
                         "provider": "fake", "model": "fake-gl"}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 0, "output": 0, "cacheRead": 0,
                                   "cacheWrite": 0, "totalTokens": 0,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
            {"type": "agent_end",
             "messages": [
                 {"role": "user",
                  "content": [{"type": "text", "text": "go"}]},
                 {"role": "assistant", "stopReason": "error", "content": [],
                  "errorMessage": "400 maximum context length is 1000 "
                                  "tokens."}]},
            {"type": "compaction_start", "reason": "overflow"},
            {"type": "compaction_end", "reason": "overflow",
             "willRetry": True},
            {"type": "turn_start"},
            {"type": "message_end",
             "message": {"role": "assistant",
                         "content": [{"type": "text",
                                      "text": "RECOVERED_AFTER_COMPACTION"}]}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 1004, "output": 20,
                                   "cacheRead": 0, "cacheWrite": 0,
                                   "totalTokens": 1024,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
        ]
        # The normal agent_end from the tail closes the turn: exactly one
        # done, after the recovered text, with no runner_error.
    elif "COMPACTFAIL" in text:
        # Same overflow shape, but compaction does NOT retry (willRetry:
        # false): the held provider error must surface as runner_error just
        # BEFORE the done compaction_end delivers. No further records — the
        # process exits.
        out += [
            {"type": "turn_start"},
            {"type": "message_end",
             "message": {"role": "assistant", "stopReason": "error",
                         "content": [],
                         "errorMessage": "400 maximum context length is 1000 "
                                         "tokens. Your messages resulted in "
                                         "2000 tokens.",
                         "provider": "fake", "model": "fake-gl"}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 0, "output": 0, "cacheRead": 0,
                                   "cacheWrite": 0, "totalTokens": 0,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
            {"type": "agent_end",
             "messages": [
                 {"role": "user",
                  "content": [{"type": "text", "text": "go"}]},
                 {"role": "assistant", "stopReason": "error", "content": [],
                  "errorMessage": "400 maximum context length is 1000 "
                                  "tokens."}]},
            {"type": "compaction_start", "reason": "overflow"},
            {"type": "compaction_end", "reason": "overflow",
             "willRetry": False},
        ]
        for obj in out:
            sys.stdout.write(json.dumps(obj) + "\n")
        return 0
    elif "OVFERR" in text:
        # A plain provider error with no recovery at all: the held error is
        # flushed at EOF as runner_error just before the done.
        out += [
            {"type": "turn_start"},
            {"type": "message_end",
             "message": {"role": "assistant", "stopReason": "error",
                         "content": [],
                         "errorMessage": "400 maximum context length is 1000 "
                                         "tokens. Your messages resulted in "
                                         "2000 tokens.",
                         "provider": "fake", "model": "fake-gl"}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 0, "output": 0, "cacheRead": 0,
                                   "cacheWrite": 0, "totalTokens": 0,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
            {"type": "agent_end",
             "messages": [
                 {"role": "user",
                  "content": [{"type": "text", "text": "go"}]},
                 {"role": "assistant", "stopReason": "error", "content": [],
                  "errorMessage": "400 maximum context length is 1000 "
                                  "tokens."}]},
        ]
        for obj in out:
            sys.stdout.write(json.dumps(obj) + "\n")
        return 0
    elif "ERRPARTIAL" in text:
        # A provider error AFTER content streamed (final review 0d64351c
        # finding 1): the message_end carries BOTH text and stopReason=error.
        # That text must still reach the durable log — only the runner_error
        # is held provisionally, then flushed just before the done.
        out += [
            {"type": "turn_start"},
            {"type": "message_update",
             "assistantMessageEvent": {"type": "text_delta",
                                       "delta": "PARTIAL ANSWER BEFORE "}},
            {"type": "message_end",
             "message": {"role": "assistant", "stopReason": "error",
                         "content": [{"type": "text",
                                      "text": "PARTIAL ANSWER BEFORE "
                                              "PROVIDER ERROR"}],
                         "errorMessage": "content_filter: the reply was cut "
                                         "by the provider filter",
                         "provider": "fake", "model": "fake-gl"}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 5, "output": 7, "cacheRead": 0,
                                   "cacheWrite": 0, "totalTokens": 12,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
            {"type": "agent_end",
             "messages": [
                 {"role": "user",
                  "content": [{"type": "text", "text": "go"}]},
                 {"role": "assistant", "stopReason": "error",
                  "content": [{"type": "text",
                               "text": "PARTIAL ANSWER BEFORE PROVIDER "
                                       "ERROR"}],
                  "errorMessage": "content_filter"}]},
        ]
        for obj in out:
            sys.stdout.write(json.dumps(obj) + "\n")
        return 0
    elif "TOOL" in text:
        err = "ERROR" in text
        out += [
            {"type": "turn_start"},
            {"type": "tool_execution_start", "toolCallId": "t1",
             "toolName": "bash", "args": {"command": "echo hi"}},
            {"type": "tool_execution_end", "toolCallId": "t1",
             "toolName": "bash", "isError": err,
             "result": {"content": [{"type": "text",
                                     "text": "boom" if err else "ran"}],
                        "details": {}}},
            {"type": "message_end",
             "message": {"role": "assistant",
                         "content": [{"type": "text",
                                      "text": "did it" if not err else "nope"}]}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 100, "output": 20,
                                   "cacheRead": 0, "cacheWrite": 0,
                                   "totalTokens": 120,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
        ]
    else:
        out += [
            {"type": "turn_start"},
            {"type": "message_end",
             "message": {"role": "assistant",
                         "content": [{"type": "text", "text": "PIRPC-OK"}]}},
            {"type": "turn_end",
             "message": {"role": "assistant",
                         "usage": {"input": 10, "output": 2,
                                   "cacheRead": 0, "cacheWrite": 0,
                                   "totalTokens": 12,
                                   "cost": {"input": 0, "output": 0,
                                            "total": 0}}}},
        ]
    out.append({"type": "agent_end"})
    for obj in out:
        sys.stdout.write(json.dumps(obj) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
