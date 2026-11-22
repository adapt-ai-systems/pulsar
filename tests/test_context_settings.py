"""Tests for owner settings and public context rendering (S1)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

import pulsar.launch.context as context_mod
import pulsar.launch.diet as diet_mod
import pulsar.launch.skills as skills_mod
from pulsar.daemon.handoff import prompts as handoff_prompts
from pulsar.store import Store


@pytest.fixture()
def temp_store(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    data_dir.mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(data_dir))
    s = Store(str(data_dir / "pulsar.db"))
    yield s
    s.close()


@pytest.fixture()
def empty_skills(tmp_path, monkeypatch):
    root = tmp_path / "skills"
    root.mkdir()
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", root)
    return root


def test_no_hardcoded_hub_constants():
    for name in ("HUB_URL", "HUB_ROOT", "HUB_INDEXER"):
        assert not hasattr(context_mod, name), name


def test_default_context_uses_neutral_owner_and_host_tz(
        tmp_path, temp_store, empty_skills, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    home = tmp_path / "data" / "pulses" / "p1"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="claude",
                              title="Default Pulse")
    body = (home / "cli" / "PULSAR.md").read_text()

    assert "## Time zone — UTC" in body
    assert "All times you give the user" in body
    assert "## Reporting to the user's Inbox" in body
    assert "blocked on the user. Their reply" in body
    assert "$PULSAR_PULSE_HOME/NOTES.md" in body
    assert "## Showing " not in body
    assert "## Where you run" not in body


def test_custom_owner_timezone_and_hub_from_store(
        tmp_path, temp_store, empty_skills, monkeypatch):
    temp_store.put_setting("owner.name", "Dana")
    temp_store.put_setting("owner.timezone", "Europe/Berlin")
    temp_store.put_setting("hub.url", "https://hub.example.com/")
    monkeypatch.delenv("PULSAR_HUB_PAGES_DIR", raising=False)

    home = tmp_path / "data" / "pulses" / "p2"
    diet_mod.materialize_diet("p2", home, {"clean": True}, vessel="claude",
                              title="Custom Pulse")
    body = (home / "cli" / "PULSAR.md").read_text()

    assert "## Time zone — Europe/Berlin" in body
    assert "All times you give Dana" in body
    assert "convert them to Europe/Berlin" in body
    assert "## Reporting to Dana's Inbox" in body
    assert "ADDRESSED TO Dana" in body
    assert "blocked on Dana. Their reply" in body
    assert "## Showing Dana something in a browser" in body
    assert "catalog   https://hub.example.com/" in body
    assert "one page  https://hub.example.com/<slug>/" in body
    assert "https://hub.example.com//" not in body
    assert "Publish pages under" not in body
    assert "the user" not in body

    monkeypatch.setenv("PULSAR_HUB_PAGES_DIR", "/srv/hub")
    diet_mod.materialize_diet("p2", home, {"clean": True}, vessel="claude",
                              title="Custom Pulse")
    body_with_root = (home / "cli" / "PULSAR.md").read_text()
    assert "Publish pages under `/srv/hub`" in body_with_root


def test_helper_renders_owner_name_and_drops_lead_sections(
        tmp_path, temp_store, empty_skills):
    temp_store.put_setting("owner.name", "Dana")
    temp_store.put_setting("hub.url", "https://hub.example.com")

    home = tmp_path / "data" / "pulses" / "h1"
    context_mod.write_role(home, "helper", lead_id="lead-1", lead_title="Lead")
    diet_mod.materialize_diet("h1", home, {"clean": True}, vessel="claude",
                              title="Helper")
    body = (home / "cli" / "PULSAR.md").read_text()

    assert "## You are a helper — your lead is 'Lead' (pulse lead-1)" in body
    assert "Normally Dana goes through your lead" in body
    assert "not to Dana." in body
    assert "Use Dana's Inbox and `pulsar-ask` only as a fallback" in body
    assert "decision is Dana's alone" in body
    for dropped in ("## Reporting to Dana's Inbox",
                    "## Showing Dana something in a browser",
                    "## Delegating work to a new pulse",
                    "## Managing helper pulses"):
        assert dropped not in body, dropped


def test_prompt_parts_and_part_text_work_with_custom_owner(
        tmp_path, temp_store, empty_skills):
    temp_store.put_setting("owner.name", "Dana")
    temp_store.put_setting("hub.url", "https://hub.example.com")

    home = tmp_path / "data" / "pulses" / "p3"
    diet_mod.materialize_diet(
        "p3", home,
        {"clean": True, "prompt_parts": ["identity", "inbox", "pages"]},
        vessel="claude", title="Picked")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "## Reporting to Dana's Inbox" in body
    assert "## Showing Dana something in a browser" in body
    assert "## Watches" not in body

    custom_inbox = "## Custom Inbox\n\nSend updates here."
    diet_mod.materialize_diet(
        "p3", home,
        {"clean": True, "harness_overrides": {
            "part_text": {"inbox": custom_inbox}}},
        vessel="claude", title="Picked")
    overridden = (home / "cli" / "PULSAR.md").read_text()
    assert custom_inbox in overridden
    assert "## Reporting to Dana's Inbox" not in overridden


def test_corrupt_owner_settings_degrade_to_defaults(
        tmp_path, temp_store, empty_skills, monkeypatch):
    monkeypatch.setenv("TZ", "UTC")
    temp_store.put_setting("owner.name", 12345)
    temp_store.put_setting("owner.timezone", "Not/A_Real_Zone")
    temp_store.put_setting("hub.url", ["not", "a", "string"])

    lines = context_mod._context_lines("p4", "Corrupt", store=temp_store)
    body = "\n".join(lines)
    assert "## Time zone — UTC" in body
    assert "## Reporting to the user's Inbox" in body
    assert "## Showing " not in body


def test_omp_prompt_and_handoff_prompts_are_owner_neutral():
    omp = (Path(context_mod.__file__).resolve().parents[1]
           / "prompts" / "omp_interface.md").read_text()
    assert "the user's short, conversational messages" in omp
    assert "constraints the user actually stated" in omp
    assert "the user's newest instruction" in omp
    addendum = handoff_prompts.notes_addendum("p1")
    assert "the user's standing" in addendum


def test_daemon_url_rendered_from_env_in_context_and_handoff(
        tmp_path, temp_store, empty_skills, monkeypatch):
    from pulsar.store import Pulse

    monkeypatch.delenv("PULSAR_DAEMON", raising=False)
    monkeypatch.setenv("PULSAR_PORT", "18726")
    assert context_mod.daemon_url() == "http://127.0.0.1:18726"

    home = tmp_path / "data" / "pulses" / "p-port"
    diet_mod.materialize_diet("p-port", home, {"clean": True}, vessel="claude",
                              title="Port Pulse")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "http://127.0.0.1:18726" in body
    assert "http://127.0.0.1:8726" not in body

    pulse = Pulse(id="p-port", title="Port Pulse", model="claude-opus-5-5",
                  vessel="claude-code", state="running", grid_id=None,
                  position=0, created_at="2026-01-01T00:00:00Z",
                  updated_at="2026-01-01T00:00:00Z", handoff_tokens=None,
                  archived_at=None, settings={})
    warn = handoff_prompts.warning_text(pulse, 100_000, 200_000)
    assert "http://127.0.0.1:18726/pulses/p-port/handoff" in warn

    # Explicit PULSAR_DAEMON overrides PULSAR_PORT in both lead/helper context and handoff warning.
    monkeypatch.setenv("PULSAR_DAEMON", "http://127.0.0.1:19999/")
    assert context_mod.daemon_url() == "http://127.0.0.1:19999"

    hhome = tmp_path / "data" / "pulses" / "h-port"
    context_mod.write_role(hhome, "helper", lead_id="lead-9", lead_title="Lead")
    diet_mod.materialize_diet("h-port", hhome, {"clean": True}, vessel="claude",
                              title="Helper Port")
    hbody = (hhome / "cli" / "PULSAR.md").read_text()
    assert "http://127.0.0.1:19999/pulses/lead-9/messages" in hbody
    assert "http://127.0.0.1:8726" not in hbody
    assert "http://127.0.0.1:19999/pulses/p-port/handoff" in handoff_prompts.warning_text(
        pulse, 100_000, 200_000)


def test_daemon_main_binds_localhost_only(monkeypatch):
    import importlib
    import pulsar.daemon.__main__ as daemon_main

    monkeypatch.setenv("PULSAR_HOST", "0.0.0.0")
    monkeypatch.setenv("PULSAR_PORT", "18726")
    try:
        reloaded = importlib.reload(daemon_main)
        assert reloaded.HOST == "127.0.0.1"
        assert reloaded.PORT == 18726
    finally:
        monkeypatch.delenv("PULSAR_HOST", raising=False)
        monkeypatch.delenv("PULSAR_PORT", raising=False)
        importlib.reload(daemon_main)
