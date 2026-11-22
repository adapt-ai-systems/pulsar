"""Auto-handoff engine tests — thresholds, reply capture, the full flow.

    venv/bin/python -m pytest tests -k handoff -q

No real runners or tokens: spawn/sockclient are monkeypatched (same fakes
pattern as test_daemon.py) and we write heartbeat.json / events.jsonl into a
tmp PULSAR_DATA ourselves. run_handoff's reply wait is driven by appending the
pulse's "reply" to events.jsonl mid-flight, exactly as a live runner would.
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest

import pulsar.launch.units as units_mod
from pulsar.daemon import lifecycle
from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import paths
from pulsar.store import Store

pytestmark = pytest.mark.anyio


# --------------------------------------------------------------------------- #
# Fixtures (PULSAR_DATA tmp dir; store; fake spawn/sock on the shared modules)
# --------------------------------------------------------------------------- #
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


class _SockErr(Exception):
    pass


@pytest.fixture()
def fakes(monkeypatch):
    """Fake spawn + socket stop on the modules handoff.py actually calls."""
    calls = {"spawned": [], "sock_stopped": [], "unit_stopped": []}

    def spawn_runner(pulse_id, home, model, vessel, resume=None,
                     memory_max="6G", tasks_max=512, diet=None, title=None,
                     effort=None, fast=False):
        calls["spawned"].append({"pulse_id": pulse_id, "model": model,
                                 "vessel": vessel, "resume": resume,
                                 "memory_max": memory_max, "diet": diet,
                                 "effort": effort, "fast": fast})

    def sock_stop(sock_path, pulse_id):
        calls["sock_stopped"].append(pulse_id)
        # A stopped runner's heartbeat says stopped; simulate it so
        # _await_stopped returns immediately.
        _write_heartbeat(pulse_id, state="stopped")
        return {"v": 1, "ok": True}

    monkeypatch.setattr(units_mod, "spawn_runner", spawn_runner)
    monkeypatch.setattr(units_mod, "stop_unit",
                        lambda pid: calls["unit_stopped"].append(pid) or True)
    monkeypatch.setattr(lifecycle.sockclient, "stop", sock_stop)
    monkeypatch.setattr(lifecycle.sockclient, "SocketError", _SockErr)
    # Fast polls so the timeout test doesn't take minutes.
    monkeypatch.setattr(handoff_mod.engine, "REPLY_POLL_S", 0.05)
    monkeypatch.setattr(handoff_mod.state, "STOP_SETTLE_S", 2.0)
    # Undelivered requests and re-asks must not stall a test for minutes.
    monkeypatch.setattr(handoff_mod.state, "DELIVERY_TIMEOUT_S", 0.5)
    monkeypatch.setattr(handoff_mod.state, "REASK_TIMEOUT_S", 0.3)
    return calls


def _write_heartbeat(pulse_id: str, state: str = "idle",
                     ctx: int | None = None,
                     cli_session_id: str = "sess-1") -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    beat = {"ts": ts, "pid": 1, "cli_pid": 2, "state": state,
            "cli_session_id": cli_session_id, "last_ctx_tokens": ctx,
            "protocol": 1}
    (home / "heartbeat.json").write_text(json.dumps(beat))


def _append_events(pulse_id: str, events: list[dict]) -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    with (home / "events.jsonl").open("a") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")


def _mk_pulse(store, model="claude-opus-5", vessel="claude-code",
              handoff_tokens=None, state="running"):
    return store.create_pulse("t", model, vessel, state=state,
                              handoff_tokens=handoff_tokens)


# --------------------------------------------------------------------------- #
# threshold_for — per-model defaults table
# --------------------------------------------------------------------------- #
def test_threshold_explicit_beats_defaults(store):
    p = _mk_pulse(store, model="claude-opus-5", handoff_tokens=42_000)
    assert handoff_mod.threshold_for(p) == 42_000


@pytest.mark.parametrize("model,expected", [
    ("claude-opus-5", 300_000),
    ("claude-fable-5", 200_000),
    ("gpt-5.5-codex", 225_000),
    ("codex-mini", 225_000),
    ("pi-qwen27-interface", 100_000),
    ("pi-glm-52", 170_000),
    ("codex-ds4-flash", 175_000),
    ("qwen3-coder", 190_000),
    ("mystery-model", 200_000),  # fallback
])
def test_threshold_family_defaults(store, model, expected):
    p = _mk_pulse(store, model=model)
    assert handoff_mod.threshold_for(p) == expected


def test_omp_interface_uses_compact_packet_handoff(store):
    p = _mk_pulse(store, model="pi-qwen27-interface", vessel="pi")
    request = handoff_mod.handoff_prompt_for(p)
    assert "TEAM-PACKET" in request
    assert "maximum 1,500 tokens" in request
    assert "credentials" in request
    assert handoff_mod.successor_prefix_for(p).startswith("[pulsar TEAM-PACKET]")


# --------------------------------------------------------------------------- #
# extract_reply — assistant text between our message and its done
# --------------------------------------------------------------------------- #
def test_extract_reply_complete_turn():
    evs = [
        {"type": "user_message", "text": "old", "msg_file": "a.json"},
        {"type": "assistant_text", "text": "old reply"},
        {"type": "done"},
        {"type": "user_message", "text": "handoff pls", "msg_file": "h.json"},
        {"type": "assistant_text", "text": "part one"},
        {"type": "assistant_text", "text": "part two"},
        {"type": "done"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") == "part one\npart two"


def test_extract_reply_incomplete_turn_returns_none():
    evs = [
        {"type": "user_message", "text": "handoff pls", "msg_file": "h.json"},
        {"type": "assistant_text", "text": "still going"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") is None


def test_extract_reply_empty_reply_returns_none():
    evs = [
        {"type": "user_message", "msg_file": "h.json"},
        {"type": "done"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") is None


# 2026-08-23: a pulse message killed the wrap-up turn at 15:12:47
# (done{interrupted}, no text). The model then wrote a complete handoff as the
# next turn's final text at 15:14:30 — and extract_reply, which stopped at that
# first done, threw it away and left the daemon polling for the whole
# REPLY_TIMEOUT_S before failing the handoff.

def test_extract_reply_survives_empty_interrupted_turn():
    evs = [
        {"type": "user_message", "msg_file": "h.json"},
        {"type": "done", "reason": "interrupted"},
        {"type": "user_message", "text": "a babysitter's review",
         "msg_file": "b.json"},
        {"type": "assistant_text", "text": "Handing off — context is full."},
        {"type": "done", "reason": "result"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") == \
        "Handing off — context is full."


def test_extract_reply_survives_qwen_style_interrupted_subtype():
    """The qwen kill-and-resume path says so in subtype, not reason."""
    evs = [
        {"type": "user_message", "msg_file": "h.json"},
        {"type": "done", "reason": "result", "subtype": "interrupted"},
        {"type": "assistant_text", "text": "the real handoff"},
        {"type": "done", "reason": "result"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") == "the real handoff"


def test_extract_reply_keeps_partial_text_from_interrupted_turn():
    """Text the model DID write for us beats discarding it and waiting."""
    evs = [
        {"type": "user_message", "msg_file": "h.json"},
        {"type": "assistant_text", "text": "half a handoff"},
        {"type": "done", "reason": "interrupted"},
        {"type": "assistant_text", "text": "something else entirely"},
        {"type": "done", "reason": "result"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") == "half a handoff"


def test_extract_reply_clean_empty_done_still_returns_none():
    """Only an INTERRUPTED empty turn keeps scanning; a clean one still ends
    it, so a model that genuinely replied with nothing is not papered over
    with unrelated text from a later turn."""
    evs = [
        {"type": "user_message", "msg_file": "h.json"},
        {"type": "done", "reason": "result"},
        {"type": "assistant_text", "text": "unrelated later work"},
        {"type": "done", "reason": "result"},
    ]
    assert handoff_mod.extract_reply(evs, "h.json") is None


# --------------------------------------------------------------------------- #
# due_pulses — heartbeat ctx vs threshold, cooldown
# --------------------------------------------------------------------------- #
def test_due_pulses_over_threshold(store):
    p = _mk_pulse(store, model="claude-fable-5")  # 200k default
    _write_heartbeat(p.id, ctx=250_000)
    due = handoff_mod.monitor.due_pulses(store)
    assert [(d[0].id, d[1], d[2]) for d in due] == [(p.id, 250_000, 200_000)]


def test_due_pulses_under_threshold_or_missing_beat(store):
    p1 = _mk_pulse(store)
    _write_heartbeat(p1.id, ctx=100)
    _mk_pulse(store)  # no heartbeat at all
    assert handoff_mod.monitor.due_pulses(store) == []


def test_due_pulses_cooldown_skips(store):
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    store.update_settings(p.id, {"last_handoff_at":
                                 datetime.now(timezone.utc).isoformat()})
    assert handoff_mod.monitor.due_pulses(store) == []


def test_due_pulses_stopped_pulse_ignored(store):
    p = _mk_pulse(store, state="stopped")
    _write_heartbeat(p.id, ctx=999_999)
    assert handoff_mod.monitor.due_pulses(store) == []


def test_due_pulses_stale_heartbeat_ignored(store):
    """A just-dead runner's last heartbeat still carries a huge ctx. Firing at
    it would wait ten minutes for a reply nobody can send — that pulse belongs
    to the dead-runner sweep."""
    p = _mk_pulse(store, model="claude-fable-5")
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    old = datetime.fromtimestamp(time.time() - 120, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    (home / "heartbeat.json").write_text(json.dumps(
        {"ts": old, "state": "idle", "last_ctx_tokens": 250_000}))
    assert handoff_mod.monitor.due_pulses(store) == []


def test_due_pulses_dead_runner_state_ignored(store):
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, state="cli_dead", ctx=250_000)
    assert handoff_mod.monitor.due_pulses(store) == []
    _write_heartbeat(p.id, state="stopped", ctx=250_000)
    assert handoff_mod.monitor.due_pulses(store) == []


# --------------------------------------------------------------------------- #
# run_handoff — the full flow
# --------------------------------------------------------------------------- #
async def _reply_when_asked(pulse_id: str, text: str) -> None:
    """Watch inbox/new for the handoff request, then 'answer' via events.jsonl."""
    new_dir = paths.inbox_new(pulse_id)
    for _ in range(200):
        files = sorted(new_dir.glob("handoff-2*.json")) if new_dir.exists() else []
        files = [f for f in files if not f.name.startswith("handoff-seed")]
        if files:
            _append_events(pulse_id, [
                {"type": "user_message", "msg_file": files[0].name},
                {"type": "assistant_text", "text": text},
                {"type": "done"},
            ])
            return
        await asyncio.sleep(0.02)
    raise AssertionError("handoff request message never appeared")


async def test_run_handoff_happy_path(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    # handoff text saved, numbered
    saved = paths.pulse_home(p.id) / "handoffs" / "1.md"
    assert saved.read_text() == "HANDOFF TEXT"
    # old runner stopped, fresh (resume=None) runner spawned
    assert fakes["sock_stopped"] == [p.id]
    assert fakes["spawned"][0]["resume"] is None
    assert fakes["spawned"][0]["model"] == "claude-opus-5"
    # successor seeded with the handoff text
    seeds = list(paths.inbox_new(p.id).glob("handoff-seed-*.json"))
    assert len(seeds) == 1
    assert "HANDOFF TEXT" in json.loads(seeds[0].read_text())["text"]
    # bookkeeping: cooldown stamp + count + done marker
    fresh = store.get_pulse(p.id)
    assert fresh.settings.get("handoff_count") == 1
    assert fresh.settings.get("last_handoff_at")
    assert handoff_mod.read_state(p.id)["phase"] == "done"


async def test_run_handoff_model_switch(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=10)
    replier = asyncio.create_task(_reply_when_asked(p.id, "SWITCH HANDOFF"))
    ok = await handoff_mod.engine.run_handoff(store, p, new_model="gpt-5.5-codex",
                                       new_vessel="codex",
                                       reason="model_switch")
    await replier
    assert ok is True
    assert fakes["spawned"][0]["model"] == "gpt-5.5-codex"
    assert fakes["spawned"][0]["vessel"] == "codex"
    fresh = store.get_pulse(p.id)
    assert (fresh.model, fresh.vessel) == ("gpt-5.5-codex", "codex")


async def test_run_handoff_keeps_glm52_effort(store, fakes):
    p = _mk_pulse(store, model="pi-glm-52", vessel="pi")
    store.update_settings(p.id, {"effort": "high"})
    p = store.get_pulse(p.id)
    _write_heartbeat(p.id, ctx=310_000)
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    assert fakes["spawned"][0]["effort"] == "high"


async def test_run_handoff_keeps_valid_effort(store, fakes):
    p = _mk_pulse(store)
    store.update_settings(p.id, {"effort": "max"})
    p = store.get_pulse(p.id)
    _write_heartbeat(p.id, ctx=310_000)
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    assert fakes["spawned"][0]["effort"] == "max"


async def test_run_handoff_cross_vessel_drops_invalid_effort(store, fakes):
    """codex "none" means nothing to claude — the successor spawns without it
    and the stale setting is cleared, not smuggled into a broken spawn."""
    p = _mk_pulse(store, model="gpt-6-sol", vessel="codex")
    store.update_settings(p.id, {"effort": "none"})
    p = store.get_pulse(p.id)
    _write_heartbeat(p.id, ctx=10)
    replier = asyncio.create_task(_reply_when_asked(p.id, "SWITCH HANDOFF"))
    ok = await handoff_mod.engine.run_handoff(store, p, new_model="claude-opus-5",
                                       new_vessel="claude-code",
                                       reason="model_switch")
    await replier
    assert ok is True
    assert fakes["spawned"][0]["effort"] is None
    assert store.get_pulse(p.id).settings.get("effort") is None


async def test_run_handoff_reply_timeout_aborts(store, fakes, monkeypatch):
    monkeypatch.setattr(handoff_mod.state, "REPLY_TIMEOUT_S", 0.2)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    ok = await handoff_mod.engine.run_handoff(store, p)
    assert ok is False
    # old session untouched: nothing stopped, nothing spawned
    assert fakes["sock_stopped"] == []
    assert fakes["spawned"] == []
    assert handoff_mod.read_state(p.id)["phase"] == "failed"
    # cooldown stamped on the ATTEMPT — no tight retry loop
    assert handoff_mod.monitor.due_pulses(store) == []


async def test_run_handoff_reply_timeout_uses_fallback(store, fakes,
                                                       monkeypatch):
    """A loop-rescue caller supplies fallback_reply: a reply timeout then
    completes the handoff with the mechanical seed instead of aborting."""
    monkeypatch.setattr(handoff_mod.state, "REPLY_TIMEOUT_S", 0.2)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=118_000)
    ok = await handoff_mod.engine.run_handoff(
        store, p, reason="loop_rescue",
        fallback_reply="MECHANICAL RESCUE SEED")
    assert ok is True
    assert fakes["sock_stopped"] == [p.id]
    assert len(fakes["spawned"]) == 1
    seed = next(paths.inbox_new(p.id).glob("handoff-seed-*.json"))
    assert "MECHANICAL RESCUE SEED" in json.loads(seed.read_text())["text"]
    assert handoff_mod.read_state(p.id)["phase"] == "done"


async def test_run_handoff_marks_pulse_running(store, fakes):
    """The dead-runner sweep can mark the pulse stopped while handoff waits.
    A successful successor spawn must put the store back to running, or the
    live runner is invisible to schedules, auto-handoff and the sweep."""
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    store.set_state(p.id, "stopped")   # what the sweep would have done
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    assert store.get_pulse(p.id).state == "running"


async def test_run_handoff_single_flight(store, fakes):
    """The monitor and a manual model switch both call run_handoff. The second
    one is refused while the durable phase is in flight — two would double-stop
    the runner and spawn competing successors."""
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    handoff_mod.state.write_state(p.id, "requested", reason="context_threshold")

    ok = await handoff_mod.engine.run_handoff(store, p, new_model="gpt-5.5-codex",
                                       new_vessel="codex",
                                       reason="model_switch")
    assert ok is False
    assert fakes["spawned"] == []
    assert fakes["sock_stopped"] == []
    # The in-flight marker is untouched — the real handoff still owns it.
    assert handoff_mod.read_state(p.id)["phase"] == "requested"


async def test_forced_switch_overtakes_unanswered_request(store, fakes,
                                                         monkeypatch):
    """A switch off a dead model previously left a handoff "requested" for the
    whole reply wait and the forced retry was refused. A forced handoff now
    overtakes it, and the old waiter bows out without touching the state."""
    monkeypatch.setattr(handoff_mod.engine, "REPLY_POLL_S", 0.01)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=10_000)
    stuck = asyncio.create_task(handoff_mod.engine.run_handoff(
        store, p, new_model="gpt-5.5-codex", new_vessel="codex",
        reason="model_switch"))
    await asyncio.sleep(0.05)
    assert handoff_mod.read_state(p.id)["phase"] == "requested"

    ok = await handoff_mod.engine.run_handoff(store, p, new_model="gpt-5.5-codex",
                                       new_vessel="codex",
                                       reason="model_switch", force=True)
    assert ok is True
    assert await asyncio.wait_for(stuck, 2) is False
    assert len(fakes["spawned"]) == 1
    assert handoff_mod.read_state(p.id)["phase"] == "done"
    # The overtaken attempt's ledger line says so (not the default "raised").
    assert "superseded" in [r["outcome"] for r in _ledger()]


async def test_switch_reply_timeout_seeds_from_transcript(store, fakes):
    """A model switch waits a bounded time for notes, then moves anyway."""
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=10_000)
    ok = await handoff_mod.engine.run_handoff(
        store, p, new_model="gpt-5.5-codex", new_vessel="codex",
        reason="model_switch", reply_timeout_s=0.1,
        fallback_to_transcript=True)
    assert ok is True
    assert len(fakes["spawned"]) == 1
    assert handoff_mod.read_state(p.id)["phase"] == "done"


def test_switch_prompt_does_not_claim_full_context(store):
    p = _mk_pulse(store)
    text = handoff_mod.handoff_prompt_for(p, "model_switch")
    assert text.startswith("[pulsar] This pulse is switching")
    assert "nearly full" not in text
    assert "nearly full" in handoff_mod.handoff_prompt_for(p)


def test_manual_handoff_says_requested_and_seed_is_not_a_request(store):
    """A manual handoff must not claim a full context, and the successor must
    not re-answer the user's last (already answered) message."""
    p = _mk_pulse(store)
    text = handoff_mod.handoff_prompt_for(p, "manual")
    assert text.startswith("[pulsar] A fresh session was requested")
    assert "nearly full" not in text
    seed = handoff_mod.successor_prefix_for(p)
    assert "act on it" not in seed and "not a new request" in seed
    assert "start nothing until the user writes" not in seed
    idle = handoff_mod.successor_prefix_for(p, was_idle=True)
    assert "start nothing until the user writes" in idle


