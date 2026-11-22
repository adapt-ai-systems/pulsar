#!/usr/bin/env python3
"""A fake `claude` CLI standing in for the real binary in vessel tests.

It speaks just enough of the stream-json contract that ``ClaudeVessel`` can be
exercised end-to-end WITHOUT spawning the real CLI (which would burn tokens):

- Reads stream-json user messages from stdin (one NDJSON object per line).
- On the FIRST user message, emits a realistic conversation on stdout:
    system/init line (carries session_id)  ->
    assistant text                          ->
    assistant tool_use (Bash, WITH description) ->
    user tool_result (Bash output)          ->
    assistant text + usage block            ->
    result line (turn boundary, carries session_id)
- On any SUBSEQUENT user message (used by the resume test), emits a short
  acknowledgement turn (assistant text + usage + result) so the resume relaunch
  produces an observable, distinguishable event sequence.
- Exits (EOF-clean) when stdin closes.

The session id is taken from the ``--session-id`` argv value if present, so the
resume test can assert the vessel captured and replayed the same id.
"""

import json
import os
import sys
import time


def _sid_from_argv() -> str:
    argv = sys.argv[1:]
    for i, a in enumerate(argv):
        if a == "--session-id" and i + 1 < len(argv):
            return argv[i + 1]
        if a == "--resume" and i + 1 < len(argv):
            return argv[i + 1]
    return "fake-session-0000"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def first_turn(sid: str) -> None:
    emit({"type": "system", "subtype": "init", "session_id": sid})
    emit(
        {
            "type": "assistant",
            "session_id": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "On it — I'll check the file."}
                ],
                "usage": {"input_tokens": 10, "output_tokens": 5},
            },
        }
    )
    emit(
        {
            "type": "assistant",
            "session_id": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {
                        "type": "tool_use",
                        "id": "toolu_bash_1",
                        "name": "Bash",
                        # The description field Pulsar's UI renders.
                        "description": "List the source directory",
                        "input": {"command": "ls scraper/"},
                    }
                ],
                "usage": {"input_tokens": 12, "output_tokens": 8},
            },
        }
    )
    emit(
        {
            "type": "user",
            "session_id": sid,
            "message": {
                "role": "user",
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "toolu_bash_1",
                        "content": [
                            {"type": "text", "text": "reviews.py\nscrape.py"}
                        ],
                        "is_error": False,
                    }
                ],
            },
        }
    )
    if os.environ.get("FAKE_CLAUDE_DELTAS"):
        # Streaming mode (--include-partial-messages shape): text deltas for
        # the closing reply, held open long enough that the runner's throttled
        # partial.json write is observable from outside. Env-gated so the
        # timing of every other dependent test is untouched.
        for piece in ("Two files: ", "reviews.py and scrape.py."):
            emit(
                {
                    "type": "stream_event",
                    "session_id": sid,
                    "event": {
                        "type": "content_block_delta",
                        "index": 0,
                        "delta": {"type": "text_delta", "text": piece},
                    },
                }
            )
        time.sleep(1.2)
    emit(
        {
            "type": "assistant",
            "session_id": sid,
            "message": {
                "role": "assistant",
                "content": [
                    {"type": "text", "text": "Two files: reviews.py and scrape.py."}
                ],
                "usage": {
                    "input_tokens": 20,
                    "cache_read_input_tokens": 100,
                    "cache_creation_input_tokens": 3,
                    "output_tokens": 14,
                },
            },
        }
    )
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid})


def followup_turn(sid: str) -> None:
    emit(
        {
            "type": "assistant",
            "session_id": sid,
            "message": {
                "role": "assistant",
                "content": [{"type": "text", "text": "Resumed. Understood."}],
                "usage": {"input_tokens": 30, "output_tokens": 4},
            },
        }
    )
    emit({"type": "result", "subtype": "success", "is_error": False, "session_id": sid})


def _is_resume() -> bool:
    return "--resume" in sys.argv[1:]


def main() -> int:
    sid = _sid_from_argv()
    # A resumed launch is a distinct process (turn counter resets per process),
    # so branch on the --resume flag to emit the shorter ack turn — that lets the
    # resume test assert a sequence distinguishable from a fresh spawn.
    resumed = _is_resume()
    turn = 0
    # readline() (not `for line in sys.stdin`) so we react to each message as it
    # arrives — iterating the file object read-ahead-buffers and would stall the
    # first turn until more stdin bytes appear.
    while True:
        line = sys.stdin.readline()
        if line == "":  # EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            json.loads(line)
        except json.JSONDecodeError:
            # A malformed input line: ignore, matching the real CLI's tolerance.
            continue
        if resumed or turn > 0:
            followup_turn(sid)
        else:
            first_turn(sid)
        turn += 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
