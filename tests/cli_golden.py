"""Golden-transcript harness for the ops/bin CLIs that were ported from bash.

Each CLI has a test module tests/test_cli_<name>.py with a list of cases and one
golden file tests/cli_goldens/<name>.json. A case runs `ops/bin/pulsar-<name>` as a
subprocess against a stdlib fake daemon and records argv, stdout, stderr, the exit code
and every HTTP request (method, path, content type, JSON body). The goldens were recorded
from the bash versions; the Python ports must pass them unchanged.

Safety: PULSAR_DAEMON always points at a fake daemon or at a closed port, never at the
live daemon. A real pulsar-wait/ask call would create live watches,
questions or messages for the user.

Normalisation (so the transcript is the same on every machine and for bash and Python):
- the fake daemon's URL becomes <DAEMON>, the test's temp directory becomes <TMP>;
- ISO timestamps become <TS> (pulsar-wait in; its offset is checked separately);
- lines "curl: ..." on stderr are dropped (curl's own diagnostics, curl-version dependent);
- a Python traceback on stderr becomes the single line <TRACEBACK>.
"""
from __future__ import annotations

import json
import os
import re
import socket
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BIN = ROOT / "ops" / "bin"
GOLDEN_DIR = Path(__file__).with_name("cli_goldens")
PULSE = "pid123abc"
UPDATE_HINT = "pytest tests/test_cli_{name}.py --update-goldens"
_TS = re.compile(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(\.\d+)?")
OK = (200, {"ok": True, "value": {}})
REFUSED = "refused"


class Case:
    """One CLI invocation. `reply` is (status, payload) or REFUSED (nothing listens).

    cwd: plain | pulse (<data>/pulses/<PULSE>/cli) | nested (<data>/pulses/x/sub/cli)
         | link (a symlink to the pulse cli dir, with PWD set to the symlink)."""

    def __init__(self, id, argv, reply=OK, env=None, cwd="plain"):
        self.id, self.argv, self.reply, self.env, self.cwd = id, list(argv), reply, env or {}, cwd


class FakeDaemon:
    def __init__(self, reply):
        self.requests = []
        status, payload = reply
        data = payload if isinstance(payload, bytes) else (
            payload.encode() if isinstance(payload, str) else json.dumps(payload).encode())
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b""
                owner.requests.append({
                    "method": self.command, "path": self.path,
                    "content_type": self.headers.get("Content-Type"),
                    "body": json.loads(raw) if raw else None,
                })
                self.send_response(status)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def _dead_url():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return "http://127.0.0.1:%d" % port


def _make_cwd(kind, tmp):
    data = tmp / "data" / "pulses"
    if kind == "pulse":
        path = data / PULSE / "cli"
    elif kind == "nested":
        path = data / "x" / "sub" / "cli"
    elif kind == "link":
        target = data / "linked" / "cli"
        target.mkdir(parents=True)
        path = tmp / "link"
        path.symlink_to(target)
        return path
    else:
        path = tmp / "plain"
    path.mkdir(parents=True)
    return path


def _normalise(text, daemon_url, tmp):
    text = text.replace(daemon_url, "<DAEMON>").replace(str(tmp), "<TMP>")
    return _TS.sub("<TS>", text)


def _normalise_stderr(text, daemon_url, tmp):
    if "Traceback (most recent call last)" in text:
        return "<TRACEBACK>\n"
    text = "".join(line for line in text.splitlines(keepends=True) if not line.startswith("curl: "))
    return _normalise(text, daemon_url, tmp)


def run_case(name, case, tmp_path):
    """Run ops/bin/pulsar-<name> for `case`; return (record, raw requests)."""
    tmp = tmp_path.resolve()
    cwd = _make_cwd(case.cwd, tmp)
    fake = None if case.reply == REFUSED else FakeDaemon(case.reply)
    url = _dead_url() if fake is None else fake.url
    assert not url.endswith(":8726"), "a test must never reach the live daemon"
    env = {"PATH": os.environ["PATH"], "HOME": str(tmp), "LC_ALL": "C.UTF-8",
           "PULSAR_DAEMON": url, **case.env}
    if case.cwd == "link":
        env["PWD"] = str(cwd)
    try:
        done = subprocess.run([str(BIN / f"pulsar-{name}"), *case.argv], env=env, cwd=cwd,
                              stdin=subprocess.DEVNULL, capture_output=True, timeout=30)
    finally:
        requests = fake.requests if fake else []
        if fake:
            fake.close()
    record = {
        "argv": case.argv,
        "env": case.env,
        "cwd": case.cwd,
        "reply": None if fake is None else [case.reply[0], case.reply[1] if isinstance(
            case.reply[1], (dict, list, str)) else case.reply[1].decode("utf-8", "replace")],
        "exit": done.returncode,
        "stdout": _normalise(done.stdout.decode("utf-8", "replace"), url, tmp),
        "stderr": _normalise_stderr(done.stderr.decode("utf-8", "replace"), url, tmp),
        "requests": json.loads(_normalise(json.dumps(requests), url, tmp)),
    }
    return record, requests


class Goldens:
    def __init__(self, name, update):
        self.name, self.update = name, update
        self.path = GOLDEN_DIR / f"{name}.json"
        self.data = json.loads(self.path.read_text("utf-8")) if self.path.exists() else {}
        self.touched = False

    def check(self, case_id, record):
        if self.update:
            self.data[case_id] = record
            self.touched = True
            return
        hint = UPDATE_HINT.format(name=self.name)
        assert case_id in self.data, f"no golden for {case_id!r}; record it with: {hint}"
        assert record == self.data[case_id], (
            f"pulsar-{self.name} output drifted from the golden transcript for {case_id!r}. "
            f"A port or refactor commit must not change it. If the change is intentional, "
            f"regenerate in the same commit and say so in the message: {hint}")

    def save(self):
        if self.update and self.touched:
            GOLDEN_DIR.mkdir(exist_ok=True)
            self.path.write_text(json.dumps(self.data, indent=1, sort_keys=True) + "\n", "utf-8")


def goldens_fixture(name):
    """A module-scoped fixture: `goldens = goldens_fixture("say")` in the test module."""
    @pytest.fixture(scope="module")
    def goldens(request):
        value = Goldens(name, request.config.getoption("--update-goldens"))
        yield value
        value.save()
    return goldens

