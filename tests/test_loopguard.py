"""Loop-guard tests — detection, the strike ladder, and the rescue fallback.

    venv/bin/python -m pytest tests -k loopguard -q

The degenerate fixture is the REAL event from the 2026-08-15 incident (a DS4
Flash pulse repeating "lderGo." 143 times inside one 19.6k-char
assistant_text); the healthy fixture is a normal long progress note from the
same pulse. No real runners or tokens: spawn/sock are
monkeypatched exactly like test_handoff.py.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import loopguard as lg
from pulsar.daemon import paths
from pulsar.store import Store

pytestmark = pytest.mark.anyio

FIXTURES = Path(__file__).parent / "fixtures"
LOOP_TEXT = (FIXTURES / "degenerate_loop.txt").read_text()
RUNON_TEXT = (FIXTURES / "degenerate_runon.txt").read_text()
VARIED_TEXT = (FIXTURES / "degenerate_varied.txt").read_text()
HEALTHY_TEXT = (FIXTURES / "healthy_long.txt").read_text()


# --------------------------------------------------------------------------- #
# Fixtures
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


@pytest.fixture(autouse=True)
def fresh_guard_state():
    """The guard's strike memory is module-level; isolate every test."""
    lg._strikes.clear()
    lg._handled_ts.clear()
    lg._last_alert.clear()
    yield


def _now_iso(offset_s: float = 0.0) -> str:
    return (datetime.now(timezone.utc)
            + timedelta(seconds=offset_s)).isoformat()


def _write_heartbeat(pulse_id: str, state: str = "busy") -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S%z")
    (home / "heartbeat.json").write_text(json.dumps(
        {"ts": ts, "pid": 1, "cli_pid": 2, "state": state,
         "cli_session_id": "sess-1", "last_ctx_tokens": 118_000,
         "protocol": 1}))


def _append_event(pulse_id: str, text: str, ts: str | None = None,
                  etype: str = "assistant_text") -> None:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    with (home / "events.jsonl").open("a") as fh:
        fh.write(json.dumps({"type": etype, "text": text,
                             "ts": ts or _now_iso()}) + "\n")


def _queued_messages(pulse_id: str, prefix: str = "") -> list[Path]:
    new = paths.inbox_new(pulse_id)
    if not new.exists():
        return []
    return sorted(p for p in new.glob("*.json") if p.name.startswith(prefix))


# --------------------------------------------------------------------------- #
# looks_degenerate — the detector
# --------------------------------------------------------------------------- #
def test_detector_catches_the_real_incident():
    # 2026-08-15: "lderGo." x143 interleaved with "Let me run
    # check.js." barks — now caught by the "Let me" density path.
    hit = lg.looks_degenerate(LOOP_TEXT)
    assert hit is not None
    line, repeats = hit
    assert line.lower().startswith("let me")
    assert repeats >= 100


def test_detector_catches_runon_barks():
    # 2026-08-16: barks run together on single lines with
    # no newlines ("Let me run it.Let me do it.") — invisible to the original
    # line-splitting detector, caught via sentence-boundary normalization.
    hit = lg.looks_degenerate(RUNON_TEXT)
    assert hit is not None
    assert hit[1] >= 100


def test_detector_catches_varied_phrasing():
    # 2026-08-15: varied barks ("Applying." / "Now applying." /
    # "Let me apply.") — each exact line under the old family minimum; this
    # one shipped 17h after the guard went live and did not strike.
    hit = lg.looks_degenerate(VARIED_TEXT)
    assert hit is not None
    assert hit[1] >= 50


def test_detector_passes_healthy_long_text():
    assert lg.looks_degenerate(HEALTHY_TEXT) is None


def test_detector_passes_small_blocks_even_if_repetitive():
    assert lg.looks_degenerate("Go.\n" * 20) is None  # under MIN_EVENT_CHARS


def test_detector_passes_repetitive_markdown_table():
    rows = ["| svc%03d | active | 87%02d | some description |" % (i, i % 100)
            for i in range(150)]
    rows += ["|---|---|---|---|"] * 12
    assert lg.looks_degenerate("\n".join(rows)) is None


