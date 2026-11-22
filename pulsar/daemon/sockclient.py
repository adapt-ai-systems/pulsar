"""Tiny synchronous client for the daemon<->runner protocol (v1).

Speaks newline-delimited JSON over a pulse's ``runner.sock`` (see
``pulsar.runner.protocol``). One request, one reply, then close. A 5s timeout
bounds every phase (connect / send / recv); a version other than
``PROTOCOL_V`` in the reply is refused loudly (log + raise) — a mismatched
daemon must never trust baby-talk from an old runner.

Standing rule: no silent fail/wait/retry. Every timeout, connection refusal,
malformed reply, and version mismatch emits a timestamped log line carrying the
pulse id, op, outcome, and duration.
"""

from __future__ import annotations

import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, Optional

from pulsar.runner.protocol import PROTOCOL_V

logger = logging.getLogger("pulsar.daemon.sock")

TIMEOUT_S = 5.0


class SocketError(Exception):
    """A runner socket op failed (down, timeout, malformed, or mismatch)."""


class ProtocolMismatch(SocketError):
    """The runner replied with a protocol version we do not speak."""


def _op(sock_path: Path, op: str, pulse_id: str,
        extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    """Send one ``{v, op, ...}`` request, return the parsed reply dict.

    Raises :class:`SocketError` on any failure (missing socket, connect
    refused, timeout, malformed reply) and :class:`ProtocolMismatch` when the
    reply's ``v`` is not ``PROTOCOL_V``.
    """
    req = {"v": PROTOCOL_V, "op": op}
    if extra:
        req.update(extra)
    payload = (json.dumps(req) + "\n").encode()
    started = time.monotonic()

    if not sock_path.exists():
        dur = time.monotonic() - started
        logger.error("sock op=%s pulse=%s NO-SOCKET path=%s in %.3fs",
                     op, pulse_id, sock_path, dur)
        raise SocketError(f"no runner socket for {pulse_id} at {sock_path}")

    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(TIMEOUT_S)
    try:
        try:
            sock.connect(str(sock_path))
        except OSError as exc:
            dur = time.monotonic() - started
            logger.error("sock op=%s pulse=%s CONNECT-FAILED err=%s in %.3fs",
                         op, pulse_id, exc, dur)
            raise SocketError(f"connect failed for {pulse_id}: {exc}") from exc

        try:
            sock.sendall(payload)
            buf = _read_line(sock)
        except socket.timeout as exc:
            dur = time.monotonic() - started
            logger.error("sock op=%s pulse=%s TIMEOUT after %.3fs (limit %.1fs)",
                         op, pulse_id, dur, TIMEOUT_S)
            raise SocketError(f"timeout talking to {pulse_id}") from exc
        except OSError as exc:
            dur = time.monotonic() - started
            logger.error("sock op=%s pulse=%s IO-FAILED err=%s in %.3fs",
                         op, pulse_id, exc, dur)
            raise SocketError(f"io failed for {pulse_id}: {exc}") from exc
    finally:
        sock.close()

    dur = time.monotonic() - started
    if not buf:
        logger.error("sock op=%s pulse=%s EMPTY-REPLY in %.3fs", op, pulse_id, dur)
        raise SocketError(f"empty reply from {pulse_id}")
    try:
        reply = json.loads(buf)
    except json.JSONDecodeError as exc:
        logger.error("sock op=%s pulse=%s MALFORMED-REPLY err=%s raw=%r in %.3fs",
                     op, pulse_id, exc, buf[:200], dur)
        raise SocketError(f"malformed reply from {pulse_id}") from exc

    reply_v = reply.get("v")
    if reply_v != PROTOCOL_V:
        logger.error(
            "sock op=%s pulse=%s PROTOCOL-MISMATCH runner_v=%r daemon_v=%d in %.3fs",
            op, pulse_id, reply_v, PROTOCOL_V, dur,
        )
        raise ProtocolMismatch(
            f"runner {pulse_id} speaks v{reply_v!r}, daemon speaks v{PROTOCOL_V}"
        )

    logger.info("sock op=%s pulse=%s ok=%s in %.3fs",
                op, pulse_id, reply.get("ok"), dur)
    return reply


def _read_line(sock: socket.socket) -> str:
    """Read until the first newline (or EOF). Bounded by the socket timeout."""
    chunks: list[bytes] = []
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        chunks.append(chunk)
        if b"\n" in chunk:
            break
    return b"".join(chunks).split(b"\n", 1)[0].decode()


def status(sock_path: Path, pulse_id: str) -> dict[str, Any]:
    """Ask the runner for live status (hello-shape). Raises on failure."""
    return _op(sock_path, "status", pulse_id)


def stop(sock_path: Path, pulse_id: str) -> dict[str, Any]:
    """Ask the runner to shut its CLI down and exit. Raises on failure."""
    return _op(sock_path, "stop", pulse_id)


def interrupt(sock_path: Path, pulse_id: str) -> dict[str, Any]:
    """Ask the runner to abort its vessel's in-flight turn (pulse stays up).

    Raises on failure; a vessel with no turn interrupt replies
    ``{"ok": false, "error": "not_interruptible"}`` (surfaced, not raised).
    """
    return _op(sock_path, "interrupt", pulse_id)


def probe(sock_path: Path, pulse_id: str) -> Optional[dict[str, Any]]:
    """Best-effort live status: return the reply or None if the runner is down.

    Never raises — used to decorate GET responses with live state. A down or
    mismatched runner logs and yields None rather than failing the request.
    """
    try:
        return status(sock_path, pulse_id)
    except SocketError as exc:
        logger.info("sock probe pulse=%s down: %s", pulse_id, exc)
        return None
