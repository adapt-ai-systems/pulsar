"""pulsar-wait: register a daemon watch or reminder and end the turn. Stdlib only.

Ported from the bash script; the output is pinned by tests/test_cli_wait.py.
Any 2xx reply counts as registered.
"""
from __future__ import annotations

import datetime
import json
import os
import re
import sys
import traceback

from .client import TransportError, send
from .common import cwd_pulse_id, daemon_address, err, logical_pwd, out, text_of, write

USAGE = """\
pulsar-wait — register a wait and END YOUR TURN. Never sleep in a tool call.

  pulsar-wait file <path>                      wake when <path> exists
  pulsar-wait log <path> <regex>               wake when <regex> appears in <path>
  pulsar-wait http <url> [--contains S] [--up] wake on non-2xx/missing text; --up = wait for UP
  pulsar-wait cmd '<shell>' [--ok]             wake on non-zero exit; --ok = wait until it succeeds
  pulsar-wait pulse <id> [--idle] [--max-ctx N] wake when pulse <id> stops, goes stale, or ctx >= N
                                               --idle: ALSO wake every time its turn ends (helpers!)
  pulsar-wait in <N>[s|m|h] [note]             wake once after N seconds/minutes/hours
  pulsar-wait list                             your open watches (id, kind, note)
  pulsar-wait rm <id>                          delete a watch — DO this when the work ends

Options for file/log/http/cmd/pulse:
  --edge          keep the watch after it fires; refire only after the condition clears
                  (default is once: the watch closes after its first fire)
  --every N       check every N seconds (default 60, min 30)
  --timeout N     http/cmd only: give the probe N seconds (max 20)
  --note 'text'   what to echo back in the wake-up message

An open watch outlives your turn and your task: `pulsar-wait rm <id>` when
the job is done, or the daemon keeps waking a pulse that has nothing to do.

"""

_NEEDS = {"--every": "seconds", "--timeout": "seconds", "--note": "text", "--max-ctx": "a token count"}


def usage(code: int) -> int:
    write(sys.stdout, USAGE)
    return code


def _parse_id(body: str) -> str:
    """The created object's id from {"ok":true,"value":{...}} (or a bare object); '' if none."""
    try:
        d = json.loads(body)
    except Exception:
        return ""
    v = d.get("value", d) if isinstance(d, dict) else {}
    if isinstance(v, dict):
        v = v.get("watch") or v.get("schedule") or v
    return str(v.get("id", "") if isinstance(v, dict) else "").rstrip("\n")


def _register(path: str, body, cond: str) -> int:
    try:
        reply = send("POST", path, body, base=daemon_address(), timeout=None)
    except TransportError:
        err(f"pulsar-wait: cannot reach the daemon at {daemon_address()}")
        return 1
    answer = text_of(reply.body)
    if not 200 <= reply.status < 300:
        err(f"pulsar-wait: daemon returned {reply.status}:")
        err(answer)
        return 1
    found = _parse_id(answer)
    if found:
        out(f"id: {found}")
    out(f"registered — the daemon will message you when {cond}. End your turn now.")
    return 0


def _no_pulse() -> int:
    err(f"pulsar-wait: cannot derive pulse id from cwd ({logical_pwd()}). "
        "Run from a pulse's cli dir or pass --pulse <id>.")
    return 2