async def test_run_handoff_ignores_stale_in_flight_marker(store, fakes,
                                                          monkeypatch):
    """A marker left by a daemon that died mid-handoff must not wedge the
    pulse out of handoffs forever."""
    monkeypatch.setattr(handoff_mod.state, "IN_FLIGHT_STALE_S", 0.0)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    handoff_mod.state.write_state(p.id, "stopping", reason="context_threshold")

    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    assert handoff_mod.read_state(p.id)["phase"] == "done"


async def test_tick_fires_due_handoff(store, fakes):
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    replier = asyncio.create_task(_reply_when_asked(p.id, "TICK HANDOFF"))
    fired = await handoff_mod.monitor.tick(store)
    await replier
    assert fired == 1
    assert await handoff_mod.monitor.drain() == [True]
    assert (paths.pulse_home(p.id) / "handoffs" / "1.md").exists()


# Live 2026-10-13: due_pulses() was built once per tick and run_handoff ran
# serially with a 600s wait each — archived pulses still got requests, one
# silent pulse blocked every other handoff, and a handoff racing an archive
# respawned the archived pulse.
async def test_tick_skips_pulse_archived_after_due_list_built(
        store, fakes, monkeypatch):
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    due = handoff_mod.monitor.due_pulses(store)
    assert [d[0].id for d in due] == [p.id]
    store.archive_pulse(p.id)
    monkeypatch.setattr(handoff_mod.monitor, "due_pulses", lambda s: due)
    assert await handoff_mod.monitor.tick(store) == 0
    await handoff_mod.monitor.drain()
    assert not list(paths.inbox_new(p.id).glob("handoff-2*.json")) \
        if paths.inbox_new(p.id).exists() else True
    assert fakes["spawned"] == []


