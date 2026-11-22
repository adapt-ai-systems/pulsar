"""Clean-by-default diet — spawn materialization + daemon API.

Unit tests drive ``diet_mod.materialize_diet`` against tmp dirs (skills root
monkeypatched); daemon tests reuse the fake-spawn harness from
``test_daemon`` (its FakeSpawn calls the REAL materialize_diet, so
diet.json + symlinks are asserted on disk end-to-end).
"""

from __future__ import annotations

import json
import tomllib
import types
from pathlib import Path

import pytest

import pulsar.launch.context as context_mod
import pulsar.launch.diet as diet_mod
import pulsar.launch.cages as cages_mod
import pulsar.launch.skills as skills_mod
import pulsar.launch.units as units_mod
import pulsar.vessel.profiles as profiles_mod

# Captured at import, BEFORE conftest's autouse no_real_systemd patches the
# name — the argv tests below drive the REAL spawn_runner with _run stubbed,
# so nothing reaches systemd.
_REAL_SPAWN_RUNNER = units_mod.spawn_runner

# Reuse the daemon test harness (fixtures resolve via direct import).
from tests.test_daemon import (  # noqa: F401
    FakeSpawn, FakeSock, _SockErr, _write_heartbeat,
    data_root, fake_spawn, fake_sock, store, client,
)


# --------------------------------------------------------------------------- #
# materialize_diet unit tests
# --------------------------------------------------------------------------- #
@pytest.fixture()
def skills_root(tmp_path, monkeypatch):
    root = tmp_path / "skills-root"
    root.mkdir()
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", root)
    return root


def test_none_diet_writes_clean_default(tmp_path, skills_root):
    home = tmp_path / "home"
    written = diet_mod.materialize_diet("p1", home, None)
    assert written == {"clean": True}
    assert json.loads((home / "diet.json").read_text()) == {"clean": True}
    # no half-written tmp file left behind
    assert not (home / "diet.json.tmp").exists()


def test_skills_symlinked_and_stale_cleared(tmp_path, skills_root):
    (skills_root / "alpha-skill").mkdir()
    (skills_root / "beta-skill").mkdir()
    home = tmp_path / "home"
    links = home / "cli" / ".claude" / "skills"

    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "memory": False, "skills": ["alpha-skill"], "cages": []})
    assert (links / "alpha-skill").is_symlink()
    assert (links / "alpha-skill").resolve() == (skills_root / "alpha-skill")

    # Respawn with a different skill set: old link cleared, new one made.
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "memory": False, "skills": ["beta-skill"], "cages": []})
    assert not (links / "alpha-skill").exists()
    assert (links / "beta-skill").is_symlink()


def test_only_diet_skills_are_linked(tmp_path, skills_root):
    """Nothing is forced in: the clean default links no skills at all, and a
    diet links exactly what it names (former baseline skills included only
    when checked)."""
    for name in ("alpha-skill", "plain-style", "gamma-skill"):
        (skills_root / name).mkdir()
    home = tmp_path / "home"
    links = home / "cli" / ".claude" / "skills"

    diet_mod.materialize_diet("p1", home, None)
    assert sorted(p.name for p in links.iterdir()) == []

    diet = {"clean": True, "skills": ["alpha-skill"], "cages": []}
    diet_mod.materialize_diet("p1", home, diet)
    assert sorted(p.name for p in links.iterdir()) == ["alpha-skill"]

    diet = {"clean": True, "skills": ["alpha-skill", "plain-style"]}
    diet_mod.materialize_diet("p1", home, diet)
    assert sorted(p.name for p in links.iterdir()) == ["alpha-skill", "plain-style"]


def test_missing_or_invalid_skill_skipped(tmp_path, skills_root, caplog):
    home = tmp_path / "home"
    with caplog.at_level("ERROR", logger="pulsar.spawn"):
        diet_mod.materialize_diet(
            "p1", home,
            {"clean": True, "skills": ["nope", "../etc", "a/b"]})
    links = home / "cli" / ".claude" / "skills"
    assert sorted(e.name for e in links.iterdir()) == []
    # every skip logged loudly (standing rule)
    assert sum("skipped" in r.message for r in caplog.records) == 3
    # diet.json still written despite the skips
    assert (home / "diet.json").exists()


def test_full_fat_diet_links_nothing(tmp_path, skills_root):
    (skills_root / "beta-skill").mkdir()
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": False, "skills": ["beta-skill"], "cages": []})
    assert json.loads((home / "diet.json").read_text())["clean"] is False
    assert list((home / "cli" / ".claude" / "skills").iterdir()) == []


# --------------------------------------------------------------------------- #
# Per-vessel skill delivery (skills are NOT claude-only, 2026-07-27)
# --------------------------------------------------------------------------- #
@pytest.fixture()
def codex_real_home(tmp_path, monkeypatch):
    """Stand-in for ~/.codex — the private home symlinks its auth.json."""
    real = tmp_path / "dot-codex"
    real.mkdir()
    (real / "auth.json").write_text('{"tokens": "real"}')
    (real / "AGENTS.md").write_text("# always load weave\n")
    monkeypatch.setattr(profiles_mod, "CODEX_REAL_HOME", real)
    return real


@pytest.fixture()
def grok_real_home(tmp_path, monkeypatch):
    """Stand-in for ambient ~/.grok state a clean pulse must not inherit."""
    real = tmp_path / "dot-grok"
    (real / "bundled" / "skills" / "superpowers").mkdir(parents=True)
    (real / "skills" / "ambient-grok-skill").mkdir(parents=True)
    (real / "auth.json").write_text('{"token": "real"}')
    (real / "config.toml").write_text(
        '[plugins]\npaths = ["~/ambient-plugin"]\n')
    claude_plugins = tmp_path / "installed_plugins.json"
    claude_plugins.write_text(json.dumps({
        "version": 2,
        "plugins": {
            "superpowers@claude-plugins-official": [],
            "caveman@caveman": [],
        },
    }))
    monkeypatch.setattr(profiles_mod, "GROK_REAL_HOME", real)
    monkeypatch.setattr(profiles_mod, "GROK_CLAUDE_PLUGINS_FILE", claude_plugins)
    return real


def test_codex_agents_md_carries_context_not_a_skills_index(tmp_path,
                                                            skills_root,
                                                            codex_real_home):
    # Skills reach codex natively (CODEX_HOME/skills); AGENTS.md is the pulse
    # context — the same file every vessel gets, catalog included (clean means
    # no ambient vessel config, not no index). The retired per-vessel AGENTS.md
    # skill index (which drifted) is gone: the context is regenerated every
    # spawn from one generator.
    (skills_root / "beta-skill").mkdir()
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["beta-skill"], "cages": []}, vessel="codex")
    body = (home / "cli" / "AGENTS.md").read_text()
    assert context_mod._CTX_MARKER in body
    assert body == (home / "cli" / "PULSAR.md").read_text()