def _watch(verb: str, args: list, me: str) -> int:
    mode, every, timeout, usernote, maxctx, idle = "once", "", "", "", "", False
    rest = []
    while args:
        flag = args.pop(0)
        if flag in _NEEDS:
            if not args:
                err(f"{flag} needs {_NEEDS[flag]}")
                return 2
            value = args.pop(0)
            if flag == "--every":
                every = value
            elif flag == "--timeout":
                timeout = value
            elif flag == "--note":
                usernote = value
            else:
                maxctx = value
        elif flag == "--edge":
            mode = "edge"
        elif flag == "--idle":
            idle, mode = True, "edge"
        else:
            rest.append(flag)

    args = rest
    spec_error = None  # a non-numeric --every/--timeout/--max-ctx: bash posted an empty body
    if verb == "file":
        if not args:
            err("usage: pulsar-wait file <path>")
            return 2
        path = args.pop(0)
        if args:
            err(f"unknown arg '{args[0]}'")
            return usage(1)
        kind, spec = "file", {"path": path, "exists": True}
        cond, note = f"the file {path} appears", f"pulsar-wait: file {path}"
    elif verb == "log":
        if len(args) < 2:
            err("usage: pulsar-wait log <path> <regex>")
            return 2
        path, pattern = args[0], args[1]
        del args[:2]
        if args:
            err(f"unknown arg '{args[0]}'")
            return usage(1)
        kind, spec = "log", {"path": path, "pattern": pattern}
        cond, note = f"/{pattern}/ shows up in {path}", f"pulsar-wait: log {path} {pattern}"
    elif verb == "http":
        if not args:
            err("usage: pulsar-wait http <url> [--contains S] [--up]")
            return 2
        url = args.pop(0)
        contains, invert = "", False
        while args:
            flag = args.pop(0)
            if flag == "--contains":
                if not args:
                    err("--contains needs a value")
                    return 2
                contains = args.pop(0)
            elif flag == "--up":
                invert = True
            else:
                err(f"unknown flag '{flag}'")
                return usage(1)
        kind, spec = "http", {"url": url}
        if contains:
            spec["contains"] = contains
        if invert:
            spec["invert"] = True
            containing = f", containing '{contains}'" if contains else ""
            cond = f"{url} answers (2xx{containing})"
        else:
            cond = f"{url} fails or misses '{contains or 'its text'}'"
        note = f"pulsar-wait: http {url}{' --contains ' + contains if contains else ''}{' --up' if invert else ''}"
    elif verb == "cmd":
        if not args:
            err("usage: pulsar-wait cmd '<shell>' [--ok]")
            return 2
        shellcmd = args.pop(0)
        invert = False
        while args:
            flag = args.pop(0)
            if flag == "--ok":
                invert = True
            else:
                err(f"unknown flag '{flag}'")
                return usage(1)
        kind, spec = "cmd", {"cmd": shellcmd}
        if invert:
            spec["invert"] = True
        cond = "the command exits 0" if invert else "the command fails (non-zero)"
        head = shellcmd.encode("utf-8", "surrogateescape")[:120].decode("utf-8", "surrogateescape")
        note = f"pulsar-wait: cmd {head.rstrip(chr(10))}{' --ok' if invert else ''}"
    else:  # pulse
        if not args:
            err("usage: pulsar-wait pulse <id>")
            return 2
        target = args.pop(0)
        if args:
            err(f"unknown arg '{args[0]}'")
            return usage(1)
        kind, spec = "pulse_state", {"target_id": target, "stopped": True, "stale": True}
        try:
            if maxctx:
                spec["max_ctx"] = int(maxctx)
        except ValueError:
            traceback.print_exc()
            spec_error = True
        if idle:
            spec["idle"] = True
        cond = (f"pulse {target} stops or goes stale{' or ends a turn (idle)' if idle else ''}"
                f"{f' or its context reaches {maxctx} tokens' if maxctx else ''}")
        note = f"pulsar-wait: pulse {target}{' --idle' if idle else ''}{f' --max-ctx {maxctx}' if maxctx else ''}"

    body = None
    if not spec_error:
        try:
            if timeout and kind in ("http", "cmd"):
                spec["timeout_s"] = int(timeout)
            body = {"kind": kind, "pulse_id": me, "spec": spec, "mode": mode, "note": usernote or note}
            if every:
                body["interval_seconds"] = int(every)
        except ValueError:
            traceback.print_exc()
            body = None
    return _register("/watches/conditions", body, cond)


def _list(me: str) -> int:
    try:
        reply = send("GET", f"/watches/conditions?pulse_id={me}&state=watching",
                     base=daemon_address(), timeout=None, headers={})
        d = json.loads(reply.body)
        v = d.get("value", d)
        ws = v.get("watches", v) if isinstance(v, dict) else v
        if not ws:
            print("no open watches")
            return 0
        for w in ws:
            print(w.get("id", ""), " ", (w.get("kind") or "").ljust(11), (w.get("mode") or "").ljust(5),
                  w.get("note") or "")
    except Exception:  # bash piped curl into python3: its traceback, exit 1
        traceback.print_exc()
        return 1
    return 0


def _rm(args: list) -> int:
    if not args:
        err("usage: pulsar-wait rm <id>")
        return 2
    try:
        code = send("DELETE", f"/watches/conditions/{args[0]}", base=daemon_address(), timeout=None,
                    headers={}).status
    except TransportError:
        code = 0
    if 200 <= code < 300:
        out(f"deleted watch {args[0]}")
        return 0
    err(f"pulsar-wait: delete returned {code:03d}")
    return 1


def _in(args: list, me_of) -> int:
    if not args:
        err("usage: pulsar-wait in <N>[s|m|h] [note]")
        return 2
    dur, note = args[0], " ".join(args[1:]) or "pulsar-wait reminder"
    count = dur[:-1] if dur[-1:] in ("s", "m", "h") else dur
    if not re.fullmatch(r"[0-9]+", count):
        err(f"pulsar-wait: bad duration '{dur}' (want e.g. 90s, 10m, 2h)")
        return 2
    secs = int(count) * {"s": 1, "m": 60, "h": 3600}.get(dur[-1:], 1)
    if secs <= 0:
        err("pulsar-wait: duration must be positive")
        return 2
    me = me_of()
    if not me:
        return _no_pulse()
    at = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=secs)
    at_iso = at.isoformat().replace("+00:00", "")
    body = {"kind": "once", "at_iso": at_iso, "prompt": f"[pulsar-wait] {note}"}
    return _register(f"/pulses/{me}/schedules", body,
                     f"your scheduled reminder fires: {note} (at {at_iso.split('.')[0]}Z)")


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        return usage(1)
    override = os.environ.get("PULSE_ID", "")
    if args[0] == "--pulse":
        if len(args) < 3:
            err("usage: pulsar-wait --pulse <id> <verb ...>")
            return usage(1)
        override = args[1]
        del args[:2]

    def me_of():
        return override or cwd_pulse_id()

    verb, args = args[0], args[1:]
    if verb in ("-h", "--help", "help"):
        return usage(0)
    if verb in ("file", "log", "http", "cmd", "pulse"):
        me = me_of()
        return _watch(verb, args, me) if me else _no_pulse()
    if verb == "in":
        return _in(args, me_of)
    if verb == "list":
        me = me_of()
        if not me:
            err("pulsar-wait: cannot derive pulse id from cwd")
            return 2
        return _list(me)
    if verb == "rm":
        return _rm(args)
    err(f"unknown verb '{verb}'")
    return usage(1)