async def test_tick_one_silent_pulse_does_not_block_others(
        store, fakes, monkeypatch):
    monkeypatch.setattr(handoff_mod.state, "DELIVERY_TIMEOUT_S", 30.0)
    monkeypatch.setattr(handoff_mod.engine, "has_sections", lambda *a: True)
    silent = _mk_pulse(store, model="claude-fable-5")
    talker = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(silent.id, ctx=250_000)
    _write_heartbeat(talker.id, ctx=250_000)
    replier = asyncio.create_task(_reply_when_asked(talker.id, "TALKER"))
    try:
        fired = await asyncio.wait_for(handoff_mod.monitor.tick(store), timeout=2.0)
        assert fired == 2
        await replier
        for _ in range(100):
            if (paths.pulse_home(talker.id) / "handoffs" / "1.md").exists():
                break
            await asyncio.sleep(0.05)
        assert (paths.pulse_home(talker.id) / "handoffs" / "1.md").exists()
        assert silent.id in handoff_mod.monitor._ACTIVE
    finally:
        # Archive the silent one: its wait must end promptly, not at 30s.
        store.archive_pulse(silent.id)
        results = await asyncio.wait_for(handoff_mod.monitor.drain(), timeout=5.0)
    assert sorted(results) == [False]
    assert [c["pulse_id"] for c in fakes["spawned"]] == [talker.id]


async def test_handoff_racing_archive_never_respawns(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)

    async def archive_then_reply():
        new_dir = paths.inbox_new(p.id)
        for _ in range(200):
            if new_dir.exists() and [f for f in new_dir.glob("handoff-2*.json")]:
                break
            await asyncio.sleep(0.02)
        store.archive_pulse(p.id)
        await _reply_when_asked(p.id, "LATE HANDOFF TEXT")

    replier = asyncio.create_task(archive_then_reply())
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is False
    assert fakes["spawned"] == []
    assert fakes["sock_stopped"] == []
    assert store.get_pulse(p.id).state == "archived"


async def test_handoff_archived_during_spawn_stops_successor(
        store, fakes, monkeypatch):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    original = units_mod.spawn_runner

    def spawn_then_archive(*args, **kwargs):
        original(*args, **kwargs)
        store.archive_pulse(p.id)

    monkeypatch.setattr(units_mod, "spawn_runner", spawn_then_archive)
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is False
    assert len(fakes["sock_stopped"]) == 2  # old runner, then the successor
    assert store.get_pulse(p.id).state == "archived"


