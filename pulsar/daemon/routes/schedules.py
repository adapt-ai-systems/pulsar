"""Schedule routes: create, list, update and delete pulse schedules."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from pulsar.daemon import loop_triggers
from pulsar.daemon import scheduler as scheduler_mod
from pulsar.daemon.http import (
    ApiError,
    get_pulse as _get_pulse,
    ok as _ok,
    store as _store,
)
from pulsar.daemon.schemas import CreateScheduleBody, UpdateScheduleBody
from pulsar.daemon.views import schedule_dict as _schedule_dict
from pulsar.store import UnknownSchedule

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- schedules --------------------------------------------------------- #
    @router.post("/pulses/{pulse_id}/schedules")
    async def create_schedule(request: Request, pulse_id: str,
                              body: CreateScheduleBody):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        try:
            scheduler_mod.validate_timing(
                body.kind, body.interval_minutes, body.at_iso,
                body.cron_hhmm, body.days)
            triggers = loop_triggers.normalize(body.triggers, body.kind)
            mode = loop_triggers.normalize_mode(body.trigger_mode, body.kind)
            sched = st.create_schedule(
                pulse.id, body.kind, body.prompt,
                interval_minutes=body.interval_minutes,
                at_iso=body.at_iso, cron_hhmm=body.cron_hhmm, days=body.days,
                target_pulse_id=body.target_pulse_id, triggers=triggers,
                trigger_mode=mode)
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        logger.info("schedule.create id=%s pulse=%s kind=%s ok",
                    sched.id, pulse.id, sched.kind)
        return _ok(_schedule_dict(sched), status=201)

    @router.get("/pulses/{pulse_id}/schedules")
    async def list_pulse_schedules(request: Request, pulse_id: str):
        st = _store(request)
        pulse = _get_pulse(request, pulse_id)
        scheds = st.list_schedules(pulse_id=pulse.id)
        return _ok([_schedule_dict(s) for s in scheds])

    @router.get("/schedules")
    async def list_all_schedules(request: Request,
                                 enabled_only: bool = False):
        st = _store(request)
        scheds = st.list_schedules(enabled_only=enabled_only)
        return _ok([_schedule_dict(s) for s in scheds])

    @router.patch("/schedules/{schedule_id}")
    async def update_schedule(request: Request, schedule_id: str,
                              body: UpdateScheduleBody):
        st = _store(request)
        try:
            sched = st.get_schedule(schedule_id)
        except UnknownSchedule:
            logger.error("schedule.update id=%s NOT-FOUND", schedule_id)
            raise ApiError("not_found", f"no schedule {schedule_id}", 404)
        editing_loop = (body.prompt is not None or body.interval_minutes is not None
                        or body.triggers is not None or body.trigger_mode is not None)
        if editing_loop and sched.kind != "loop":
            raise ApiError("bad_request",
                           "only loop schedules can be edited", 400)
        if body.prompt is not None and not body.prompt.strip():
            raise ApiError("bad_request", "prompt cannot be empty", 400)
        if body.interval_minutes is not None and body.interval_minutes <= 0:
            raise ApiError("bad_request", "interval_minutes must be > 0", 400)
        try:
            triggers = loop_triggers.normalize(body.triggers, "loop")
            mode = loop_triggers.normalize_mode(body.trigger_mode, "loop")
        except ValueError as exc:
            raise ApiError("bad_request", str(exc), 400)
        try:
            if editing_loop:
                sched = st.update_schedule(schedule_id, prompt=body.prompt,
                                           interval_minutes=body.interval_minutes,
                                           triggers=triggers, trigger_mode=mode)
            if body.enabled is not None:
                sched = st.set_schedule_enabled(schedule_id, body.enabled)
        except UnknownSchedule:
            logger.error("schedule.update id=%s NOT-FOUND", schedule_id)
            raise ApiError("not_found", f"no schedule {schedule_id}", 404)
        logger.info("schedule.update id=%s enabled=%s ok",
                    schedule_id, body.enabled)
        return _ok(_schedule_dict(sched))

    @router.delete("/schedules/{schedule_id}")
    async def delete_schedule(request: Request, schedule_id: str):
        st = _store(request)
        try:
            st.delete_schedule(schedule_id)
        except UnknownSchedule:
            logger.error("schedule.delete id=%s NOT-FOUND", schedule_id)
            raise ApiError("not_found", f"no schedule {schedule_id}", 404)
        logger.info("schedule.delete id=%s ok", schedule_id)
        return _ok({"deleted": schedule_id})

    return router
