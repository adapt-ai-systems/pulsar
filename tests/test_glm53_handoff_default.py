"""Regression coverage for the production GLM-5.3 handoff default."""

from pulsar.daemon import handoff
from pulsar.store import Store


def test_pi_glm_flash_defaults_to_150k_handoff(tmp_path):
    store = Store(str(tmp_path / "pulsar.db"))
    try:
        pulse = store.create_pulse(
            "glm", "pi-glm-flash", "pi", handoff_tokens=None
        )
        # 54fa1b4 (2026-09-03) moved the production default 250k -> 150k on
        # the TP4 ring; the assertion lagged until 2026-09-06.
        assert handoff.threshold_for(pulse) == 150_000
    finally:
        store.close()
