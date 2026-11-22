"""pulsar-ask: ask the user a question that stays pinned in their pane. Stdlib only.

Ported from the bash script; the output is pinned by tests/test_cli_ask.py.
Any 2xx reply counts as posted. A reply that is not the expected JSON prints a traceback
and goes on, exactly as the bash script's embedded python did.
"""
from __future__ import annotations

import json
import os
import sys
import traceback

from .client import TransportError, send
from .common import cwd_pulse_id, daemon_address, err, logical_pwd, out, text_of, write

USAGE = """\
pulsar-ask — ask the user a question that stays pinned in your pane until they
answer. Then END YOUR TURN; the answer arrives as a message.

  pulsar-ask "<question>"                                     free-text answer
  pulsar-ask "<question>" --choice A --choice B [--default A]  tap-to-answer buttons
  pulsar-ask "<question>" --context "<one line: why / what happens next>"
  pulsar-ask --list                                           your open questions
"""


def usage(code: int) -> int:
    write(sys.stdout, USAGE)
    return code


def _list(pulse: str) -> int:
    try:
        reply = send("GET", f"/questions?pulse_id={pulse}", base=daemon_address(), timeout=None,
                     headers={})
        d = json.loads(reply.body)
        qs = d.get("value", []) if isinstance(d, dict) else []
        if not qs:
            print("no open questions")
        for q in qs:
            print(q["id"], q["created_at"], q["text"], sep="  ")
    except Exception:  # the bash `| python3 -c` printed the traceback and still exited 0
        traceback.print_exc()
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return usage(2)
    if args[0] in ("-h", "--help"):
        return usage(0)
    pulse = os.environ.get("PULSE_ID") or cwd_pulse_id()
    if not pulse:
        err(f"pulsar-ask: cannot derive pulse id from cwd ({logical_pwd()}). "
            "Run from a pulse's cli dir or set PULSE_ID.")
        return 2
    if args[0] == "--list":
        return _list(pulse)

    text, rest = args[0], args[1:]
    choices, default, context = [], "", ""
    while rest:
        flag = rest[0]
        if flag in ("--choice", "--default", "--context"):
            if len(rest) < 2:
                err(f"{flag} needs a value")
                return 2
            if flag == "--choice":
                choices.append(rest[1])
            elif flag == "--default":
                default = rest[1]
            else:
                context = rest[1]
            del rest[:2]
        else:
            err(f"unknown arg '{flag}'")
            return usage(1)

    body = {"pulse_id": pulse, "text": text, "choices": choices}
    if default:
        body["default_choice"] = default
    if context:
        body["context"] = context
    try:
        reply = send("POST", "/questions", body, base=daemon_address(), timeout=None)
    except TransportError:
        err(f"pulsar-ask: cannot reach the daemon at {daemon_address()}")
        return 1
    answer = text_of(reply.body)
    if not 200 <= reply.status < 300:
        err(f"pulsar-ask: daemon returned {reply.status}:")
        err(answer)
        return 1
    try:
        d = json.loads(answer)
        question = str(d.get("value", d).get("id", "")).rstrip("\n")
    except Exception:
        traceback.print_exc()
        question = ""
    out(f"question {question} posted — it stays in your pane until the user answers.")
    out(f'END YOUR TURN now; the answer arrives as a message starting "[The user answered your question {question}]".')
    return 0
