from unittest.mock import Mock
import pytest
import pulsar.launch.units as units_mod
from pulsar.store import Store
from pulsar.daemon import paths, watch_sleep


@pytest.mark.parametrize("original", ["stopped", "archived", "running"])
@pytest.mark.parametrize("block", ["none", "unacked", "busy", "new", "feeding", "stop_failed"])
def test_return_after_work(tmp_path, monkeypatch, original, block):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    store = Store(str(tmp_path / "test.db"))
    pulse = store.create_pulse("Watcher", "m", "v", state="stopped")
    if original == "archived":
        pulse = store.archive_pulse(pulse.id)
    elif original == "running":
        pulse = store.set_state(pulse.id, "running")
    watch_sleep.remember(store, pulse, "wake.json")
    if original == "archived":
        store.unarchive_pulse(pulse.id)
    store.set_state(pulse.id, "running")
    home = paths.pulse_home(pulse.id)
    for folder in ("new", "feeding", "acked"):
        (home / "inbox" / folder).mkdir(parents=True)
    if block != "unacked":
        (home / "inbox/acked/wake.json").write_text("{}")
    if block in ("new", "feeding"):
        (home / "inbox" / block / "other.json").write_text("{}")
    monkeypatch.setattr(paths, "heartbeat_fresh", lambda beat: True)
    stop = Mock(return_value=block != "stop_failed")
    monkeypatch.setattr(units_mod, "stop_unit", stop)
    result = watch_sleep.finish(store, store.get_pulse(pulse.id),
                               {"state": "busy" if block == "busy" else "idle"})
    expected = original != "running" and block == "none"
    assert result == expected
    assert store.get_pulse(pulse.id).state == (original if expected else "running")
    store.close()


def test_repeat_preserves_archive_and_tracks_latest_message(tmp_path):
    store = Store(str(tmp_path / "test.db"))
    pulse = store.create_pulse("Watcher", "m", "v", state="stopped")
    pulse = store.archive_pulse(pulse.id)
    watch_sleep.remember(store, pulse, "first.json")
    store.unarchive_pulse(pulse.id)
    store.set_state(pulse.id, "running")
    watch_sleep.remember(store, store.get_pulse(pulse.id), "second.json")
    assert store.get_pulse(pulse.id).settings[watch_sleep.KEY] == {
        "state": "archived", "message": "second.json"}
    store.close()


@pytest.mark.parametrize("kind", ["condition", "delegation"])
@pytest.mark.parametrize("original", ["stopped", "archived"])
def test_delivery_remembers_resting_state(tmp_path, monkeypatch, kind, original):
    from types import SimpleNamespace
    from pulsar.daemon import lifecycle, condwatch, watcher
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    store = Store(str(tmp_path / "test.db"))
    pulse = store.create_pulse("Watcher", "m", "v", state=original)
    monkeypatch.setattr(lifecycle, "wake_if_stopped",
                        lambda s, p: s.set_state(p.id, "running"))
    watch = SimpleNamespace(pulse_id=pulse.id, spawner_id=pulse.id)
    notify = condwatch._notify_owner if kind == "condition" else watcher._notify_spawner
    notify(store, watch, "Do the work and report")
    if kind == "delegation" and original == "archived":
        assert store.get_pulse(pulse.id).state == "archived"
        assert watch_sleep.KEY not in store.get_pulse(pulse.id).settings
        assert not paths.inbox_new(pulse.id).exists()
        store.close()
        return
    saved = store.get_pulse(pulse.id).settings[watch_sleep.KEY]
    assert saved["state"] == original
    assert (paths.inbox_new(pulse.id) / saved["message"]).is_file()
    store.close()