def test_codex_private_home_holds_only_the_optins(tmp_path, skills_root,
                                                  codex_real_home):
    (skills_root / "beta-skill").mkdir()
    (skills_root / "alpha-skill").mkdir()
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["beta-skill"], "cages": []}, vessel="codex")

    ch = profiles_mod.codex_home(home)
    assert (sorted(p.name for p in (ch / "skills").iterdir())
            == ["beta-skill"])
    assert (ch / "skills" / "beta-skill").resolve() == (skills_root / "beta-skill")
    # Its own config: the cwd trusted, and NOTHING from ~/.codex — the
    # curated-plugin block there is what pulled in the connector set.
    cfg = (ch / "config.toml").read_text()
    assert str(home / "cli") in cfg
    assert "[plugins" not in cfg  # the ~/.codex block that pulled them in
    assert tomllib.loads(cfg)["skills"]["bundled"]["enabled"] is False
    # The login is shared by symlink, never copied.
    auth = ch / "auth.json"
    assert auth.is_symlink() and auth.resolve() == codex_real_home / "auth.json"
    # And the dir the unit binds over ~/.agents exists to bind.
    assert profiles_mod.codex_empty_agents(home).is_dir()
    assert list(profiles_mod.codex_empty_agents(home).iterdir()) == []


def test_codex_private_home_clears_stale_skills(tmp_path, skills_root,
                                                codex_real_home):
    (skills_root / "beta-skill").mkdir()
    (skills_root / "alpha-skill").mkdir()
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["alpha-skill"], "cages": []}, vessel="codex")
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["beta-skill"], "cages": []}, vessel="codex")
    assert (sorted(p.name for p in
                   (profiles_mod.codex_home(home) / "skills").iterdir())
            == ["beta-skill"])


def test_codex_private_home_appends_caller_workload_config(
    tmp_path, skills_root, codex_real_home
):
    home = tmp_path / "home"
    fragment = home / "cli" / ".codex" / "pulsar-config.toml"
    fragment.parent.mkdir(parents=True)
    fragment.write_text(
        '[mcp_servers.workload]\ncommand = "python3"\nargs = ["tools.py"]\n'
    )

    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="codex")

    cfg = (profiles_mod.codex_home(home) / "config.toml").read_text()
    assert "# caller-owned workload config" in cfg
    assert "[mcp_servers.workload]" in cfg
    assert 'args = ["tools.py"]' in cfg


def test_codex_auth_symlink_healed_on_respawn(tmp_path, skills_root,
                                              codex_real_home):
    # If the CLI ever replaces the link with a real file (token refresh), the
    # next spawn must restore it — otherwise the pulse drifts onto a private
    # token while the user's own codex refreshes the real one.
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="codex")
    auth = profiles_mod.codex_home(home) / "auth.json"
    auth.unlink()
    auth.write_text('{"tokens": "stale copy"}')
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="codex")
    assert auth.is_symlink() and auth.resolve() == codex_real_home / "auth.json"


def test_codex_full_fat_gets_no_private_home(tmp_path, skills_root,
                                             codex_real_home):
    # clean=False means "give me my whole setup" — ambient ~/.codex is the point.
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": False}, vessel="codex")
    assert not profiles_mod.codex_home(home).exists()


@pytest.mark.parametrize("selected", [[], ["beta-skill"]])
def test_clean_codex_omits_unselected_library(
    tmp_path, skills_root, codex_real_home, selected
):
    # Real front matter matters: empty fixture folders hid the catalog leak.
    for name in ("beta-skill", "ambient"):
        folder = skills_root / name
        folder.mkdir()
        (folder / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: Load {name} automatically.\n---\n"
        )
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": selected}, vessel="codex")
    # Clean = no ambient vessel config, NOT no index. The catalog (Pulsar
    # content) rides the context so an unselected skill is discoverable by
    # path; only the SELECTED skills are pre-loaded.
    for filename in ("AGENTS.md", "PULSAR.md"):
        body = (home / "cli" / filename).read_text()
        assert context_mod._CTX_MARKER in body
        assert "## Skill library" in body
        assert "Load ambient automatically" in body
        assert "ambient/SKILL.md" not in body.split("## Skill library")[0]
    skills = home / "codex-home" / "skills"
    assert sorted(p.name for p in skills.iterdir()) == selected


def test_grok_private_home_holds_only_selected_skills(
    tmp_path, skills_root, grok_real_home
):
    (skills_root / "beta-skill").mkdir()
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["beta-skill"], "cages": []}, vessel="grok")

    gh = profiles_mod.grok_home(home)
    native_skills = home / "cli" / ".grok" / "skills"
    assert (sorted(p.name for p in native_skills.iterdir())
            == ["beta-skill"])
    assert (native_skills / "beta-skill").is_dir()
    assert not (native_skills / "beta-skill").is_symlink()
    assert (gh / "auth.json").is_symlink()
    assert (gh / "auth.json").resolve() == grok_real_home / "auth.json"
    cfg = (gh / "config.toml").read_text()
    assert "skills = false" in cfg
    assert 'disabled = ["caveman", "superpowers"]' in cfg
    assert "respect_gitignore = false" in cfg
    assert str(grok_real_home / "skills") in cfg
    assert str(grok_real_home / "bundled" / "skills") in cfg
    assert profiles_mod.grok_empty_agents(home).is_dir()
    assert profiles_mod.grok_empty_claude_plugins(home).is_dir()


def test_clean_grok_context_keeps_pulsar_skill_catalog(
    tmp_path, skills_root, grok_real_home
):
    # The catalog is Pulsar's index, not grok clutter — a clean grok pulse
    # gets it like every other vessel (nothing pre-loaded).
    (skills_root / "ambient").mkdir()
    (skills_root / "ambient" / "SKILL.md").write_text(
        "---\nname: ambient\ndescription: Findable by path.\n---\n")
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="grok")
    body = (home / "cli" / "AGENTS.md").read_text()
    assert context_mod._CTX_MARKER in body
    assert "## Skill library" in body
    assert "Findable by path" in body
    assert "ambient [loaded]" not in body   # findable, not pre-loaded


