"""Characterization of CLI-side daemon calls.

These tests pin envelope rules and error policies against stdlib fake daemons
on random ports. The real daemon on :8726 is never contacted.
"""
from __future__ import annotations

import json
import runpy
import socket
import subprocess
import sys
import threading
import urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


class FakeDaemon:
    """A daemon that records every request and answers from `handler`."""

    def __init__(self):
        self.requests = []
        self.handler = lambda method, path, body: (200, b'{"ok": true, "value": {}}')
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def serve(self):
                size = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(size) if size else b""
                owner.requests.append({
                    "method": self.command, "path": self.path,
                    "body": json.loads(raw) if raw else None,
                    "content_type": self.headers.get("Content-Type"),
                    "accept": self.headers.get("Accept"),
                })
                status, payload = owner.handler(self.command, self.path, raw)
                self.send_response(status)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = serve

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = "http://127.0.0.1:%d" % self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def reply(self, status, payload):
        data = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
        self.handler = lambda method, path, body: (status, data)

    def close(self):
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture(autouse=True)
def never_the_real_daemon(monkeypatch):
    """Default every in-process caller to a closed port; tests point it at a fake."""
    monkeypatch.setenv("PULSAR_DAEMON", "http://127.0.0.1:9")


@pytest.fixture
def daemon():
    value = FakeDaemon()
    yield value
    value.close()


@pytest.fixture
def second_daemon():
    value = FakeDaemon()
    yield value
    value.close()


@pytest.fixture
def dead_url():
    """A loopback URL where nothing listens (connection refused)."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return "http://127.0.0.1:%d" % port




# ------------------------------------------------------- pulsar.cli.client

def test_client_send_returns_every_status_and_explicit_base_wins(daemon, second_daemon, monkeypatch):
    from pulsar.cli import client
    monkeypatch.setenv("PULSAR_DAEMON", daemon.url)
    second_daemon.reply(404, b"nope")
    got = client.send("GET", "/x", base=second_daemon.url + "/")
    assert (got.status, got.body, got.reason) == (404, b"nope", "Not Found")
    assert daemon.requests == [] and len(second_daemon.requests) == 1
    assert client.send("GET", "/y").status == 200 and daemon.requests[0]["path"] == "/y"


def test_client_send_raises_only_transport_error(dead_url):
    from pulsar.cli import client
    with pytest.raises(client.TransportError) as caught:
        client.send("GET", "/x", base=dead_url)
    assert isinstance(caught.value.cause, urllib.error.URLError)


@pytest.mark.parametrize("envelope, status, payload, expected", [
    ("value", 200, {"ok": True, "value": {"a": 1}}, {"a": 1}),
    ("value", 201, {"a": 1}, {"a": 1}),
    ("value", 204, b"", {}),
    ("value", 200, [1, 2], [1, 2]),
    ("raw", 200, {"ok": True, "value": {"a": 1}}, {"ok": True, "value": {"a": 1}}),
    ("raw", 204, b"", {}),
    ("ok", 200, {"ok": True, "value": 7}, 7),
])
def test_client_request_envelopes(daemon, envelope, status, payload, expected):
    from pulsar.cli import client
    daemon.reply(status, payload)
    assert client.request("GET", "/x", base=daemon.url, envelope=envelope) == expected


def test_client_request_errors_carry_status_and_body(daemon, dead_url):
    from pulsar.cli import client
    daemon.reply(500, b"boom")
    with pytest.raises(client.DaemonError) as caught:
        client.request("GET", "/x", base=daemon.url, envelope="value")
    assert (caught.value.status, caught.value.body) == (500, b"boom")
    assert isinstance(caught.value, ValueError)
    with pytest.raises(client.DaemonError) as caught:
        client.request("GET", "/x", base=dead_url)
    assert caught.value.status is None and str(caught.value).startswith("cannot reach daemon at ")


def test_cli_package_is_stdlib_only():
    import ast
    import sys as _sys
    for source in (ROOT / "pulsar" / "cli").glob("*.py"):
        for node in ast.walk(ast.parse(source.read_text())):
            if isinstance(node, ast.Import):
                roots = [alias.name.split(".")[0] for alias in node.names]
            elif isinstance(node, ast.ImportFrom) and not node.level:
                roots = [node.module.split(".")[0]]
            else:
                continue
            assert all(r in _sys.stdlib_module_names or r == "__future__" for r in roots), (source, roots)
