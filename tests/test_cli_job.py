"""pulsar-job: background work runs in its own systemd unit."""
from __future__ import annotations

import json
import shlex

from pulsar.cli import job
from pulsar.vessel.claude import JOB_HOOKS, diet_flags_env


def test_hook_wraps_background_bash():
    reply = job.hook_rewrite({"tool_name": "Bash", "tool_input": {
        "command": "pytest -q 'a b'", "run_in_background": True, "description": "tests"}})
    out = reply["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    cmd = out["updatedInput"]["command"]
    argv = shlex.split(cmd)
    assert argv[:3] == ["pulsar-job", "run", "--follow"]
    assert argv[-1] == "pytest -q 'a b'" and argv[-2] == "--"
    assert out["updatedInput"]["run_in_background"] is True
    assert out["updatedInput"]["description"] == "tests"
    assert "pulsar-job stop" in out["additionalContext"]


def test_hook_leaves_other_calls_alone():
    assert job.hook_rewrite({"tool_name": "Bash", "tool_input": {"command": "ls"}}) is None
    assert job.hook_rewrite({"tool_name": "Read", "tool_input": {"run_in_background": True}}) is None
    assert job.hook_rewrite({"tool_name": "Bash", "tool_input": {
        "command": "pulsar-job run x", "run_in_background": True}}) is None


def test_build_command_own_unit_and_exit_writer(tmp_path):
    script = tmp_path / "n.sh"
    cmd = job.build_command("abcdef0123", "n", script, tmp_path / "n.log", tmp_path / "n.exit",
                            "/tmp", "2G", {"PATH": "/bin", "INVOCATION_ID": "x", "BAD-NAME": "y"})
    assert cmd[:2] == ["systemd-run", "--user"]
    assert "--unit=pulsar-job-abcdef01-n.service" in cmd
    assert "--slice=pulsar-jobs.slice" in cmd  # a child of pulsar.slice: the aggregate fence holds
    assert "MemoryMax=2G" in cmd and "WorkingDirectory=/tmp" in cmd
    assert "--setenv=PATH=/bin" in cmd
    assert not any("INVOCATION_ID" in a or "BAD-NAME" in a for a in cmd)
    assert f"ExecStopPost=/bin/sh {tmp_path / 'n.post'} {tmp_path / 'n.exit'}" in cmd
    assert (tmp_path / "n.post").read_text().count("EXIT_STATUS") == 1
    assert cmd[-2:] == ["/bin/bash", str(script)]


def test_build_command_forwards_pulsar_data_and_daemon(tmp_path):
    script = tmp_path / "j.sh"
    cmd = job.build_command(
        "abcdef0123", "j", script, tmp_path / "j.log", tmp_path / "j.exit",
        "/tmp", "2G",
        {"PULSAR_DATA": "/tmp/custom-data", "PULSAR_DAEMON": "http://127.0.0.1:18726"},
    )
    assert "--setenv=PULSAR_DATA=/tmp/custom-data" in cmd
    assert "--setenv=PULSAR_DAEMON=http://127.0.0.1:18726" in cmd


def test_clean_name():
    assert job.clean_name("my job/1") == "my-job-1"
    assert job.clean_name("../x") == "x"


def test_every_claude_pulse_gets_the_hook():
    full_fat, _ = diet_flags_env(None)
    assert json.loads(full_fat[full_fat.index("--settings") + 1]) == {"hooks": JOB_HOOKS}
    clean, _ = diet_flags_env({"clean": True, "skills": []})
    settings = json.loads(clean[clean.index("--settings") + 1])
    assert settings["hooks"] == JOB_HOOKS and settings["disableBundledSkills"] is True
    assert JOB_HOOKS["PreToolUse"][0]["hooks"][-1]["command"].endswith("ops/bin/pulsar-job hook")
