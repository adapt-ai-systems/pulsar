"""pulsar-guard: refuse accidental mass deletes before a pulse runs them."""
from __future__ import annotations

import json

import httpx
import pytest

from pulsar.cli import guard
from pulsar.daemon.app import create_app
from pulsar.vessel import pi
from pulsar.vessel.claude import JOB_HOOKS, diet_flags_env
from tests.test_daemon import (  # noqa: F401
    FakeSock, FakeSpawn, data_root, fake_sock, fake_spawn, store,
)

HOME = guard.HOME
DATA = str(guard.REPO_ROOT / "data")
TRAVERSAL_CMD = (
    "rm -rf /srv/app/../app 2>/dev/null; ssh -o BatchMode=yes user@example.com "
    "'rm -rf /tmp/archive-test'; pulsar-job run --name archive -- \"ssh example.com 'cd "
    "/srv/app/server && python3 scripts/archive.py'\""
)


@pytest.mark.parametrize("cmd", [
    TRAVERSAL_CMD,
    "rm -rf /etc",
    "rm -rf /etc/",
    "rm -rf /etc/*",
    "rm -rf ~",
    "rm -rf $HOME",
    "ssh user@192.0.2.10 'rm -rf /etc'",        # protected path inside quotes
    'bash -c "cd /tmp && rm -rf /var"',
    "sudo rm -rf /usr",
    "/usr/bin/env rm -rf /etc",                 # absolute prefix binary
    "/bin/rm -rf /etc",                         # absolute rm (skips the PATH shim)
    "/usr/bin/env rm -rf /tmp/x/../x",
    "rm -rf /", "rm -rf /home", f"rm -rf {HOME}", "rm -rf /mnt",
    "rm -rf $R",                                # unexpanded variable
    "R=/tmp/x; rm -rf $R",
    "rm -rf ../work",
    "rm -rf *",
    "find /etc -name '*.conf' -delete",
    "find /var -name '*.log' -exec rm {} \\;",
    "python3 - <<'PY'\nimport shutil\nshutil.rmtree('/etc')\nPY",
    "rmdir /etc",
])
def test_refused(cmd):
    assert guard.check_command(cmd, cwd="/tmp") is not None


def test_refused_pulsar_data_dir(tmp_path, monkeypatch):
    data = tmp_path / "pulsar-data"
    monkeypatch.setenv("PULSAR_DATA", str(data))
    assert guard.check_command(f"rm -r {data}", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {data}/", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {data}/*", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {data}/pulses", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {data}/pulses/abc123", cwd="/tmp") is not None
    # deleting inside a pulse dir (grandchild of pulses/) is allowed
    assert guard.check_command(f"rm -f {data}/pulses/abc123/tmp.log", cwd="/tmp") is None


def test_refused_default_data_and_config_dirs(tmp_path, monkeypatch):
    # Even when PULSAR_DATA is unset or points elsewhere, the default data dir
    # (~/.local/share/pulsar) and config dir (~/.config/pulsar) and their
    # top-level entries stay protected.
    monkeypatch.delenv("PULSAR_DATA", raising=False)
    monkeypatch.delenv("XDG_DATA_HOME", raising=False)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)

    default_data = f"{HOME}/.local/share/pulsar"
    default_cfg = f"{HOME}/.config/pulsar"
    for cmd in (
        f"rm -rf {default_data}",
        f"rm -rf {default_data}/",
        f"rm -rf {default_data}/*",
        f"rm -rf {default_data}/pulsar.db",
        f"rm -rf {default_data}/pulses",
        f"rm -rf {default_data}/pulses/*",
        f"rm -rf {default_data}/pulses/abc123",
        f"rm -rf {default_cfg}",
        f"rm -rf {default_cfg}/*",
        f"rm -f {default_cfg}/pulsar.env",
        "rm -rf ~/.local/share/pulsar",
        "rm -rf ~/.config/pulsar",
    ):
        assert guard.check_command(cmd, cwd="/tmp") is not None, cmd

    # A pulse can still delete files inside its own pulse home.
    assert guard.check_command(f"rm -f {default_data}/pulses/abc123/tmp.log", cwd="/tmp") is None
    assert guard.check_command("rm -rf scratch", cwd=f"{default_data}/pulses/abc123") is None

    # Custom XDG_DATA_HOME / XDG_CONFIG_HOME are honored too.
    xdg_data = tmp_path / "xdg-data"
    xdg_cfg = tmp_path / "xdg-cfg"
    monkeypatch.setenv("XDG_DATA_HOME", str(xdg_data))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(xdg_cfg))
    assert guard.check_command(f"rm -rf {xdg_data}/pulsar", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {xdg_data}/pulsar/pulses", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {xdg_data}/pulsar/pulses/p1", cwd="/tmp") is not None
    assert guard.check_command(f"rm -f {xdg_data}/pulsar/pulses/p1/out.tmp", cwd="/tmp") is None
    assert guard.check_command(f"rm -rf {xdg_cfg}/pulsar", cwd="/tmp") is not None
    assert guard.check_command(f"rm -f {xdg_cfg}/pulsar/pulsar.env", cwd="/tmp") is not None


