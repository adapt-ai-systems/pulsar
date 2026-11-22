"""PATCH /pulses/{id}/backup — validation + round trip.

    venv/bin/python -m pytest tests/test_daemon_backup_route.py -q

settings.backup is the pulse's failover target. No real runners,
no CLIs: the tests drive the ASGI app with a tmp ``PULSAR_DATA``.
"""

from __future__ import annotations

import httpx
import pytest

from pulsar.daemon.app import create_app
from pulsar.store import Store

pytestmark = pytest.mark.anyio


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


@pytest.fixture()
async def client(store):
    """ASGI client. PATCH /backup is pure config — no spawn, no socket; the
    shared no_real_systemd guard covers the lifespan's loops."""
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c:
        async with app.router.lifespan_context(app):
            yield c


def _mk_pulse(store, *, state="stopped", stop_reason="vessel error: boom",
              backup=None, model="claude-opus-5", vessel="claude-code"):
    p = store.create_pulse("t", model, vessel, state=state)
    settings = {}
    if stop_reason is not None:
        settings["stop_reason"] = stop_reason
    if backup is not None:
        settings["backup"] = backup
    if settings:
        p = store.update_settings(p.id, settings)
    return p


async def test_patch_sets_backup_and_derives_the_vessel(client, store):
    p = _mk_pulse(store, state="running", stop_reason=None)
    r = await client.patch(f"/pulses/{p.id}/backup",
                           json={"model": "gpt-6-luna", "effort": "xhigh"})
    assert r.status_code == 200, r.text
    cfg = r.json()["value"]["settings"]["backup"]
    # model + effort persist; the vessel is DERIVED, never stored.
    assert cfg == {"model": "gpt-6-luna", "effort": "xhigh"}
    assert store.get_pulse(p.id).settings["backup"] == cfg


async def test_patch_null_model_disables(client, store):
    p = _mk_pulse(store, state="running", stop_reason=None,
                  backup={"model": "gpt-6-luna"})
    r = await client.patch(f"/pulses/{p.id}/backup", json={"model": None})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["settings"]["backup"] is None
    assert store.get_pulse(p.id).settings.get("backup") is None


async def test_patch_rejects_an_effort_the_backup_vessel_cannot_speak(
        client, store):
    p = _mk_pulse(store, state="running", stop_reason=None)
    # "max" is a claude/codex level; the grok CLI stops at xhigh.
    r = await client.patch(f"/pulses/{p.id}/backup",
                           json={"model": "grok-4.6", "effort": "max"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_effort"
    assert "backup" not in store.get_pulse(p.id).settings


async def test_patch_forbids_unknown_fields(client, store):
    """extra=forbid — the 2026-08-06 silent-drop guard. A caller sending the
    dropped `vessel`/`enabled` fields learns so, loudly."""
    p = _mk_pulse(store, state="running", stop_reason=None)
    r = await client.patch(f"/pulses/{p.id}/backup",
                           json={"model": "gpt-6-luna", "vessel": "codex"})
    assert r.status_code == 422
    r = await client.patch(f"/pulses/{p.id}/backup",
                           json={"model": "gpt-6-luna", "enabled": True})
    assert r.status_code == 422


async def test_patch_requires_the_model_field(client, store):
    """An ABSENT model is a 422, not a silent disable."""
    p = _mk_pulse(store, state="running", stop_reason=None,
                  backup={"model": "gpt-6-luna"})
    r = await client.patch(f"/pulses/{p.id}/backup", json={})
    assert r.status_code == 422
    assert store.get_pulse(p.id).settings["backup"] == {"model": "gpt-6-luna"}


async def test_patch_works_on_a_stopped_pulse_and_get_surfaces_it(client,
                                                                  store):
    p = _mk_pulse(store)
    r = await client.patch(f"/pulses/{p.id}/backup",
                           json={"model": "claude-fable-5"})
    assert r.status_code == 200, r.text
    got = await client.get(f"/pulses/{p.id}")
    assert got.json()["value"]["settings"]["backup"] == {
        "model": "claude-fable-5"}