async def test_forced_switch_seeds_before_spawn_and_retires_stale_requests(
        store, data_root, fakes, monkeypatch):
    p = _mk_pulse(store, model="pi-qwen38-flash", vessel="pi", state="stopped")
    home = paths.pulse_home(p.id)
    _write_heartbeat(p.id, state="cli_dead", cli_session_id="foreign-pi-id")
    for folder in ("new", "feeding"):
        d = home / "inbox" / folder
        d.mkdir(parents=True, exist_ok=True)
        (d / f"handoff-{folder}.json").write_text('{"text":"old request"}')
    user = home / "inbox" / "new" / "user.json"
    user.write_text('{"text":"new task"}')
    _append_events(p.id, [{"type": "assistant_text", "text": "Saved both draft notes."}])
    original = units_mod.spawn_runner

    def check_spawn(*args, **kwargs):
        assert kwargs["resume"] is None
        assert not paths.heartbeat_path(p.id).exists()
        seeds = list((home / "inbox" / "new").glob("handoff-*.json"))
        assert len(seeds) == 1 and seeds[0].name.startswith("handoff-seed-")
        assert "Saved both draft notes." in seeds[0].read_text()
        assert not list((home / "inbox" / "feeding").glob("handoff-*.json"))
        assert user.exists()
        return original(*args, **kwargs)

    async def must_not_ask(*args, **kwargs):
        pytest.fail("forced switch must not wait for the dead model")

    monkeypatch.setattr(units_mod, "spawn_runner", check_spawn)
    monkeypatch.setattr(handoff_mod.engine, "_await_reply", must_not_ask)
    assert await handoff_mod.engine.run_handoff(store, p, new_model="gpt-6-luna",
                                        new_vessel="codex", force=True)
    assert fakes["spawned"][-1]["model"] == "gpt-6-luna"
    assert store.get_pulse(p.id).vessel == "codex"
    assert len(list((home / "inbox" / "acked").glob("handoff-*.json"))) == 2


async def test_run_handoff_respawn_merges_default_skills(store, fakes):
    # Found in review 2026-09-17: handoff.py builds its own spawn call, so the
    # default-skills merge (store.diet_with_defaults) must be applied here too.
    store.put_setting("pulse.default_skills", ["keep-going"])
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    assert fakes["spawned"][0]["diet"]["skills"] == ["keep-going"]


# --------------------------------------------------------------------------- #
# Notes file — persists across handoffs, pasted into every seed
# --------------------------------------------------------------------------- #
def test_handoff_request_asks_to_curate_notes(store):
    p = _mk_pulse(store)
    request = handoff_mod.handoff_prompt_for(p)
    assert str(handoff_mod.notes_path(p.id)) in request


async def test_run_handoff_seed_carries_notes_file(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    handoff_mod.notes_path(p.id).write_text("CORE: never touch prod DB\n")
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    assert await handoff_mod.engine.run_handoff(store, p) is True
    await replier
    seed = json.loads(next(paths.inbox_new(p.id).glob("handoff-seed-*.json")).read_text())
    assert "HANDOFF TEXT" in seed["text"]
    assert "CORE: never touch prod DB" in seed["text"]


async def test_run_handoff_seed_without_notes_file(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    replier = asyncio.create_task(_reply_when_asked(p.id, "HANDOFF TEXT"))
    assert await handoff_mod.engine.run_handoff(store, p) is True
    await replier
    seed = json.loads(next(paths.inbox_new(p.id).glob("handoff-seed-*.json")).read_text())
    assert "notes file" not in seed["text"]


# --------------------------------------------------------------------------- #
# Capture fixes (handoff review 2026-10-02)
# --------------------------------------------------------------------------- #
GOOD_NOTE = ("1. Working on X.\n2. State: Y done.\n3. Next: Z.\n"
             "4. Paths: /tmp/a.")


async def _reply_to_nth_request(pulse_id: str, n: int, text: str,
                                filler: int = 0) -> None:
    """Answer the n-th handoff request (1-based) that appears in inbox/new."""
    new_dir = paths.inbox_new(pulse_id)
    seen: list[str] = []
    for _ in range(400):
        if new_dir.exists():
            for f in sorted(new_dir.glob("handoff-2*.json")):
                if f.name not in seen:
                    seen.append(f.name)
        if len(seen) >= n:
            evs = [{"type": "user_message", "msg_file": seen[n - 1]}]
            evs += [{"type": "tool_result", "snippet": "x" * 1000}] * filler
            evs += [{"type": "assistant_text", "text": text}, {"type": "done"}]
            _append_events(pulse_id, evs)
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"request #{n} never appeared")


def test_has_sections():
    assert handoff_mod.prompts.has_sections(GOOD_NOTE)
    assert handoff_mod.prompts.has_sections("**1.** a\n## 2) b\n- 3: c\n4. d")
    assert not handoff_mod.prompts.has_sections("That's dispatched. Meanwhile, "
                                        "decision 2 is still waiting on you.")
    assert handoff_mod.prompts.has_sections("# TEAM-PACKET\n## Goal\nx", team=True)


def test_extract_reply_takes_final_sectioned_block():
    evs = [{"type": "user_message", "msg_file": "h.json"},
           {"type": "assistant_text", "text": "Let me wrap up the test run."},
           {"type": "assistant_text", "text": GOOD_NOTE},
           {"type": "done"}]
    assert handoff_mod.extract_reply(evs, "h.json") == GOOD_NOTE


async def test_reply_found_beyond_64k_tail(store, fakes):
    """The reply turn wrote >64 KiB after the request's user_message; the old
    tail lost the user_message and never found the reply."""
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    _append_events(p.id, [{"type": "assistant_text", "text": "old" * 100}])
    replier = asyncio.create_task(
        _reply_to_nth_request(p.id, 1, GOOD_NOTE, filler=100))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert ok is True
    assert (paths.pulse_home(p.id) / "handoffs" / "1.md").read_text() == GOOD_NOTE


async def test_reply_clock_starts_at_delivery(store, fakes, monkeypatch):
    """REPLY_TIMEOUT_S counts from the request's user_message, not from when
    the file was written: a late delivery still gets its full reply window."""
    monkeypatch.setattr(handoff_mod.state, "REPLY_TIMEOUT_S", 0.3)
    monkeypatch.setattr(handoff_mod.state, "DELIVERY_TIMEOUT_S", 3.0)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)

    async def late():
        new_dir = paths.inbox_new(p.id)
        while not list(new_dir.glob("handoff-2*.json")):
            await asyncio.sleep(0.01)
        name = sorted(new_dir.glob("handoff-2*.json"))[0].name
        await asyncio.sleep(0.5)            # delivered after REPLY_TIMEOUT_S
        _append_events(p.id, [{"type": "user_message", "msg_file": name}])
        await asyncio.sleep(0.15)           # reply within window from delivery
        _append_events(p.id, [{"type": "assistant_text", "text": GOOD_NOTE},
                              {"type": "done"}])

    t = asyncio.create_task(late())
    ok = await handoff_mod.engine.run_handoff(store, p)
    await t
    assert ok is True


async def test_final_rescan_catches_reply_at_deadline(data_root, monkeypatch):
    """A reply that lands during the last sleep is found by the rescan."""
    monkeypatch.setattr(handoff_mod.state, "DELIVERY_TIMEOUT_S", 0.1)
    monkeypatch.setattr(handoff_mod.engine, "REPLY_POLL_S", 0.4)
    pid = "p" * 32

    async def land():
        await asyncio.sleep(0.2)        # after the deadline, inside the sleep
        _append_events(pid, [{"type": "user_message", "msg_file": "h.json"},
                             {"type": "assistant_text", "text": GOOD_NOTE},
                             {"type": "done"}])
    paths.pulse_home(pid).mkdir(parents=True, exist_ok=True)
    t = asyncio.create_task(land())
    reply = await handoff_mod.engine._await_reply(pid, "h.json", offset=0)
    await t
    assert reply == GOOD_NOTE


async def test_failed_attempt_restamps_cooldown(store, fakes, monkeypatch):
    """Cooldown counts from the END of a failed attempt, so a reply timeout
    does not re-fire on the next tick."""
    stamps = iter(f"2026-10-02T00:00:{i:02d}+00:00" for i in range(60))
    monkeypatch.setattr(handoff_mod.engine, "_now_iso", lambda: next(stamps))
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    assert await handoff_mod.engine.run_handoff(store, p) is False
    first = "2026-10-02T00:00:00+00:00"
    assert store.get_pulse(p.id).settings["last_handoff_at"] > first


