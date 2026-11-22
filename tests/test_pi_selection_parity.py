"""Selection parity between the rendered skill catalog and Pi's argv.

The ``[loaded]`` marks in the rendered
PULSAR.md must agree with the skill bodies Pi's ``--system-prompt``
argument actually delivers, for every ``skill_catalog`` style and every
``skill_bodies`` selection — including malformed entries that fall back
to the legacy body file.
"""

from __future__ import annotations

import json

import pytest

import pulsar.launch.diet as diet_mod
import pulsar.launch.skills as skills_mod
import pulsar.vessel.pi as pi


@pytest.mark.parametrize("body_selection", [
    None, [], ["alpha"], ["beta"], 7, ["alpha", 7], ["alpha", "\0"],
])
@pytest.mark.parametrize("style", ["full", "names"])
def test_catalog_selection_equals_pi_argv(
        tmp_path, monkeypatch, style, body_selection):
    monkeypatch.setenv("PULSAR_PI_BIN", "/bin/true")

    root = tmp_path / "skills"
    alpha = root / "alpha"
    alpha.mkdir(parents=True)
    (alpha / "SKILL.md").write_text(
        "---\ndescription: alpha skill\n---\nBODY\n")
    (root / "catalog.json").write_text(json.dumps({"Lab": ["alpha"]}))
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", root)

    overrides = {"skill_catalog": style}
    if body_selection is not None:
        overrides["skill_bodies"] = body_selection

    home = tmp_path / "home"
    home.mkdir()
    # A legacy body file that disagrees with the override must never win.
    (home / "pi-skill-bodies.json").write_text("[]")

    diet_mod.materialize_diet(
        "p1", home,
        {"clean": True, "skills": ["alpha"], "harness_overrides": overrides},
        vessel="pi", title="Fixture")
    text = (home / "cli" / "PULSAR.md").read_bytes()

    sess = pi._Session(
        sid="s", cwd=str(home / "cli"), session_dir=home / "sd",
        cli_session_id="c", model="pi-glm53-full", env={})
    argv = pi.PiVessel()._build_command(sess, "hi")

    marked = b"alpha [loaded]" in text
    delivered = any(x.endswith("/alpha/SKILL.md") for x in argv)
    assert marked == delivered, (
        f"catalog marked={marked}, actual body delivered={delivered}")
