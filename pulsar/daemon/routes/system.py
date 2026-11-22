"""Daemon identity and catalog routes: model registry, health, skills, cages."""

from __future__ import annotations

import logging

from fastapi import APIRouter, Request

from pulsar import models as models_mod
from pulsar.daemon.http import ApiError, ok as _ok
from pulsar.launch import cages as cages_mod
from pulsar.launch import skills as skills_mod
from pulsar.vessel import tools as tools_mod

logger = logging.getLogger("pulsar.daemon")


def build_router() -> APIRouter:
    router = APIRouter()

    # -- health ------------------------------------------------------------ #
    @router.get("/models")
    async def get_models(request: Request):
        """The model registry (pulsar/models.json) — what the picker shows and
        what POST /pulses accepts, for pulses and scripts that spawn helpers."""
        probe = getattr(request.app.state, "slot_probe", None)
        statuses = probe.snapshot() if probe is not None else None
        return _ok(models_mod.registry_payload(
            statuses, vessel_tools=tools_mod.detect_tools()))

    @router.get("/tools")
    async def get_tools():
        """Detected agent CLI availability on this host (read-only)."""
        return _ok(tools_mod.tools_summary())

    @router.get("/healthz")
    async def healthz():
        return _ok({"status": "ok"})

    @router.get("/skills")
    async def list_skills(request: Request):
        """Skill names for the UI checkboxes — dirs under ~/.claude/skills."""
        root = skills_mod.SKILLS_ROOT
        try:
            # "."/"_" prefixes are infra (.git backups, _hooks), not skills.
            names = sorted(p.name for p in root.iterdir()
                           if p.is_dir() and not p.name.startswith((".", "_")))
        except FileNotFoundError:
            return _ok([])
        except OSError as exc:
            logger.error("skills.list root=%s FAILED err=%s", root, exc)
            raise ApiError("skills_unreadable", str(exc), 500)
        return _ok(names)

    @router.get("/cages")
    async def list_cages(request: Request):
        """Cage names for the UI checkboxes — dirs under ~/.claude/cages
        holding a cage.ts."""
        return _ok(cages_mod.list_cages())

    return router
