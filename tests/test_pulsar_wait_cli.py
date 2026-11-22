"""B27 regression — ``pulsar-wait`` notes only carry --up/--ok when the flag was given."""
from __future__ import annotations

import json
import os
import subprocess
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "ops" / "bin" / "pulsar-wait"


def _run(args):
    bodies = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            n = int(self.headers.get("Content-Length", 0))
            bodies.append(json.loads(self.rfile.read(n)))
            out = json.dumps({"ok": True, "value": {"id": "w1"}}).encode()
            self.send_response(200)
            self.send_header("Content-Length", str(len(out)))
            self.end_headers()
            self.wfile.write(out)

        def log_message(self, *a):
            pass

    srv = HTTPServer(("127.0.0.1", 0), Handler)
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        env = dict(os.environ, PULSAR_DAEMON=f"http://127.0.0.1:{srv.server_port}", PULSE_ID="pid")
        r = subprocess.run([str(SCRIPT), *args], env=env, capture_output=True, text=True, timeout=30)
    finally:
        srv.shutdown()
        srv.server_close()
    assert r.returncode == 0, r.stderr
    return bodies[0]["note"]


def test_http_note_has_no_up_without_flag():
    note = _run(["http", "http://127.0.0.1:1"])
    assert "--up" not in note


def test_http_note_has_up_with_flag():
    assert _run(["http", "http://127.0.0.1:1", "--up"]).endswith(" --up")


def test_cmd_note_has_no_ok_without_flag():
    note = _run(["cmd", "true"])
    assert "--ok" not in note


def test_cmd_note_has_ok_with_flag():
    assert _run(["cmd", "true", "--ok"]).endswith(" --ok")
