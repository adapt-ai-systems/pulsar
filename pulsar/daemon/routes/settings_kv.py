"""The settings key/value routes."""

from __future__ import annotations

import json

from fastapi import APIRouter, Request
from pydantic import ValidationError

from pulsar.cli import guard as guard_mod
from pulsar.daemon.failover import DEFAULT_BACKUP_SETTING_KEY
from pulsar.daemon.http import ApiError, ok as _ok, store as _store
from pulsar.daemon.routes.common import _validate_backup_config
from pulsar.daemon.schemas import SetBackupBody

_GUARD_SETTING_KEYS = frozenset({"guard.enabled", "guard.extra_paths"})


def build_router() -> APIRouter:
    router = APIRouter()

    # -- app settings KV (themes etc.) -------------------------------------- #
    @router.get("/settings/{key}")
    async def get_setting(request: Request, key: str):
        return _ok(_store(request).get_setting(key))

    @router.put("/settings/{key}")
    async def put_setting(request: Request, key: str):
        try:
            value = json.loads(await request.body())
        except json.JSONDecodeError:
            raise ApiError("bad_request", "body must be valid JSON", 400)
        if key == DEFAULT_BACKUP_SETTING_KEY:
            if not isinstance(value, dict):
                raise ApiError(
                    "bad_backup",
                    "default_backup must be a JSON object with a model",
                    422,
                )
            try:
                body = SetBackupBody.model_validate(value)
            except ValidationError as exc:
                raise ApiError("bad_backup", str(exc), 422)
            value = _validate_backup_config(
                body.model,
                body.effort,
                allow_none=False,
                require_known_model=True,
                error_status=422,
            )
        st = _store(request)
        st.put_setting(key, value)
        if key in _GUARD_SETTING_KEYS:
            guard_mod.sync_from_store(st)
        return _ok(value)

    @router.delete("/settings/{key}")
    async def delete_setting(request: Request, key: str):
        st = _store(request)
        st.delete_setting(key)
        if key in _GUARD_SETTING_KEYS:
            guard_mod.sync_from_store(st)
        return _ok(None)

    return router
