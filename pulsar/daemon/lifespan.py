"""App lifespan: store open, boot reconcile, the background loop table and shutdown."""

from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI

from pulsar.daemon import (
    failover as failover_mod,
    handoff as handoff_mod,
    loopguard as loopguard_mod,
    loops as loops_mod,
    paths,
    reconcile as reconcile_mod,
    scheduler as scheduler_mod,
    slots as slots_mod,
)
from pulsar.store import Store

logger = logging.getLogger("pulsar.daemon")


def make_lifespan(store: Optional[Store], owns_store: bool):
    """Build the FastAPI lifespan for ``create_app``: ``store`` is the injected
    store (None when the app opens its own, ``owns_store``)."""

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        started = time.monotonic()
        if owns_store:
            db_path = paths.data_root() / "pulsar.db"
            db_path.parent.mkdir(parents=True, exist_ok=True)
            logger.info("daemon boot: opening store at %s", db_path)
            app.state.store = Store(str(db_path))
        else:
            app.state.store = store
        try:
            from pulsar.cli import guard as guard_mod
            guard_mod.sync_from_store(app.state.store)
        except Exception:
            logger.exception("daemon boot guard config sync RAISED — serving anyway")
        try:
            pruned = app.state.store.prune_closed_watches()
            if pruned:
                logger.info("daemon boot: pruned %d closed delegation_watches",
                            pruned)
        except Exception:
            logger.exception("daemon boot prune_closed_watches RAISED — serving anyway")
        # Boot reconcile — store vs reality — before we serve a single request.
        try:
            summary = reconcile_mod.reconcile(app.state.store)
            logger.info("daemon boot reconcile summary=%s in %.3fs",
                        summary, time.monotonic() - started)
        except Exception:
            logger.exception("daemon boot reconcile RAISED — serving anyway")

        # Background loops, one table: (name, tick, seconds, in_thread). All
        # state is on disk or in memory per loop, so a daemon restart never
        # double-fires; every task shares one stop_event and is cancelled on
        # shutdown below. Each task lands on app.state.<name>_task.
        store_ = app.state.store
        stop_event = asyncio.Event()
        app.state.scheduler_stop = stop_event

        def _sweep_dead_runners() -> None:
            # A runner dying mid-flight (vessel error, crash, external kill)
            # becomes an honest stopped tile with a reason within ~10s instead
            # of an eternal "stalled" (live find 07-26). Offload:
            # finish() may stop a systemd unit and block for 30 seconds.
            swept = reconcile_mod.sweep_dead_runners(
                store_, grace_s=20.0, quiet_alive=True, lock=True)
            if swept["stopped"]:
                logger.error("dead-runner sweep: marked %d stopped",
                             swept["stopped"])
                cache = getattr(app.state, "roster_cache", None)
                if cache is not None:
                    cache.invalidate()

        # Failover (backup model): baseline the old failures, THEN
        # retire stop-and-ask leftovers (its settings writes bump updated_at,
        # which the baseline reads).
        failover_monitor = failover_mod.Monitor(store_)
        try:
            failover_monitor.take_baseline()
        except Exception:
            logger.exception("failover boot baseline raised")
        try:  # idempotent
            failover_mod.retire_stop_ask(store_)
        except Exception:
            logger.exception("failover boot migration raised")

        loop_table = (
            # Schedules, then delegation watches, then condition watches,
            # serial in ONE thread: scheduler.tick runs the three
            # in that order, so two of them can never wake the same stopped
            # pulse at once. Do not split into concurrent rows. tick() can
            # shell out to systemd, hence in_thread.
            ("scheduler", lambda: scheduler_mod.tick(store_),
             scheduler_mod.TICK_SECONDS, True),
            # Auto-handoff monitor.
            ("handoff", lambda: handoff_mod.monitor.tick(store_),
             handoff_mod.monitor.TICK_SECONDS, False),
            # Model failover (backup model): a pulse whose model
            # failed moves to its backup model.
            ("failover", failover_monitor.tick, failover_mod.TICK_SECONDS,
             False),
            # Degenerate-loop guard (2026-08-15): detect a pulse repeating
            # itself and heal it — steer, then loop_rescue handoff, then Inbox.
            ("loopguard", lambda: loopguard_mod.tick(store_),
             loopguard_mod.TICK_SECONDS, False),
            ("dead_runner", _sweep_dead_runners, 10.0, True),
        )
        for name, tick, seconds, in_thread in loop_table:
            setattr(app.state, f"{name}_task", asyncio.create_task(
                loops_mod.run_every(name, tick, seconds, stop_event=stop_event,
                                    in_thread=in_thread)))
            logger.info("daemon boot: %s loop started (%.1fs)", name, seconds)
        app.state.slot_probe = slots_mod.SlotProbe()
        app.state.slot_task = asyncio.create_task(
            app.state.slot_probe.run(stop_event))
        logger.info("daemon boot: model slot probe started (30s)")

        yield

        # Signal + cancel the background tasks cleanly before closing the store.
        logger.info("daemon shutdown: stopping background loops")
        stop_event.set()
        for name in [n for n, *_ in loop_table] + ["slot"]:
            name = f"{name}_task"
            task = getattr(app.state, name, None)
            if task is None:
                continue
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("daemon shutdown %s raised", name)

        # Monitor handoffs run as their own tasks (handoff._ACTIVE) and outlive
        # the handoff_task cancel above — cancel and await them too, before the
        # store closes under them (B35).
        for pid, task in list(handoff_mod.monitor._ACTIVE.items()):
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("daemon shutdown handoff %s raised", pid)

        if owns_store:
            logger.info("daemon shutdown: closing store")
            try:
                app.state.store.close()
            except Exception:
                logger.exception("daemon shutdown store.close raised")

    return lifespan
