"""Lean spawn: a caller picks which system-prompt parts a pulse gets.

``prompt_parts`` on POST /pulses is an allowlist of spawn.PROMPT_PARTS,
stored in the diet so every respawn (restore, handoff, wake) applies it.
Omitted = the standard prompt, unchanged.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pulsar.launch.context as context_mod
import pulsar.launch.diet as diet_mod
import pulsar.launch.skills as skills_mod
from pulsar.vessel import pi as pi_mod

from tests.test_daemon import (  # noqa: F401
    FakeSpawn, FakeSock, _SockErr, _write_heartbeat,
    data_root, fake_spawn, fake_sock, store, client,
)


@pytest.fixture()
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / "skills-root"
    root.mkdir()
    for name in ("keep-going", "beta-skill"):
        (root / name).mkdir()
        (root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} skill\n---\nbody\n")
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", root)
    return root


def _pulsar_md(home: Path) -> str:
    return (home / "cli" / "PULSAR.md").read_text()


def test_every_template_heading_is_a_prompt_part():
    """A new template section must get a part name, or [] would leak it."""
    for name in ("PULSAR.md", "HELPER.md"):
        for line in context_mod._template(name):
            if line.startswith("## ") or line.startswith("# "):
                assert any(line.startswith(h)
                           for h in context_mod.PROMPT_SECTION_PARTS.values()), line


def test_omitted_parts_match_all_parts(tmp_path, skills_root):
    a, b = tmp_path / "a", tmp_path / "b"
    diet_mod.materialize_diet("p1", a, {"clean": True, "skills": []},
                               vessel="pi", title="T")
    diet_mod.materialize_diet(
        "p1", b, {"clean": True, "skills": [],
                  "prompt_parts": list(context_mod.PROMPT_PARTS)},
        vessel="pi", title="T")
    assert _pulsar_md(a) == _pulsar_md(b)
    assert "## Skill library" in _pulsar_md(a)


def test_empty_parts_leaves_only_the_marker(tmp_path, skills_root):
    home = tmp_path / "h"
    diet = {"clean": True, "skills": [], "prompt_parts": []}
    diet_mod.materialize_diet("p1", home, diet, vessel="pi", title="T",
                               model="qwen3.8-27b")
    text = _pulsar_md(home)
    assert text.strip() == context_mod._CTX_MARKER
    # The marker keeps the file ours: the next spawn still rewrites it.
    diet_mod.materialize_diet(
        "p1", home, dict(diet, prompt_parts=["identity"]), vessel="pi",
        title="T")
    assert "pulse id `p1`" in _pulsar_md(home)


def test_picked_parts_only(tmp_path, skills_root):
    home = tmp_path / "h"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [],
                     "prompt_parts": ["identity", "messaging"]},
        vessel="pi", title="T")
    text = _pulsar_md(home)
    assert "# Pulsar pulse: T" in text
    assert "## Messaging other pulses" in text
    for gone in ("## How Pulsar runs you", "## Watches", "## Skill library",
                 "## Scope contract", "## The pulse roster"):
        assert gone not in text


def test_helper_section_is_a_part(tmp_path, skills_root):
    home = tmp_path / "h"
    context_mod.write_role(home, "helper", lead_id="L1", lead_title="Lead")
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [], "prompt_parts": ["helper"]},
        vessel="pi", title="T")
    text = _pulsar_md(home)
    assert "## You are a helper" in text and "## Messaging" not in text


def test_pi_todo_follows_parts(tmp_path, skills_root):
    home = tmp_path / "h"
    diet_mod.materialize_diet("p1", home, {"clean": True, "skills": []},
                               vessel="pi")
    assert pi_mod.PI_TODO_PROMPT in pi_mod.pi_todo_args(home / "cli")
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [], "prompt_parts": []},
        vessel="pi")
    assert pi_mod.pi_todo_args(home / "cli") == []
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [], "prompt_parts": ["todo"]},
        vessel="pi")
    assert pi_mod.PI_TODO_PROMPT in pi_mod.pi_todo_args(home / "cli")


def test_default_skills_follow_parts(store):
    store.put_setting("pulse.default_skills", ["keep-going"])
    assert store.diet_with_defaults(
        {"clean": True, "skills": ["beta-skill"], "prompt_parts": []}
    )["skills"] == ["beta-skill"]
    assert store.diet_with_defaults(
        {"clean": True, "skills": [], "prompt_parts": ["default_skills"]}
    )["skills"] == ["keep-going"]


@pytest.mark.anyio
async def test_api_lean_spawn_survives_restore(client, store, fake_spawn,
                                               skills_root):
    r = await client.put("/settings/pulse.default_skills",
                         content=json.dumps(["keep-going"]))
    assert r.status_code == 200, r.text
    r = await client.post("/pulses", json={
        "title": "lean", "model": "claude-haiku-4-5",
        "prompt_parts": ["identity", "identity"]})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings["diet"]["prompt_parts"] == ["identity"]
    assert fake_spawn.spawned[-1]["diet"]["skills"] == []
    home = Path(fake_spawn.spawned[-1]["home"])
    assert "## Messaging" not in _pulsar_md(home)

    # PATCH /diet without prompt_parts keeps the stored choice.
    r = await client.patch(f"/pulses/{pid}/diet", json={"skills": ["beta-skill"]})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings["diet"]["prompt_parts"] == ["identity"]

    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["diet"]["prompt_parts"] == ["identity"]
    assert fake_spawn.spawned[-1]["diet"]["skills"] == ["beta-skill"]
    text = _pulsar_md(Path(fake_spawn.spawned[-1]["home"]))
    assert "pulse id `" in text and "## Skill library" not in text

    # Explicit null in PATCH restores the full prompt.
    r = await client.patch(f"/pulses/{pid}/diet",
                           json={"skills": [], "prompt_parts": None})
    assert "prompt_parts" not in store.get_pulse(pid).settings["diet"]


@pytest.mark.anyio
async def test_api_omitted_parts_store_nothing(client, store, fake_spawn,
                                               skills_root):
    r = await client.post("/pulses", json={"title": "std",
                                           "model": "claude-haiku-4-5"})
    pid = r.json()["value"]["id"]
    assert "prompt_parts" not in store.get_pulse(pid).settings["diet"]


@pytest.mark.anyio
async def test_api_unknown_part_is_400(client, fake_spawn, skills_root):
    r = await client.post("/pulses", json={
        "title": "bad", "model": "claude-haiku-4-5",
        "prompt_parts": ["identity", "nope"]})
    assert r.status_code == 400, r.text
    assert "nope" in r.text
