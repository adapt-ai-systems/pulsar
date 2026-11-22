"""Pulse-to-pulse message tests — send, wake, refusals, the
/messages endpoint's from_pulse_id path, the scheduler ride, and the
spawn-time context file that teaches every pulse the recipe.

    venv/bin/python -m pytest tests -k messages -q

Same fakes pattern as the other daemon tests: PULSAR_DATA tmp dir, fake
spawn_runner on the shared spawn module.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
import pytest

import pulsar.launch.context as context_mod
import pulsar.launch.diet as diet_mod
import pulsar.launch.units as units_mod
from pulsar.daemon import paths
from pulsar.daemon import messages as messages_mod
from pulsar.daemon import scheduler as scheduler_mod
from pulsar.daemon.app import create_app
from pulsar.store import Store


@pytest.fixture()
def anyio_backend():
    return "asyncio"


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
def fake_spawn(monkeypatch):
    spawned = []

    def spawn_runner(pulse_id, home, model, vessel, resume=None, **kw):
        spawned.append(pulse_id)

    # lifecycle.wake_if_stopped calls spawn.spawn_runner on the shared module
    monkeypatch.setattr(units_mod, "spawn_runner", spawn_runner)
    return spawned


def _mk(store, title, state="running"):
    return store.create_pulse(title, "claude-haiku-4-5-20251001",
                              "claude-code", state=state)


def _inbox(pulse_id: str) -> list[Path]:
    d = paths.inbox_new(pulse_id)
    return sorted(d.glob("*.json")) if d.exists() else []


# --------------------------------------------------------------------------- #
# send
# --------------------------------------------------------------------------- #
def test_send_delivers_with_sender_and_reply_address(store, fake_spawn):
    a = _mk(store, "spec writer")
    b = _mk(store, "builder")
    name = messages_mod.send(store, a.id, b.id, "build the spec")
    msgs = _inbox(b.id)
    assert [m.name for m in msgs] == [name]
    body = json.loads(msgs[0].read_text())
    assert "build the spec" in body["text"]
    assert "spec writer" in body["text"]     # sender named
    assert a.id in body["text"]              # reply address included
    assert body["source"] == "pulse"


def test_send_wakes_stopped_target(store, fake_spawn):
    a = _mk(store, "spec writer")
    b = _mk(store, "builder", state="stopped")
    messages_mod.send(store, a.id, b.id, "hello")
    assert fake_spawn == [b.id]
    assert store.get_pulse(b.id).state == "running"


def test_send_refuses_self_and_archived(store, fake_spawn):
    a = _mk(store, "spec writer")
    with pytest.raises(messages_mod.MessageError):
        messages_mod.send(store, a.id, a.id, "hi")
    b = _mk(store, "old", state="stopped")
    store.archive_pulse(b.id)
    with pytest.raises(messages_mod.MessageError):
        messages_mod.send(store, a.id, b.id, "hi")
    assert _inbox(b.id) == []


# --------------------------------------------------------------------------- #
# the /messages endpoint's from_pulse_id path
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_endpoint_from_pulse_id(store, fake_spawn, monkeypatch):
    monkeypatch.setattr(units_mod, "list_pulse_units", lambda: [])
    a = _mk(store, "spec writer")
    b = _mk(store, "builder")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport,
                                 base_url="http://daemon") as c, \
            app.router.lifespan_context(app):
        r = await c.post(f"/pulses/{b.id}/messages",
                         json={"text": "ship it", "from_pulse_id": a.id})
        assert r.status_code == 202, r.text
        text = json.loads(_inbox(b.id)[0].read_text())["text"]
        assert "ship it" in text and "spec writer" in text

        # self-send refused; unknown sender 404; plain sends unaffected
        r = await c.post(f"/pulses/{b.id}/messages",
                         json={"text": "hi", "from_pulse_id": b.id})
        assert r.status_code == 400
        r = await c.post(f"/pulses/{b.id}/messages",
                         json={"text": "hi", "from_pulse_id": "nope"})
        assert r.status_code == 404
        r = await c.post(f"/pulses/{b.id}/messages", json={"text": "human"})
        assert r.status_code == 202
        assert len(_inbox(b.id)) == 2


# --------------------------------------------------------------------------- #
# scheduled cross-pulse message rides the scheduler
# --------------------------------------------------------------------------- #
def test_scheduled_message_fires_at_target(store, fake_spawn):
    a = _mk(store, "worker")
    b = _mk(store, "advisor")
    store.create_schedule(a.id, "loop", "hourly check-in",
                          interval_minutes=1, target_pulse_id=b.id)
    now = datetime.now(timezone.utc) + timedelta(minutes=2)
    summary = scheduler_mod.tick(store, now=now)
    assert summary["fired"] == 1
    # prompt landed at the TARGET, named as from the schedule's own pulse
    msgs = _inbox(b.id)
    assert len(msgs) == 1
    text = json.loads(msgs[0].read_text())["text"]
    assert "hourly check-in" in text and "worker" in text
    assert _inbox(a.id) == []


# --------------------------------------------------------------------------- #
# spawn-time context file (how a pulse learns the recipe)
# --------------------------------------------------------------------------- #
def test_context_file_written_per_vessel(tmp_path):
    home = tmp_path / "home"
    diet_mod.materialize_diet("pid1", home, {"clean": True},
                               vessel="claude-code", title="Spec Writer")
    ctx = (home / "cli" / "PULSAR.md").read_text()
    assert "pid1" in ctx and "Spec Writer" in ctx
    assert "from_pulse_id" in ctx and "/pulses" in ctx
    # repo-work rules are an opt-in skill, NOT ambient context
    assert "git status" not in ctx
    assert not (home / "cli" / "AGENTS.md").exists()

    diet_mod.materialize_diet("pid2", home, {"clean": True},
                               vessel="codex", title="Builder")
    agents = (home / "cli" / "AGENTS.md").read_text()
    assert "pid2" in agents and "from_pulse_id" in agents


def test_context_file_never_clobbers_foreign_file(tmp_path):
    home = tmp_path / "home"
    cli = home / "cli"
    cli.mkdir(parents=True)
    (cli / "PULSAR.md").write_text("hand-written notes")
    diet_mod.materialize_diet("pid1", home, {"clean": True},
                               vessel="claude-code", title="T")
    assert (cli / "PULSAR.md").read_text() == "hand-written notes"


def test_image_blind_context_explains_vision_helper():
    import shlex
    import sys
    ctx = "\n".join(context_mod._context_lines(
        "pid1", "Vision Worker", model="pi-glm-53"))

    assert "You have NO native vision" in ctx
    assert "<vision_observation>" in ctx
    assert "configured vision sidecar" in ctx
    assert f"{shlex.quote(sys.executable)} -m pulsar.vessel.vision_sidecar ask" in ctx
    assert "untrusted image-derived data" in ctx


def test_ds4_context_keeps_clarity_without_vision_helper():
    """DS4/V4 Flash read images natively; only the clarify contract stays."""
    ctx = "\n".join(context_mod._context_lines(
        "pid1", "Worker", model="codex-ds4-flash"))

    assert "## When instructions are unclear" in ctx
    assert "You have NO native vision" not in ctx


def test_non_ds4_context_omits_vision_helper():
    ctx = "\n".join(context_mod._context_lines(
        "pid1", "Frontier Worker", model="gpt-5.4"))

    assert "You have NO native vision" not in ctx
    assert "pulsar.vessel.vision_sidecar ask" not in ctx


def test_context_teaches_reporting_to_inbox():
    """Every pulse's spawn context carries the Report-to-Inbox recipe:
    the POST /inbox/items curl with from_pulse_id=self plus the
    notify/quiet/thread_key/reminders guidance."""
    ctx = "\n".join(context_mod._context_lines(
        "pid-abc123", "Stock Watcher", model="gpt-5.4"))

    assert "## Reporting to the user's Inbox" in ctx
    assert "/inbox/items" in ctx
    # the curl must bake in the pulse's own id as from_pulse_id
    assert '"from_pulse_id": "pid-abc123"' in ctx

    # rules of thumb are present and compact
    assert 'priority "notify"' in ctx
    assert '"priority": "quiet"' in ctx
    assert "STABLE thread_key" in ctx
    assert "needs_response" in ctx and "blocked on the user" in ctx
    assert "Reminders are NOT an inbox feature" in ctx
    # channel requirement + caps
    assert "Unknown names auto-create" in ctx or "auto-create" in ctx


def test_inbox_recipe_present_in_every_vessel_context(tmp_path):
    """The inbox reporting recipe rides every vessel's context file, not
    just claude — codex gets AGENTS.md."""
    home = tmp_path / "home"
    diet_mod.materialize_diet("pid9", home, {"clean": True},
                               vessel="codex", title="Builder")
    agents = (home / "cli" / "AGENTS.md").read_text()
    assert "Reporting to the user's Inbox" in agents
    assert "/inbox/items" in agents