@pytest.mark.parametrize("cmd", [
    "ls -la /etc",
    "rm -rf /tmp/archive-test",
    "ssh user@example.com 'rm -rf /tmp/archive-test'",
    "cd /tmp/work && rm -rf round2/prepared && mkdir -p round2/prepared",
    "find /tmp/work/build -name '*.o' -delete",
    "find /etc -name '*.conf' | wc -l",         # find without delete
    "grep -rn 'rm -rf' /tmp/repo",              # the words, not the command
    "echo 'rm -rf /etc is bad'",                # quoted prose... see note below
    "git status",
    "",
])
def test_allowed(cmd):
    # The echo case IS refused: the guard reads inside quotes on purpose, so a
    # quoted delete of a protected root is refused even when it is prose.
    if cmd.startswith("echo"):
        assert guard.check_command(cmd, cwd="/tmp") is not None
        return
    assert guard.check_command(cmd, cwd="/tmp") is None


def test_relative_paths_resolve_against_cwd(tmp_path, monkeypatch):
    data = tmp_path / "pulsar-data"
    monkeypatch.setenv("PULSAR_DATA", str(data))
    assert guard.check_command("rm -rf abc123", cwd=f"{data}/pulses") is not None
    assert guard.check_command("rm -rf build", cwd=f"{data}/pulses/abc123") is None


def test_guard_default_on_when_config_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path / "fresh"))
    assert not guard.config_path().exists()
    assert guard.check_command("rm -rf /etc", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {tmp_path}/fresh/pulses/p1", cwd="/tmp") is not None


def test_guard_off_allows_when_disabled(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path / "data"))
    guard.write_config(enabled=False, extra_paths=[])
    assert guard.check_command("rm -rf /etc", cwd="/tmp") is None
    assert guard.check_command("rm -rf /tmp/a/../a", cwd="/tmp") is None
    assert guard.main(["check", "--cwd", "/tmp", "--", "rm -rf /etc"]) == 0


def test_guard_extra_path_refused(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path / "data"))
    extra = str(tmp_path / "projects")
    # Not refused before being added
    assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is None
    assert guard.check_command(f"rm -rf {extra}/app1", cwd="/tmp") is None
    guard.write_config(enabled=True, extra_paths=[extra])
    # Refused for the root itself, wildcard, and direct child; allowed deeper inside
    assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {extra}/*", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {extra}/app1", cwd="/tmp") is not None
    assert guard.check_command(f"rm -rf {extra}/app1/build", cwd="/tmp") is None


@pytest.mark.anyio
async def test_daemon_settings_sync_guard_config(store, fake_spawn, fake_sock, tmp_path):
    app = create_app(store=store)
    extra = str(tmp_path / "protected-tree")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://daemon") as client:
        async with app.router.lifespan_context(app):
            # Default on at boot
            assert guard.check_command("rm -rf /etc", cwd="/tmp") is not None
            # Add extra protected path via settings API
            r = await client.put("/settings/guard.extra_paths", json=[extra])
            assert r.status_code == 200
            assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is not None
            assert guard.check_command(f"rm -rf {extra}/svc", cwd="/tmp") is not None
            # Disable via settings API
            r = await client.put("/settings/guard.enabled", json=False)
            assert r.status_code == 200
            assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is None
            # Delete guard.enabled -> back to default on
            r = await client.delete("/settings/guard.enabled")
            assert r.status_code == 200
            assert guard.check_command(f"rm -rf {extra}", cwd="/tmp") is not None


def test_hook_denies_with_reason(tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "DENIALS_LOG", tmp_path / "d.jsonl")
    reply = guard.hook_reply({"tool_name": "Bash", "cwd": "/tmp",
                              "tool_input": {"command": TRAVERSAL_CMD}})
    out = reply["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "deny"
    assert "app/../app" in out["permissionDecisionReason"]
    rec = json.loads((tmp_path / "d.jsonl").read_text().splitlines()[0])
    assert rec["command"] == TRAVERSAL_CMD and rec["source"] == "claude-hook"


def test_hook_leaves_other_calls_alone():
    assert guard.hook_reply({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert guard.hook_reply({"tool_name": "Read", "tool_input": {"file_path": "/x"}}) is None


def test_check_verb_exit_codes(capsys, tmp_path, monkeypatch):
    monkeypatch.setattr(guard, "DENIALS_LOG", tmp_path / "d.jsonl")
    assert guard.main(["check", "--cwd", "/tmp", "--", "ls"]) == 0
    assert guard.main(["check", "--cwd", "/tmp", "--", "rm -rf /etc"]) == 2
    assert "pulsar-guard:" in capsys.readouterr().out


def test_every_claude_pulse_gets_the_guard_hook():
    cmds = [h["command"] for h in JOB_HOOKS["PreToolUse"][0]["hooks"]]
    assert cmds[0].endswith("ops/bin/pulsar-guard hook")
    full_fat, _ = diet_flags_env(None)
    assert json.loads(full_fat[full_fat.index("--settings") + 1]) == {"hooks": JOB_HOOKS}
    clean, _ = diet_flags_env({"clean": True})
    assert json.loads(clean[clean.index("--settings") + 1])["hooks"] == JOB_HOOKS


def test_every_pi_pulse_gets_the_guard_extension():
    assert pi.PI_GUARD_EXTENSION.is_file()
    src = pi.PI_GUARD_EXTENSION.read_text()
    assert 'pi.on("tool_call"' in src and "pulsar-guard" in src
    assert "DEFAULT_BASH_TIMEOUT_S = 120" in src
    assert pi.pi_guard_args() == ["-e", str(pi.PI_GUARD_EXTENSION),
                                  "--append-system-prompt", pi.PI_BASH_PROMPT]
    assert "120 s" in pi.PI_BASH_PROMPT and "cd persists" in pi.PI_BASH_PROMPT
