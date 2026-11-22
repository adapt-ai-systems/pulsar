"""pulsar-roster: one line per pulse, instead of the 13.8KB /pulses JSON. Stdlib only.

Ported from the bash script; the output is pinned by tests/test_cli_roster.py.
Any 2xx reply is read (curl -f failed only for >= 400).
"""
from __future__ import annotations

import json
import re
import sys

from .client import TransportError, send
from .common import daemon_address, err, write

USAGE = """\
pulsar-roster — one line per pulse, instead of pulling the 13.8KB /pulses
JSON into your context.

  pulsar-roster [--running] [--grep REGEX] [--json] [--all]

    --running   only pulses whose state != stopped
    --grep RE   only pulses whose title matches RE (case-insensitive)
    --json      compact array of {id,state,vessel,model,title[,role,lead_id]} objects
    --all       include archived pulses (default hides them)
"""


def usage(code: int) -> int:
    write(sys.stdout, USAGE)
    return code


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    running, grep_re, as_json, want_all = False, "", False, False
    while args:
        flag = args.pop(0)
        if flag in ("-h", "--help", "help"):
            return usage(0)
        elif flag == "--running":
            running = True
        elif flag == "--grep":
            if not args:
                err("pulsar-roster: --grep needs a regex")
                return 2
            grep_re = args.pop(0)
        elif flag == "--json":
            as_json = True
        elif flag == "--all":
            want_all = True
        else:
            err(f"pulsar-roster: unknown flag '{flag}'")
            return usage(1)

    try:
        reply = send("GET", "/pulses", base=daemon_address(), timeout=None, headers={})
        if reply.status >= 400:
            raise TransportError(OSError(f"HTTP {reply.status}"))
    except TransportError:
        err(f"pulsar-roster: cannot reach the daemon at {daemon_address()}")
        return 1

    try:
        d = json.loads(reply.body.decode("utf-8", "replace"))
    except Exception as e:
        err(f"pulsar-roster: bad response: {e}")
        return 1

    pulses = d.get("value", d) if isinstance(d, dict) else d
    if not isinstance(pulses, list):
        err("pulsar-roster: unexpected response shape")
        return 1

    rx = None
    if grep_re:
        try:
            rx = re.compile(grep_re, re.IGNORECASE)
        except re.error as e:
            err(f"pulsar-roster: bad regex: {e}")
            return 2

    rows = []
    for p in pulses:
        if not want_all and p.get("archived_at"):
            continue
        state = p.get("state") or "?"
        if running and state == "stopped":
            continue
        row = {
            "id": p.get("id", ""),
            "state": state,
            "vessel": p.get("vessel") or "?",
            "model": p.get("model") or "?",
            "title": p.get("title") or "?",
        }
        st = p.get("settings") or {}
        if st.get("role") == "helper":
            row["role"] = "helper"
            row["lead_id"] = st.get("lead_id") or st.get("spawned_by") or ""
        if rx and not rx.search(row["title"]):
            continue
        rows.append(row)

    if as_json:
        print(json.dumps(rows, separators=(",", ":")))
    else:
        for r in rows:
            tag = f"  [helper of {r['lead_id'][:8]}]" if r.get("role") == "helper" else ""
            print(f"{r['id']} {r['state']} {r['vessel']} {r['model']} {r['title']}{tag}")
    return 0
