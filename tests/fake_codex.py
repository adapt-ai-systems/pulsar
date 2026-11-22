#!/usr/bin/env python3
"""A fake `codex` CLI standing in for the real binary in vessel tests.

It speaks just enough of the ``codex app-server --listen stdio://`` JSON-RPC
contract that :class:`CodexVessel` can be exercised end-to-end WITHOUT spawning
the real CLI (which would burn tokens):

- Reads JSON-RPC frames (one per line) from stdin.
- Answers ``initialize`` and (on the ``initialized`` notification) is ready.
- On ``thread/start`` returns a thread id (taken from argv/env so the resume test
  can assert continuity). On the FIRST ``turn/start`` it streams a realistic
  turn as notifications:
      item/started (command_execution)   -> tool_call
      item/completed (command_execution) -> tool_result
      item/completed (agent_message)     -> assistant_text
      thread/tokenUsage/updated          -> usage
      turn/completed                     -> done(result)
- On ``thread/resume`` it accepts and, on the next ``turn/start``, streams a
  shorter ack turn (agent_message + usage + turn/completed) so the resume
  relaunch produces a distinguishable sequence.
- Exits (EOF-clean) when stdin closes.

The thread id is derived from the argv-passed model or a fixed value so the
resume test can assert the vessel captured and replayed the same id.
"""

import json
import os
import sys

_THREAD_ID = "fake-thread-0001"


def emit(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj) + "\n")
    sys.stdout.flush()


def respond(rpc_id, result: dict) -> None:
    emit({"jsonrpc": "2.0", "id": rpc_id, "result": result})


def notify(method: str, params: dict) -> None:
    emit({"jsonrpc": "2.0", "method": method, "params": params})


def first_turn(turn_id: str) -> None:
    notify(
        "item/started",
        {
            "turnId": turn_id,
            "item": {
                "id": "item_cmd_1",
                "type": "commandExecution",
                "command": "ls scraper/",
            },
        },
    )
    notify(
        "item/completed",
        {
            "turnId": turn_id,
            "item": {
                "id": "item_cmd_1",
                "type": "commandExecution",
                "command": "ls scraper/",
                "aggregatedOutput": "reviews.py\nscrape.py",
                "exitCode": 0,
            },
        },
    )
    notify(
        "item/completed",
        {
            "turnId": turn_id,
            "item": {
                "id": "item_msg_1",
                "type": "agentMessage",
                "text": "Two files: reviews.py and scrape.py.",
            },
        },
    )
    notify(
        "thread/tokenUsage/updated",
        {
            "turnId": turn_id,
            "tokenUsage": {
                "last": {
                    "inputTokens": 123,
                    "cachedInputTokens": 100,
                    "outputTokens": 14,
                    "reasoningOutputTokens": 6,
                }
            },
        },
    )
    notify("turn/completed", {"turn": {"id": turn_id, "status": "completed"}})


def followup_turn(turn_id: str) -> None:
    notify(
        "item/completed",
        {
            "turnId": turn_id,
            "item": {"id": "item_msg_2", "type": "agentMessage", "text": "Resumed. Understood."},
        },
    )
    notify(
        "thread/tokenUsage/updated",
        {
            "turnId": turn_id,
            "tokenUsage": {"last": {"inputTokens": 30, "outputTokens": 4}},
        },
    )
    notify("turn/completed", {"turn": {"id": turn_id, "status": "completed"}})


def main() -> int:
    turn_counter = 0
    resumed = False
    while True:
        line = sys.stdin.readline()
        if line == "":  # EOF
            break
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, dict):
            continue
        method = msg.get("method")
        rpc_id = msg.get("id")

        if method == "initialize":
            respond(rpc_id, {"userAgent": "fake-codex/0.130.0"})
        elif method == "initialized":
            pass  # notification, no response
        elif method == "thread/start":
            respond(rpc_id, {"thread": {"id": _THREAD_ID}})
        elif method == "thread/resume":
            resumed = True
            respond(rpc_id, {"thread": {"id": _THREAD_ID}})
        elif method == "turn/start":
            turn_id = f"turn_{turn_counter}"
            turn_counter += 1
            # turn/start responds with the turn, THEN the notifications stream.
            respond(rpc_id, {"turn": {"id": turn_id}})
            if turn_counter == 1 and os.environ.get("FAKE_CODEX_RECONNECT"):
                for attempt in (1, 2):
                    notify("error", {
                        "threadId": _THREAD_ID, "turnId": turn_id,
                        "willRetry": True,
                        "error": {"message": f"Reconnecting... {attempt}/5"},
                    })
            if resumed or turn_counter > 1:
                followup_turn(turn_id)
            else:
                first_turn(turn_id)
        elif method == "turn/steer":
            respond(rpc_id, {})
        elif rpc_id is not None:
            # Any other request: respond so the vessel never hangs.
            respond(rpc_id, {})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
