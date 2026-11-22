"""Run the Pulsar daemon: ``python -m pulsar.daemon``.

Serves the FastAPI app on 127.0.0.1:8726 (port range 8721-8750) via
uvicorn. Logging is configured here (one line format, INFO) so every daemon
log line is timestamped per the standing rule.
"""

from __future__ import annotations

import logging
import os

import uvicorn

from pulsar.daemon.app import create_app

HOST = "127.0.0.1"
PORT = int(os.environ.get("PULSAR_PORT", "8726"))

log = logging.getLogger("pulsar.daemon")


def main() -> None:
    os.umask(0o077)
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    log.info("pulsar daemon starting on %s:%d", HOST, PORT)
    # Protocol-level WS pings keep /live past proxy idle timeouts. The
    # client watchdog only sees application messages, so this is the
    # server half of the reconnect-churn fix (SILENT_MS is the other).
    uvicorn.run(
        create_app(),
        host=HOST,
        port=PORT,
        log_level="info",
        ws_ping_interval=10.0,
        ws_ping_timeout=20.0,
    )


if __name__ == "__main__":
    main()
