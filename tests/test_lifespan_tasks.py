"""Lifespan background tasks — start on boot, all done after shutdown.

TestClient runs the app LIFESPAN, and boot reconcile's orphan sweep stops any
real pulsar-pulse-* unit its store doesn't know (the 2026-07-27 self-kill, see
conftest.no_real_systemd). So the shared fakes (fake_spawn) are mandatory here,
same as test_daemon.
"""

from __future__ import annotations

import asyncio

from starlette.testclient import TestClient

from pulsar.store import Store
from pulsar.daemon.app import create_app

# Shared daemon fakes/fixtures (tmp PULSAR_DATA, faked spawn module).
from tests.test_daemon import data_root, fake_spawn  # noqa: F401

TASK_NAMES = ("scheduler_task", "handoff_task", "failover_task",
              "loopguard_task", "dead_runner_task")


def test_lifespan_starts_six_tasks_and_finishes_them(data_root, fake_spawn):  # noqa: F811
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    with TestClient(app):
        tasks = {n: getattr(app.state, n) for n in TASK_NAMES}
        for name, task in tasks.items():
            assert task is not None, name
            assert not task.done(), name
    for name, task in tasks.items():
        assert task.done(), name
    store.close()


def test_dead_runner_sweep_is_offloaded(data_root, fake_spawn, monkeypatch):  # noqa: F811
    """A slow systemd stop in the sweep must not block HTTP's event loop."""
    from pulsar.daemon import lifespan as lifespan_mod

    seen = {}

    async def capture(name, tick, seconds, *, stop_event=None, in_thread=False):
        seen[name] = in_thread
        await stop_event.wait()

    monkeypatch.setattr(lifespan_mod.loops_mod, "run_every", capture)
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    with TestClient(app) as client:
        assert client.get("/healthz").status_code == 200
        assert seen["dead_runner"] is True
    store.close()


def test_shutdown_cancels_inflight_handoffs_before_store_close(  # noqa: F811
        data_root, fake_spawn, monkeypatch):
    """B35: a monitor handoff in flight (handoff._ACTIVE) must be cancelled and
    awaited by the lifespan BEFORE store.close(), or it can touch a closed DB."""
    import threading
    from types import SimpleNamespace

    from pulsar.daemon import handoff as handoff_mod

    events: list[str] = []
    started = threading.Event()
    fake_pulse = SimpleNamespace(id="p-slow")

    async def slow_handoff(store, pulse, **kw):
        started.set()
        try:
            await asyncio.sleep(60)
        except asyncio.CancelledError:
            events.append("handoff_cancelled")
            raise
        return True

    real_close = Store.close

    def spy_close(self):
        events.append("store_close")
        return real_close(self)

    monkeypatch.setattr(handoff_mod.monitor, "due_pulses",
                        lambda store: [(fake_pulse, 1, 1)])
    monkeypatch.setattr(handoff_mod.state, "store_state",
                        lambda store, pid: "running")
    monkeypatch.setattr(handoff_mod.engine, "run_handoff", slow_handoff)
    monkeypatch.setattr(Store, "close", spy_close)

    app = create_app()  # owns its store, so the lifespan closes it
    with TestClient(app):
        assert started.wait(5), "monitor never started the handoff"
        task = handoff_mod.monitor._ACTIVE["p-slow"]
        assert not task.done()
    assert task.done()
    assert events == ["handoff_cancelled", "store_close"]
    assert handoff_mod.monitor._ACTIVE == {}
