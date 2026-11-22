"""Dynamic probes for the fixed local-model slots.

The slot name is stable while the model served behind its endpoint may change.
Probes use the same OpenAI-compatible ``GET /models`` contract as
``ops/bin/pulsar-models`` and always run in a worker thread when called from
the daemon.
"""

from __future__ import annotations

import asyncio
import json
import urllib.request
from datetime import datetime, timezone
from typing import Any, Callable, Mapping

from pulsar import models as models_mod

PROBE_INTERVAL_S = 30.0
PROBE_TIMEOUT_S = 5.0


def checked_at() -> str:
    return datetime.now(timezone.utc).isoformat()


def _status(*, live: bool, served_id: str | None, error: str | None,
            at: str | None = None, base_url: str | None = None) -> dict[str, Any]:
    return {"live": live, "served_id": served_id,
            "checked_at": at or checked_at(), "error": error,
            "base_url": base_url}


def slot_entries() -> tuple[models_mod.Model, ...]:
    return tuple(m for m in models_mod.MODELS.values() if m.slot)


def probe_slot(slot: models_mod.Model, *, opener: Callable[..., Any] | None = None,
               timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """Probe every address the slot may be served on; first live one wins.

    Local model servers can swap models or move between ports, so a slot
    follows whichever configured address is answering.
    """
    result = _status(live=False, served_id=None, error="no_base_url")
    for url in (slot.base_urls or ((slot.base_url,) if slot.base_url else ())):
        result = probe_endpoint(url, opener=opener, timeout=timeout)
        if result.get("live"):
            return result
    return result


def probe_endpoint(base_url: str, *, opener: Callable[..., Any] | None = None,
                   timeout: float = PROBE_TIMEOUT_S) -> dict[str, Any]:
    """Probe one endpoint and return the public slot status shape.

    ``urllib`` is deliberately used here to keep this daemon probe independent
    of the ASGI client stack. The caller controls the worker thread; this
    function itself is synchronous so it is also useful to the spawn path.
    """
    opener = opener or urllib.request.urlopen
    try:
        with opener(base_url.rstrip("/") + "/models", timeout=timeout) as resp:
            payload = json.load(resp)
        data = payload.get("data") if isinstance(payload, dict) else None
        if not isinstance(data, list):
            raise ValueError("/models response has no data list")
        ids = [item.get("id") for item in data
               if isinstance(item, dict) and isinstance(item.get("id"), str)]
        served_id = ids[0] if ids else None
        if served_id is None:
            raise ValueError("/models response has no served model")
        return _status(live=True, served_id=served_id, error=None,
                       base_url=base_url)
    except Exception as exc:  # noqa: BLE001 - status must survive all failures
        return _status(live=False, served_id=None,
                       error=type(exc).__name__)


class SlotProbe:
    """In-memory status cache and 30-second asynchronous probe loop."""

    def __init__(self, *, opener: Callable[..., Any] | None = None,
                 timeout: float = PROBE_TIMEOUT_S) -> None:
        self.opener = opener
        self.timeout = timeout
        self._statuses: dict[str, dict[str, Any]] = {
            m.id: _status(live=False, served_id=None, error="not_checked")
            for m in slot_entries()
        }

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {slot: dict(status) for slot, status in self._statuses.items()}

    def status(self, slot_id: str) -> dict[str, Any] | None:
        value = self._statuses.get(slot_id)
        return dict(value) if value is not None else None

    def _probe(self, slot: models_mod.Model) -> dict[str, Any]:
        return probe_slot(slot, opener=self.opener, timeout=self.timeout)

    async def refresh(self) -> dict[str, dict[str, Any]]:
        slots = slot_entries()
        values = await asyncio.gather(*(
            asyncio.to_thread(self._probe, slot) for slot in slots))
        self._statuses = {slot.id: value for slot, value in zip(slots, values)}
        return self.snapshot()

    async def resolve(self, slot_id: str) -> dict[str, Any]:
        slot = models_mod.model_by_id(slot_id)
        if slot is None or not slot.slot:
            raise ValueError(f"{slot_id!r} is not a model slot")
        value = await asyncio.to_thread(self._probe, slot)
        self._statuses[slot_id] = value
        return dict(value)

    async def run(self, stop_event: asyncio.Event) -> None:
        while not stop_event.is_set():
            try:
                await self.refresh()
            except Exception:  # pragma: no cover - defensive task boundary
                # A malformed registry or executor failure must not take down
                # the daemon's other monitors. Keep the previous state.
                pass
            try:
                await asyncio.wait_for(stop_event.wait(), PROBE_INTERVAL_S)
            except asyncio.TimeoutError:
                pass


def resolve_slot(slot_id: str, *, opener: Callable[..., Any] | None = None,
                 timeout: float = PROBE_TIMEOUT_S) -> tuple[models_mod.Model,
                                                            dict[str, Any]]:
    """Resolve a slot's currently served id for a synchronous spawn caller."""
    slot = models_mod.model_by_id(slot_id)
    if slot is None or not slot.slot:
        raise ValueError(f"{slot_id!r} is not a model slot")
    return slot, probe_slot(slot, opener=opener, timeout=timeout)