def test_clean_pi_gets_private_pi_home_provider(tmp_path, skills_root,
                                                monkeypatch):
    # REVIEW 13: a clean pi pulse must get its OWN PI_HOME holding the
    # baseline providers — never the shared ~/.pi.
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="pi")
    models = profiles_mod.pi_home(home) / "agent" / "models.json"
    assert models.is_file()
    data = json.loads(models.read_text())
    prov = data["providers"]["dspair"]
    assert prov["baseUrl"] == "http://192.0.2.10:8000/v1"
    assert [m["id"] for m in prov["models"]] == ["deepseek-v4-flash"]
    # v4 flash now accepts images: the model config must declare image input so
    # pi sends image bytes (read tool gates on model.input) instead of omitting.
    assert "image" in prov["models"][0]["input"]
    local = data["providers"]["localqwen"]
    assert local["baseUrl"] == "http://192.0.2.12:8888/v1"
    assert [m["id"] for m in local["models"]] == [
        "qwen3.8-27b", "qwen3.8-27b-nvfp4", "qwen3.6-35b-a3b-fast"]
    q27 = next(m for m in local["models"] if m["id"] == "qwen3.8-27b")
    assert q27["reasoning"] is True
    assert q27["input"] == ["text", "image"]
    assert q27["contextWindow"] == 196608
    assert q27["thinkingLevelMap"] == {"off": "none", "minimal": None,
        "low": "low", "medium": "medium", "high": None, "xhigh": "xhigh"}
    # EXL3 27B reads thinking control only via
    # chat_template_kwargs.enable_thinking (qwen-chat-template). The old
    # "openai" format sent nothing on effort-off, so the server's always-on
    # template thinking ate the whole 8192 budget — silent empty-turn deaths.
    # maxTokens 32768 fits a heavy think plus a full reply under the window.
    assert q27["maxTokens"] == 32768
    assert q27["compat"]["thinkingFormat"] == "qwen-chat-template"
    assert q27["compat"]["supportsReasoningEffort"] is True
    regular = data["providers"]["glm53local"]
    flash = next(m for m in regular["models"] if m["id"] == "glm-5.3-flash")
    assert regular["baseUrl"] == "http://192.0.2.10:8000/v1"
    assert flash["contextWindow"] == 1048576
    assert flash["maxTokens"] == 131072
    assert flash["input"] == ["text", "image"]
    glm = data["providers"]["glmlocal"]
    assert glm["baseUrl"] == "http://192.0.2.11:8888/v1"
    glm_ids = [m["id"] for m in glm["models"]]
    # Qwen3.8-Flash-Next must resolve even when the host ~/.pi provider map
    # (merged over this built-in) replaces the glmlocal entry.
    assert "Qwen3.8-Flash-Next-NVFP4" in glm_ids
    assert "glm-5.2" in glm_ids
    # reasoning MUST be True for the qwen-chat-template format: pi-ai only
    # sends chat_template_kwargs.enable_thinking when model.reasoning is set,
    # and the served template defaults thinking ON. With False, `--thinking off`
    # turns still emitted hidden reasoning.
    q = next(m for m in glm["models"] if m["id"] == "Qwen3.8-Flash-Next-NVFP4")
    assert q["thinkingLevelMap"]["high"] == "xhigh"
    assert q["thinkingLevelMap"]["medium"] == "medium"
    assert q["thinkingLevelMap"]["low"] == "low"
    assert q["reasoning"] is True
    m0 = next(m for m in glm["models"] if m["id"] == "glm-5.2")
    assert m0["id"] == "glm-5.2"
    assert m0["reasoning"] is True
    assert m0["thinkingLevelMap"]["high"] == "high"
    assert m0["thinkingLevelMap"]["xhigh"] == "max"
    assert m0["thinkingLevelMap"]["low"] is None
    fmt = (m0.get("compat") or glm["compat"]).get("thinkingFormat")
    assert fmt == "qwen-chat-template"
    # context file is written so the pulse is never mute
    assert (home / "cli" / "PULSAR.md").is_file()


def test_omp_interface_gets_compact_context_and_selected_skills(
        tmp_path, skills_root):
    (skills_root / "pulse-team").mkdir()
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["pulse-team"]},
        vessel="pi", title="Front desk", model="pi-qwen27-interface")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "# Pulsar Local Interface AI" in body
    assert f"- pulse-team: {skills_root / 'pulse-team' / 'SKILL.md'}" in body
    assert "## Pulse-team core" in body
    assert "update the durable brief before waking" in body
    assert "## Skill library" not in body
    assert "Reporting to the user's Inbox" not in body
    assert len(body.split()) < 1000


def test_clean_pi_bridge_exports_pi_coding_agent_dir(tmp_path, skills_root,
                                                   monkeypatch):
    # pi resolves its agent dir from PI_CODING_AGENT_DIR, NOT PI_HOME — a clean
    # pi pulse must hand pi the PRIVATE agent dir (<pi-home>/agent), or it
    # silently reads the shared ~/.pi/agent instead (no clean isolation).
    from pulsar.runner.vessel_bridge import make_vessel
    import os
    fake_pi = tmp_path / "pi"
    fake_pi.write_text("#!/bin/sh\n")
    fake_pi.chmod(0o755)
    pinned = units_mod.DEFAULT_PI_BIN
    monkeypatch.setenv("PULSAR_PI_BIN", pinned if os.path.exists(pinned) else str(fake_pi))
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="pi")
    v = make_vessel("pi", model="pi-v4-flash", workdir=home / "cli",
                    diet={"clean": True},
                    fast=False)
    assert v.env == {"PI_CODING_AGENT_DIR": str(home / "pi-home" / "agent")}
    assert "PI_HOME" not in v.env  # the wrong var would be silently ignored


def test_pi_full_fat_gets_no_private_home(tmp_path, skills_root):
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": False}, vessel="pi")
    assert not profiles_mod.pi_home(home).exists()


def test_grok_full_fat_gets_no_private_home(tmp_path, skills_root,
                                             grok_real_home):
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": False}, vessel="grok")
    assert not profiles_mod.grok_home(home).exists()


def _stub_spawn(monkeypatch):
    """Record systemd-run argv instead of running it."""
    calls: list[list[str]] = []
    results = []

    def _run(cmd, what):
        calls.append(list(cmd))
        rc = results.pop(0) if results else 0
        return types.SimpleNamespace(returncode=rc, stderr="boom")

    monkeypatch.setattr(units_mod, "_await_unit_gone", lambda *a, **k: None)
    monkeypatch.setattr(units_mod, "list_pulse_units", lambda: [])
    monkeypatch.setattr(units_mod, "_run", _run)
    return calls, results


def test_clean_codex_unit_binds_an_empty_agents_dir(tmp_path, skills_root,
                                                    codex_real_home,
                                                    monkeypatch):
    # ~/.agents/skills is the one ambient source codex resolves from HOME, so
    # only a mount namespace can hide it.
    calls, _ = _stub_spawn(monkeypatch)
    home = tmp_path / "home"
    _REAL_SPAWN_RUNNER("p1", home, "gpt-6-sol", "codex",
                       diet={"clean": True})
    want = (f"BindPaths={profiles_mod.codex_empty_agents(home)}:"
            f"{profiles_mod.CODEX_AGENTS_DIR}")
    assert want in calls[0]


def test_clean_grok_unit_binds_an_empty_agents_dir(tmp_path, skills_root,
                                                   grok_real_home,
                                                   monkeypatch):
    calls, _ = _stub_spawn(monkeypatch)
    home = tmp_path / "home"
    _REAL_SPAWN_RUNNER("p1", home, "grok-4.6", "grok",
                       diet={"clean": True})
    want = (f"BindPaths={profiles_mod.grok_empty_agents(home)}:"
            f"{profiles_mod.GROK_AGENTS_DIR} "
            f"{profiles_mod.grok_empty_claude_plugins(home)}:"
            f"{profiles_mod.GROK_CLAUDE_PLUGINS_DIR}")
    assert want in calls[0]


@pytest.mark.parametrize("vessel,diet", [("claude-code", {"clean": True}),
                                         ("codex", {"clean": False})])
def test_bind_only_for_clean_codex(tmp_path, skills_root, codex_real_home,
                                   monkeypatch, vessel, diet):
    calls, _ = _stub_spawn(monkeypatch)
    _REAL_SPAWN_RUNNER("p1", tmp_path / "home", "m", vessel, diet=diet)
    assert not any(a.startswith("BindPaths=") for a in calls[0])


def test_bind_rejection_retries_without_it(tmp_path, skills_root,
                                           codex_real_home, monkeypatch,
                                           caplog):
    # A pulse that boots slightly dirty beats one that won't boot — but the
    # degradation must be shouted, not swallowed.
    calls, results = _stub_spawn(monkeypatch)
    results.append(1)  # first systemd-run rejects BindPaths
    with caplog.at_level("ERROR", logger="pulsar.spawn"):
        _REAL_SPAWN_RUNNER("p1", tmp_path / "home", "gpt-6-sol", "codex",
                           diet={"clean": True})
    assert len(calls) == 2
    assert not any(a.startswith("BindPaths=") for a in calls[1])
    # the property AND its "-p" are gone, not a dangling flag
    assert calls[1].count("-p") == calls[0].count("-p") - 1
    assert any("WILL see the ambient" in r.message for r in caplog.records)


