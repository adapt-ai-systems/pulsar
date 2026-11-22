"""Review regression cases (expected contract behavior).

Accepted-contract cases the review findings were demonstrated with.
No real spawns.
"""

from __future__ import annotations

import json

import pytest

import pulsar.launch.diet as diet_mod
import pulsar.launch.skills as skills_mod
import pulsar.vessel.pi as pi
from pulsar.daemon.schemas import HarnessOverridesBody, harness_overrides_json
from pulsar.store import Store


@pytest.fixture(autouse=True)
def skills_root(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")
    root = tmp_path / "skills"
    root.mkdir()
    for name in ("alpha", "beta"):
        d = root / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\ndescription: {name} description\n---\n"
            f"{name} BODY\n")
    (root / "catalog.json").write_text(
        json.dumps({"Lab": ["alpha", "beta"]}))
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", root)


def _render(home, overrides):
    diet_mod.materialize_diet(
        "p1", home,
        {"clean": True, "skills": ["alpha"], "harness_overrides": overrides},
        vessel="pi", title="Fixture")
    return (home / "cli" / "PULSAR.md").read_bytes()


def test_override_empty_disables_default_skills(tmp_path):
    store = Store(str(tmp_path / "store.db"))
    try:
        store.put_setting("pulse.default_skills", ["beta"])
        actual = store.diet_with_defaults({
            "clean": True, "skills": ["alpha"],
            "harness_overrides": {"prompt_parts": []}})
        assert actual["skills"] == ["alpha"]
    finally:
        store.close()


def test_override_empty_disables_todo(tmp_path):
    home = tmp_path / "h"
    _render(home, {"prompt_parts": []})
    overrides = pi.pi_harness_overrides(home / "cli")
    assert pi.pi_todo_args(home / "cli", overrides) == []


def test_null_keeps_all_parts_semantics():
    assert harness_overrides_json(
        HarnessOverridesBody(prompt_parts=None)) == {"prompt_parts": None}


@pytest.mark.parametrize("md", [
    "## Time zone\r\n\r\nUTC\r\n",
    "## Time zone\n\nUTC\n\n\n\n",
])
def test_part_text_is_byte_verbatim(tmp_path, md):
    assert md.encode() in _render(tmp_path / "h", {"part_text": {
        "time_zone": md}})


def test_replacement_does_not_reparse_override_headings(tmp_path):
    zone = ("## Custom zone\n\n## Messaging other pulses\n"
            "This is inside the zone override.")
    msg = ("## Custom message\n\n"
           "This replaces the original messaging section.")
    doc = _render(tmp_path / "h", {"part_text": {
        "time_zone": zone, "messaging": msg}})
    assert zone.encode() in doc
    assert b"Pulses are parallel sessions" not in doc


def test_names_marks_only_actual_loaded_skills(tmp_path):
    doc = _render(tmp_path / "h", {"skill_catalog": "names",
                                   "skill_bodies": []})
    assert b"alpha [loaded]" not in doc


@pytest.mark.parametrize("overrides", [
    {"part_text": "bad"},
    {"part_text": {"time_zone": 7}},
    {"prompt_parts": 4},
    {"part_text": {"time_zone": "\ud800"}},
])
def test_malformed_persisted_overrides_do_not_block_materialize(
        tmp_path, overrides):
    # Production-safety requirement: corrupt disk/DB input must degrade
    # to defaults.
    assert _render(tmp_path / "h", overrides)
