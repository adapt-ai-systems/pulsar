"""pulsar-msg: send a message to another pulse without hand-building JSON in bash.

A curl -d '{"text": "..."}' breaks on the first apostrophe, parenthesis or
quote in the text (two GLM Full failures on 2026-10-24); this CLI takes the
text as argv or stdin and encodes it itself. Stdlib only.
"""
from __future__ import annotations

import os
import sys

from .client import TransportError, send
from .common import cwd_pulse_id, daemon_address, err, out, text_of, write

USAGE = """\
pulsar-msg — message another pulse (wakes it if stopped).

  pulsar-msg <target_pulse_id> "the text"
  pulsar-msg <target_pulse_id> < report.md        # text from stdin
  pulsar-msg --from <my_id> <target_pulse_id> "..."

The sender id (from_pulse_id) is taken from the cwd (pulses run in
<data>/pulses/<id>/cli), then $PULSAR_PULSE_ID, or --from. The target sees
"[message from 'Your title' (pulse <your id>)] the text". Find ids with
pulsar-roster --grep <title>. Any apostrophes, quotes, parentheses or
newlines in the text are fine: the JSON is built here, not in your shell.
"""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    sender = ""
    if args[:1] == ["--from"]:
        sender = args[1] if len(args) > 1 else ""
        del args[:2]
    if not args or args[0] in ("-h", "--help"):
        write(sys.stdout, USAGE)
        return 0 if args and args[0] in ("-h", "--help") else 2
    target = args.pop(0)
    text = " ".join(args) if args else sys.stdin.read()
    if not text.strip():
        err("pulsar-msg: empty message (pass the text as arguments or on stdin)")
        return 2
    sender = sender or cwd_pulse_id() or os.environ.get("PULSAR_PULSE_ID", "")
    if not sender:
        err("pulsar-msg: can't tell which pulse you are; pass --from <id>")
        return 1
    if target == sender:
        err("pulsar-msg: target is your own pulse id")
        return 2
    try:
        reply = send("POST", f"/pulses/{target}/messages",
                     {"text": text, "from_pulse_id": sender}, base=daemon_address())
    except TransportError:
        err(f"pulsar-msg: cannot reach the daemon at {daemon_address()}")
        return 1
    if not 200 <= reply.status < 300:
        err(f"pulsar-msg: daemon returned {reply.status}: {text_of(reply.body)}")
        return 1
    # 202 = the target is mid-turn; the daemon queued the message and
    # delivers it at the next turn boundary (first live use, 2026-10-24).
    state = "queued for" if reply.status == 202 else "sent to"
    out(f"{state} {target} ({len(text)} chars)")
    return 0