def test_spawn_runner_passes_data_dir_and_daemon_env_and_guard_reads_it(
        tmp_path, skills_root, monkeypatch):
    from pulsar.cli import guard

    data = tmp_path / "custom-data"
    data.mkdir()
    extra = str(tmp_path / "protected-extra")
    monkeypatch.setenv("PULSAR_DATA", str(data))
    monkeypatch.delenv("PULSAR_DAEMON", raising=False)
    monkeypatch.setenv("PULSAR_PORT", "18726")

    calls, _ = _stub_spawn(monkeypatch)
    home = data / "pulses" / "p1"
    _REAL_SPAWN_RUNNER("p1", home, "claude-opus-5-5", "claude-code",
                       diet={"clean": True})
    cmd = calls[0]
    assert f"Environment=PULSAR_DATA={data}" in cmd
    assert "Environment=PULSAR_DAEMON=http://127.0.0.1:18726" in cmd

    # Explicit PULSAR_DAEMON takes precedence over PULSAR_PORT.
    monkeypatch.setenv("PULSAR_DAEMON", "http://127.0.0.1:19001/")
    _REAL_SPAWN_RUNNER("p2", data / "pulses" / "p2", "claude-opus-5-5",
                       "claude-code", diet={"clean": True})
    assert "Environment=PULSAR_DAEMON=http://127.0.0.1:19001" in calls[1]

    # Guard running with the unit's PULSAR_DATA reads guard.json from there and
    # protects both the real data dir and configured extra_paths.
    unit_env = dict(
        item.split("=", 1)[1].split("=", 1)
        for item in cmd
        if item.startswith("Environment=")
    )
    assert unit_env["PULSAR_DATA"] == str(data)
    assert unit_env["PULSAR_DAEMON"] == "http://127.0.0.1:18726"

    guard.write_config(enabled=True, extra_paths=[extra], path=data / "guard.json")
    monkeypatch.setenv("PULSAR_DATA", unit_env["PULSAR_DATA"])
    assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {data}/pulses/p1", cwd="/tmp") is not None
    guard.write_config(enabled=False, extra_paths=[extra], path=data / "guard.json")
    assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is None


@pytest.mark.parametrize("vessel,fname", [("claude", "PULSAR.md"),
                                          ("codex", "AGENTS.md")])
def test_hub_rule_rides_every_vessels_context(tmp_path, skills_root, store,
                                              monkeypatch, vessel, fname):
    # The pages hub section renders only when hub.url is configured in
    # app_settings; unset leaves the section out altogether.
    home = tmp_path / "home-off"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel=vessel)
    body = (home / "cli" / fname).read_text()
    assert "## Showing " not in body

    store.put_setting("hub.url", "https://hub.example.com")
    monkeypatch.delenv("PULSAR_HUB_PAGES_DIR", raising=False)
    home_on = tmp_path / "home-on"
    diet_mod.materialize_diet("p1", home_on, {"clean": True}, vessel=vessel)
    body = (home_on / "cli" / fname).read_text()
    assert "## Showing the user something in a browser" in body
    assert "https://hub.example.com/" in body
    assert "https://hub.example.com/<slug>/" in body
    assert "Publish pages under" not in body

    monkeypatch.setenv("PULSAR_HUB_PAGES_DIR", "/srv/pages")
    home_root = tmp_path / "home-root"
    diet_mod.materialize_diet("p1", home_root, {"clean": True}, vessel=vessel)
    body_root = (home_root / "cli" / fname).read_text()
    assert "Publish pages under `/srv/pages`" in body_root


@pytest.mark.parametrize("vessel,fname", [("claude", "PULSAR.md"),
                                          ("codex", "AGENTS.md")])
def test_scope_contract_rides_every_vessels_context(tmp_path, skills_root,
                                                    vessel, fname):
    # The anti-invention/anti-scope-creep contract is first-class and
    # vessel-agnostic — it rides the context file itself, not an opt-in skill,
    # and briefs inherit it explicitly.
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel=vessel)
    body = (home / "cli" / fname).read_text()
    assert "## Scope contract — do what was asked" in body
    assert "NEVER invent requirements, constraints, guardrails" in body
    assert "never silently built into the work" in body
    assert "## Task list — keep it current" in body
    assert "mark the item you are on in_progress" in body
    assert "## Finish, then prove it" in body
    assert "Another pulse's report is a claim, not a fact" in body


@pytest.mark.parametrize("vessel,fname", [("claude", "PULSAR.md"),
                                          ("codex", "AGENTS.md"),
                                          ("pi", "PULSAR.md")])
def test_search_rule_rides_every_vessels_context(tmp_path, skills_root,
                                                 vessel, fname):
    # Disk-wide walks are slow on any vessel, so the pulsar-search rule rides
    # every pulse's context file rather than an opt-in skill.
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel=vessel)
    body = (home / "cli" / fname).read_text()
    assert "## Finding files and text" in body
    assert "pulsar-search file <name>" in body
    assert "pulsar-search text <regex>" in body
    assert "Never run them" in body
    assert "a vague name matches junk" in body


def test_search_rule_rides_the_compact_omp_profile(tmp_path, skills_root):
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True}, vessel="pi", title="Front desk",
        model="pi-qwen27-interface")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "pulsar-search file <name>" in body
    assert "Never run `find /`" in body


@pytest.mark.parametrize("vessel,fname", [("claude", "PULSAR.md"),
                                          ("codex", "AGENTS.md"),
                                          ("pi", "PULSAR.md")])
def test_wait_rule_rides_every_vessels_context(tmp_path, skills_root,
                                               vessel, fname):
    # Companion to the pulsar-search rule: waiting is a daemon WATCH
    # (pulsar-wait), never a sleep loop inside a tool call. Any vessel can
    # hang a turn, so the rule rides every context file.
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel=vessel)
    body = (home / "cli" / fname).read_text()
    assert "## Waiting for things" in body
    assert "pulsar-wait" in body


def test_wait_rule_rides_the_compact_omp_profile(tmp_path, skills_root):
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True}, vessel="pi", title="Front desk",
        model="pi-qwen27-interface")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "pulsar-wait" in body
    assert "Never sleep in a tool call" in body


@pytest.mark.parametrize("vessel,fname", [("claude", "PULSAR.md"),
                                          ("codex", "AGENTS.md"),
                                          ("pi", "PULSAR.md")])
def test_roster_rule_rides_every_vessels_context(tmp_path, skills_root,
                                                 vessel, fname):
    # Companion to the pulsar-wait rule: roster lookups go through
    # pulsar-roster (one line per pulse), never a raw /pulses JSON dump.
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel=vessel)
    body = (home / "cli" / fname).read_text()
    assert "## The pulse roster" in body
    assert "pulsar-roster" in body
    assert "Never dump the full /pulses JSON" in body


def test_roster_rule_rides_the_compact_omp_profile(tmp_path, skills_root):
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True}, vessel="pi", title="Front desk",
        model="pi-qwen27-interface")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "pulsar-roster" in body
    assert "Never dump the full /pulses JSON" in body


def test_scope_rule_rides_the_compact_omp_profile(tmp_path, skills_root):
    # The OMP front desk writes worker briefs, so the compact profile carries
    # its own one-line form of the anti-invention rule.
    home = tmp_path / "home"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True}, vessel="pi", title="Front desk",
        model="pi-qwen27-interface")
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "never invent requirements or policies" in body