def test_detector_catches_synthetic_loop():
    text = ("Executing now.\nGo.\nRun it.\n" * 300)
    hit = lg.looks_degenerate(text)
    assert hit is not None
    assert hit[1] >= 300


def test_detector_passes_repeated_checklist_verdicts():
    # Real false positive from the sweep: a 51-transaction audit repeating
    # the same bullet verdict. Structured lines stay out of the pool.
    items = "".join(f"**Txn {i:03d}** analysis text here.\n- ✅ Matches proposal\n"
                    for i in range(60))
    assert lg.looks_degenerate(items) is None


def test_detector_passes_path_prefixed_table():
    # Real false positive from the sweep: table rows sharing a path prefix
    # collapse into one fuzzy family unless table rows are excluded.
    rows = "\n".join(f"| /home/user/workspace/project-p{i}-cf{i:04x} | "
                     f"{i}M | INCLUDE | merged, keep |" for i in range(40))
    assert lg.looks_degenerate(rows) is None


def test_detector_passes_healthy_letme_planning():
    # A few "Let me" sentences inside real prose stay under both the count
    # and fraction bars.
    para = ("Let me check the config first. The config file lives under the "
            "daemon directory and controls thresholds for every subsystem "
            "we ship, including handoff and vision. After reviewing it I "
            "will compare the values against production behavior in detail, "
            "since drift there has bitten us before.\n")
    assert lg.looks_degenerate(para * 12) is None


# --------------------------------------------------------------------------- #
# find_fresh_loop — freshness + handled gates
# --------------------------------------------------------------------------- #
def test_find_fresh_loop_finds_recent_event(data_root):
    _append_event("p1", LOOP_TEXT)
    hit = lg.find_fresh_loop("p1")
    assert hit is not None and hit["line"].lower().startswith("let me")


def test_find_fresh_loop_ignores_stale_events(data_root):
    _append_event("p1", LOOP_TEXT, ts=_now_iso(-lg.FRESH_EVENT_S - 60))
    assert lg.find_fresh_loop("p1") is None


def test_find_fresh_loop_ignores_already_handled(data_root):
    ts = _now_iso()
    _append_event("p1", LOOP_TEXT, ts=ts)
    lg._handled_ts["p1"] = ts
    assert lg.find_fresh_loop("p1") is None


def test_find_fresh_loop_ignores_healthy_events(data_root):
    _append_event("p1", HEALTHY_TEXT)
    assert lg.find_fresh_loop("p1") is None


# --------------------------------------------------------------------------- #
# The strike ladder
# --------------------------------------------------------------------------- #
def _mk_pulse(store, model="codex-ds4-flash", vessel="codex"):
    return store.create_pulse("Codebase map diagram", model, vessel,
                              state="running")


async def test_strike_one_steers(store):
    pulse = _mk_pulse(store)
    await lg._strike(store, pulse, {"ts": _now_iso(), "line": "Go.",
                                    "repeats": 100})
    msgs = _queued_messages(pulse.id, "loopguard")
    assert len(msgs) == 1
    body = json.loads(msgs[0].read_text())
    assert body["source"] == "loopguard"
    assert (body["origin"], body["via"]) == ("daemon", "loopguard")
    assert "loop-guard" in body["text"]


async def test_second_strike_within_grace_holds(store, monkeypatch):
    pulse = _mk_pulse(store)
    fired = []

    async def fake_handoff(*a, **k):
        fired.append(k)
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_handoff)
    hit = {"ts": _now_iso(), "line": "Go.", "repeats": 100}
    await lg._strike(store, pulse, hit)
    await lg._strike(store, pulse, {**hit, "ts": _now_iso(1)})
    assert fired == []          # steer grace — no rescue yet
    assert len(lg._strikes[pulse.id]) == 2


