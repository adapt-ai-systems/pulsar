"""Per-model spawn cap: models.json ``max_concurrent``.

A local serve shares one KV pool across sessions, so POST /pulses refuses a
spawn (409 model_busy) once that many pulses on the model are not stopped.
Stopped and archived pulses do not count; models without the field are
uncapped.
"""
from __future__ import annotations

import dataclasses

import httpx
import pytest

from pulsar import models as models_mod
from pulsar.daemon.app import create_app
from tests.test_daemon import (  # noqa: F401
    FakeSpawn, FakeSock, _SockErr, data_root, fake_sock, fake_spawn, store,
)

CAPPED = "claude-opus-5-5"


@pytest.fixture()
async def client(store, fake_spawn, fake_sock, monkeypatch):  # noqa: F811
    base = models_mod.MODELS[CAPPED]
    monkeypatch.setitem(models_mod.MODELS, CAPPED,
                        dataclasses.replace(base, max_concurrent=2))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c:
        async with app.router.lifespan_context(app):
            yield c


async def _spawn(client, title="p", model=CAPPED):
    return await client.post("/pulses", json={"title": title, "model": model})


@pytest.mark.anyio
async def test_cap_refuses_third_spawn_and_frees_on_stop(client, store,
                                                         fake_spawn):
    a = await _spawn(client, "a")
    b = await _spawn(client, "b")
    assert a.status_code == 201 and b.status_code == 201
    before = len(fake_spawn.spawned)
    r = await _spawn(client, "c")
    assert r.status_code == 409, r.text
    err = r.json()["error"]
    assert err["code"] == "model_busy"
    assert "cap 2" in err["message"] and CAPPED in err["message"]
    # refused means NOT created and NOT spawned
    assert len(fake_spawn.spawned) == before
    assert len(store.list_pulses()) == 2
    # a stopped pulse frees a seat
    store.set_state(a.json()["value"]["id"], "stopped")
    r = await _spawn(client, "c")
    assert r.status_code == 201, r.text


@pytest.mark.anyio
async def test_cap_counts_only_the_capped_model(client):
    for t in ("a", "b"):
        assert (await _spawn(client, t)).status_code == 201
    # another model is not capped (no field) and is not counted
    r = await _spawn(client, "other", model="claude-sonnet-5-5")
    assert r.status_code == 201, r.text
    r = await _spawn(client, "other2", model="claude-sonnet-5-5")
    assert r.status_code == 201, r.text


def test_registry_loads_glm_full_cap():
    m = models_mod.model_by_id("pi-glm53-full")
    assert m is not None and m.max_concurrent == 2
    assert models_mod.model_by_id("claude-opus-5-5").max_concurrent is None
    raw = {e["id"]: e for e in models_mod.registry_payload()["models"]}
    assert raw["pi-glm53-full"]["max_concurrent"] == 2
