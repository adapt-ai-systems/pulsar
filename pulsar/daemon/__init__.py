"""Pulsar daemon — the disposable FastAPI layer on 127.0.0.1:8726.

All product logic lives here; runners are dumb and the store is truth. The
daemon is restartable at will: every bit of state is on disk (SQLite store +
per-pulse dirs), and boot reconciles the store against reality (heartbeats +
systemd units) before serving.

Public surface: :func:`create_app` builds the ASGI app; ``python -m
pulsar.daemon`` serves it via uvicorn on 127.0.0.1:8726.
"""

from pulsar.daemon.app import create_app

__all__ = ["create_app"]