async def test_sectionless_note_is_reasked_once(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    r1 = asyncio.create_task(_reply_to_nth_request(
        p.id, 1, "That's dispatched. Meanwhile, decision 2 waits."))
    r2 = asyncio.create_task(_reply_to_nth_request(p.id, 2, GOOD_NOTE))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await r1
    await r2
    assert ok is True
    assert (paths.pulse_home(p.id) / "handoffs" / "1.md").read_text() == GOOD_NOTE


async def test_failed_reask_keeps_first_reply(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    r1 = asyncio.create_task(_reply_to_nth_request(p.id, 1, "short note"))
    ok = await handoff_mod.engine.run_handoff(store, p)
    await r1
    assert ok is True
    assert (paths.pulse_home(p.id) / "handoffs" / "1.md").read_text() == "short note"


# --------------------------------------------------------------------------- #
# Handoff ledger — one JSON line per attempt
# --------------------------------------------------------------------------- #
def _ledger() -> list[dict]:
    f = handoff_mod.ledger_path()
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


async def test_ledger_records_done(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    r = asyncio.create_task(_reply_to_nth_request(p.id, 1, GOOD_NOTE))
    assert await handoff_mod.engine.run_handoff(store, p)
    await r
    [rec] = _ledger()
    assert rec["pulse_id"] == p.id and rec["outcome"] == "done"
    assert rec["n"] == 1 and rec["ctx_at_request"] == 310_000
    assert rec["threshold"] == 300_000 and rec["reason"] == "context_threshold"
    assert rec["note_chars"] == len(GOOD_NOTE) and rec["note_has_sections"]
    assert rec["requested_at"] and rec["delivered_at"] and rec["replied_at"]


async def test_ledger_records_failures(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    assert await handoff_mod.engine.run_handoff(store, p) is False     # no reply
    handoff_mod.state.write_state(p.id, "requested", reason="x")
    assert await handoff_mod.engine.run_handoff(store, p) is False     # in flight
    assert [r["outcome"] for r in _ledger()] == ["reply_timeout", "refused"]


# --------------------------------------------------------------------------- #
# Carry-over block — mechanical facts from the event log in every seed
# --------------------------------------------------------------------------- #
def _session_log(pid: str) -> None:
    _append_events(pid, [
        {"type": "user_message", "msg_file": "20260101T000000Z-a.json",
         "text": "OLD SESSION MESSAGE", "ts": "2026-01-01T00:00:00Z"},
        {"type": "tool_call", "name": "Write", "args": {"file_path": "/old.py"}},
        {"type": "user_message", "msg_file": "handoff-seed-20260102T000000Z-b.json",
         "text": "[pulsar handoff] ..."},
        {"type": "user_message", "msg_file": "20260102T000001Z-c.json",
         "text": "please fix the parser", "ts": "2026-01-02T00:00:01Z"},
        {"type": "user_message", "msg_file": "20260102T000002Z-d.json",
         "text": "[message from 'Lead' (pulse x)] status?"},
        {"type": "user_message", "msg_file": "sched-20260102T000003Z-e.json",
         "text": "scheduled wake"},
        {"type": "tool_call", "name": "TaskCreate", "args": {"subject": "Fix parser"}},
        {"type": "tool_call", "name": "TaskCreate", "args": {"subject": "Write tests"}},
        {"type": "tool_call", "name": "TaskUpdate",
         "args": {"taskId": "2", "status": "completed"}},
        {"type": "tool_call", "name": "Edit", "args": {"file_path": "/src/parser.py"}},
        {"type": "tool_call", "name": "file_change",
         "args": {"changes": [{"path": "/src/b.c"}]}},
        {"type": "tool_call", "name": "apply_patch",
         "args": {"input": "*** Begin Patch\n*** Add File: /src/new.rs\n+x"}},
    ])


def test_carry_over_block_contents(data_root):
    pid = "c" * 32
    _session_log(pid)
    block = handoff_mod.carry_over_block(pid)
    assert "from the log, verify" in block
    assert str(paths.events_path(pid)) in block
    assert "handoffs" in block
    assert "please fix the parser" in block
    assert "OLD SESSION MESSAGE" not in block       # predecessor's session
    assert "status?" not in block and "scheduled wake" not in block
    assert "[pending] Fix parser" in block and "Write tests" not in block
    for f in ("/src/parser.py", "/src/b.c", "/src/new.rs"):
        assert f in block
    assert "/old.py" not in block


def test_carry_over_block_capped(data_root):
    pid = "d" * 32
    _append_events(pid, [{"type": "user_message",
                          "msg_file": f"2026010{i}T0Z-x.json",
                          "text": "y" * 5000} for i in range(5)])
    block = handoff_mod.carry_over_block(pid, budget_tokens=500)
    assert len(block) <= 500 * 4 + 2


def test_carry_over_todo_write_replaces(data_root):
    pid = "e" * 32
    _append_events(pid, [
        {"type": "tool_call", "name": "todo_write",
         "args": {"todos": [{"id": "a", "content": "old", "status": "pending"}]}},
        {"type": "tool_call", "name": "todo_write",
         "args": {"todos": [{"id": "b", "content": "new", "status": "in_progress"},
                            {"id": "c", "content": "gone", "status": "completed"}]}},
    ])
    block = handoff_mod.carry_over_block(pid)
    assert "[in_progress] new" in block
    assert "old" not in block.split("Open tasks:")[1] and "gone" not in block


async def test_seed_carries_block(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    _session_log(p.id)
    r = asyncio.create_task(_reply_to_nth_request(p.id, 1, GOOD_NOTE))
    assert await handoff_mod.engine.run_handoff(store, p)
    await r
    seed = next(paths.inbox_new(p.id).glob("handoff-seed-*.json"))
    text = json.loads(seed.read_text())["text"]
    assert GOOD_NOTE in text and "please fix the parser" in text
    assert text.index(GOOD_NOTE) < text.index("Carry-over")


# --------------------------------------------------------------------------- #
# Early warning at 80% + pulse-supplied note
# --------------------------------------------------------------------------- #
def _warnings(pid: str) -> list[Path]:
    d = paths.inbox_new(pid)
    return sorted(d.glob("handoff-warn-*.json")) if d.exists() else []


def test_warning_sent_once_per_session(store):
    p = _mk_pulse(store, model="claude-fable-5")       # 200k threshold
    _write_heartbeat(p.id, ctx=150_000)                 # 75%: nothing
    assert handoff_mod.warn_pulses(store) == 0
    _write_heartbeat(p.id, ctx=165_000)                 # 82.5%: warn
    assert handoff_mod.warn_pulses(store) == 1
    text = json.loads(_warnings(p.id)[0].read_text())["text"]
    assert "165,000 of 200,000" in text and "NOTES.md" in text
    assert '"note_path"' in text and f"/pulses/{p.id}/handoff" in text
    _write_heartbeat(p.id, ctx=170_000)                 # same session: quiet
    assert handoff_mod.warn_pulses(store) == 0
    _write_heartbeat(p.id, ctx=170_000, cli_session_id="sess-2")  # new session
    assert handoff_mod.warn_pulses(store) == 1
    assert any(r.get("event") == "warning" for r in _ledger())


def test_no_warning_at_or_over_threshold(store):
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=200_000)
    assert handoff_mod.warn_pulses(store) == 0


def test_warning_is_steerable_not_a_request():
    from pulsar.runner.runner import Runner
    assert not Runner._is_handoff(Path("handoff-warn-20261002T000000Z-ab.json"))


def test_read_note_file(tmp_path):
    good = tmp_path / "next.md"
    good.write_text(GOOD_NOTE * 10)
    assert handoff_mod.read_note_file(str(good)) == (GOOD_NOTE * 10).strip()
    short = tmp_path / "short.md"
    short.write_text("done")
    for bad in (str(short), "relative.md", str(tmp_path / "missing.md")):
        with pytest.raises(ValueError):
            handoff_mod.read_note_file(bad)


async def test_run_handoff_with_supplied_note(store, fakes, monkeypatch):
    async def must_not_ask(*a, **k):
        pytest.fail("a supplied note must not trigger the request")
    monkeypatch.setattr(handoff_mod.engine, "_ask_for_note", must_not_ask)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=250_000)
    assert await handoff_mod.engine.run_handoff(store, p, reason="note_file",
                                         note_text=GOOD_NOTE)
    assert fakes["sock_stopped"] == [p.id] and len(fakes["spawned"]) == 1
    seed = next(paths.inbox_new(p.id).glob("handoff-seed-*.json"))
    assert GOOD_NOTE in json.loads(seed.read_text())["text"]
    [rec] = _ledger()
    assert rec["reason"] == "note_file" and rec["note_supplied"] is True


# --------------------------------------------------------------------------- #
# One routine seed style Pulsar-wide: the note only, on
# every handoff number — no verbatim transcript tail. Recorded in the ledger.
# --------------------------------------------------------------------------- #
def test_tail_caps_tool_results(data_root):
    pid = "r" * 32
    _append_events(pid, [{"type": "tool_result", "ok": True, "snippet": "y" * 5000},
                         {"type": "tool_result", "ok": False, "snippet": "short"}])
    block = handoff_mod.forced_seed(pid)
    assert "y" * handoff_mod.seed.RESULT_CHARS + "…" in block
    assert "y" * (handoff_mod.seed.RESULT_CHARS + 1) not in block
    assert "RESULT (error): short" in block


async def _handoff_once(store, p) -> str:
    r = asyncio.create_task(_reply_to_nth_request(p.id, 1, GOOD_NOTE))
    assert await handoff_mod.engine.run_handoff(store, p)
    await r
    seed = next(paths.inbox_new(p.id).glob("handoff-seed-*.json"))
    text = json.loads(seed.read_text())["text"]
    seed.unlink()
    return text


async def test_every_routine_handoff_is_note_only(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000)
    _append_events(p.id, [{"type": "assistant_text", "text": "RECENT DETAIL"}])
    first = await _handoff_once(store, p)              # n=1
    handoff_mod.state.write_state(p.id, "done")
    p = store.get_pulse(p.id)
    _write_heartbeat(p.id, ctx=310_000)
    second = await _handoff_once(store, p)             # n=2: no tail either
    for seed in (first, second):
        assert GOOD_NOTE in seed
        assert "RECENT DETAIL" not in seed and "word for word" not in seed
    recs = [r for r in _ledger() if r.get("outcome") == "done"]
    assert [r["seed_variant"] for r in recs] == ["note", "note"]


# --------------------------------------------------------------------------- #
# Mid-turn handoff: messages the old session already received are retired,
# not re-fed to the successor (live 2026-10-11: briefs re-fed ~1s after the
# seed). Everything else stays where the successor's boot finds it.
# --------------------------------------------------------------------------- #
def _inbox_file(pid: str, folder: str, name: str, text: str = "x") -> Path:
    d = paths.pulse_home(pid) / "inbox" / folder
    d.mkdir(parents=True, exist_ok=True)
    f = d / name
    f.write_text(json.dumps({"text": text}))
    return f


def _where(pid: str, name: str) -> str | None:
    inbox = paths.pulse_home(pid) / "inbox"
    hits = [d for d in ("new", "feeding", "acked") if (inbox / d / name).exists()]
    assert len(hits) <= 1, hits
    return hits[0] if hits else None


async def test_supplied_note_retires_received_keeps_the_rest(
        store, fakes, monkeypatch):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, state="busy")
    _inbox_file(p.id, "feeding", "brief.json", "the brief")
    _inbox_file(p.id, "feeding", "steer.json")
    _append_events(p.id, [
        {"type": "user_message", "msg_file": "brief.json"},
        {"type": "assistant_text", "text": "working"},
        {"type": "user_message", "msg_file": "steer.json", "injected": True},
    ])
    _inbox_file(p.id, "feeding", "claimed.json")   # claimed, never fed
    _inbox_file(p.id, "new", "queued.json")        # never reached the runner
    real_stop = lifecycle.sockclient.stop

    def stop_after_late_inject(sock_path, pid):
        # The turn goes on after the note was posted: a steer lands before
        # the stop arrives. The note cannot know it, so it must survive.
        _inbox_file(pid, "feeding", "late.json")
        _append_events(pid, [{"type": "user_message", "msg_file": "late.json",
                              "injected": True}])
        return real_stop(sock_path, pid)

    monkeypatch.setattr(lifecycle.sockclient, "stop", stop_after_late_inject)
    assert await handoff_mod.engine.run_handoff(store, p, note_text="NOTE " * 60)
    assert _where(p.id, "brief.json") == "acked"
    assert _where(p.id, "steer.json") == "acked"
    assert _where(p.id, "claimed.json") == "feeding"
    assert _where(p.id, "late.json") == "feeding"
    assert _where(p.id, "queued.json") == "new"


async def test_requested_note_cutoff_is_end_of_reply(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000, state="busy")
    _inbox_file(p.id, "feeding", "brief.json", "the brief")
    _append_events(p.id, [{"type": "user_message", "msg_file": "brief.json"}])
    new_dir = paths.inbox_new(p.id)

    async def reply_in_same_turn():
        for _ in range(200):
            reqs = [f for f in new_dir.glob("handoff-2*.json")]
            if reqs:
                break
            await asyncio.sleep(0.02)
        # Request injected into the running brief turn; a steer lands before
        # the note; the note closes the turn; the runner starts the next
        # message before the daemon notices the reply.
        _inbox_file(p.id, "feeding", "before.json")
        _inbox_file(p.id, "feeding", "next.json")
        _append_events(p.id, [
            {"type": "user_message", "msg_file": reqs[0].name, "injected": True},
            {"type": "user_message", "msg_file": "before.json", "injected": True},
            {"type": "assistant_text", "text": "HANDOFF TEXT"},
            {"type": "done"},
            {"type": "user_message", "msg_file": "next.json"},
        ])

    replier = asyncio.create_task(reply_in_same_turn())
    assert await handoff_mod.engine.run_handoff(store, p)
    await replier
    assert _where(p.id, "brief.json") == "acked"
    assert _where(p.id, "before.json") == "acked"
    assert _where(p.id, "next.json") == "feeding"


async def test_forced_handoff_retires_received(store, fakes):
    p = _mk_pulse(store, state="stopped")
    _write_heartbeat(p.id, state="cli_dead")
    _inbox_file(p.id, "feeding", "brief.json", "the brief")
    _inbox_file(p.id, "feeding", "claimed.json")
    _append_events(p.id, [{"type": "user_message", "msg_file": "brief.json"},
                          {"type": "assistant_text", "text": "half done"}])
    assert await handoff_mod.engine.run_handoff(store, p, force=True)
    assert _where(p.id, "brief.json") == "acked"
    assert _where(p.id, "claimed.json") == "feeding"


async def test_reply_timeout_fallback_retires_received(store, fakes,
                                                       monkeypatch):
    monkeypatch.setattr(handoff_mod.state, "REPLY_TIMEOUT_S", 0.2)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=310_000, state="busy")
    _inbox_file(p.id, "feeding", "brief.json")
    _append_events(p.id, [{"type": "user_message", "msg_file": "brief.json"}])
    assert await handoff_mod.engine.run_handoff(store, p, fallback_reply="FALLBACK")
    assert _where(p.id, "brief.json") == "acked"


def test_cursor_ends_track_byte_offsets(data_root):
    from pulsar.daemon import events_io
    pid = "cursor-pulse"
    _append_events(pid, [{"type": "a"}, {"type": "b", "text": "é"}])
    f = paths.events_path(pid)
    cur = events_io.EventCursor(pid, 0)
    cur.poll()
    assert cur.ends[-1] == f.stat().st_size
    assert f.read_bytes()[:cur.ends[0]].endswith(b"\n")


async def test_successor_runner_gets_only_unreceived_messages(
        store, fakes, monkeypatch, tmp_path):
    """End to end: a real successor runner (fake claude CLI) boots on the
    pulse home after a mid-turn self-requested handoff. It is fed the seed and
    the claimed-but-never-fed message, and NOT the brief the old session was
    already working on."""
    import os
    import subprocess
    import sys
    import time as _time
    repo = Path(__file__).resolve().parent.parent
    py = sys.executable
    shim = tmp_path / "claude-shim"
    shim.write_text(f"#!/usr/bin/env bash\nexec {py} "
                    f"{repo / 'tests' / 'fake_claude.py'} \"$@\"\n")
    shim.chmod(0o755)
    # A short data root: runner.sock under pytest's long tmp path overflows
    # the unix-socket path limit and the runner cannot bind.
    import shutil
    import tempfile
    short = tempfile.mkdtemp(prefix="ho-")
    monkeypatch.setenv("PULSAR_DATA", short)
    p = _mk_pulse(store)
    _write_heartbeat(p.id, state="busy")
    _inbox_file(p.id, "feeding", "20261011T100000_brief.json", "the brief")
    _inbox_file(p.id, "feeding", "20261011T100500_claimed.json", "claimed")
    _append_events(p.id, [{"type": "user_message",
                           "msg_file": "20261011T100000_brief.json"}])
    procs = []

    def spawn_real(pulse_id, home, model, vessel, resume=None, **kw):
        procs.append(subprocess.Popen(
            [py, "-m", "pulsar.runner", "--pulse-id", pulse_id,
             "--home", str(home), "--model", model, "--vessel", vessel],
            cwd=repo, env=dict(os.environ, PULSAR_CLAUDE_BIN=str(shim)),
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))

    from pulsar.daemon import events_io
    monkeypatch.setattr(units_mod, "spawn_runner", spawn_real)
    offset = events_io.events_size(p.id)
    assert await handoff_mod.engine.run_handoff(store, p, note_text="NOTE " * 60)
    acked = paths.pulse_home(p.id) / "inbox" / "acked"
    try:
        deadline = _time.monotonic() + 20
        while _time.monotonic() < deadline:
            if (acked / "20261011T100500_claimed.json").exists() and \
                    any(acked.glob("handoff-seed-*.json")):
                break
            await asyncio.sleep(0.2)
    finally:
        for proc in procs:
            proc.terminate()
            proc.wait(timeout=10)
    raw = paths.events_path(p.id).read_bytes()[offset:].decode()
    shutil.rmtree(short, ignore_errors=True)
    fed = [json.loads(l).get("msg_file") for l in raw.splitlines()
           if json.loads(l).get("type") == "user_message"]
    assert fed[0].startswith("handoff-seed-")
    assert "20261011T100500_claimed.json" in fed
    assert "20261011T100000_brief.json" not in fed


# --------------------------------------------------------------------------- #
# _is_human — origin first, today's rules as the fallback
# --------------------------------------------------------------------------- #
# (file name, text start, origin, via, handoff human today)
_HUMAN_MATRIX = [
    ("20261013T000000000000Z-aaaaaaaa.json", "fix the bug", "user", "pane", True),
    ("20261013T000000000000Z-aaaaaaaa.json", "do it", "user", "first_message", True),
    ("20261013T000000000000Z-aaaaaaaa.json", "[message from 'lead' (pulse x)] do it",
     "peer", "first_message", False),
    ("20261013T000000000000Z-aaaaaaaa.json", "[message from 'a' (pulse x)] hi",
     "peer", "pulse", False),
    ("20261013T000000000000Z-aaaaaaaa.json", "[message from 'a' (pulse x)] ping",
     "peer", "schedule", False),
    ("20261013T000000000000Z-aaaaaaaa.json", "[The user replied via Inbox re: t] ok",
     "user", "inbox_reply", True),
    ("20261013T000000000000Z-aaaaaaaa.json", "[The user answered your question q1] A",
     "user", "question", True),
    ("sched-20261013T000000000000Z-aaaaaaaa.json", "run check",
     "daemon", "schedule", False),
    ("watch-20261013T000000000000Z-aaaaaaaa.json", "[delegation watch: 'h']",
     "daemon", "watch", False),
    ("condwatch-20261013T000000000000Z-aaaaaaaa.json", "[watch: n] met",
     "daemon", "condwatch", False),
    ("handoff-20261013T000000000000Z-aaaaaaaa.json", "[pulsar] write a note",
     "daemon", "handoff", False),
    ("handoff-warn-20261013T000000000000Z-aaaaaaaa.json", "[pulsar] warn",
     "daemon", "handoff", False),
    ("handoff-seed-20261013T000000000000Z-aaaaaaaa.json", "[pulsar handoff] seed",
     "daemon", "handoff", False),
    ("loopguard-20261013T000000000000Z-aaaaaaaa.json", "[pulsar loop-guard] stop",
     "daemon", "loopguard", False),
]


@pytest.mark.parametrize("name,text,origin,via,human", _HUMAN_MATRIX)
def test_is_human_matrix_with_and_without_origin(name, text, origin, via, human):
    base = {"type": "user_message", "msg_file": name, "text": text}
    assert handoff_mod._is_human(base) is human
    assert handoff_mod._is_human({**base, "origin": origin, "via": via}) is human


def test_is_human_ignores_non_user_message_events():
    assert handoff_mod._is_human(
        {"type": "assistant_text", "text": "x", "msg_file": "1.json",
         "origin": "user"}) is False


# --------------------------------------------------------------------------- #
# Monitor skip paths — what due_pulses / warn_pulses / tick skip and what
# they log. Characterization tests, written before the monitor moved to
# handoff/monitor.py: the log lines are an ops contract.
# --------------------------------------------------------------------------- #
def _tick_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records
            if r.name == "pulsar.daemon.handoff"]


