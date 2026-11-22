"""Characterization tests for vessel profiles: PROFILES keys, make_vessel
table, and fresh-process import direction."""

from __future__ import annotations

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from pulsar import models as models_mod
import pulsar.launch.cages as cages_mod
import pulsar.launch.context as context_mod
import pulsar.launch.diet as diet_mod
import pulsar.launch.skills as skills_mod
import pulsar.launch.units as units_mod
from pulsar.runner.vessel_bridge import make_vessel
import pulsar.vessel.profiles as profiles_mod

_REPO_SIDE_PATHS = frozenset({
    "CONTEXT_TEMPLATES",
    "_PI_PROVIDERS_PATH",
})

# Which pulsar.launch module owns each patched import-time root (moved
# out of the old runner/spawn.py).
_ROOT_OWNER = {
    "SKILLS_ROOT": skills_mod,
    "CAGES_ROOT": cages_mod,
}

_PATCHED_ROOT_ATTRS = (
    "SKILLS_ROOT",
    "CAGES_ROOT",
    "CODEX_REAL_HOME",
    "CODEX_AGENTS_DIR",
    "GROK_REAL_HOME",
    "GROK_AGENTS_DIR",
    "GROK_CLAUDE_PLUGINS_DIR",
    "GROK_CLAUDE_PLUGINS_FILE",
)


