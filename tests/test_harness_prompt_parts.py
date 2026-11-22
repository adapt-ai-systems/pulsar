"""harness_overrides context-render knobs (an experiment, Brief D):

``prompt_parts``, ``part_text``, ``skill_catalog`` inside
``harness_overrides`` reshape the rendered PULSAR.md at materialize time.
All three are optional: an absent key renders today's document,
byte-for-byte. Validation lives in the schema (HarnessOverridesBody):
an unknown part name or a bad skill_catalog value is a 422 naming it.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pulsar.launch.skills as skills_mod
import pulsar.daemon.schemas as schemas
import pulsar.launch.diet as diet_mod

from tests.test_daemon import (  # noqa: F401
    FakeSock, _SockErr, _write_heartbeat, client, data_root, fake_sock,
    fake_spawn, store,
)


@pytest.fixture()
def skills_root(tmp_path, monkeypatch):
    """A tiny skill library so the catalog render is deterministic."""
    root = tmp_path / "skills-root"
    root.mkdir()
    for name, desc in (("alpha", "Do alpha things well"),
                       ("beta", "Beta helper")):
        d = root / name
        d.mkdir()
        (d / "SKILL.md").write_text(
            f"---\ndescription: {desc}\n---\n\n# {name}\n")
    (root / "catalog.json").write_text(json.dumps(
        {"Lab group": ["alpha", "beta"]}))
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", root)
    return root


def _render(home: Path, diet: dict | None) -> str:
    diet_mod.materialize_diet("p1", home, diet, vessel="claude-code",
                              title="T")
    return (home / "cli" / "PULSAR.md").read_text()


def _ho(**kw) -> dict:
    return schemas.harness_overrides_json(
        schemas.HarnessOverridesBody(**kw))


# --------------------------------------------------------------------------- #
# prompt_parts
# --------------------------------------------------------------------------- #
def test_prompt_parts_absent_is_today(tmp_path, skills_root):
    home = tmp_path / "home"
    doc = _render(home, {"clean": True, "skills": []})
    assert "## Time zone" in doc and "## Watches" in doc
    # A harness_overrides object without prompt_parts changes nothing.
    other = tmp_path / "home2"
    doc2 = _render(other, {"clean": True, "skills": [],
                           "harness_overrides": _ho(thinking="low")})
    assert doc2 == doc


def test_prompt_parts_from_overrides(tmp_path, skills_root):
    home = tmp_path / "home"
    doc = _render(home, {"clean": True, "skills": [],
                         "harness_overrides": _ho(
                             prompt_parts=["identity", "messaging"])})
    assert doc.count("## ") <= 2          # only the allowlisted sections
    assert "## Time zone" not in doc
    assert "## Messaging other pulses" in doc


def test_prompt_parts_overrides_win_over_diet(tmp_path, skills_root):
    home = tmp_path / "home"
    doc = _render(home, {"clean": True, "skills": [],
                         "prompt_parts": ["identity", "watches"],
                         "harness_overrides": _ho(
                             prompt_parts=["identity", "messaging"])})
    assert "## Messaging other pulses" in doc
    assert "## Watches" not in doc        # the diet's list lost


def test_prompt_parts_diet_still_applies_alone(tmp_path, skills_root):
    home = tmp_path / "home"
    doc = _render(home, {"clean": True, "skills": [],
                         "prompt_parts": ["identity", "watches"]})
    assert "## Watches" in doc
    assert "## Messaging other pulses" not in doc


# --------------------------------------------------------------------------- #
# part_text
# --------------------------------------------------------------------------- #
def test_part_text_absent_is_today(tmp_path, skills_root):
    doc = _render(tmp_path / "home", {"clean": True, "skills": []})
    assert "## Time zone" in doc
    assert "All times you give the user" in doc


def test_part_text_replaces_section_verbatim(tmp_path, skills_root):
    home = tmp_path / "home"
    md = "## Time zone\n\nAll times are UTC. Deal with it."
    doc = _render(home, {"clean": True, "skills": [],
                         "harness_overrides": _ho(
                             part_text={"time_zone": md})})
    assert md in doc                      # verbatim, where the part was
    assert "All times you give the user" not in doc
    # The neighbors keep their order around the replaced part.
    assert doc.index("## How Pulsar runs you") > doc.index(md)


def test_part_text_replaces_appended_parts(tmp_path, skills_root):
    md = "## Skill library\n\nThere is no catalog. Ask the user."
    doc = _render(tmp_path / "home", {"clean": True, "skills": [],
                                      "harness_overrides": _ho(
                                          part_text={"skill_catalog": md})})
    assert md in doc
    assert "### Lab group" not in doc     # the real catalog is gone


def test_part_text_skipped_when_part_filtered_out(tmp_path, skills_root,
                                                  caplog):
    home = tmp_path / "home"
    doc = _render(home, {"clean": True, "skills": [],
                         "harness_overrides": _ho(
                             prompt_parts=["identity", "messaging"],
                             part_text={"time_zone": "## No tz\n\nX."})})
    assert "## No tz" not in doc          # part_text never resurrects a part
    with caplog.at_level("WARNING", logger="pulsar.spawn"):
        _render(tmp_path / "home2", {"clean": True, "skills": [],
                                     "harness_overrides": _ho(
                                         part_text={"time_zone": "## No tz"})})
    assert any("not rendered" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# skill_catalog
# --------------------------------------------------------------------------- #
def test_skill_catalog_absent_is_full(tmp_path, skills_root):
    doc = _render(tmp_path / "home", {"clean": True, "skills": ["alpha"]})
    assert "## Skill library" in doc
    assert "- alpha [loaded] — Do alpha things well" in doc
    assert "- beta — Beta helper" in doc


def test_skill_catalog_names(tmp_path, skills_root):
    doc = _render(tmp_path / "home", {"clean": True, "skills": ["alpha"],
                                      "harness_overrides": _ho(
                                          skill_catalog="names")})
    assert "### Lab group" in doc
    assert "alpha [loaded], beta" in doc  # one line, names only, marks hold
    assert "Do alpha things well" not in doc


def test_skill_catalog_none(tmp_path, skills_root):
    doc = _render(tmp_path / "home", {"clean": True, "skills": [],
                                      "harness_overrides": _ho(
                                          skill_catalog="none")})
    assert "## Skill library" not in doc
    assert "### Lab group" not in doc


def test_skill_catalog_part_text_wins_over_style(tmp_path, skills_root):
    md = "## Skill library\n\nCustom catalog text."
    doc = _render(tmp_path / "home", {"clean": True, "skills": [],
                                      "harness_overrides": _ho(
                                          skill_catalog="names",
                                          part_text={"skill_catalog": md})})
    assert md in doc
    assert "### Lab group" not in doc


# --------------------------------------------------------------------------- #
# schema 422s (through the API, like the PR-1 tests)
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_api_unknown_part_name_is_422_naming_it(client):
    r = await client.post("/pulses", json={
        "title": "bad-part", "model": "claude-haiku-4-5",
        "harness_overrides": {"part_text": {"no_such_part": "x"}}})
    assert r.status_code == 422, r.text
    assert "no_such_part" in r.text


@pytest.mark.anyio
async def test_api_unknown_prompt_part_is_422_naming_it(client):
    r = await client.post("/pulses", json={
        "title": "bad-pp", "model": "claude-haiku-4-5",
        "harness_overrides": {"prompt_parts": ["nope"]}})
    assert r.status_code == 422, r.text
    assert "nope" in r.text


@pytest.mark.anyio
async def test_api_bad_skill_catalog_is_422(client):
    r = await client.post("/pulses", json={
        "title": "bad-cat", "model": "claude-haiku-4-5",
        "harness_overrides": {"skill_catalog": "titles"}})
    assert r.status_code == 422, r.text
    assert "skill_catalog" in r.text


# --------------------------------------------------------------------------- #
# Edge cases found in review (2026-10-23)
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
@pytest.mark.parametrize("value", ["\ud800bad", "bad\x00tz"])
async def test_api_rejects_unusable_part_text_with_422_naming_it(
        client, value):
    # F1: a part_text value that can never be written to disk or reach a
    # process (lone surrogate / embedded NUL) is refused at the API
    # boundary, naming the field. Escaped-surrogate JSON goes on the wire
    # as raw content — httpx cannot UTF-8-encode it itself.
    body = {"title": "bad-part-text", "model": "claude-haiku-4-5",
            "harness_overrides": {"part_text": {"time_zone": value}}}
    r = await client.post("/pulses", content=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert "part_text" in r.text


@pytest.mark.anyio
async def test_api_corrupt_stored_knobs_degrade_to_defaults_across_restore_and_reset(  # noqa: E501
        client, store, fake_spawn):
    # F1: corrupt persisted knobs (written straight into the DB, bypassing
    # the API validators) must never block a spawn — pause/restore AND reset
    # both return 200 and PULSAR.md renders the default document.
    r = await client.post("/pulses", json={
        "title": "corrupt", "model": "claude-haiku-4-5",
        "harness_overrides": {"prompt_parts": ["identity"]}})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    settings = store.get_pulse(pid).settings
    diet = dict(settings["diet"])
    diet["harness_overrides"] = {
        "part_text": "not-an-object",
        "prompt_parts": "not-a-list",
        "skill_catalog": 42}
    store.update_settings(pid, {"diet": diet})

    for verb in ("restore", "reset"):
        await client.post(f"/pulses/{pid}/pause")
        r = await client.post(f"/pulses/{pid}/{verb}")
        assert r.status_code == 200, (verb, r.text)
        doc = (Path(fake_spawn.spawned[-1]["home"]) / "cli" /
               "PULSAR.md").read_text()
        assert "## Time zone" in doc
        assert "All times you give the user" in doc   # defaults rendered
        assert "not-an-object" not in doc


def test_diet_with_defaults_prompt_parts_exclusion_and_re_enable(store):
    # F2: ONE effective allowlist — a harness-overrides prompt_parts list
    # reaches the default-skills merge too: [] opts out, a re-including
    # list re-enables on a lean diet, explicit null means all parts.
    store.put_setting("pulse.default_skills", ["alpha"])
    base = {"clean": True, "skills": ["beta"],
            "prompt_parts": ["identity"]}
    # the override's [] wins over the diet's list: no default skills
    d = store.diet_with_defaults({**base, "harness_overrides": {
        "prompt_parts": []}})
    assert d["skills"] == ["beta"]
    # an override list with default_skills re-enables the merge
    d = store.diet_with_defaults({**base, "harness_overrides": {
        "prompt_parts": ["default_skills"]}})
    assert d["skills"] == ["beta", "alpha"]
    # explicit null = all parts: the diet's lean list is overridden
    d = store.diet_with_defaults({**base, "harness_overrides": {
        "prompt_parts": None}})
    assert d["skills"] == ["beta", "alpha"]
    # no override: the diet's own lean list still excludes them
    d = store.diet_with_defaults(dict(base))
    assert d["skills"] == ["beta"]


@pytest.mark.parametrize("reversed_order", [False, True])
def test_part_text_colliding_heading_both_dict_orders(
        tmp_path, skills_root, reversed_order):
    # F3: replacements are computed on the ORIGINAL document, so dict order
    # cannot matter — even when one override's text contains a heading that
    # collides with another part's section header.
    zone = ("## Custom zone\n\n## Messaging other pulses\n"
            "This is inside the zone override.")
    msg = "## Custom message\n\nThis replaces the original messaging section."
    pt = {"time_zone": zone, "messaging": msg}
    if reversed_order:
        pt = {"messaging": msg, "time_zone": zone}
    doc = _render(tmp_path / "h", {"clean": True, "skills": [],
                                   "harness_overrides": _ho(part_text=pt)})
    assert zone in doc                       # verbatim, where the part was
    assert msg in doc
    assert "Pulses are parallel sessions" not in doc  # original gone


def test_prompt_parts_omitted_null_empty_vs_diet_list(tmp_path, skills_root):
    # F4: with a diet prompt_parts list set, an OMITTED override lets the
    # diet list apply, an explicit NULL means ALL parts (never the diet's
    # list, never []), and an EMPTY list means NO parts.
    base = {"clean": True, "skills": [],
            "prompt_parts": ["identity", "messaging"]}
    doc = _render(tmp_path / "h1", dict(base))
    assert "## Messaging other pulses" in doc
    assert "## Watches" not in doc
    doc = _render(tmp_path / "h2", {**base, "harness_overrides": _ho(
        prompt_parts=None)})
    assert "## Watches" in doc
    doc = _render(tmp_path / "h3", {**base, "harness_overrides": _ho(
        prompt_parts=[])})
    assert "## " not in doc
    assert "# Pulsar pulse:" not in doc


@pytest.mark.parametrize("override,legacy_file,marked", [
    ("absent", None, {"alpha", "beta"}),   # no override, no file: arm A
    ("null", None, {"alpha", "beta"}),     # explicit null: arm A too
    ([], None, set()),                       # []: arm B, nothing marked
    (["alpha"], None, {"alpha"}),            # a list: arm C
    ("absent", ["beta"], {"beta"}),         # the legacy file alone
    (["alpha"], ["beta"], {"alpha"}),        # the override WINS over the file
])
def test_pi_catalog_marks_effective_bodies(tmp_path, skills_root,
                                           override, legacy_file, marked):
    # F5: the [loaded] marks must match the bodies pi will actually append
    # at launch — the SAME effective selection pi_skill_args uses (the
    # harness-overrides key first, then the legacy pi-skill-bodies.json).
    desc = {"alpha": "Do alpha things well", "beta": "Beta helper"}
    home = tmp_path / "h"
    home.mkdir(parents=True)
    if legacy_file is not None:
        (home / "pi-skill-bodies.json").write_text(json.dumps(legacy_file))
    ho = {}
    if override == "null":
        ho = _ho(skill_bodies=None)
    elif override != "absent":
        ho = _ho(skill_bodies=override)
    diet_mod.materialize_diet("p1", home, {"clean": True,
                                           "skills": ["alpha", "beta"],
                                           "harness_overrides": ho},
                              vessel="pi", title="T")
    doc = (home / "cli" / "PULSAR.md").read_text()
    for name, d in desc.items():
        assert (f"- {name} [loaded] — {d}" in doc) == (name in marked)
        assert (f"- {name} — {d}" in doc) == (name not in marked)


def test_part_text_file_bytes_verbatim_crlf(tmp_path, skills_root):
    # F6: the override's bytes land in PULSAR.md unchanged — no CRLF
    # normalization, no trailing-newline strip (read as bytes).
    md = "## Time zone\r\n\r\nAll times are UTC.\r\n"
    home = tmp_path / "h"
    diet_mod.materialize_diet("p1", home, {"clean": True, "skills": [],
                                           "harness_overrides": _ho(
                                               part_text={"time_zone": md})},
                              vessel="claude-code", title="T")
    raw = (home / "cli" / "PULSAR.md").read_bytes()
    assert md.encode() in raw
    assert b"All times you give the user" not in raw


@pytest.mark.anyio
async def test_api_render_keys_round_trip_through_diet(client, store,
                                                       fake_spawn):
    r = await client.post("/pulses", json={
        "title": "lab-d", "model": "claude-haiku-4-5",
        "harness_overrides": {"prompt_parts": ["identity", "messaging"],
                              "part_text": {"messaging": "## Messaging\n\nX."},
                              "skill_catalog": "names"}})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    want = {"prompt_parts": ["identity", "messaging"],
            "part_text": {"messaging": "## Messaging\n\nX."},
            "skill_catalog": "names"}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = Path(fake_spawn.spawned[-1]["home"])
    assert json.loads(
        (home / "harness-overrides.json").read_text()) == want