def _write_old_heartbeat(pulse_id: str, ctx: int, state: str = "idle") -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    old = datetime.fromtimestamp(time.time() - 120, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    (home / "heartbeat.json").write_text(json.dumps(
        {"ts": old, "state": state, "last_ctx_tokens": ctx,
         "cli_session_id": "sess-1"}))


def test_due_pulses_silent_without_ctx_beat_or_headroom(store, caplog):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    _mk_pulse(store, model="claude-fable-5")               # no heartbeat
    p2 = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p2.id, ctx=None)                       # no ctx
    p3 = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p3.id, ctx=150_000)                    # under threshold
    assert handoff_mod.monitor.due_pulses(store) == []
    assert _tick_lines(caplog) == []


def test_due_pulses_logs_stale_heartbeat_skip(store, caplog):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_old_heartbeat(p.id, 250_000)
    assert handoff_mod.monitor.due_pulses(store) == []
    [line] = _tick_lines(caplog)
    assert line.startswith(f"handoff.tick pulse={p.id} ctx=250000 but "
                           "heartbeat stale (")
    assert line.endswith("s) — skipped")


@pytest.mark.parametrize("beat_state", ["stopped", "cli_dead"])
def test_due_pulses_logs_dead_runner_skip(store, caplog, beat_state):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, state=beat_state, ctx=250_000)
    assert handoff_mod.monitor.due_pulses(store) == []
    assert _tick_lines(caplog) == [
        f"handoff.tick pulse={p.id} ctx=250000 but runner says "
        f"{beat_state} — skipped"]


