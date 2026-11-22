"""Dynamic model slots: probe state machine, GET /models shape,
spawn refusal on a down slot, and the private provider map a slot pulse
gets (provider named after the slot, served id resolved at spawn).

    venv/bin/python -m pytest tests/test_daemon_slots.py -q
"""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

import pulsar.launch.diet as diet_mod
import pulsar.launch.units as units_mod
import pulsar.daemon.slots as slots_mod
from pulsar import models as models_mod
from pulsar.daemon.app import create_app
from pulsar.daemon.slots import SlotProbe
from pulsar.store import Store

LIVE = {"live": True, "served_id": "glm-5.3-flash", "checked_at": "t",
        "error": None}
DOWN = {"live": False, "served_id": None, "checked_at": "t",
        "error": "URLError"}


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    return root


@pytest.fixture()
def store(data_root):
    s = Store(str(data_root / "pulsar.db"))
    yield s
    s.close()


def _fake_probe(statuses: dict[str, dict]):
    """probe_endpoint stub keyed by base_url."""
    def fake(base_url, opener=None, timeout=slots_mod.PROBE_TIMEOUT_S):
        return dict(statuses.get(base_url, DOWN))
    return fake


# --------------------------------------------------------------------------- #
# Probe state machine (mock HTTP)
# --------------------------------------------------------------------------- #
def test_probe_state_machine_live_and_down(monkeypatch):
    statuses = {
        "http://192.0.2.10:8000/v1": LIVE,
    }
    monkeypatch.setattr(slots_mod, "probe_endpoint", _fake_probe(statuses))
    probe = SlotProbe()
    snap = probe.snapshot()
    assert all(v["error"] == "not_checked" for v in snap.values())

    import asyncio
    snap = asyncio.get_event_loop_policy().new_event_loop().run_until_complete(
        probe.refresh())
    assert snap["gpu-ring"] == LIVE
    assert snap["gpu-pair"]["live"] is False
    assert snap["gpu-pair"]["error"] == "URLError"


def test_probe_endpoint_parses_served_id():
    class R:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return json.dumps({"data": [{"id": "qwen3.8-27b"}]}).encode()

    def opener(url, timeout=None):
        assert url == "http://x:1/v1/models"
        return R()

    status = slots_mod.probe_endpoint("http://x:1/v1", opener=opener)
    assert status == {"live": True, "served_id": "qwen3.8-27b",
                      "checked_at": status["checked_at"], "error": None,
                      "base_url": "http://x:1/v1"}


# --------------------------------------------------------------------------- #
# GET /models shape
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_get_models_decorates_slots(store, monkeypatch):
    monkeypatch.setattr(slots_mod, "probe_endpoint", _fake_probe(
        {"http://192.0.2.10:8000/v1": LIVE}))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as client:
        async with app.router.lifespan_context(app):
            await app.state.slot_probe.refresh()
            r = await client.get("/models")
    assert r.status_code == 200
    entries = {m["id"]: m for m in r.json()["value"]["models"]}
    ring = entries["gpu-ring"]
    assert ring["slot"] is True
    assert ring["status"] == LIVE
    assert ring["label_live"] == "Local ring (TP4): glm-5.3-flash"
    pair = entries["gpu-pair"]
    assert pair["status"]["live"] is False
    assert pair["label_live"] == "Local pair (TP2): unavailable"
    # non-slot entries unchanged
    assert "slot" not in entries["claude-fable-5-1"]
    assert "status" not in entries["claude-fable-5-1"]


@pytest.mark.anyio
async def test_get_models_without_probe_leaves_slots_undecorated(store):
    # registry_payload() (no statuses) is the verbatim file.
    payload = models_mod.registry_payload()
    ring = next(m for m in payload["models"] if m["id"] == "gpu-ring")
    assert "status" not in ring and "label_live" not in ring


# --------------------------------------------------------------------------- #
# Spawn on a slot: 409 when down, provider map when live
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_spawn_on_down_slot_is_409(store, monkeypatch):
    monkeypatch.setattr(slots_mod, "probe_endpoint", _fake_probe({}))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as client:
        async with app.router.lifespan_context(app):
            r = await client.post("/pulses", json={
                "title": "slot pulse", "model": "gpu-ring"})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "slot_unavailable"


def test_slot_effort_table_by_served_prefix():
    assert models_mod.slot_efforts("glm-5.3-flash") == \
        ("off", "low", "high", "max")
    assert models_mod.slot_efforts("qwen3.8-27b") == ("low", "medium", "xhigh")
    assert models_mod.slot_efforts("mimo-v2") == ("low", "high", "max")
    assert models_mod.slot_efforts("deepseek-v4-flash") == \
        ("80", "70", "50", "30", "none")
    assert models_mod.slot_efforts("mystery-model") == ()  # pi default


def test_spawn_writes_slot_provider_map(data_root, monkeypatch):
    home = data_root / "pulse-home"
    home.mkdir(parents=True)
    diet_mod._materialize_pi_diet(
        "p1", home, [],
        slot={"id": "gpu-ring", "base_url": "http://192.0.2.10:8000/v1",
              "served_id": "glm-5.3-flash"})
    models = json.loads(
        (home / "pi-home" / "agent" / "models.json").read_text())
    provider = models["providers"]["gpu-ring"]
    assert provider["baseUrl"] == "http://192.0.2.10:8000/v1"
    assert provider["models"][0]["id"] == "glm-5.3-flash"


def test_pi_resolves_slot_wire_model_from_private_map(data_root):
    from pulsar.vessel.pi import _wire_model_for
    cli_dir = data_root / "p1" / "cli"
    cli_dir.mkdir(parents=True)
    agent = data_root / "p1" / "pi-home" / "agent"
    agent.mkdir(parents=True)
    (agent / "models.json").write_text(json.dumps({"providers": {
        "gpu-ring": {"models": [{"id": "glm-5.3-flash"}]},
    }}))
    assert _wire_model_for("gpu-ring", cli_dir) == "gpu-ring/glm-5.3-flash"
    # a normal registry id still resolves through _CLI, untouched
    assert _wire_model_for("pi-qwen38-flash-tp4", cli_dir).startswith("qwen38tp4/")
    # unknown id without a private map passes through unchanged
    assert _wire_model_for("some-model", cli_dir) == "some-model"
