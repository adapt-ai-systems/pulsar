"""The tool-shell PATH every pulse gets (runner.tool_path)."""
import os
from pathlib import Path

from pulsar.launch import units
from pulsar.runner import runner

# conftest.no_real_systemd blocks units.spawn_runner in every test. Keep the
# real function; the test below fakes units._run, so systemd is not reached.
_REAL_SPAWN_RUNNER = units.spawn_runner


def test_tool_path_adds_existing_user_dirs_after_inherited(tmp_path):
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    (tmp_path / ".cargo" / "bin").mkdir(parents=True)
    out = runner.tool_path("/usr/bin:/bin", tmp_path).split(os.pathsep)
    assert out[0] == str(runner.GUARD_BIN_DIR)
    assert out[1:3] == ["/usr/bin", "/bin"]
    assert out[3:] == [str(tmp_path / ".local" / "bin"), str(tmp_path / ".cargo" / "bin")]


def test_tool_path_skips_missing_and_duplicate_dirs(tmp_path):
    (tmp_path / ".local" / "bin").mkdir(parents=True)
    local = str(tmp_path / ".local" / "bin")
    out = runner.tool_path(f"/usr/bin:{local}", tmp_path).split(os.pathsep)
    assert out.count(local) == 1
    assert not any(p.endswith(".cargo/bin") for p in out)


def test_tool_path_puts_daemon_dirs_before_inherited_dirs(tmp_path):
    # The daemon found a tool in an nvm-style directory. The pulse must search
    # that directory first, and keep every inherited directory.
    nvm = "/home/u/.nvm/versions/node/v22/bin"
    out = runner.tool_path("/usr/sbin:/usr/bin:/bin", tmp_path,
                           f"{nvm}:/usr/bin:/bin").split(os.pathsep)
    assert out == [str(runner.GUARD_BIN_DIR), nvm, "/usr/bin", "/bin",
                   "/usr/sbin"]


def test_tool_path_without_daemon_path_is_unchanged(tmp_path):
    assert (runner.tool_path("/usr/bin:/bin", tmp_path, "")
            == runner.tool_path("/usr/bin:/bin", tmp_path))


def test_spawn_runner_passes_daemon_path_to_the_unit(tmp_path, monkeypatch):
    import types

    calls: list[list[str]] = []
    monkeypatch.setattr(units, "_await_unit_gone", lambda *a, **k: None)
    monkeypatch.setattr(units, "list_pulse_units", lambda: [])
    monkeypatch.setattr(units, "materialize_diet", lambda *a, **k: {})
    monkeypatch.setattr(
        units, "_run",
        lambda cmd, what: (calls.append(list(cmd))
                           or types.SimpleNamespace(returncode=0, stderr="")))
    monkeypatch.setenv("PATH", "/opt/my tools/bin:/usr/bin")
    _REAL_SPAWN_RUNNER("p1", tmp_path / "home", "some-model", "claude-code")
    cmd = calls[0]
    want = "--setenv=PULSAR_DAEMON_PATH=/opt/my tools/bin:/usr/bin"
    assert want in cmd
    # A systemd-run option must come before the program to run.
    assert cmd.index(want) < cmd.index("-m")


def _spawn_cmd(tmp_path, monkeypatch, vessel):
    import types

    calls: list[list[str]] = []
    monkeypatch.setattr(units, "_await_unit_gone", lambda *a, **k: None)
    monkeypatch.setattr(units, "list_pulse_units", lambda: [])
    monkeypatch.setattr(units, "materialize_diet", lambda *a, **k: {})
    monkeypatch.setattr(
        units, "_run",
        lambda cmd, what: (calls.append(list(cmd))
                           or types.SimpleNamespace(returncode=0, stderr="")))
    _REAL_SPAWN_RUNNER("p1", tmp_path / "home", "some-model", vessel)
    return calls[0]


def test_spawn_runner_passes_the_tool_override_for_the_vessel(tmp_path,
                                                              monkeypatch):
    # pulsar.env reaches the daemon only. The override for the pulse's vessel
    # must reach the runner unit; the others are not needed there.
    monkeypatch.setenv("PULSAR_CLAUDE_BIN", "/opt/x/claude")
    monkeypatch.setenv("PULSAR_CODEX_BIN", "/opt/x/codex")
    cmd = _spawn_cmd(tmp_path, monkeypatch, "claude-code")
    assert "--setenv=PULSAR_CLAUDE_BIN=/opt/x/claude" in cmd
    assert not any("PULSAR_CODEX_BIN" in a for a in cmd)


def test_spawn_runner_pins_no_pi_path_when_only_a_path_pi_exists(tmp_path,
                                                                 monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pi = bindir / "pi"
    pi.write_text("#!/bin/sh\n")
    pi.chmod(0o755)
    monkeypatch.setattr(units, "DEFAULT_PI_BIN", str(tmp_path / "no" / "pi"))
    monkeypatch.setattr(units, "PI_BIN", units.DEFAULT_PI_BIN)
    monkeypatch.setenv("PATH", str(bindir))
    cmd = _spawn_cmd(tmp_path, monkeypatch, "pi")
    assert not any("PULSAR_PI_BIN" in a for a in cmd)


def test_spawn_runner_pins_the_default_pi_path_when_no_pi_exists(tmp_path,
                                                                 monkeypatch):
    monkeypatch.setattr(units, "DEFAULT_PI_BIN", str(tmp_path / "no" / "pi"))
    monkeypatch.setattr(units, "PI_BIN", units.DEFAULT_PI_BIN)
    monkeypatch.setenv("PATH", str(tmp_path))
    cmd = _spawn_cmd(tmp_path, monkeypatch, "pi")
    assert f"Environment=PULSAR_PI_BIN={units.DEFAULT_PI_BIN}" in cmd


def test_spawn_runner_pins_an_explicit_pi_path(tmp_path, monkeypatch):
    monkeypatch.setattr(units, "PI_BIN", "/fake/bin/pi")
    cmd = _spawn_cmd(tmp_path, monkeypatch, "pi")
    assert "Environment=PULSAR_PI_BIN=/fake/bin/pi" in cmd