def test_due_pulses_logs_cooldown_skip(store, caplog):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    store.update_settings(p.id, {"last_handoff_at":
                                 datetime.now(timezone.utc).isoformat()})
    assert handoff_mod.monitor.due_pulses(store) == []
    assert _tick_lines(caplog) == [
        f"handoff.tick pulse={p.id} ctx=250000 >= thr=200000 but in "
        "cooldown — skipped"]


def test_due_pulses_does_not_check_in_flight(store, caplog):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    handoff_mod.state.write_state(p.id, "requested")
    caplog.clear()
    assert [d[0].id for d in handoff_mod.monitor.due_pulses(store)] == [p.id]
    assert _tick_lines(caplog) == []


def _assert_warn_silent(store, caplog, pulse) -> None:
    caplog.clear()
    assert handoff_mod.warn_pulses(store) == 0
    assert _tick_lines(caplog) == []
    assert _warnings(pulse.id) == []


def test_warn_pulses_silent_skips(store, caplog):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")           # 200k threshold
    _assert_warn_silent(store, caplog, p)                   # no heartbeat
    _write_heartbeat(p.id, ctx=None)
    _assert_warn_silent(store, caplog, p)                   # no ctx
    _write_old_heartbeat(p.id, 170_000)
    _assert_warn_silent(store, caplog, p)                   # stale beat
    for beat_state in ("stopped", "cli_dead"):
        _write_heartbeat(p.id, state=beat_state, ctx=170_000)
        _assert_warn_silent(store, caplog, p)               # dead runner
    _write_heartbeat(p.id, ctx=159_999)
    _assert_warn_silent(store, caplog, p)                   # under 80%
    _write_heartbeat(p.id, ctx=200_000)
    _assert_warn_silent(store, caplog, p)                   # at threshold
    _write_heartbeat(p.id, ctx=170_000)
    store.update_settings(p.id, {"handoff_warned": "0:sess-1"})
    _assert_warn_silent(store, caplog, p)                   # already warned
    store.update_settings(p.id, {"handoff_warned": ""})
    handoff_mod.state.write_state(p.id, "requested")
    _assert_warn_silent(store, caplog, p)                   # handoff in flight


