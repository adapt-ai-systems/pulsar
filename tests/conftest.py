"""Shared pytest config for the daemon's async tests.

The daemon tests use anyio's pytest plugin (bundled with the ``anyio``
package). Pin the backend to asyncio — trio isn't installed and the daemon
runs under asyncio in production anyway.
"""


import os
from pathlib import Path

import pytest

# Point the model registry and Pi provider baseline at scrubbed test fixtures
# BEFORE any pulsar module imports them, so import-time tables (pi._CLI,
# INTERFACE_MODELS, EFFORT_LEVELS_BY_MODEL, VISION_MODELS) follow cleanly.
_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures"
os.environ.setdefault("PULSAR_MODELS_PATH",
                      str(_FIXTURES_DIR / "models_fixture.json"))
os.environ.setdefault("PULSAR_PI_PROVIDERS_PATH",
                      str(_FIXTURES_DIR / "pi_providers_fixture.json"))

import pulsar.launch.units as _units_mod

# Every launch.units function that reaches the REAL systemd user manager.
_SYSTEMD_TOUCHERS = ("spawn_runner", "stop_unit", "list_pulse_units",
                     "fence_report", "unit_active")


@pytest.fixture
def anyio_backend():
    return "asyncio"


def pytest_addoption(parser):
    parser.addoption(
        "--update-goldens",
        action="store_true",
        default=False,
        help="Regenerate scaffolding golden files in-place.",
    )


@pytest.fixture(autouse=True)
def isolated_data_root(monkeypatch, tmp_path):
    """Every test writes under its own tmp data root, never the live one.

    2026-08-29: test_condwatch fired watches into paths.inbox_new(<random
    id>) with PULSAR_DATA unset — 123 junk data/pulses/<id>/inbox dirs in
    the PRODUCTION data dir in one day. paths.data_root() reads the env
    live, so setting it here covers every module that goes through paths.
    """
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path / "data"))


@pytest.fixture(autouse=True)
def allow_asgi_test_hosts(monkeypatch):
    """Allow synthetic ASGI client hosts used by existing unit tests.

    Tests that exercise request validation clear or set PULSAR_WEB_ALLOWED_HOSTS
    explicitly.
    """
    monkeypatch.setenv("PULSAR_WEB_ALLOWED_HOSTS", "daemon,d,t,testserver")


@pytest.fixture(autouse=True)
def no_real_systemd(monkeypatch):
    """No test may reach the real systemd user manager. Ever.

    2026-07-27 post-mortem: a test booted the app lifespan without faking
    spawn; boot reconcile's orphan sweep listed the HOST'S real
    pulsar-pulse-* units against an empty tmp store and stop_unit'd them —
    the suite killed the very pulse that was running it.

    Autouse fixtures apply first, so tests that need spawn behavior override
    these names afterwards (exactly what FakeSpawn does). An unfaked call
    now raises loudly instead of murdering live pulses.
    """
    def _blocked(name):
        def _fail(*args, **kwargs):
            raise RuntimeError(
                f"pulsar.launch.units.{name} called from a test without a "
                f"fake — this reaches the REAL systemd user manager "
                f"(conftest.no_real_systemd; guards against touching live "
                f"systemd units during tests)")
        return _fail
    for name in _SYSTEMD_TOUCHERS:
        monkeypatch.setattr(_units_mod, name, _blocked(name))


@pytest.fixture(autouse=True)
def no_real_slot_probe(monkeypatch):
    """The daemon lifespan starts a slot probe loop; no test may let it dial
    the real slot hosts (remote hosts time out at 5 s per probe and the
    app teardown waits for them). An injected opener still goes through the
    real code; tests that need a probe result patch probe_endpoint themselves.
    """
    from pulsar.daemon import slots as _slots
    _real = _slots.probe_endpoint

    def _offline(base_url, *, opener=None, timeout=_slots.PROBE_TIMEOUT_S):
        if opener is not None:
            return _real(base_url, opener=opener, timeout=timeout)
        return _slots._status(live=False, served_id=None,
                              error="offline_in_tests")
    monkeypatch.setattr(_slots, "probe_endpoint", _offline)
