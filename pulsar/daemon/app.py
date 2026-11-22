"""The Pulsar daemon — FastAPI app on 127.0.0.1:8726.

Thin by design: product logic lives here, the store is truth, runners are dumb.
The daemon is disposable — boot reconciles the store against reality
(``pulsar.daemon.reconcile``) before serving, so a restart never loses or
duplicates a running pulse.

Every response is enveloped: ``{"ok": true, "value": ...}`` on success,
``{"ok": false, "error": {"code", "message"}}`` with a real HTTP status on
failure. Every request failure, socket timeout, spawn failure, and reconcile
action writes a timestamped log line with the pulse id, outcome, and duration
(standing rule — no silent paths).
"""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError

from pulsar.daemon.http import ApiError, err as _err
from pulsar.daemon.lifespan import make_lifespan
from pulsar.daemon.routes import events as events_routes
from pulsar.daemon.routes import inbox as inbox_routes
from pulsar.daemon.routes import lifecycle as lifecycle_routes
from pulsar.daemon.routes import messages as messages_routes
from pulsar.daemon.routes import pulses as pulses_routes
from pulsar.daemon.routes import questions as questions_routes
from pulsar.daemon.routes import schedules as schedules_routes
from pulsar.daemon.routes import settings_kv as settings_kv_routes
from pulsar.daemon.routes import system as system_routes
from pulsar.daemon.routes import toggles as toggles_routes
from pulsar.daemon.routes import usage as usage_routes
from pulsar.daemon.routes import watches as watches_routes
from pulsar.daemon.hubpages import build_router as hubpages_build_router  # hub pages (removable)
from pulsar.daemon.live import build_router as live_build_router  # /live push channel
from pulsar.daemon.security import RequestValidationMiddleware
from pulsar.store import Store

logger = logging.getLogger("pulsar.daemon")


# --------------------------------------------------------------------------- #
# App factory
# --------------------------------------------------------------------------- #
def create_app(store: Optional[Store] = None) -> FastAPI:
    """Build the ASGI app. Pass a ``store`` to inject one (tests); otherwise a
    Store is opened at ``<PULSAR_DATA>/pulsar.db`` on startup and reconcile runs.
    """
    owns_store = store is None

    lifespan = make_lifespan(store, owns_store)

    app = FastAPI(title="Pulsar daemon", lifespan=lifespan)
    app.add_middleware(RequestValidationMiddleware)

    # -- error handlers ---------------------------------------------------- #
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, exc: ApiError):
        return _err(exc.code, exc.message, exc.status, exc.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError):
        # Envelope FastAPI's body validation like every other failure — an
        # agent-caller must get {"ok": false, ...} with the offending field
        # named (e.g. an unknown spawn field), never a bare {"detail": ...}.
        msg = "; ".join(
            f"{'.'.join(str(l) for l in e.get('loc', ()))}: {e.get('msg')}"
            for e in exc.errors())
        logger.error("request %s %s VALIDATION %s", request.method,
                     request.url.path, msg)
        return _err("bad_request", msg or "invalid request body", 422)

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception):
        logger.exception("request %s %s UNHANDLED", request.method, request.url.path)
        return _err("internal", "internal error", 500)

    app.include_router(system_routes.build_router())

    app.include_router(pulses_routes.build_router())

    app.include_router(messages_routes.build_router())

    app.include_router(watches_routes.build_router())

    app.include_router(questions_routes.build_router())

    app.include_router(settings_kv_routes.build_router())

    # toggles before lifecycle: PATCH /pulses/{pulse_id}/handoff must match
    # before POST (a DELETE there answers 405 with Allow: PATCH).
    app.include_router(toggles_routes.build_router())

    app.include_router(lifecycle_routes.build_router())

    app.include_router(events_routes.build_router())

    app.include_router(usage_routes.build_router())

    app.include_router(schedules_routes.build_router())

    app.include_router(inbox_routes.build_router())

    app.include_router(hubpages_build_router())
    app.include_router(live_build_router(app))

    return app