async def test_second_strike_after_grace_fires_rescue(store, monkeypatch):
    pulse = _mk_pulse(store)
    fired = []

    async def fake_handoff(s, p, **k):
        fired.append(k)
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_handoff)
    # Backdate strike 1 past the steer grace.
    import time as _time
    lg._strikes[pulse.id] = [_time.time() - lg.STEER_GRACE_S - 5]
    await lg._strike(store, pulse, {"ts": _now_iso(), "line": "Go.",
                                    "repeats": 100})
    assert len(fired) == 1
    assert fired[0]["reason"] == "loop_rescue"
    assert "degenerate output loop" in fired[0]["fallback_reply"]
    assert pulse.id not in lg._strikes   # success wipes the slate


async def test_failed_rescue_alerts_inbox_once(store, monkeypatch):
    pulse = _mk_pulse(store)

    async def fake_handoff(*a, **k):
        return False

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_handoff)
    import time as _time
    lg._strikes[pulse.id] = [_time.time() - lg.STEER_GRACE_S - 5]
    await lg._strike(store, pulse, {"ts": _now_iso(), "line": "Go.",
                                    "repeats": 100})
    items = store.list_inbox_items(channel="system")
    assert len(items) == 1
    assert "loop-guard" in items[0].body
    assert items[0].source_kind == "loopguard"
    # Third strike inside the alert cooldown: no second Inbox alert.
    lg._strikes[pulse.id] = [_time.time() - lg.STEER_GRACE_S - 5]
    await lg._strike(store, pulse, {"ts": _now_iso(1), "line": "Go.",
                                    "repeats": 100})
    assert len(store.list_inbox_items(channel="system")) == 1


async def test_rescue_blocked_by_cooldown_escalates_to_alert(store,
                                                             monkeypatch):
    pulse = _mk_pulse(store)
    fired = []

    async def fake_handoff(*a, **k):
        fired.append(k)
        return True

    monkeypatch.setattr(handoff_mod.engine, "run_handoff", fake_handoff)
    # A handoff just happened — cooldown active.
    store.update_settings(pulse.id, {"last_handoff_at": _now_iso()})
    pulse = store.get_pulse(pulse.id)
    import time as _time
    lg._strikes[pulse.id] = [_time.time() - lg.STEER_GRACE_S - 5]
    await lg._strike(store, pulse, {"ts": _now_iso(), "line": "Go.",
                                    "repeats": 100})
    assert fired == []
    items = store.list_inbox_items(channel="system")
    assert len(items) == 1
    assert items[0].source_kind == "loopguard"


# --------------------------------------------------------------------------- #
# tick — end to end against store + disk
# --------------------------------------------------------------------------- #
async def test_tick_steers_a_looping_pulse(store):
    pulse = _mk_pulse(store)
    _write_heartbeat(pulse.id)
    _append_event(pulse.id, LOOP_TEXT)
    struck = await lg.tick(store)
    assert struck == 1
    assert len(_queued_messages(pulse.id, "loopguard")) == 1
    # Same event never strikes twice.
    assert await lg.tick(store) == 0


async def test_tick_ignores_healthy_pulse(store):
    pulse = _mk_pulse(store)
    _write_heartbeat(pulse.id)
    _append_event(pulse.id, HEALTHY_TEXT)
    assert await lg.tick(store) == 0
    assert _queued_messages(pulse.id) == []


async def test_tick_ignores_stale_heartbeat(store):
    pulse = _mk_pulse(store)
    home = paths.pulse_home(pulse.id)
    home.mkdir(parents=True, exist_ok=True)
    (home / "heartbeat.json").write_text(json.dumps(
        {"ts": "2020-01-01T00:00:00+0000", "state": "busy"}))
    _append_event(pulse.id, LOOP_TEXT)
    assert await lg.tick(store) == 0


# --------------------------------------------------------------------------- #
# Mechanical handoff text — the rescue seed for a too-far-gone model
# --------------------------------------------------------------------------- #
def test_mechanical_handoff_skips_degenerate_blocks(data_root):
    _append_event("p1", "brief text", etype="user_message")
    _append_event("p1", "Healthy progress: built nodes.json with 89 nodes.")
    _append_event("p1", LOOP_TEXT)
    text = lg._mechanical_handoff_text("p1")
    assert "degenerate output loop" in text
    assert "Healthy progress" in text
    assert "lderGo." not in text
