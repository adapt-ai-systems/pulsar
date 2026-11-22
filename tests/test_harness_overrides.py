"""Per-pulse harness overrides for the pi vessel (an experiment,
2026-10-22).

``harness_overrides`` on POST /pulses and PATCH /pulses/{id}/diet is a
validated object of pi-vessel knobs: stored in the diet (like prompt_parts,
so it survives restore/handoff/model switch), materialized to
``<pulse home>/harness-overrides.json`` at every spawn, and read by the pi
vessel at every launch. Unknown keys are a 422 naming the key — callers must
never think a knob applied when it did not. Absent = today's behaviour.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import pulsar.launch.diet as diet_mod

from tests.test_daemon import (  # noqa: F401
    FakeSpawn, FakeSock, _SockErr, _write_heartbeat,
    data_root, fake_spawn, fake_sock, store, client,
)


def test_materialize_writes_and_removes_the_file(tmp_path):
    home = tmp_path / "h"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [],
                     "harness_overrides": {"base_prompt": "B"}},
        vessel="claude-code", title="T")
    assert json.loads(
        (home / "harness-overrides.json").read_text()) == {"base_prompt": "B"}
    # A later spawn with no overrides removes the stale file.
    diet_mod.materialize_diet("p1", home, {"clean": True, "skills": []},
                              vessel="claude-code", title="T")
    assert not (home / "harness-overrides.json").exists()
    # An empty object is "no overrides" too — never a half-applied spawn.
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [], "harness_overrides": {}},
        vessel="claude-code", title="T")
    assert not (home / "harness-overrides.json").exists()


def test_materialize_bad_sidecar_path_is_nonfatal_both_ways(tmp_path, caplog):
    # A directory sitting at the sidecar path must
    # never block a spawn (any vessel, not only pi). It is renamed aside —
    # never rmtree'd — and a failed update cannot leave a stale override
    # applied: after the blockage, the path is empty.
    import logging
    home = tmp_path / "h"
    home.mkdir()
    (home / "harness-overrides.json").mkdir()  # the pathological path
    with caplog.at_level(logging.WARNING, logger="pulsar.launch.diet"):
        diet_mod.materialize_diet(
            "p1", home, {"clean": True, "skills": [],
                         "harness_overrides": {"base_prompt": "B"}},
            vessel="claude-code", title="T")
    assert not (home / "harness-overrides.json").exists()
    assert (home / "harness-overrides.json.stale").is_dir()
    assert "could not update harness-overrides.json" in caplog.text

    # The no-override removal path with a fresh directory in the way is
    # equally nonfatal.
    home2 = tmp_path / "h2"
    home2.mkdir()
    (home2 / "harness-overrides.json").mkdir()
    diet_mod.materialize_diet("p1", home2, {"clean": True, "skills": []},
                              vessel="claude-code", title="T")
    assert not (home2 / "harness-overrides.json").exists()
    assert (home2 / "harness-overrides.json.stale").is_dir()
def test_repeated_io_error_never_leaves_old_override_applied(tmp_path, caplog):
    # An earlier fault can leave a DIRECTORY at
    # the fixed .stale quarantine name. A later blocked update must then
    # quarantine to a collision-free name — it must never leave the OLD
    # override at the sidecar path where the vessel keeps reading it.
    import logging
    from pulsar.vessel import pi
    home = tmp_path / "h"
    cli = home / "cli"
    cli.mkdir(parents=True)
    (home / "harness-overrides.json.stale").mkdir()  # quarantine occupied
    # A first launch recovers and materializes a valid override.
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": [],
                     "harness_overrides": {"thinking": "low"}},
        vessel="claude-code", title="T")
    assert json.loads(
        (home / "harness-overrides.json").read_text()) == {"thinking": "low"}
    # A directory at the .tmp path blocks the next update (thinking=high).
    (home / "harness-overrides.json.tmp").mkdir()
    with caplog.at_level(logging.WARNING, logger="pulsar.launch.diet"):
        diet_mod.materialize_diet(
            "p1", home, {"clean": True, "skills": [],
                         "harness_overrides": {"thinking": "high"}},
            vessel="claude-code", title="T")
    # The stale override went to a collision-free quarantine name, so the
    # vessel reads NOTHING instead of the old "low".
    assert not (home / "harness-overrides.json").exists()
    quarantined = list(home.glob("harness-overrides.json.stale-*"))
    assert len(quarantined) == 1
    assert json.loads(quarantined[0].read_text()) == {"thinking": "low"}
    assert "stale override quarantined at" in caplog.text
    assert pi.pi_harness_overrides(cli) == {}


def test_unlink_fallback_when_quarantine_impossible(tmp_path, caplog,
                                                     monkeypatch):
    # If even the collision-free rename fails, the stale REGULAR sidecar is
    # unlinked so a failed update can never leave an old override applied.
    # (An unexpected directory is never removed — only logged loudly.)
    import logging
    home = tmp_path / "h"
    home.mkdir()
    (home / "harness-overrides.json").write_text(
        json.dumps({"thinking": "low"}))
    # Block the atomic write itself (a directory at the .tmp path), and
    # make every quarantine rename fail so only the unlink remains.
    (home / "harness-overrides.json.tmp").mkdir()

    def _no_rename(self, target):
        raise OSError("rename blocked")

    monkeypatch.setattr(Path, "rename", _no_rename)
    with caplog.at_level(logging.WARNING, logger="pulsar.launch.diet"):
        diet_mod.materialize_diet(
            "p1", home, {"clean": True, "skills": [],
                         "harness_overrides": {"thinking": "high"}},
            vessel="claude-code", title="T")
    assert not (home / "harness-overrides.json").exists()
    assert "stale file removed so it cannot apply" in caplog.text


@pytest.mark.anyio
async def test_api_unknown_override_key_is_422_naming_the_key(client):
    r = await client.post("/pulses", json={
        "title": "bad", "model": "claude-haiku-4-5",
        "harness_overrides": {"base_prompt": "x", "bogus_knob": 1}})
    assert r.status_code == 422, r.text
    assert "bogus_knob" in r.text


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [
    ("base_prompt", "bad\x00base"),
    ("todo_prompt", "bad\x00todo"),
    ("thinking", "bad\x00thinking"),
    ("base_prompt", "\ud800bad"),           # lone surrogate
    ("base_guidelines", ["ok", "bad\x00line"]),
    ("bash_prompt", "bad\x00bash"),
])
async def test_api_rejects_unusable_argv_text_with_422_naming_the_field(
        client, field, value):
    # An embedded NUL or lone surrogate can never
    # reach a real pi process — refuse it at the API boundary, naming the
    # field, instead of failing at spawn time. Sent as raw JSON content
    # because httpx cannot UTF-8-encode a lone surrogate itself — a hostile
    # client can send the escaped form exactly like this.
    body = {"title": "bad", "model": "claude-haiku-4-5",
            "harness_overrides": {field: value}}
    r = await client.post("/pulses", content=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert field in r.text


@pytest.mark.anyio
@pytest.mark.parametrize("field,value", [
    ("snippet_max", 150),           # below the 200 floor
    ("snippet_max", True),          # bool is not an int here
    ("tools", []),                  # empty allowlist
    ("tools", ["read,bash"]),       # comma — the allowlist is comma-joined
    ("tools", ["bad\x00tool"]),     # NUL cannot reach argv
    ("tools", ["ok", 5]),           # not all strings
    ("compaction", "maybe"),        # not a bool (pydantic lax-coerces
                                    # yes/no/on/off; anything else is a 422)
])
async def test_api_rejects_bad_brief_c_values_with_422(client, field, value):
    # Brief C validation at the API boundary: types, snippet_max >= 200,
    # non-empty tools, comma-free tool names. Same raw-content pattern as
    # the surrogate test (httpx cannot encode some of these itself).
    body = {"title": "bad", "model": "claude-haiku-4-5",
            "harness_overrides": {field: value}}
    r = await client.post("/pulses", content=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert field in r.text


@pytest.mark.anyio
async def test_api_accepts_all_four_brief_c_keys_end_to_end(
        client, store, fake_spawn):
    # All four brief C keys accepted on create: stored in the diet exactly
    # as sent and materialized to the pulse home sidecar the vessel reads.
    want = {"tools": ["read", "bash", "grep", "ls", "todo_write"],
            "bash_prompt": "CUSTOM BASH LINE",
            "snippet_max": 250,
            "compaction": True}
    r = await client.post("/pulses", json={
        "title": "labC", "model": "claude-haiku-4-5",
        "harness_overrides": want})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = Path(fake_spawn.spawned[-1]["home"])
    assert json.loads(
        (home / "harness-overrides.json").read_text()) == want


@pytest.mark.anyio
async def test_api_patch_diet_rejects_unusable_argv_text(client):
    r = await client.post("/pulses", json={"title": "lab",
                                           "model": "claude-haiku-4-5"})
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/diet", json={
        "skills": [], "harness_overrides": {"todo_prompt": "bad\x00todo"}})
    assert r.status_code == 422, r.text
    assert "todo_prompt" in r.text


@pytest.mark.anyio
async def test_api_omitted_overrides_store_nothing(client, store):
    r = await client.post("/pulses", json={"title": "std",
                                           "model": "claude-haiku-4-5"})
    pid = r.json()["value"]["id"]
    assert "harness_overrides" not in store.get_pulse(pid).settings["diet"]
    r = await client.post("/pulses", json={
        "title": "empty", "model": "claude-haiku-4-5",
        "harness_overrides": {}})
    pid = r.json()["value"]["id"]
    assert "harness_overrides" not in store.get_pulse(pid).settings["diet"]


@pytest.mark.anyio
async def test_api_overrides_survive_restore_and_diet_edits(
        client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "lab", "model": "claude-haiku-4-5",
        "harness_overrides": {"thinking": "off", "skill_bodies": None}})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    # Only the keys the caller sent are stored; explicit null is kept
    # (skill_bodies=null is arm A: every body).
    want = {"thinking": "off", "skill_bodies": None}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    # GET /pulses/{id} exposes the diet, so the lab can verify what the
    # pulse really got.
    r = await client.get(f"/pulses/{pid}")
    assert r.status_code == 200, r.text
    ho = r.json()["value"]["settings"]["diet"]["harness_overrides"]
    assert ho == want
    # The spawn materialized the file into the pulse home.
    home = Path(fake_spawn.spawned[-1]["home"])
    assert json.loads(
        (home / "harness-overrides.json").read_text()) == want

    # PATCH /diet without harness_overrides keeps the stored object.
    r = await client.patch(f"/pulses/{pid}/diet", json={"skills": []})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want

    # An explicit new object replaces it.
    r = await client.patch(f"/pulses/{pid}/diet", json={
        "skills": [], "harness_overrides": {"todo_prompt": "KEEP LIST"}})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == {
        "todo_prompt": "KEEP LIST"}

    # Explicit null clears it; the next spawn removes the materialized file.
    r = await client.patch(f"/pulses/{pid}/diet", json={
        "skills": [], "harness_overrides": None})
    assert r.status_code == 200, r.text
    assert "harness_overrides" not in store.get_pulse(pid).settings["diet"]
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    home = Path(fake_spawn.spawned[-1]["home"])
    assert not (home / "harness-overrides.json").exists()

    # And a restore with overrides set writes the file again.
    r = await client.patch(f"/pulses/{pid}/diet", json={
        "skills": [], "harness_overrides": {"thinking": "low"}})
    assert r.status_code == 200, r.text
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    home = Path(fake_spawn.spawned[-1]["home"])
    assert json.loads(
        (home / "harness-overrides.json").read_text()) == {"thinking": "low"}


# -- Compaction is RPC-only (2026-10-23) --------------------------------- #

@pytest.mark.anyio
async def test_api_rejects_compaction_on_print_mode_pi(client, store):
    # pi-v4-flash runs pi in print mode: a compaction retry's answer can
    # never reach stdout, so the knob is refused at create with a 422
    # naming the field and the model.
    r = await client.post("/pulses", json={
        "title": "print-compaction", "model": "pi-v4-flash",
        "harness_overrides": {"compaction": True}})
    assert r.status_code == 422, r.text
    assert "compaction" in r.text
    assert "pi-v4-flash" in r.text
    # Refused at the boundary: no pulse was created.
    assert not [p for p in store.list_pulses() if p.title == "print-compaction"]


@pytest.mark.anyio
async def test_api_accepts_compaction_on_rpc_pi(client, store):
    r = await client.post("/pulses", json={
        "title": "rpc-compaction", "model": "pi-qwen38-flash",
        "harness_overrides": {"compaction": True}})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == \
        {"compaction": True}


# -- Model switch must not bypass the compaction gate (2026-10-23) ------- #


@pytest.mark.anyio
async def test_switch_model_stopped_rejects_compaction_to_print_mode_pi(
        client, store, fake_spawn, fake_sock):
    # The stored diet harness_overrides ride every
    # future spawn, so switching an RPC pi pulse with compaction=true onto a
    # print-mode pi model must 422 before any mutation — the knob can never
    # apply there, and a silent accept would leave it stored but dead.
    r = await client.post("/pulses", json={
        "title": "rpc-comp", "model": "pi-qwen38-flash",
        "harness_overrides": {"compaction": True}})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    spawns_before = len(fake_spawn.spawned)

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "pi-v4-flash"})
    assert r.status_code == 422, r.text
    assert "compaction" in r.text
    assert "pi-v4-flash" in r.text
    # Refused before any mutation: model, vessel and the stored override
    # are all unchanged, and nothing was spawned.
    row = store.get_pulse(pid)
    assert (row.model, row.vessel, row.state) == \
        ("pi-qwen38-flash", "pi", "stopped")
    assert row.settings["diet"]["harness_overrides"] == {"compaction": True}
    assert len(fake_spawn.spawned) == spawns_before


@pytest.mark.anyio
async def test_switch_model_running_rejects_compaction_to_print_mode_pi(
        client, store, fake_spawn, fake_sock):
    # Same gate on the running (cross-vessel handoff) path: the 422 comes
    # before the runner is stopped, respawned or handed off.
    r = await client.post("/pulses", json={
        "title": "rpc-comp-run", "model": "pi-qwen38-flash",
        "harness_overrides": {"compaction": True}})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    spawns_before = len(fake_spawn.spawned)

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "pi-v4-flash"})
    assert r.status_code == 422, r.text
    assert "compaction" in r.text
    assert fake_sock.stopped == []          # old runner was not stopped
    assert len(fake_spawn.spawned) == spawns_before
    row = store.get_pulse(pid)
    assert (row.model, row.vessel) == ("pi-qwen38-flash", "pi")
    assert row.settings["diet"]["harness_overrides"] == {"compaction": True}


@pytest.mark.anyio
async def test_api_patch_diet_rejects_compaction_on_print_mode_pi(client):
    r = await client.post("/pulses", json={"title": "print-pi",
                                           "model": "pi-v4-flash"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/diet", json={
        "skills": [], "harness_overrides": {"compaction": True}})
    assert r.status_code == 422, r.text
    assert "compaction" in r.text


@pytest.mark.anyio
async def test_api_patch_diet_accepts_compaction_on_rpc_pi(client):
    r = await client.post("/pulses", json={"title": "rpc-pi",
                                           "model": "pi-qwen38-flash"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/diet", json={
        "skills": [], "harness_overrides": {"compaction": True}})
    assert r.status_code == 200, r.text