@pytest.mark.parametrize("vessel,fname", [("claude", "PULSAR.md")])
def test_skill_catalog_rides_context_grouped_and_marked(tmp_path, skills_root,
                                                        vessel, fname):
    # Tier-0 discovery: a clean pulse must still SEE the whole library —
    # grouped by catalog.json, diet-linked skills marked [loaded], folded (>)
    # descriptions flattened, ungrouped under Other.
    (skills_root / "crm").mkdir()
    (skills_root / "crm" / "SKILL.md").write_text(
        "---\nname: crm\ndescription: CRM integration hub.\n---\n# body\n")
    (skills_root / "seo").mkdir()
    (skills_root / "seo" / "SKILL.md").write_text(
        "---\nname: seo\ndescription: >\n  On-page SEO\n  two modes.\n---\n")
    (skills_root / "empty-dir").mkdir()  # no SKILL.md — must be skipped
    (skills_root / "catalog.json").write_text('{"CRM": ["crm", "ghost"]}')
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True, "skills": ["crm"]},
                               vessel=vessel)
    body = (home / "cli" / fname).read_text()
    assert "## Skill library" in body
    assert "### CRM" in body
    assert "- crm [loaded] — CRM integration hub." in body
    assert "- seo — On-page SEO two modes." in body  # folded, not pinned
    assert "### Other" in body
    assert "ghost" not in body      # in catalog.json but not on disk
    assert "empty-dir" not in body  # on disk but no SKILL.md


def test_skill_catalog_survives_missing_catalog_json(tmp_path, skills_root):
    # No catalog.json → flat list under Other, never a failed spawn.
    (skills_root / "seo").mkdir()
    (skills_root / "seo" / "SKILL.md").write_text(
        "---\ndescription: On-page SEO.\n---\n")
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True})
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "- seo — On-page SEO." in body
    assert "### Other" in body


@pytest.mark.parametrize("vessel,bodies,marked", [
    ("pi", None, {"crm", "seo"}),      # arm A (no file): every body loaded
    ("pi", "[]", set()),               # arm B: no body, so no [loaded]
    ("pi", '["seo"]', {"seo"}),        # arm C: only the named body
    ("pi", "not json", {"crm", "seo"}),  # malformed = arm A, like pi.py
    ("claude", "[]", {"crm", "seo"}),  # the file is pi-only
])
def test_pi_skill_bodies_file_sets_catalog_loaded_marks(tmp_path, skills_root,
                                                        vessel, bodies, marked):
    # "Ones marked [loaded] are already in your context" must stay true
    # under the pi skill arms, and stay as-is for claude.
    for name in ("crm", "seo"):
        (skills_root / name).mkdir()
        (skills_root / name / "SKILL.md").write_text(
            f"---\nname: {name}\ndescription: {name} hub.\n---\n")
    home = tmp_path / "home"
    home.mkdir()
    if bodies is not None:
        (home / "pi-skill-bodies.json").write_text(bodies)
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["crm", "seo"]}, vessel=vessel)
    body = (home / "cli" / "PULSAR.md").read_text()
    assert "Ones marked [loaded] are already in your context" in body
    for name in ("crm", "seo"):
        line = (f"- {name} [loaded] — {name} hub." if name in marked
                else f"- {name} — {name} hub.")
        assert line in body, line


def test_make_vessel_points_clean_codex_at_its_private_home(tmp_path,
                                                            skills_root,
                                                            codex_real_home):
    from pulsar.runner.vessel_bridge import make_vessel
    home = tmp_path / "home"
    diet = {"clean": True}
    diet_mod.materialize_diet("p1", home, diet, vessel="codex")
    v = make_vessel("codex", model="gpt-6-sol", workdir=home / "cli",
                    diet=diet)
    assert v.env == {"CODEX_HOME": str(profiles_mod.codex_home(home))}


def test_make_vessel_full_fat_codex_keeps_the_ambient_home(tmp_path,
                                                           skills_root):
    from pulsar.runner.vessel_bridge import make_vessel
    v = make_vessel("codex", model="gpt-6-sol",
                    workdir=tmp_path / "home" / "cli", diet={"clean": False})
    assert v.env is None


def test_make_vessel_points_clean_grok_at_its_private_home(
    tmp_path, skills_root, grok_real_home
):
    from pulsar.runner.vessel_bridge import make_vessel
    home = tmp_path / "home"
    diet = {"clean": True}
    diet_mod.materialize_diet("p1", home, diet, vessel="grok")
    v = make_vessel("grok", model="grok-4.6", workdir=home / "cli",
                    diet=diet)
    assert v.env == {
        "GROK_HOME": str(profiles_mod.grok_home(home)),
        "GROK_AUTH_PATH": str(grok_real_home / "auth.json"),
        "GROK_MEMORY": "0",
        "GROK_CLAUDE_SKILLS_ENABLED": "false",
        "GROK_CURSOR_SKILLS_ENABLED": "false",
    }


def test_clean_grok_uses_shared_auth_even_if_local_symlink_is_replaced(
    tmp_path, skills_root, grok_real_home
):
    """A Grok refresh must not strand credentials inside one pulse home."""
    from pulsar.runner.vessel_bridge import make_vessel
    home = tmp_path / "home"
    diet = {"clean": True}
    diet_mod.materialize_diet("p1", home, diet, vessel="grok")
    local_auth = profiles_mod.grok_home(home) / "auth.json"

    # Grok persists refreshed credentials with an atomic rename.  Reproduce
    # the resulting replaced symlink and prove the vessel still points at the
    # single ambient credential/lock location.
    local_auth.unlink()
    local_auth.write_text('{"token": "stranded"}')
    v = make_vessel("grok", model="grok-4.6", workdir=home / "cli",
                    diet=diet)

    assert not local_auth.is_symlink()
    assert v.env["GROK_AUTH_PATH"] == str(grok_real_home / "auth.json")


def test_make_vessel_shouts_when_the_private_home_is_missing(tmp_path,
                                                             skills_root,
                                                             caplog):
    # Never silently fall back to ~/.codex on a clean pulse.
    from pulsar.runner.vessel_bridge import make_vessel
    with caplog.at_level("ERROR", logger="pulsar.runner.vessel"):
        v = make_vessel("codex", model="gpt-6-sol",
                        workdir=tmp_path / "home" / "cli",
                        diet={"clean": True})
    assert v.env is None
    assert any("ambient" in r.message for r in caplog.records)


def test_codex_hand_written_agents_md_untouched(tmp_path, skills_root, caplog):
    (skills_root / "beta-skill").mkdir()
    home = tmp_path / "home"
    (home / "cli").mkdir(parents=True)
    agents = home / "cli" / "AGENTS.md"
    agents.write_text("# User's own notes\n")
    with caplog.at_level("WARNING", logger="pulsar.spawn"):
        diet_mod.materialize_diet(
            "p1", home, {"clean": True, "skills": ["beta-skill"], "cages": []}, vessel="codex")
    assert agents.read_text() == "# User's own notes\n"
    assert any("not ours" in r.message for r in caplog.records)