def test_warn_pulses_ignores_cooldown_and_logs(store, caplog):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=170_000)
    store.update_settings(p.id, {"last_handoff_at":
                                 datetime.now(timezone.utc).isoformat()})
    assert handoff_mod.warn_pulses(store) == 1
    assert _tick_lines(caplog) == [
        f"handoff.warn pulse={p.id} ctx=170000 thr=200000 — early warning sent"]


async def test_tick_skips_pulse_already_active(store, caplog, monkeypatch):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    due = handoff_mod.monitor.due_pulses(store)
    monkeypatch.setattr(handoff_mod.monitor, "due_pulses", lambda s: due)
    parked = asyncio.get_running_loop().create_future()
    handoff_mod.monitor._ACTIVE[p.id] = parked
    try:
        caplog.clear()
        assert await handoff_mod.monitor.tick(store) == 0
        assert handoff_mod.monitor._ACTIVE == {p.id: parked}
        assert [l for l in _tick_lines(caplog)
                if l.startswith("handoff.tick")] == []
    finally:
        handoff_mod.monitor._ACTIVE.pop(p.id, None)


async def test_tick_logs_pulse_no_longer_running(store, caplog, monkeypatch):
    caplog.set_level("INFO", logger="pulsar.daemon.handoff")
    p = _mk_pulse(store, model="claude-fable-5")
    _write_heartbeat(p.id, ctx=250_000)
    due = handoff_mod.monitor.due_pulses(store)
    store.archive_pulse(p.id)
    monkeypatch.setattr(handoff_mod.monitor, "due_pulses", lambda s: due)
    caplog.clear()
    assert await handoff_mod.monitor.tick(store) == 0
    assert (f"handoff.tick pulse={p.id} no longer running — skipped"
            in _tick_lines(caplog))

async def test_capped_wait_covers_delivery_plus_reply(data_root, monkeypatch):
    """total_timeout_s is measured from the start: a request delivered late
    gets what is LEFT of the cap, not a fresh reply_timeout_s (a model switch
    otherwise waited ~2x and broke a reverse proxy's 60 s timeout)."""
    import time as _time
    monkeypatch.setattr(handoff_mod.engine, "REPLY_POLL_S", 0.02)
    pid = "q" * 32

    async def deliver_late():
        await asyncio.sleep(0.3)
        _append_events(pid, [{"type": "user_message", "msg_file": "h.json"}])
    paths.pulse_home(pid).mkdir(parents=True, exist_ok=True)
    t = asyncio.create_task(deliver_late())
    t0 = _time.monotonic()
    reply = await handoff_mod.engine._await_reply(pid, "h.json", timeout_s=0.4,
                                           offset=0, total_timeout_s=0.4)
    took = _time.monotonic() - t0
    await t
    assert reply is None
    assert took < 0.55, f"waited {took:.2f}s past a 0.4 s cap"


async def test_capped_reply_without_sections_is_logged_as_such(store, fakes):
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=10_000)
    timing: dict = {}
    replier = asyncio.create_task(_reply_when_asked(p.id, "short wrap-up"))
    reply = await handoff_mod.engine._ask_for_note(p, timing=timing,
                                            reply_timeout_s=2.0)
    await replier
    assert reply == "short wrap-up"
    assert timing["note_has_sections"] is False


async def test_switch_fallback_retires_message_injected_during_wait(
        store, fakes):
    """A message the old session received while we waited for its note is in
    the transcript fallback seed; it must be retired, not re-fed to the
    successor from feeding/."""
    p = _mk_pulse(store)
    _write_heartbeat(p.id, ctx=10_000, state="busy")
    _inbox_file(p.id, "feeding", "brief.json", "the brief")
    _append_events(p.id, [{"type": "user_message", "msg_file": "brief.json"}])

    async def inject_during_wait():
        await asyncio.sleep(0.1)
        _inbox_file(p.id, "feeding", "steer.json", "steer")
        _append_events(p.id, [{"type": "user_message",
                               "msg_file": "steer.json", "injected": True}])
    inj = asyncio.create_task(inject_during_wait())
    ok = await handoff_mod.engine.run_handoff(
        store, p, new_model="gpt-5.5-codex", new_vessel="codex",
        reason="model_switch", reply_timeout_s=0.4,
        fallback_to_transcript=True)
    await inj
    assert ok is True
    assert _where(p.id, "steer.json") == "acked"
    assert _where(p.id, "brief.json") == "acked"


# --------------------------------------------------------------------------- #
# NOTES.md paste cap (2026-10-21: a pulse's 125–230 KB notes ate
# 10–25% of every successor's context before its first turn)
# --------------------------------------------------------------------------- #
def test_notes_block_pastes_small_file_whole(store):
    p = _mk_pulse(store)
    handoff_mod.notes_path(p.id).parent.mkdir(parents=True, exist_ok=True)
    handoff_mod.notes_path(p.id).write_text("CORE: a\nCORE: b\n")
    block = handoff_mod.prompts.notes_block(p.id)
    assert "CORE: a\nCORE: b" in block and "truncated" not in block


def test_notes_block_caps_big_file_at_head_with_pointer(store):
    p = _mk_pulse(store)
    head = "MAP LINE " * 100                       # ~900 B, must survive
    body = "\n".join(f"old line {i} " + "x" * 80 for i in range(1500))  # ~135 KB
    handoff_mod.notes_path(p.id).parent.mkdir(parents=True, exist_ok=True)
    handoff_mod.notes_path(p.id).write_text(head + "\n" + body)
    block = handoff_mod.prompts.notes_block(p.id)
    assert head.strip() in block
    assert "old line 1499" not in block
    assert len(block.encode()) < handoff_mod.prompts.NOTES_PASTE_BYTES + 600
    assert "truncated: the file is 1" in block
    assert str(handoff_mod.notes_path(p.id)) in block


def test_context_warning_carries_notes_size_alarm(store):
    p = _mk_pulse(store, model="claude-fable-5")       # 200k threshold
    handoff_mod.notes_path(p.id).parent.mkdir(parents=True, exist_ok=True)
    handoff_mod.notes_path(p.id).write_text("x" * (handoff_mod.prompts.NOTES_WARN_BYTES + 1))
    _write_heartbeat(p.id, ctx=165_000)
    assert handoff_mod.warn_pulses(store) == 1
    text = json.loads(_warnings(p.id)[0].read_text())["text"]
    assert "Your notes file is 16 KB" in text and "condense" in text
    # a small notes file adds nothing
    q = _mk_pulse(store, model="claude-fable-5")
    handoff_mod.notes_path(q.id).parent.mkdir(parents=True, exist_ok=True)
    handoff_mod.notes_path(q.id).write_text("small")
    _write_heartbeat(q.id, ctx=165_000)
    assert handoff_mod.warn_pulses(store) == 1
    assert "Your notes file is" not in json.loads(_warnings(q.id)[0].read_text())["text"]
