"""The pulsar.launch modules (units, diet, context, skills, cages) must stay
light to import: no FastAPI, no pulsar.daemon app (26 MB ->
53 MB RSS per runner otherwise)."""

import subprocess
import sys
from pathlib import Path


def test_importing_launch_does_not_pull_in_the_daemon_app():
    code = (
        "import sys, pulsar.launch.units, pulsar.launch.diet, "
        "pulsar.launch.context, pulsar.launch.skills, pulsar.launch.cages;"
        "bad = sorted(m for m in sys.modules if m == 'fastapi' or "
        "m.startswith(('fastapi.', 'pulsar.daemon')));"
        "print(','.join(bad))")
    root = Path(__file__).resolve().parents[1]
    out = subprocess.run([sys.executable, "-c", code], text=True, cwd=root,
                         capture_output=True, check=True).stdout.strip()
    assert out == ""