# --------------------------------------------------------------------------- #
# Daemon API
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_create_defaults_clean_diet(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "clean one", "model": "claude-haiku-4-5"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    diet = store.get_pulse(pid).settings["diet"]
    assert diet == {"clean": True, "memory": False, "skills": [], "cages": []}
    assert fake_spawn.spawned[0]["diet"] == diet
    home = Path(fake_spawn.spawned[0]["home"])
    assert json.loads((home / "diet.json").read_text()) == diet


@pytest.mark.anyio
async def test_create_with_optins(client, store, fake_spawn, skills_root):
    (skills_root / "alpha-skill").mkdir()
    r = await client.post("/pulses", json={
        "title": "fat one", "model": "claude-haiku-4-5",
        "memory": True, "skills": ["alpha-skill"], "cages": []})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    diet = store.get_pulse(pid).settings["diet"]
    assert diet == {"clean": True, "memory": True, "skills": ["alpha-skill"], "cages": []}
    home = Path(fake_spawn.spawned[0]["home"])
    assert (home / "cli" / ".claude" / "skills" / "alpha-skill").is_symlink()


@pytest.mark.anyio
async def test_patch_diet_applies_on_next_spawn(client, store, fake_spawn,
                                                skills_root):
    (skills_root / "beta-skill").mkdir()
    r = await client.post("/pulses", json={
        "title": "dieter", "model": "claude-haiku-4-5"})
    pid = r.json()["value"]["id"]

    r = await client.patch(f"/pulses/{pid}/diet", json={
        "clean": True, "memory": True, "skills": ["beta-skill"], "cages": []})
    assert r.status_code == 200, r.text
    body = r.json()["value"]
    assert body["applies"] == "next_spawn"
    new_diet = {"clean": True, "memory": True, "skills": ["beta-skill"], "cages": []}
    assert store.get_pulse(pid).settings["diet"] == new_diet
    # running CLI untouched: still only the create spawn so far
    assert len(fake_spawn.spawned) == 1

    # pause + restore respawns with the NEW diet
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["diet"] == new_diet
    home = Path(fake_spawn.spawned[-1]["home"])
    assert json.loads((home / "diet.json").read_text()) == new_diet
    assert (home / "cli" / ".claude" / "skills" / "beta-skill").is_symlink()


@pytest.mark.anyio
async def test_get_skills_lists_dirs(client, skills_root):
    (skills_root / "b-skill").mkdir()
    (skills_root / "a-skill").mkdir()
    (skills_root / "not-a-skill.md").write_text("file, not dir")
    (skills_root / ".git.disconnected").mkdir()
    (skills_root / "_hooks").mkdir()
    r = await client.get("/skills")
    assert r.status_code == 200
    assert (r.json()["value"]
            == ["a-skill", "b-skill"])


@pytest.mark.anyio
async def test_get_skills_missing_root_returns_empty(client, tmp_path, monkeypatch):
    from pulsar.launch import skills as skills_mod
    monkeypatch.setattr(skills_mod, "SKILLS_ROOT", tmp_path / "does-not-exist")
    r = await client.get("/skills")
    assert r.status_code == 200
    assert r.json()["value"] == []


def test_ds41_provider_normalizes_legacy_host_config(tmp_path, skills_root, monkeypatch):
    host = tmp_path / "host"
    cfg = host / ".pi" / "agent" / "models.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(json.dumps({"providers": {"ds41local": {
        "baseUrl": "http://example.invalid/v1",
        "compat": {"supportsReasoningEffort": False},
        "models": [{"id": "deepseek-v4.1-flash", "reasoning": False,
                    "input": ["text"]}],
    }}}))
    monkeypatch.setattr(Path, "home", lambda: host)
    home = tmp_path / "pulse"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="pi")
    data = json.loads((profiles_mod.pi_home(home) / "agent" / "models.json").read_text())
    provider = data["providers"]["ds41local"]
    assert provider["baseUrl"] == "http://example.invalid/v1"
    model = provider["models"][0]
    assert model["name"] == "DeepSeek V4.1 Flash (1M ctx)"
    # Served max_model_len 1048576 minus one.
    assert model["contextWindow"] == 1048575
    assert model["reasoning"] is True
    assert model["input"] == ["text", "image"]
    assert model["compat"]["thinkingFormat"] == "openrouter"
    assert model["compat"]["supportsReasoningEffort"] is True
    from pulsar.vessel.pi import pi_thinking_level
    # The dial is the number the server gets.
    for effort in ("80", "70", "50", "30"):
        assert model["thinkingLevelMap"][pi_thinking_level("pi-ds41-flash", effort)] == effort
    assert pi_thinking_level("pi-ds41-flash", "none") == "off"
    # A pulse still stored on an old name keeps working.
    old = {"low": "50", "high": "80", "xhigh": "80", "max": "80"}
    for name, number in old.items():
        assert model["thinkingLevelMap"][pi_thinking_level("pi-ds41-flash", name)] == number
    assert pi_thinking_level("pi-ds41-flash", "off") == "off"


def test_altds_provider_gets_numeric_ds41_map(tmp_path, skills_root, monkeypatch):
    host = tmp_path / "host"
    host_cfg = host / ".pi" / "agent" / "models.json"
    host_cfg.parent.mkdir(parents=True)
    host_cfg.write_text(json.dumps({"providers": {"altds": {
        "baseUrl": "http://example.invalid/v1",
        "models": [{"id": "deepseek-v4.1-flash", "reasoning": True,
                    "thinkingLevelMap": {"low": "low", "high": "xhigh"}}],
    }}}))
    monkeypatch.setattr(Path, "home", lambda: host)
    home = tmp_path / "pulse"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="pi")
    data = json.loads((profiles_mod.pi_home(home) / "agent" / "models.json").read_text())
    model = data["providers"]["altds"]["models"][0]
    assert model["thinkingLevelMap"] == {"minimal": "30", "low": "50", "medium": "70",
                                         "high": "80", "xhigh": None}


def test_glm_flash_image_input_survives_text_only_host_map(tmp_path, skills_root, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    host_cfg = tmp_path / ".pi" / "agent" / "models.json"
    host_cfg.parent.mkdir(parents=True)
    host_cfg.write_text(json.dumps({"providers": {
        "glm53local": {"models": [
            {"id": "glm-5.3-flash", "input": ["text"]},
            {"id": "glm-5.3", "input": ["text"]},
        ]},
        "glm53pair": {"models": [
            {"id": "glm-5.3-flash-nvfp4", "input": ["text"]},
        ]},
    }}))
    home = tmp_path / "pulse"
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="pi")
    providers = json.loads((profiles_mod.pi_home(home) / "agent" / "models.json").read_text())["providers"]
    assert providers["glm53local"]["models"][0]["input"] == ["text", "image"]
    assert providers["glm53local"]["models"][1]["input"] == ["text"]
    assert providers["glm53pair"]["models"][0]["input"] == ["text", "image"]
    assert providers["glm53exl3"]["models"][0]["input"] == ["text", "image"]
    assert providers["glm53exl3"]["models"][0]["id"] == "GLM-5.3-Flash-EXL3"
    assert providers["glm53exl3"]["baseUrl"] == "http://192.0.2.11:8888/v1"


def test_clean_grok_loads_explicit_workload_mcp(tmp_path, skills_root, grok_real_home):
    import tomllib
    home = tmp_path / "home"
    (home / "cli").mkdir(parents=True)
    server = {"command": "python3", "args": ["/work/tools.py"],
              "env": {"ROLE": "main", "TOKEN": 'quoted " and \\ slash'}}
    config = home / "cli" / ".mcp.json"
    config.write_text(json.dumps({"mcpServers": {"work.load": server}}))
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="grok")
    native = profiles_mod.grok_home(home) / "config.toml"
    parsed = tomllib.loads(native.read_text())
    assert parsed["mcp_servers"] == {"work.load": server}
    assert parsed["compat"]["claude"]["mcps"] is False
    # Removing the caller's config also removes previously materialized tools.
    config.unlink()
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="grok")
    assert "mcp_servers" not in tomllib.loads(native.read_text())


