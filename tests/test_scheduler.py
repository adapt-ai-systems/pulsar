"""Scheduler tests — drive tick() directly with a frozen `now` (no sleeping).

    venv/bin/python -m pytest tests -k scheduler -q

Covers: due-ness math for loop/once/daily including catch-up-fires-
ONCE; firing writes the inbox file (sched- prefix) and wakes a stopped pulse
(monkeypatched spawn + fabricated heartbeat, like test_daemon.py); archived is
skipped and never woken; a failed spawn logs loudly and does NOT tight-loop
(mark_fired stamped so the next tick is not due). No real runners, no systemd,
no tokens.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

import pulsar.launch.units as units_mod
from pulsar.store import Store
from pulsar.daemon import paths
from pulsar.daemon import scheduler as sched_mod


# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #
class FakeSpawn:
    """Records spawn calls; writes a fresh heartbeat on spawn (like a live one)."""

    def __init__(self) -> None:
        self.spawned: list[dict] = []
        self.spawn_should_fail = False

    def spawn_runner(self, pulse_id, home, model, vessel, resume=None,
                     memory_max="6G", tasks_max=512, diet=None, title=None,
                     effort=None, fast=False):
        self.spawned.append({
            "pulse_id": pulse_id, "resume": resume, "memory_max": memory_max,
            "diet": diet, "effort": effort, "fast": fast,
        })
        if self.spawn_should_fail:
            raise RuntimeError("systemd-run failed (test)")
        _write_heartbeat(pulse_id, state="idle",
                         cli_session_id=f"sess-{pulse_id}", age_s=0.0)


def _write_heartbeat(pulse_id: str, state: str, cli_session_id: str,
                     age_s: float) -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts_epoch = time.time() - age_s
    ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    beat = {"ts": ts, "pid": 1234, "cli_pid": 4242, "state": state,
            "cli_session_id": cli_session_id, "protocol": 1}
    (home / "heartbeat.json").write_text(json.dumps(beat))


@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    return root


@pytest.fixture()
def store(data_root):
    s = Store(str(data_root / "pulsar.db"))
    yield s
    s.close()


@pytest.fixture()
def fake_spawn(monkeypatch):
    fs = FakeSpawn()
    monkeypatch.setattr(units_mod, "spawn_runner", fs.spawn_runner)
    return fs


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


UTC = timezone.utc


# --------------------------------------------------------------------------- #
# due-ness: loop
# --------------------------------------------------------------------------- #
def test_loop_first_fire_at_created_plus_interval(store):
    created = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=30)
    # backdate created_at so we control the anchor
    store._conn.execute("UPDATE schedules SET created_at=? WHERE id=?;",
                        (_iso(created), s.id))
    s = store.get_schedule(s.id)

    assert not sched_mod.is_due(s, created + timedelta(minutes=29))
    assert sched_mod.is_due(s, created + timedelta(minutes=30))
    assert sched_mod.is_due(s, created + timedelta(minutes=90))


def test_loop_subsequent_fire_from_last_fired(store):
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=30)
    last = datetime(2026, 7, 26, 12, 0, tzinfo=UTC)
    store.mark_fired(s.id, _iso(last))
    s = store.get_schedule(s.id)
    assert not sched_mod.is_due(s, last + timedelta(minutes=15))
    assert sched_mod.is_due(s, last + timedelta(minutes=30))


def test_loop_no_interval_never_due(store):
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=None)
    assert not sched_mod.is_due(s, datetime.now(UTC))


# --------------------------------------------------------------------------- #
# due-ness: once
# --------------------------------------------------------------------------- #
def test_once_due_at_or_after_and_not_fired(store):
    at = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "once", "go", at_iso=_iso(at))
    assert not sched_mod.is_due(s, at - timedelta(minutes=1))
    assert sched_mod.is_due(s, at)
    assert sched_mod.is_due(s, at + timedelta(hours=5))  # catch-up window


def test_once_not_due_after_fired(store):
    at = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "once", "go", at_iso=_iso(at))
    store.mark_fired(s.id, _iso(at))
    s = store.get_schedule(s.id)
    assert not sched_mod.is_due(s, at + timedelta(hours=1))


# --------------------------------------------------------------------------- #
# due-ness: daily
# --------------------------------------------------------------------------- #
def test_daily_due_after_hhmm_not_yet_fired_today(store):
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "daily", "go", cron_hhmm="09:00")
    day = datetime(2026, 7, 26, 8, 59, tzinfo=UTC)
    assert not sched_mod.is_due(s, day)  # before HH:MM
    assert sched_mod.is_due(s, day.replace(hour=9, minute=0))
    assert sched_mod.is_due(s, day.replace(hour=23, minute=0))  # later same day


def test_daily_not_due_twice_same_day(store):
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "daily", "go", cron_hhmm="09:00")
    fired = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    store.mark_fired(s.id, _iso(fired))
    s = store.get_schedule(s.id)
    # same day, later — already fired today
    assert not sched_mod.is_due(s, fired.replace(hour=15))
    # next day after HH:MM — due again
    assert sched_mod.is_due(s, fired + timedelta(days=1))


def test_daily_days_filter(store):
    p = store.create_pulse("t", "m", "claude-code")
    # 2026-07-26 is a Sunday; 07-27 is Monday.
    s = store.create_schedule(p.id, "daily", "go", cron_hhmm="09:00",
                              days="mon,wed,fri")
    sunday = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)
    monday = datetime(2026, 7, 27, 10, 0, tzinfo=UTC)
    assert sunday.weekday() == 6 and monday.weekday() == 0
    assert not sched_mod.is_due(s, sunday)  # Sunday not in filter
    assert sched_mod.is_due(s, monday)      # Monday is


def test_daily_bad_cron_never_due(store):
    p = store.create_pulse("t", "m", "claude-code")
    s = store.create_schedule(p.id, "daily", "go", cron_hhmm="99:99")
    assert not sched_mod.is_due(s, datetime.now(UTC))


# --------------------------------------------------------------------------- #
# firing: writes inbox + wakes stopped pulse
# --------------------------------------------------------------------------- #
def test_fire_writes_inbox_and_wakes_stopped(store, fake_spawn):
    p = store.create_pulse("t", "m", "claude-code", state="stopped")
    # a heartbeat left by the last runner carries the resume id
    _write_heartbeat(p.id, state="stopped", cli_session_id="resume-me", age_s=99)
    s = store.create_schedule(p.id, "loop", "run the loop prompt",
                              interval_minutes=30)
    now = sched_mod._parse_iso(s.created_at) + timedelta(minutes=31)

    summary = sched_mod.tick(store, now)
    assert summary["fired"] == 1

    # woke the stopped pulse via spawn, passing resume from the heartbeat
    assert len(fake_spawn.spawned) == 1
    assert fake_spawn.spawned[0]["resume"] == "resume-me"
    assert store.get_pulse(p.id).state == "running"

    # inbox file written with sched- prefix, atomic (no .tmp), correct text
    new_dir = paths.inbox_new(p.id)
    files = list(new_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name.startswith("sched-")
    assert list(new_dir.glob("*.tmp")) == []
    payload = json.loads(files[0].read_text())
    assert payload["text"] == "run the loop prompt"
    assert payload["source"] == "scheduler"

    # last_fired_at stamped
    assert store.get_schedule(s.id).last_fired_at == sched_mod._iso(now)


def test_fire_running_pulse_does_not_respawn(store, fake_spawn):
    p = store.create_pulse("t", "m", "claude-code", state="running")
    s = store.create_schedule(p.id, "loop", "hi", interval_minutes=1)
    now = sched_mod._parse_iso(s.created_at) + timedelta(minutes=2)
    sched_mod.tick(store, now)
    # already running -> no spawn, but message still delivered
    assert fake_spawn.spawned == []
    assert len(list(paths.inbox_new(p.id).glob("*.json"))) == 1


def test_once_auto_disables_after_fire(store, fake_spawn):
    at = datetime(2026, 7, 26, 9, 0, tzinfo=UTC)
    p = store.create_pulse("t", "m", "claude-code", state="running")
    s = store.create_schedule(p.id, "once", "one shot", at_iso=_iso(at))
    sched_mod.tick(store, at + timedelta(minutes=1))
    got = store.get_schedule(s.id)
    assert got.enabled is False
    assert got.last_fired_at is not None
    # a second tick fires nothing (disabled -> not even scanned)
    fake_spawn.spawned.clear()
    inbox_before = len(list(paths.inbox_new(p.id).glob("*.json")))
    sched_mod.tick(store, at + timedelta(minutes=2))
    assert len(list(paths.inbox_new(p.id).glob("*.json"))) == inbox_before


# --------------------------------------------------------------------------- #
# catch-up fires ONCE (daemon-was-down window)
# --------------------------------------------------------------------------- #
def test_loop_catch_up_fires_once(store, fake_spawn):
    """A loop whose window passed while the daemon was down fires ONCE, not N."""
    p = store.create_pulse("t", "m", "claude-code", state="running")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=10)
    # last fired long ago (many intervals have elapsed)
    long_ago = datetime(2026, 7, 26, 8, 0, tzinfo=UTC)
    store.mark_fired(s.id, _iso(long_ago))
    now = long_ago + timedelta(hours=3)  # 18 intervals missed

    sched_mod.tick(store, now)
    # exactly one message despite 18 missed windows
    assert len(list(paths.inbox_new(p.id).glob("*.json"))) == 1
    # last_fired advanced to now -> next tick (same now) not due again
    fired_at = store.get_schedule(s.id).last_fired_at
    assert fired_at == sched_mod._iso(now)
    sched_mod.tick(store, now)
    assert len(list(paths.inbox_new(p.id).glob("*.json"))) == 1


def test_daily_catch_up_fires_once(store, fake_spawn):
    p = store.create_pulse("t", "m", "claude-code", state="running")
    s = store.create_schedule(p.id, "daily", "morning", cron_hhmm="06:00")
    # never fired; the daemon comes up at 14:00, well past 06:00
    now = datetime(2026, 7, 26, 14, 0, tzinfo=UTC)
    sched_mod.tick(store, now)
    assert len(list(paths.inbox_new(p.id).glob("*.json"))) == 1
    # a later tick same day does NOT fire again
    sched_mod.tick(store, now + timedelta(hours=2))
    assert len(list(paths.inbox_new(p.id).glob("*.json"))) == 1


# --------------------------------------------------------------------------- #
# archived is skipped, never woken
# --------------------------------------------------------------------------- #
def test_archived_pulse_never_woken(store, fake_spawn, caplog):
    p = store.create_pulse("t", "m", "claude-code", state="archived")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=1)
    now = sched_mod._parse_iso(s.created_at) + timedelta(minutes=2)

    with caplog.at_level("INFO", logger="pulsar.daemon.scheduler"):
        summary = sched_mod.tick(store, now)
    assert summary["skipped"] == 1
    assert summary["fired"] == 0
    # never spawned, no inbox file
    assert fake_spawn.spawned == []
    assert list(paths.inbox_new(p.id).glob("*.json")) == []
    # still archived
    assert store.get_pulse(p.id).state == "archived"
    # logged the skip with a reason
    assert any("SKIPPED (archived)" in r.message for r in caplog.records)
    # stamped so it doesn't spin every tick
    assert store.get_schedule(s.id).last_fired_at is not None


# --------------------------------------------------------------------------- #
# failed spawn logs loudly + does NOT tight-loop
# --------------------------------------------------------------------------- #
def test_failed_spawn_logs_and_no_tight_loop(store, fake_spawn, caplog):
    fake_spawn.spawn_should_fail = True
    p = store.create_pulse("t", "m", "claude-code", state="stopped")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=10)
    now = sched_mod._parse_iso(s.created_at) + timedelta(minutes=11)

    with caplog.at_level("ERROR", logger="pulsar.daemon.scheduler"):
        summary = sched_mod.tick(store, now)
    assert summary["failed"] == 1
    assert any("FAILED" in r.message for r in caplog.records)

    # mark_fired WAS stamped on the failed attempt -> not due again immediately
    got = store.get_schedule(s.id)
    assert got.last_fired_at == sched_mod._iso(now)
    assert not sched_mod.is_due(got, now)  # no tight retry loop

    # a second tick at the same instant does nothing (proves no tight loop)
    fake_spawn.spawned.clear()
    summary2 = sched_mod.tick(store, now)
    assert summary2["due"] == 0
    assert fake_spawn.spawned == []


# --------------------------------------------------------------------------- #
# quiet tick / disabled not scanned
# --------------------------------------------------------------------------- #
def test_disabled_schedule_not_fired(store, fake_spawn):
    p = store.create_pulse("t", "m", "claude-code", state="running")
    s = store.create_schedule(p.id, "loop", "go", interval_minutes=1)
    store.set_schedule_enabled(s.id, False)
    now = sched_mod._parse_iso(s.created_at) + timedelta(minutes=5)
    summary = sched_mod.tick(store, now)
    assert summary["scanned"] == 0
    assert list(paths.inbox_new(p.id).glob("*.json")) == []


def test_quiet_tick_logs_debug_only(store, fake_spawn, caplog):
    p = store.create_pulse("t", "m", "claude-code", state="running")
    store.create_schedule(p.id, "loop", "go", interval_minutes=60)
    now = datetime(2026, 7, 26, 10, 0, tzinfo=UTC)  # nothing due yet
    with caplog.at_level("INFO", logger="pulsar.daemon.scheduler"):
        sched_mod.tick(store, now)
    # no INFO-level tick summary when nothing fired
    assert not any("scheduler.tick scanned" in r.message
                   for r in caplog.records if r.levelname == "INFO")


def test_fire_pulse_gone_marks_and_logs(store, fake_spawn, caplog):
    """Schedule outlives its pulse: stamp + loud log, no crash, no retry loop.

    The FK stops a normal delete of a scheduled pulse, so fabricate the orphan
    by inserting a schedule row pointing at a nonexistent pulse id directly —
    the exact race the _fire guard protects against.
    """
    sid = "orphan-sched"
    created = "2026-07-26T10:00:00+00:00"
    store._conn.execute("PRAGMA foreign_keys=OFF;")
    store._conn.execute(
        "INSERT INTO schedules (id, pulse_id, kind, prompt, interval_minutes, "
        "at_iso, cron_hhmm, days, enabled, last_fired_at, created_at) "
        "VALUES (?, 'ghost-pulse', 'loop', 'go', 1, NULL, NULL, NULL, 1, "
        "NULL, ?);",
        (sid, created))
    store._conn.execute("PRAGMA foreign_keys=ON;")
    now = sched_mod._parse_iso(created) + timedelta(minutes=2)
    with caplog.at_level("ERROR", logger="pulsar.daemon.scheduler"):
        summary = sched_mod.tick(store, now)
    assert summary["failed"] == 1
    assert any("pulse gone" in r.message for r in caplog.records)
    assert store.get_schedule(sid).last_fired_at == sched_mod._iso(now)
