"""The pi `script` tool — programmatic tool calling.

tests/pi_ext_script_check.mjs drives script_runtime.ts with fake tools and,
when the tuned pi package is present, script.ts against pi's real
read/bash/edit/write/grep/ls definitions. The Python half checks the
harness override `ptc` that turns the tool on.
"""
import os
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from pulsar.daemon.schemas import HarnessOverridesBody
from pulsar.launch import units as units_mod
from pulsar.vessel import pi

ROOT = Path(__file__).resolve().parents[1]
EXT = ROOT / "pulsar" / "vessel" / "pi_ext"
CHECK = ROOT / "tests" / "pi_ext_script_check.mjs"
_PI_BIN = Path(os.environ.get("PULSAR_PI_BIN") or units_mod.DEFAULT_PI_BIN)
PI_PKG = _PI_BIN.parent.parent / "@mariozechner" / "pi-coding-agent"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_script_runtime_and_pi_tools(tmp_path):
    cmd = ["node", str(CHECK), str(EXT / "script_runtime.ts")]
    if (PI_PKG / "dist" / "index.js").is_file():
        cmd += [str(EXT / "script.ts"), str(PI_PKG)]
    done = subprocess.run(cmd, cwd=tmp_path, capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().endswith("OK")


def test_ptc_off_by_default():
    assert pi.pi_script_args({}) == []
    assert pi.pi_script_args(None) == []
    assert "script" not in pi.pi_tools_args({})[1].split(",")


def test_ptc_on_loads_extension_and_allowlists_tool():
    assert pi.pi_script_args({"ptc": True}) == ["-e", str(pi.PI_SCRIPT_EXTENSION)]
    assert pi.PI_SCRIPT_EXTENSION.is_file()
    tools = pi.pi_tools_args({"ptc": True})[1].split(",")
    assert tools[-1] == "script" and "read" in tools
    # a custom allowlist gains script too, once
    assert pi.pi_tools_args({"ptc": True, "tools": ["read", "script"]})[1] == "read,script"


def test_base_prompt_lists_script():
    prompt = pi.pi_base_args([], {"ptc": True})[-1]
    assert "- script: Run a JavaScript program" in prompt
    assert "Batch work with script" in prompt
    assert "script" not in pi.pi_base_args([], {})[-1]


def test_ptc_validation(tmp_path):
    p = tmp_path / "harness-overrides.json"
    assert pi._validated_harness_overrides({"ptc": True}, p) == {"ptc": True}
    assert pi._validated_harness_overrides({"ptc": "yes"}, p) == {}
    assert HarnessOverridesBody(ptc=True).ptc is True
    with pytest.raises(ValidationError):
        HarnessOverridesBody(ptc="maybe")