# --------------------------------------------------------------------------- #
# Default skills load on EVERY spawn — human or agent
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_default_skills_merge_into_every_spawn(client, store, fake_spawn,
                                                     skills_root):
    (skills_root / "keep-going").mkdir()
    (skills_root / "alpha-skill").mkdir()
    r = await client.put("/settings/pulse.default_skills",
                         content=json.dumps(["keep-going"]))
    assert r.status_code == 200, r.text

    # Agent-style spawn through the API with NO skills named at all.
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-haiku-4-5"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    # Stored diet stays what was asked for …
    assert store.get_pulse(pid).settings["diet"]["skills"] == []
    # … the spawn loads the default on top.
    assert fake_spawn.spawned[-1]["diet"]["skills"] == ["keep-going"]
    home = Path(fake_spawn.spawned[-1]["home"])
    assert (home / "cli" / ".claude" / "skills" / "keep-going").is_symlink()

    # Explicit picks are kept, default appended once, no duplicate.
    r = await client.post("/pulses", json={
        "title": "picked", "model": "claude-haiku-4-5",
        "skills": ["alpha-skill", "keep-going"]})
    assert r.status_code == 201, r.text
    assert fake_spawn.spawned[-1]["diet"]["skills"] == ["alpha-skill", "keep-going"]

    # A default marked AFTER creation reaches an existing pulse on restore.
    r = await client.put("/settings/pulse.default_skills",
                         content=json.dumps(["keep-going", "alpha-skill"]))
    assert r.status_code == 200, r.text
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["diet"]["skills"] == ["keep-going", "alpha-skill"]
    assert store.get_pulse(pid).settings["diet"]["skills"] == []


def test_diet_with_defaults_full_fat_untouched(store):
    store.put_setting("pulse.default_skills", ["keep-going"])
    assert store.diet_with_defaults({"clean": False, "skills": [], "cages": []}) == {
        "clean": False, "skills": [], "cages": []}
    assert store.diet_with_defaults(None) == {"clean": True,
                                              "skills": ["keep-going"]}


# --------------------------------------------------------------------------- #
# Context role: opt-in "helper", never inferred
# --------------------------------------------------------------------------- #
def _ctx(fake_spawn, idx=-1):
    return (Path(fake_spawn.spawned[idx]["home"]) / "cli" / "PULSAR.md").read_text()


@pytest.mark.anyio
async def test_helper_role_prefix_and_persistence(client, store, fake_spawn, skills_root):
    # One installed skill, in a temp root: the "## Skill library" section is
    # written only when a skill exists, and the test must not depend on the
    # skills of the machine it runs on.
    (skills_root / "ambient").mkdir()
    (skills_root / "ambient" / "SKILL.md").write_text(
        "---\nname: ambient\ndescription: Findable by path.\n---\n")
    r = await client.post("/pulses", json={"title": "Lead", "model": "claude-haiku-4-5"})
    lead = r.json()["value"]["id"]
    r = await client.post("/pulses", json={
        "title": "Helper", "model": "claude-haiku-4-5",
        "from_pulse_id": lead, "role": "helper", "first_message": "brief"})
    assert r.status_code == 201, r.text
    hid = r.json()["value"]["id"]
    assert store.get_pulse(hid).settings["role"] == "helper"
    assert store.get_pulse(hid).settings["lead_id"] == lead
    body = _ctx(fake_spawn)
    assert "## You are a helper — your lead is 'Lead' (pulse " + lead in body
    assert f"/pulses/{lead}/messages" in body
    # Lead-only sections are dropped for helpers; the skill library and
    # working rails stay.
    for gone in ("## Reporting to the user's Inbox", "## Managing helper pulses",
                 "## Delegating work", "## Showing the user something"):
        assert gone not in body, gone
    for keep in ("## Watches", "## Messaging other pulses", "## Skill library",
                 "## How Pulsar runs you", "**Workspaces (grids).**",
                 "## Finding files and text"):
        assert keep in body, keep
    lead_body = _ctx(fake_spawn, 0)
    assert "## Delegating work" in lead_body and "## Managing helper pulses" in lead_body
    # Survives a respawn without any spawn path threading it through.
    await client.post(f"/pulses/{hid}/pause")
    r = await client.post(f"/pulses/{hid}/restore")
    assert r.status_code == 200, r.text
    assert "## You are a helper" in _ctx(fake_spawn)


@pytest.mark.anyio
async def test_role_never_inferred_from_spawner(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "Lead", "model": "claude-haiku-4-5"})
    lead = r.json()["value"]["id"]
    r = await client.post("/pulses", json={
        "title": "CI-ish", "model": "claude-haiku-4-5",
        "from_pulse_id": lead, "first_message": "brief"})
    assert r.status_code == 201, r.text
    body = _ctx(fake_spawn)
    assert "## You are a helper" not in body
    assert "role" not in store.get_pulse(r.json()["value"]["id"]).settings


@pytest.mark.anyio
async def test_helper_role_requires_lead_and_valid_value(client):
    r = await client.post("/pulses", json={
        "title": "orphan", "model": "claude-haiku-4-5", "role": "helper"})
    assert r.status_code == 400 and r.json()["error"]["code"] == "bad_role"
    r = await client.post("/pulses", json={
        "title": "bad", "model": "claude-haiku-4-5", "role": "boss"})
    assert r.status_code == 400


def test_skill_catalog_prefers_index_frontmatter(tmp_path, skills_root):
    # Curated routing text via `index:` beats the truncated description
    # (fixed word quotas lose aliases and exclusions).
    (skills_root / "long").mkdir()
    (skills_root / "long" / "SKILL.md").write_text(
        "---\nname: long\ndescription: " + ("word " * 80).strip() + "\n---\n")
    (skills_root / "curated").mkdir()
    (skills_root / "curated" / "SKILL.md").write_text(
        "---\nname: curated\ndescription: A long official description that "
        "goes on and on.\nindex: delegate, worker pool — delegation "
        "hub. NOT for CI.\n---\n")
    body = "\n".join(skills_mod._skill_catalog_lines([]))
    assert "- curated — delegate, worker pool — delegation hub. NOT for CI." in body
    assert "goes on and on" not in body
    long_line = next(l for l in body.splitlines() if l.startswith("- long"))
    assert long_line.endswith(" …") and len(long_line) < 200


