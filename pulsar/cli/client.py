"""The one daemon client for the ops CLIs. Stdlib only: ops/bin CLIs run without the venv.

Two layers. `send` is the transport: it returns a Response for every HTTP status and
raises TransportError only when the daemon cannot be reached. `request` is the
convenience call that decodes a JSON envelope and raises DaemonError. Each caller keeps
its own error policy on top.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal, NamedTuple
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

DEFAULT_DAEMON = "http://127.0.0.1:8726"
JSON_HEADERS = {"Content-Type": "application/json"}


class Response(NamedTuple):
    status: int
    body: bytes
    reason: str = ""  # the HTTP reason phrase, for messages such as "HTTP Error 404: Not Found"


class TransportError(Exception):
    """The daemon could not be reached. `cause` is the URLError or TimeoutError."""

    def __init__(self, cause: BaseException):
        super().__init__(str(cause))
        self.cause = cause


class DaemonError(ValueError):
    """A failed daemon call. `status` is None when the daemon was unreachable."""

    def __init__(self, message: str, status: int | None = None, body: bytes = b""):
        super().__init__(message)
        self.message = message
        self.status = status
        self.body = body


def daemon_base() -> str:
    return os.environ.get("PULSAR_DAEMON", DEFAULT_DAEMON).rstrip("/")


def pulse_id(explicit=None):
    value = explicit or os.environ.get("PULSE_ID") or os.environ.get("PULSAR_PULSE_ID")
    if value:
        return value
    parts = Path.cwd().parts
    for i, part in enumerate(parts[:-1]):
        if part == "pulses" and i + 1 < len(parts):
            return parts[i+1]
    raise ValueError("set --pulse or PULSE_ID, or run from a pulse's CLI directory")


def send(method: str, path: str, body=None, *, base: str | None = None, timeout=20,
         headers: dict | None = None) -> Response:
    """One HTTP call. An explicit `base` always wins over PULSAR_DAEMON."""
    url = (daemon_base() if base is None else base.rstrip("/")) + path
    req = Request(url, method=method, headers=JSON_HEADERS if headers is None else headers,
                  data=json.dumps(body).encode() if body is not None else None)
    try:
        with urlopen(req, timeout=timeout) as response:
            return Response(response.status, response.read(), response.reason)
    except HTTPError as exc:
        return Response(exc.code, exc.read(), exc.msg)
    except (URLError, TimeoutError) as exc:
        raise TransportError(exc) from exc


def request(method: str, path: str, body=None, *, base: str | None = None, timeout=20,
            envelope: Literal["ok", "value", "raw"] = "ok") -> Any:
    """send + decode. "ok": {ok, value|error} envelope; "value": unwrap `value`, else the
    raw body ({} when empty); "raw": the decoded body as is ({} when empty)."""
    shown = daemon_base() if base is None else base.rstrip("/")
    try:
        response = send(method, path, body, base=base, timeout=timeout)
    except TransportError as exc:
        raise DaemonError(f"cannot reach daemon at {shown}: {exc.cause}") from exc
    if not 200 <= response.status < 300:
        try:
            error = json.loads(response.body).get("error", {})
            reason = error.get("message", str(error))
        except Exception:
            reason = f"HTTP Error {response.status}: {response.reason}"
        raise DaemonError(f"daemon {response.status}: {reason}", response.status, response.body)
    if envelope == "ok":
        result = json.loads(response.body)
        if not result.get("ok"):
            raise DaemonError(str(result.get("error", "invalid daemon response")))
        return result["value"]
    result = json.loads(response.body) if response.body else {}
    if envelope == "value" and isinstance(result, dict):
        return result.get("value", result)
    return result
