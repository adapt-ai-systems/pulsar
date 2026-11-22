"""guard.ts persists `cd` across pi bash calls.

Runs tests/pi_ext_cwd_check.mjs under node: it loads the extension with a
fake pi, pushes four bash calls through the tool_call hook and executes the
rewritten commands; the second call must start where the first one cd'd,
the exit status must survive, and a heredoc must still work.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
GUARD = ROOT / "pulsar" / "vessel" / "pi_ext" / "guard.ts"
CHECK = ROOT / "tests" / "pi_ext_cwd_check.mjs"


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
def test_cd_persists_across_bash_calls(tmp_path):
    done = subprocess.run(["node", str(CHECK), str(GUARD)], cwd=tmp_path,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip().endswith("OK")
