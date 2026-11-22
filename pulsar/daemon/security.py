"""Request Host, Origin, and Sec-Fetch-Site validation for the Pulsar daemon."""

from __future__ import annotations

import ipaddress
import logging
import os
import re
from typing import Iterable
from urllib.parse import urlsplit

from fastapi.responses import JSONResponse

logger = logging.getLogger("pulsar.daemon.security")

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
LOOPBACK_ORIGIN_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_BRACKETED_HOST_RE = re.compile(r"^\[([^\]]+)\](?::(\d+))?$")
_HOST_FORBIDDEN_RE = re.compile(r"[\s/\\@?#]")


def parse_allowed_hosts(raw: str | None = None) -> tuple[str, ...]:
    """Parse comma-separated hostnames from PULSAR_WEB_ALLOWED_HOSTS."""
    if raw is None:
        raw = os.environ.get("PULSAR_WEB_ALLOWED_HOSTS", "")
    out: list[str] = []
    for item in str(raw or "").split(","):
        cleaned = item.strip().lower()
        if cleaned:
            out.append(cleaned)
    return tuple(out)


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        return False


def _matches_extra(name: str, extra_hosts: Iterable[str]) -> bool:
    for allowed in extra_hosts:
        clean = allowed.strip().lower().strip("[]")
        if not clean:
            continue
        if clean == "*":
            return True
        if clean.startswith(".") and name.endswith(clean):
            return True
        if name == clean:
            return True
    return False


def extract_host_header_hostname(host: str | None) -> str | None:
    """Return the normalized hostname from an HTTP Host header, or None."""
    if not host or not isinstance(host, str):
        return None
    raw = host.strip()
    if not raw or _HOST_FORBIDDEN_RE.search(raw):
        return None
    if raw.startswith("["):
        m = _BRACKETED_HOST_RE.match(raw)
        if not m:
            return None
        inner = m.group(1).lower()
        return inner if _is_ip(inner) else None
    if ":" in raw:
        if raw.count(":") == 1:
            name, port = raw.rsplit(":", 1)
            if not name or not port.isdigit():
                return None
            return name.lower()
        return raw.lower() if _is_ip(raw) else None
    return raw.lower()


def is_host_allowed(host: str | None, extra_hosts: Iterable[str] = ()) -> bool:
    """True if Host is localhost, a bare IP, or listed in extra_hosts."""
    name = extract_host_header_hostname(host)
    if not name:
        return False
    if name == "localhost" or _is_ip(name):
        return True
    return _matches_extra(name, extra_hosts)


def is_origin_allowed(origin: str | None, extra_hosts: Iterable[str] = ()) -> bool:
    """True if Origin is http(s) on localhost, 127.0.0.1, [::1], or extra_hosts.

    Does not allow arbitrary bare IP addresses.
    """
    if not origin or not isinstance(origin, str):
        return False
    raw = origin.strip()
    if not raw:
        return False
    try:
        u = urlsplit(raw)
    except ValueError:
        return False
    if u.scheme not in ("http", "https"):
        return False
    if u.username is not None or u.password is not None:
        return False
    if u.path or u.query or u.fragment:
        return False
    try:
        _ = u.port
    except ValueError:
        return False
    name = (u.hostname or "").lower()
    if not name:
        return False
    if name in LOOPBACK_ORIGIN_HOSTS:
        return True
    return _matches_extra(name, extra_hosts)


class RequestValidationMiddleware:
    """Reject disallowed Host, Origin, and cross-site Sec-Fetch-Site headers.

    Applies to both HTTP and WebSocket scopes. Requests without Origin and
    without Sec-Fetch-Site (CLI tools, curl, pulses) pass when Host is allowed.
    """

    def __init__(self, app) -> None:
        self.app = app

    async def __call__(self, scope, receive, send) -> None:
        kind = scope.get("type")
        if kind not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        extra = parse_allowed_hosts()
        hosts: list[str] = []
        origins: list[str] = []
        fetch_sites: list[str] = []
        for raw_k, raw_v in scope.get("headers") or ():
            k = raw_k.decode("latin-1").lower()
            if k == "host":
                hosts.append(raw_v.decode("latin-1"))
            elif k == "origin":
                origins.append(raw_v.decode("latin-1"))
            elif k == "sec-fetch-site":
                fetch_sites.append(raw_v.decode("latin-1"))

        reject_msg: str | None = None
        if len(hosts) != 1 or not is_host_allowed(hosts[0], extra):
            reject_msg = "blocked host"
        elif kind == "websocket":
            if any(v.strip().lower() == "cross-site" for v in fetch_sites):
                reject_msg = "blocked cross-site websocket"
            elif origins and not all(is_origin_allowed(o, extra) for o in origins):
                reject_msg = "blocked websocket origin"
        else:
            method = str(scope.get("method") or "GET").upper()
            if method not in SAFE_METHODS:
                if any(v.strip().lower() == "cross-site" for v in fetch_sites):
                    reject_msg = "blocked cross-site request"
                elif origins and not all(is_origin_allowed(o, extra) for o in origins):
                    reject_msg = "blocked origin"

        if reject_msg is not None:
            path = scope.get("path", "")
            logger.warning(
                "request %s %s REJECTED %s host=%r origin=%r",
                scope.get("method", kind.upper()),
                path,
                reject_msg,
                hosts[0] if hosts else None,
                origins[0] if origins else None,
            )
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008})
                return
            resp = JSONResponse(
                {"ok": False, "error": {"code": "forbidden", "message": reject_msg}},
                status_code=403,
            )
            await resp(scope, receive, send)
            return

        await self.app(scope, receive, send)
