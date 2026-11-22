"""pi_ext/spill.ts — big tool outputs go to a file and
the harness override `spill_chars` that turns it on."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
from pydantic import ValidationError

from pulsar.daemon.schemas import HarnessOverridesBody
from pulsar.vessel import pi

ROOT = Path(__file__).resolve().parents[1]
CHECK = ROOT / "tests" / "pi_ext_spill_check.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_spill_extension(tmp_path):
    cli = tmp_path / "cli"
    cli.mkdir()
    (tmp_path / "harness-overrides.json").write_text(json.dumps({"spill_chars": 500}))
    done = subprocess.run(["node", str(CHECK), str(pi.PI_SPILL_EXTENSION)],
                          cwd=cli, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().endswith("OK")


def test_spill_off_by_default():
    assert pi.pi_spill_args({}) == []
    assert pi.pi_spill_args(None) == []


def test_spill_on_loads_extension():
    assert pi.pi_spill_args({"spill_chars": 2000}) == ["-e", str(pi.PI_SPILL_EXTENSION)]


def test_spill_validation(tmp_path):
    p = tmp_path / "harness-overrides.json"
    assert pi._validated_harness_overrides({"spill_chars": 2000}, p) == {"spill_chars": 2000}
    assert pi._validated_harness_overrides({"spill_chars": 10}, p) == {}
    assert pi._validated_harness_overrides({"spill_chars": True}, p) == {}
    assert HarnessOverridesBody(spill_chars=2000).spill_chars == 2000
    with pytest.raises(ValidationError):
        HarnessOverridesBody(spill_chars=10)
