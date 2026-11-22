"""HTTP helpers shared by the daemon's route modules.

Every response is enveloped: ``{"ok": true, "value": ...}`` on success,
``{"ok": false, "error": {"code", "message"}}`` with a real HTTP status on
failure. Handlers raise :class:`ApiError` to short-circuit to the error
envelope (the exception handler in ``create_app`` renders it).
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from fastapi import Request
from fastapi.responses import JSONResponse

from pulsar.store import Store, Pulse, UnknownPulse

logger = logging.getLogger("pulsar.daemon")


def ok(value: Any, status: int = 200) -> JSONResponse:
    return JSONResponse({"ok": True, "value": value}, status_code=status)


def err(code: str, message: str, status: int,
        extra: Optional[dict[str, Any]] = None) -> JSONResponse:
    return JSONResponse(
        {"ok": False, "error": {"code": code, "message": message,
                                **(extra or {})}},
        status_code=status,
    )


class ApiError(Exception):
    """Raised inside handlers to short-circuit to an enveloped error response."""

    def __init__(self, code: str, message: str, status: int,
                 extra: Optional[dict[str, Any]] = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status = status
        self.extra = extra


def store(request: Request) -> Store:
    return request.app.state.store


def get_pulse(request: Request, pulse_id: str) -> Pulse:
    try:
        return store(request).get_pulse(pulse_id)
    except UnknownPulse:
        logger.error("request pulse=%s NOT-FOUND", pulse_id)
        raise ApiError("not_found", f"no pulse {pulse_id}", 404)


def invalidate_roster(request: Request) -> None:
    """Drop the /live roster cache after a pulse row was created (create/fork).

    A /live frame can be sent up to ROSTER_INTERVAL_S after its snapshot was
    computed; without this, a frame served just after POST /pulses returns can
    still predate the new pulse and the client drops it again — on the mobile
    deck that un-opens the pane it just focused. No-op when the live router
    isn't mounted (unit tests).
    """
    cache = getattr(request.app.state, "roster_cache", None)
    if cache is not None:
        cache.invalidate()
