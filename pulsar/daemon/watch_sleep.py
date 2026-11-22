"""Return temporarily watcher-woken pulses to their previous resting state."""
import logging

from pulsar.daemon import paths
from pulsar.launch import units as units_mod

KEY = "watch_return"
logger = logging.getLogger("pulsar.daemon.watch_sleep")


def remember(store, pulse, message_name):
    # Repeated watches must not replace the original archived/stopped state.
    saved = pulse.settings.get(KEY) or {}
    if saved or pulse.state in ("stopped", "archived"):
        store.update_settings(pulse.id, {KEY: {
            "state": saved.get("state", pulse.state), "message": message_name,
        }})


def finish(store, pulse, beat):
    """Only reap after the wake message is acked and all queued work is done."""
    saved = pulse.settings.get(KEY)
    if not saved or saved.get("state") not in ("stopped", "archived"):
        return False
    home = paths.pulse_home(pulse.id)
    if not (home / "inbox" / "acked" / saved["message"]).is_file():
        return False
    if not paths.heartbeat_fresh(beat) or beat.get("state") != "idle":
        return False
    # A watcher that woke near its context ceiling must ROTATE, not sleep:
    # the handoff monitor only sees running pulses, so stopping it here
    # would defer the handoff to some future wake — possibly past the model's
    # hard limit. Stay running; handoff.monitor.tick fires within TICK_SECONDS and
    # the fresh successor is put to sleep on a later pass.
    from pulsar.daemon import handoff
    if handoff.over_threshold(pulse, beat):
        logger.info("watch_sleep pulse=%s idle but ctx=%s over handoff "
                    "threshold — kept running for rotation",
                    pulse.id, beat.get("last_ctx_tokens"))
        return False
    for folder in ("new", "feeding"):
        if next((home / "inbox" / folder).glob("*.json"), None) is not None:
            return False
    if not units_mod.stop_unit(pulse.id):
        return False
    store.set_state(pulse.id, "stopped")
    if saved["state"] == "archived":
        store.archive_pulse(pulse.id)
    store.update_settings(pulse.id, {KEY: None, "stop_reason": "watch work complete"})
    return True
