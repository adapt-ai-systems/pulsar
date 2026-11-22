"""Helpers shared by more than one route group (slot resolve, prompt/effort checks, message write)."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from fastapi import Request

from pulsar import models as models_mod
from pulsar.daemon import mailbox, slots as slots_mod
from pulsar.daemon.http import ApiError
from pulsar.launch import context as context_mod
from pulsar.vessel import EFFORT_LEVELS_BY_MODEL, efforts_for


async def _resolve_slot(request: Request, model: str) -> dict[str, Any] | None:
    """Resolve a slot immediately before its first runner is created."""
    entry = models_mod.model_by_id(model)
    if entry is None or not entry.slot:
        return None
    probe = getattr(request.app.state, "slot_probe", None)
    if probe is None:
        result = await asyncio.to_thread(slots_mod.probe_endpoint,
                                         entry.base_url or "")
    else:
        result = await probe.resolve(model)
    if not result.get("live"):
        raise ApiError("slot_unavailable", f"slot {model!r} is unavailable", 409)
    served_id = result.get("served_id")
    if not isinstance(served_id, str) or not served_id:
        raise ApiError("slot_unavailable", f"slot {model!r} is unavailable", 409)
    return {"id": model, "base_url": entry.base_url, "served_id": served_id}


# --------------------------------------------------------------------------- #
# Serialization & validation
# --------------------------------------------------------------------------- #
def _check_prompt_parts(parts: list[str]) -> list[str]:
    """400 on an unknown prompt part; returns the list de-duplicated."""
    bad = [p for p in parts if p not in context_mod.PROMPT_PARTS]
    if bad:
        raise ApiError("bad_prompt_parts",
                       f"unknown prompt_parts {bad}; valid: "
                       f"{list(context_mod.PROMPT_PARTS)}", 400)
    return list(dict.fromkeys(parts))


def _validate_effort(effort: str, vessel: str, model: str | None = None,
                     *, status: int = 400) -> None:
    """Raise :class:`ApiError` (default 400) unless ``effort`` is a level this
    model/vessel accepts (``pulsar.vessel.efforts_for``). A vessel with no
    dial (pi V4, historically qwen) refuses honestly rather than dropping."""
    levels = efforts_for(model, vessel)
    if effort not in levels:
        allowed = ", ".join(levels) if levels else \
            "none — this CLI has no effort control"
        who = (f"model {model!r}" if model in EFFORT_LEVELS_BY_MODEL
               else f"vessel {vessel!r}")
        raise ApiError("bad_effort",
                       f"effort {effort!r} is not supported by {who} "
                       f"(allowed: {allowed})", status)


def _validate_backup_config(
    model: Optional[str],
    effort: Optional[str],
    *,
    allow_none: bool = False,
    require_known_model: bool = False,
    error_status: int = 400,
) -> Optional[dict[str, Any]]:
    """Validate and normalize a backup model config ``{model, effort?}``.

    Shared by ``PATCH /pulses/{id}/backup`` and ``PUT /settings/default_backup``.
    Returns ``None`` when disabling (``allow_none=True`` and ``model is None``),
    or a normalized ``{"model": ..., "effort"?: ...}`` dict.
    """
    if model is None:
        if not allow_none:
            raise ApiError("bad_backup",
                           "backup model must be a non-empty string",
                           error_status)
        if effort:
            raise ApiError("bad_backup",
                           "effort makes no sense without a backup model "
                           "— send {\"model\": null} alone to disable",
                           error_status)
        return None
    if not isinstance(model, str) or not model.strip():
        raise ApiError("bad_backup",
                       "backup model must be a non-empty string",
                       error_status)
    if require_known_model and models_mod.model_by_id(model) is None:
        raise ApiError("bad_backup",
                       f"unknown backup model {model!r}",
                       error_status)
    vessel = models_mod.vessel_for(model)
    cfg: dict[str, Any] = {"model": model}
    if effort:
        effort = models_mod.canonical_effort(model, effort)
        _validate_effort(effort, vessel, model, status=error_status)
        cfg["effort"] = effort
    return cfg


def _write_message(pulse_id: str, text: str,
                   display_text: Optional[str] = None,
                   attachments: Optional[list[dict[str, Any]]] = None,
                   reply_to: Optional[dict[str, Any]] = None,
                   spawner: Optional[Any] = None,
                   via: str = "pane") -> str:
    """Atomically drop a message file into ``inbox/new/``. Returns the name.

    Origin keys: the user's pane message is ``user``; a ``spawner`` (the
    create_pulse first_message from another pulse) makes it ``peer``."""
    if spawner is not None:
        origin = mailbox.origin_keys("peer", via, from_pulse_id=spawner.id,
                                     from_title=spawner.title)
    else:
        origin = mailbox.origin_keys("user", via)
    return mailbox.write(
        pulse_id, text,
        extra={
            "display_text": display_text,
            "attachments": attachments,
            "reply_to": reply_to,
            **origin,
        },
    )
