"""The conftest systemd guard must hold on the module that DEFINES each
systemd-touching function (pulsar.launch.units): calls inside it look their
names up there."""

from pathlib import Path

import pytest

import pulsar.launch.units as units_mod
from tests.conftest import _SYSTEMD_TOUCHERS

_ARGS = {
    "spawn_runner": ("p1", Path("/nonexistent-home"), "m", "claude-code"),
    "stop_unit": ("p1",),
    "list_pulse_units": (),
    "fence_report": ("p1",),
    "unit_active": ("p1",),
}


@pytest.mark.parametrize("name", _SYSTEMD_TOUCHERS)
def test_launch_units_toucher_is_blocked(name):
    with pytest.raises(RuntimeError, match="REAL systemd"):
        getattr(units_mod, name)(*_ARGS[name])
