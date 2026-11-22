"""Per-pulse setting toggles: diet, handoff, backup, stream, reasoning, code and mode."""

from __future__ import annotations

import logging
from typing import Any

from fastapi import APIRouter, Request

from pulsar import models as models_mod
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    invalidate_roster as _invalidate_roster,
    ok as _ok,
    store as _store,
)
from pulsar.daemon.routes.common import (
    _check_prompt_parts,
    _validate_backup_config,
)
from pulsar.daemon.schemas import (
    DietBody,
    SetBackupBody,
    SetHandoffBody,
    SetModeBody,
    SetStreamBody,
    harness_overrides_json as _harness_overrides_json,
    print_mode_compaction_error,
)
from pulsar.daemon.views import pulse_view as _pulse_view

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- diet (clean-by-default) --------------------------------------------- #
    @router.patch("/pulses/{pulse_id}/diet")
    async def set_diet(request: Request, pulse_id: str, body: DietBody):
        """Store a new diet. The running CLI keeps its current diet; the new
        one applies the next time the runner spawns."""
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        diet = {"clean": body.clean, "memory": body.memory,
                "skills": body.skills, "cages": body.cages}
        if "prompt_parts" in body.model_fields_set:
            parts = body.prompt_parts
        else:
            parts = (pulse.settings.get("diet") or {}).get("prompt_parts")
        if parts is not None:
            diet["prompt_parts"] = _check_prompt_parts(parts)
        # Harness overrides (an experiment): omitted keeps the stored
        # dict; explicit null clears it; a present object replaces it.
        if "harness_overrides" in body.model_fields_set:
            ho = _harness_overrides_json(body.harness_overrides)
            comp_err = print_mode_compaction_error(pulse.vessel, pulse.model,
                                                   ho)
            if comp_err:
                raise ApiError("bad_harness_overrides", comp_err, 422)
            if ho:
                diet["harness_overrides"] = ho
        else:
            prev = (pulse.settings.get("diet") or {}).get("harness_overrides")
            if isinstance(prev, dict):
                diet["harness_overrides"] = prev
        updated = st.update_settings(pulse.id, {"diet": diet})
        logger.info("diet pulse=%s set %s (applies on next spawn)",
                    pulse.id, diet)
        return _ok({"pulse": _pulse_view(updated), "applies": "next_spawn",
                    "note": "diet applies the next time the runner spawns "
                            "(restore, model switch, handoff, or scheduled "
                            "wake)"})

    # -- handoff threshold (auto-handoff) ------------------------------------ #
    @router.patch("/pulses/{pulse_id}/handoff")
    async def set_handoff(request: Request, pulse_id: str,
                          body: SetHandoffBody):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        updated = st.set_handoff_tokens(pulse.id, body.handoff_tokens)
        logger.info("handoff-threshold pulse=%s tokens=%s ok",
                    pulse.id, body.handoff_tokens)
        return _ok(_pulse_view(updated))

    @router.patch("/pulses/{pulse_id}/backup")
    async def set_backup(request: Request, pulse_id: str,
                         body: SetBackupBody):
        """Set (or clear) the pulse's backup model — config only, any state.

        ``model`` names a model = enabled; ``model: null`` = disabled. The
        vessel is derived here and never stored, so it cannot drift away from
        the model beside it. Effort is validated against the BACKUP vessel
        ("" clears to the CLI default). The read path is free: ``_pulse_dict``
        already serializes the whole settings blob, so GET surfaces
        ``settings.backup`` with no change.
        """
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        cfg = _validate_backup_config(body.model, body.effort, allow_none=True)
        updated = st.update_settings(pulse.id, {"backup": cfg})
        if cfg is None:
            logger.info("backup-model pulse=%s DISABLED", pulse.id)
        else:
            vessel = models_mod.vessel_for(cfg["model"])
            logger.info("backup-model pulse=%s -> %s/%s effort=%s ok",
                        pulse.id, cfg["model"], vessel, cfg.get("effort") or "-")
        return _ok(_pulse_view(updated))

    # -- streaming mode (live-typing partials) ------------------------------ #
    @router.patch("/pulses/{pulse_id}/stream")
    async def set_stream(request: Request, pulse_id: str, body: SetStreamBody):
        """Per-pulse toggle for live streaming of in-progress replies.

        Stored in the pulse settings blob; the runner writes partial.json
        unconditionally, so this is purely a UI gate — flipping it needs no
        respawn. Unset defaults ON for every agent tool (see
        ``_default_stream_partials``).
        """
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        updated = st.update_settings(pulse.id, {"stream_partials": body.enabled})
        logger.info("stream-partials pulse=%s enabled=%s ok",
                    pulse.id, body.enabled)
        return _ok(_pulse_view(updated))

    @router.patch("/pulses/{pulse_id}/reasoning")
    async def set_reasoning(request: Request, pulse_id: str, body: SetStreamBody):
        """Per-pulse toggle for showing the model's reasoning trace.

        Purely a UI gate (like live streaming): the runner writes
        reasoning.json regardless, so flipping needs no respawn. Unset defaults
        ON for every vessel.
        """
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        updated = st.update_settings(pulse.id, {"stream_reasoning": body.enabled})
        logger.info("stream-reasoning pulse=%s enabled=%s ok",
                    pulse.id, body.enabled)
        return _ok(_pulse_view(updated))

    @router.patch("/pulses/{pulse_id}/code")
    async def set_code(request: Request, pulse_id: str, body: SetStreamBody):
        """Per-pulse toggle for the live code card (tool-call arguments as
        the model writes them). Purely a UI gate: the runner writes
        code.json regardless. Unset defaults ON for every vessel."""
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        updated = st.update_settings(pulse.id, {"stream_code": body.enabled})
        logger.info("stream-code pulse=%s enabled=%s ok",
                    pulse.id, body.enabled)
        return _ok(_pulse_view(updated))

    @router.patch("/pulses/{pulse_id}/mode")
    async def set_mode(request: Request, pulse_id: str, body: SetModeBody):
        """Set/clear the pulse's organizational mode (watching|paused|null)."""
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        updated = st.update_settings(pulse.id, {"mode": body.mode})
        logger.info("mode pulse=%s mode=%s ok", pulse.id, body.mode)
        _invalidate_roster(request)
        return _ok(_pulse_view(updated))

    return router