def test_qwen_flash_context_declared_one_below_served_window(tmp_path, monkeypatch):
    """vLLM rejects prompt + max_tokens > max_model_len, so a declared
    window of exactly 262144 lets pi plan a request that is dead by ONE
    token. The materialized map must clamp both served-262144 Qwen entries
    to 262143."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "pulse"
    diet_mod._materialize_pi_diet("clamp-test", home, [])
    models = json.loads(
        (home / "pi-home" / "agent" / "models.json").read_text()
    )["providers"]
    entry = next(m for m in models["glmlocal"]["models"]
                 if m["id"] == "Qwen3.8-Flash-Next-NVFP4")
    assert entry["contextWindow"] == 262143
    # qwen38tp4 comes from the host map only; when present it is clamped too.
    for prov in ("qwen38tp4", "dspair", "dspair2"):
        for m in models.get(prov, {}).get("models", []):
            if m["id"] == "Qwen3.8-Flash-Next-NVFP4":
                assert m["contextWindow"] == 262143, prov


_PI_HOST_FIXTURE = Path(__file__).with_name("fixtures") / "pi_host_models.json"
_PI_GOLDEN = Path(__file__).with_name("fixtures") / "pi_models_golden.json"


def _redact_keys(models: dict) -> dict:
    for config in models["providers"].values():
        if "apiKey" in config:
            config["apiKey"] = "<redacted>"
    return models


def test_pi_provider_map_golden(tmp_path, monkeypatch, request):
    """The materialized pi map for a fixed host map (a copy of
    the 2026-10-22 host drift with fake keys) matches the golden file. The
    golden stores apiKey as <redacted>; nothing here prints a key."""
    host = tmp_path / "host"
    cfg = host / ".pi" / "agent" / "models.json"
    cfg.parent.mkdir(parents=True)
    cfg.write_text(_PI_HOST_FIXTURE.read_text())
    monkeypatch.setattr(Path, "home", lambda: host)
    home = tmp_path / "pulse"
    diet_mod._materialize_pi_diet("golden", home, [])
    raw = json.loads((home / "pi-home" / "agent" / "models.json").read_text())
    # The host key still reaches the pulse; only the golden copy is redacted.
    assert raw["providers"]["zai"]["apiKey"] == "zai-FAKE-KEY-never-real"
    got = _redact_keys(raw)
    if request.config.getoption("--update-goldens"):
        _PI_GOLDEN.write_text(json.dumps(got, indent=2, ensure_ascii=False) + "\n")
    assert got == json.loads(_PI_GOLDEN.read_text()), (
        "pytest tests/test_diet.py -k golden --update-goldens")
    prov = got["providers"]

    def model(provider, model_id):
        return next(m for m in prov[provider]["models"] if m["id"] == model_id)

    # (b) the host baseUrl wins for a provider both maps declare; the fixture
    # seed names the same endpoint, so no host map gives the same URL.
    assert prov["ds41local"]["baseUrl"] == "http://192.0.2.13:8888/v1"
    # (d) served max_model_len - 1 for every recorded serve, in any provider.
    assert model("ds41local", "deepseek-v4.1-flash")["contextWindow"] == 1048575
    assert model("glmlocal", "Qwen3.8-Flash-Next-NVFP4")["contextWindow"] == 262143
    assert model("qwen38tp4", "Qwen3.8-Flash-Next-NVFP4")["contextWindow"] == 262143
    assert model("glm53flashexlr8", "glm53")["contextWindow"] == 163839
    # (e) maxTokens are not changed by step 2 (the plan measures first).
    assert model("ds41local", "deepseek-v4.1-flash")["maxTokens"] == 16384
    assert model("glm53flashexlr8", "glm53")["maxTokens"] == 32768
    # (a)/(c) left as the host has them until the alias decision is made.
    assert model("glm53local", "glm-5.3-flash-ab")["contextWindow"] == 262144
    assert model("glm53local", "glm-5.3")["contextWindow"] == 131066


def test_pi_seed_ds41_url_without_host_map(tmp_path, monkeypatch):
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    home = tmp_path / "pulse"
    diet_mod._materialize_pi_diet("seed", home, [])
    prov = json.loads((home / "pi-home" / "agent" / "models.json").read_text())["providers"]
    assert prov["ds41local"]["baseUrl"] == "http://192.0.2.13:8888/v1"
    assert prov["ds41local"]["models"][0]["contextWindow"] == 1048575


def test_pi_diet_succeeds_with_empty_shipped_providers(tmp_path, monkeypatch):
    """The shipping pulsar/vessel/pi_providers.json is empty; clean pi diet
    must succeed without raising when no host map exists."""
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("PULSAR_PI_PROVIDERS_PATH", raising=False)
    home = tmp_path / "pulse"
    diet_mod._materialize_pi_diet("empty-seed", home, [])
    data = json.loads((home / "pi-home" / "agent" / "models.json").read_text())
    assert data == {"providers": {}}


# --------------------------------------------------------------------------- #
# cages — diet["cages"] -> <home>/pi-args.json for the pi vessel
# --------------------------------------------------------------------------- #
@pytest.fixture()
def cages_root(tmp_path, monkeypatch):
    root = tmp_path / "cages-root"
    (root / "operator").mkdir(parents=True)
    (root / "operator" / "cage.ts").write_text("export default () => {}")
    (root / "no-ts").mkdir()                      # not a cage: no cage.ts
    (root / "_hidden").mkdir()
    (root / "_hidden" / "cage.ts").write_text("")
    monkeypatch.setattr(cages_mod, "CAGES_ROOT", root)
    return root


def test_list_cages_only_dirs_with_cage_ts(cages_root):
    assert cages_mod.list_cages() == ["operator"]
    assert cages_mod.list_cages(cages_root / "missing") == []


def test_pi_cages_write_pi_args(tmp_path, skills_root, cages_root):
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True, "cages": ["operator"]},
                               vessel="pi")
    args = json.loads((home / "pi-args.json").read_text())
    assert args == ["-e", str(cages_root / "operator" / "cage.ts")]
    assert json.loads((home / cages_mod.CAGES_MARKER).read_text()) == ["operator"]
    # the vessel picks it up through pi_extra_args (cwd = <home>/cli)
    from pulsar.vessel.pi import pi_extra_args
    assert pi_extra_args(home / "cli") == args


def test_missing_cage_skipped_loudly(tmp_path, skills_root, cages_root, caplog):
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True,
                                            "cages": ["operator", "nope", "../x"]},
                               vessel="pi")
    assert json.loads((home / "pi-args.json").read_text()) == [
        "-e", str(cages_root / "operator" / "cage.ts")]
    assert "cage 'nope' missing" in caplog.text
    assert "invalid" in caplog.text


def test_cages_cleared_only_when_ours(tmp_path, skills_root, cages_root):
    home = tmp_path / "home"
    # ours: installed then removed from the diet -> file goes away
    diet_mod.materialize_diet("p1", home, {"clean": True, "cages": ["operator"]},
                               vessel="pi")
    diet_mod.materialize_diet("p1", home, {"clean": True, "cages": []},
                               vessel="pi")
    assert not (home / "pi-args.json").exists()
    assert not (home / cages_mod.CAGES_MARKER).exists()
    # hand-written (no marker): a diet without cages leaves it alone
    (home / "pi-args.json").write_text('["-e","/x/bridge.ts"]')
    diet_mod.materialize_diet("p1", home, {"clean": True}, vessel="pi")
    assert json.loads((home / "pi-args.json").read_text()) == ["-e", "/x/bridge.ts"]


def test_cages_ignored_on_non_pi_vessel(tmp_path, skills_root, cages_root, caplog):
    home = tmp_path / "home"
    diet_mod.materialize_diet("p1", home, {"clean": True, "cages": ["operator"]},
                               vessel="claude")
    assert not (home / "pi-args.json").exists()
    assert "pi-only" in caplog.text


@pytest.mark.anyio
async def test_create_stores_cages_and_get_cages_lists(client, store, fake_spawn,
                                                        cages_root):
    r = await client.get("/cages")
    assert r.status_code == 200 and r.json()["value"] == ["operator"]
    r = await client.post("/pulses", json={"title": "op", "model": "pi-qwen38-flash",
                                           "vessel": "pi", "cages": ["operator"]})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings["diet"]["cages"] == ["operator"]
    r = await client.patch(f"/pulses/{pid}/diet",
                           json={"clean": True, "memory": False, "skills": [],
                                 "cages": []})
    assert r.status_code == 200
    assert store.get_pulse(pid).settings["diet"]["cages"] == []