def _setup_isolated_roots(root: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Redirect all 8 import-time roots in the launch modules plus Path.home() under root."""
    fake_home = root / "fake-home"
    fake_home.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", staticmethod(lambda: fake_home))

    skills_root = root / "skills-root"
    (skills_root / "beta-skill").mkdir(parents=True)
    (skills_root / "beta-skill" / "SKILL.md").write_text(
        "---\nname: beta-skill\ndescription: Beta test skill.\n---\nBody.\n",
        encoding="utf-8",
    )
    (skills_root / "ambient").mkdir(parents=True)
    (skills_root / "ambient" / "SKILL.md").write_text(
        "---\nname: ambient\ndescription: Ambient test skill.\n---\n",
        encoding="utf-8",
    )
    (skills_root / "catalog.json").write_text(
        json.dumps({"Testing": ["beta-skill", "ambient"]}),
        encoding="utf-8",
    )

    cages_root = root / "cages-root"
    cages_root.mkdir(parents=True)

    codex_real = root / "dot-codex"
    codex_real.mkdir(parents=True)
    (codex_real / "auth.json").write_text('{"tokens":"fake-codex"}', encoding="utf-8")
    codex_agents = root / "dot-agents"
    codex_agents.mkdir(parents=True)

    grok_real = root / "dot-grok"
    (grok_real / "bundled" / "skills" / "superpowers").mkdir(parents=True)
    (grok_real / "skills" / "ambient-grok").mkdir(parents=True)
    (grok_real / "auth.json").write_text('{"token":"fake-grok"}', encoding="utf-8")
    grok_plugins_dir = root / "claude-plugins"
    grok_plugins_dir.mkdir(parents=True)
    grok_plugins_file = grok_plugins_dir / "installed_plugins.json"
    grok_plugins_file.write_text(
        json.dumps({
            "version": 2,
            "plugins": {
                "superpowers@claude-plugins-official": [],
                "caveman@caveman": [],
            },
        }),
        encoding="utf-8",
    )

    spawn_mappings = {
        "SKILLS_ROOT": skills_root,
        "CAGES_ROOT": cages_root,
    }
    profile_mappings = {
        "CODEX_REAL_HOME": codex_real,
        "CODEX_AGENTS_DIR": codex_agents,
        "GROK_REAL_HOME": grok_real,
        "GROK_AGENTS_DIR": codex_agents,
        "GROK_CLAUDE_PLUGINS_DIR": grok_plugins_dir,
        "GROK_CLAUDE_PLUGINS_FILE": grok_plugins_file,
    }
    for attr, val in spawn_mappings.items():
        monkeypatch.setattr(_ROOT_OWNER[attr], attr, val)
    for attr, val in profile_mappings.items():
        monkeypatch.setattr(profiles_mod, attr, val)

    monkeypatch.setattr(units_mod, "PI_BIN", "/fake/bin/pi")
    shim_dir = root / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    pi_shim = shim_dir / "pi"
    pi_shim.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    pi_shim.chmod(pi_shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PULSAR_PI_BIN", str(pi_shim))
    return root


@pytest.fixture
def isolated_roots(tmp_path, monkeypatch):
    return _setup_isolated_roots(tmp_path / "env", monkeypatch)


def test_every_module_level_path_in_launch_is_isolated(isolated_roots):
    """Guard: every module-level Path (or tuple of Paths) in the launch modules and profiles points under isolated_roots."""
    for mod_label, mod, skip in (
        ("units", units_mod, frozenset()),
        ("diet", diet_mod, _REPO_SIDE_PATHS),
        ("context", context_mod, _REPO_SIDE_PATHS),
        ("skills", skills_mod, frozenset()),
        ("cages", cages_mod, frozenset()),
        ("profiles", profiles_mod, frozenset()),
    ):
        for name, val in vars(mod).items():
            if name in skip:
                continue
            if isinstance(val, Path):
                assert val.is_relative_to(isolated_roots), (
                    f"{mod_label}.{name} = {val} escaped isolated_roots {isolated_roots}"
                )
            elif isinstance(val, tuple) and any(isinstance(x, Path) for x in val):
                for item in val:
                    if isinstance(item, Path):
                        assert item.is_relative_to(isolated_roots), (
                            f"{mod_label}.{name} entry {item} escaped isolated_roots {isolated_roots}"
                        )


def _representative_model_for(vessel: str) -> str | None:
    """First visible model for vessel in models.json, or first hidden model if none visible."""
    visible = [
        m.id for m in models_mod.MODELS.values()
        if m.vessel == vessel and not m.hidden
    ]
    if visible:
        return visible[0]
    any_m = [m.id for m in models_mod.MODELS.values() if m.vessel == vessel]
    return any_m[0] if any_m else None


_EXPECTED_MAKE_VESSEL_TABLE = {
    # kind: (cls_name_default, cls_name_alt, clean_env_keys, fast_allowed, fast_true_warnings)
    "antigravity": ("AntigravityVessel", None, None, False, 0),
    "claude-code": ("ClaudeVessel", None, None, False, 1),
    "codex": ("CodexVessel", None, ["CODEX_HOME"], True, 0),
    "grok": (
        "GrokVessel",
        None,
        [
            "GROK_AUTH_PATH",
            "GROK_CLAUDE_SKILLS_ENABLED",
            "GROK_CURSOR_SKILLS_ENABLED",
            "GROK_HOME",
            "GROK_MEMORY",
        ],
        False,
        0,
    ),
    "pi": ("PiRpcVessel", "PiVessel", ["PI_CODING_AGENT_DIR"], False, 1),
}


@pytest.mark.parametrize("kind", sorted(models_mod.VESSELS))
@pytest.mark.parametrize("fast", [False, True])
def test_make_vessel_table(isolated_roots, caplog, kind, fast):
    assert set(_EXPECTED_MAKE_VESSEL_TABLE) == set(models_mod.VESSELS)
    cls_name, alt_cls_name, expected_env_keys, fast_allowed, fast_true_warnings = (
        _EXPECTED_MAKE_VESSEL_TABLE[kind]
    )

    home = isolated_roots / "mv" / f"{kind}-{fast}"
    model = _representative_model_for(kind) or "m"
    diet_mod.materialize_diet(
        "p1", home, {"clean": True, "skills": ["beta-skill"]}, vessel=kind, model=model
    )

    caplog.clear()
    with caplog.at_level("WARNING", logger="pulsar.runner.vessel"):
        v = make_vessel(
            kind,
            model=model,
            workdir=home / "cli",
            diet={"clean": True},
            fast=fast,
        )

    assert type(v._v).__name__ == cls_name
    if expected_env_keys is None:
        assert v.env is None
    else:
        assert v.env is not None
        assert sorted(v.env.keys()) == expected_env_keys

    fast_warnings = [
        r.message for r in caplog.records
        if "fast mode ignored: only the codex CLI takes a service-tier switch" in r.message
    ]
    if fast and not fast_allowed:
        assert v.fast is False
        assert len(fast_warnings) == fast_true_warnings
    elif fast and fast_allowed:
        assert len(fast_warnings) == 0
        assert v.fast is True
    else:
        assert len(fast_warnings) == 0
        assert v.fast is False

    if alt_cls_name is not None:
        # Verify non-RPC pi model resolves to PiVessel
        v_alt = make_vessel(
            kind,
            model="pi-v4-flash",
            workdir=home / "cli",
            diet={"clean": False},
            fast=False,
        )
        assert type(v_alt._v).__name__ == alt_cls_name


def test_profiles_keys_equal_models_json_vessels():
    from pulsar.vessel.profiles import PROFILES, profile
    from pulsar.vessel import VesselError

    assert set(PROFILES.keys()) == set(models_mod.VESSELS.keys())
    for k in models_mod.VESSELS:
        p = profile(k)
        assert p.kind == k
        assert p.fast == models_mod.VESSELS[k].fast
    with pytest.raises(VesselError, match="unknown vessel kind"):
        profile("no-such-vessel")


def test_every_profile_diet_builder_key_has_an_entry_in_launch_diet():
    from pulsar.vessel.profiles import PROFILES

    for k, prof in PROFILES.items():
        if prof.diet_builder is not None:
            assert prof.diet_builder in diet_mod._DIET_BUILDERS, (
                f"profile {k!r} diet_builder {prof.diet_builder!r} missing from launch.diet._DIET_BUILDERS"
            )
            assert callable(diet_mod._DIET_BUILDERS[prof.diet_builder])


@pytest.mark.parametrize(
    "case,code",
    [
        (
            "profiles_only_leaves_no_launch_in_sys_modules",
            (
                "import sys; "
                "import pulsar.vessel.profiles as p; "
                "[p.profile(k).vessel_cls('m') for k in p.PROFILES]; "
                "bad = [m for m in sys.modules if m.startswith('pulsar.launch')]; "
                "assert not bad, bad"
            ),
        ),
        (
            "launch_then_profiles",
            (
                "import pulsar.launch.diet; "
                "import pulsar.vessel.profiles as p; "
                "assert len(p.PROFILES) >= 5"
            ),
        ),
        (
            "profiles_then_launch",
            (
                "import pulsar.vessel.profiles as p; "
                "import pulsar.launch.diet; "
                "assert len(p.PROFILES) >= 5"
            ),
        ),
        (
            "vessel_bridge_make_vessel_leaves_no_launch_in_sys_modules",
            (
                "import sys, tempfile; from pathlib import Path; "
                "import pulsar.runner.vessel_bridge as vb; "
                "import pulsar.vessel.profiles as p; "
                "td = Path(tempfile.mkdtemp()) / 'cli'; td.mkdir(parents=True); "
                "[vb.make_vessel(k, model='m', workdir=td, diet={'clean': True}) for k in p.PROFILES]; "
                "bad = [m for m in sys.modules if m.startswith('pulsar.launch')]; "
                "assert not bad, bad"
            ),
        ),
    ],
)
def test_profiles_import_direction_in_fresh_process(tmp_path, case, code):
    import subprocess

    shim_dir = tmp_path / "bin"
    shim_dir.mkdir(parents=True, exist_ok=True)
    pi_shim = shim_dir / "pi"
    pi_shim.write_text(f"#!{sys.executable}\n", encoding="utf-8")
    pi_shim.chmod(pi_shim.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)

    repo_root = str(Path(__file__).resolve().parents[1])
    env = dict(os.environ, PYTHONPATH=repo_root, PULSAR_PI_BIN=str(pi_shim))
    proc = subprocess.run(
        [sys.executable, "-I", "-c", f"import sys; sys.path.insert(0, {repo_root!r}); {code}"],
        capture_output=True,
        text=True,
        env=env,
        timeout=15,
    )
    assert proc.returncode == 0, f"{case} failed: {proc.stderr or proc.stdout}"

