"""launch.units.unit_active: systemd's is-active verdict -> True/False/None.

Imported at module load so the test holds the REAL function (conftest's
no_real_systemd replaces the module attribute); subprocess.run is faked,
so no test reaches systemd.
"""

import subprocess

import pytest

import pulsar.launch.units as units_mod
from pulsar.launch.units import unit_active as real_unit_active


def _fake_run(stdout="", rc=0, exc=None):
    calls = []

    def run(cmd, **kw):
        calls.append(cmd)
        if exc:
            raise exc
        return subprocess.CompletedProcess(cmd, rc, stdout=stdout, stderr="")
    return run, calls


@pytest.mark.parametrize("stdout,rc,want", [
    ("active\n", 0, True),
    ("activating\n", 3, True),
    ("inactive\n", 3, False),
    ("failed\n", 3, False),
    ("", 4, None),
])
def test_unit_active_maps_systemd_state(monkeypatch, stdout, rc, want):
    run, calls = _fake_run(stdout, rc)
    monkeypatch.setattr(units_mod.subprocess, "run", run)
    assert real_unit_active("abc") is want
    assert calls == [["systemctl", "--user", "is-active",
                      "pulsar-pulse-abc.service"]]


def test_unit_active_timeout_is_unknown(monkeypatch):
    run, _ = _fake_run(exc=subprocess.TimeoutExpired("systemctl", 30))
    monkeypatch.setattr(units_mod.subprocess, "run", run)
    assert real_unit_active("abc") is None
