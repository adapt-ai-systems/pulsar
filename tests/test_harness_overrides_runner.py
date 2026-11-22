"""Per-pulse harness overrides (harness-overrides.json) — the shared reader
plus every knob that consumes it:

* steer_nudge_s / steer_prefix — the runner's inject loop
* handoff_prompt / successor_prefix / handoff_warning — handoff prompts

An absent key must be byte-for-byte today's behaviour; a malformed file
must yield {} and today's behaviour. The lower sections also walk the
full HTTP path: POST /pulses with ``harness_overrides`` stores the diet,
the spawn materializes the sidecar, and the real consumer reads it.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon.handoff import prompts as prompts_mod
from pulsar.harness_overrides import OVERRIDES_FILE, read_overrides
from pulsar.runner import runner as runner_mod
from pulsar.store import Store

from tests.test_runner_steering import (
    WedgedBashVessel,
    drop,
    events_of,
    make_runner,
    wait_for,
)

pytestmark = pytest.mark.anyio


def write_overrides(home: Path, obj) -> None:
    home = Path(home)
    home.mkdir(parents=True, exist_ok=True)
    (home / OVERRIDES_FILE).write_text(
        obj if isinstance(obj, str) else json.dumps(obj))


# --------------------------------------------------------------------------- #
# read_overrides — the shared reader
# --------------------------------------------------------------------------- #

def test_missing_file_returns_empty(tmp_path):
    assert read_overrides(tmp_path) == {}


def test_valid_object_is_returned(tmp_path):
    write_overrides(tmp_path, {"steer_nudge_s": 12, "extra": "ok"})
    assert read_overrides(tmp_path) == {"steer_nudge_s": 12, "extra": "ok"}


def test_malformed_json_returns_empty_and_warns_once(tmp_path, caplog):
    write_overrides(tmp_path, "{not json at all")
    with caplog.at_level("WARNING", logger="pulsar.harness_overrides"):
        assert read_overrides(tmp_path) == {}
        assert read_overrides(tmp_path) == {}
    warns = [r for r in caplog.records if "harness overrides" in r.message]
    assert len(warns) == 1, "one warning per path, not per read"


def test_non_object_top_level_returns_empty(tmp_path, caplog):
    write_overrides(tmp_path, [1, 2, 3])
    with caplog.at_level("WARNING", logger="pulsar.harness_overrides"):
        assert read_overrides(tmp_path) == {}
    assert any("not a JSON object" in r.message for r in caplog.records)


def test_reader_never_raises_on_unreadable(tmp_path):
    bad = tmp_path / OVERRIDES_FILE
    bad.mkdir()  # a directory where the file should be: read_text() raises
    assert read_overrides(tmp_path) == {}


# --------------------------------------------------------------------------- #
# Runner knobs — steer_prefix, steer_nudge_s
# --------------------------------------------------------------------------- #

@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner_mod, "INBOX_POLL_S", 0.02)


class PrefixSteerableVessel:
    """One open turn per send(); inject() releases the current turn."""
    can_inject = True

    def __init__(self):
        self.injected: list[str] = []
        self.releases: list[asyncio.Event] = []

    async def send(self, text):
        ev = asyncio.Event()
        self.releases.append(ev)
        yield {"type": "assistant_text", "text": "working on it"}
        await ev.wait()
        yield {"type": "done", "reason": "result"}

    async def inject(self, text):
        self.injected.append(text)
        self.releases[-1].set()


class Feed:
    """One feed_loop task kept alive across the whole test (a turn must not
    be orphaned between waits)."""

    def __init__(self, r):
        self.r = r
        self.task: asyncio.Task | None = None

    async def __aenter__(self):
        self.task = asyncio.create_task(self.r.feed_loop())
        return self

    async def __aexit__(self, *exc):
        self.r._stopping.set()
        self.task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await self.task


def acked_count(r) -> int:
    return len(list((r.home / "inbox" / "acked").glob("*.json")))


async def test_steer_prefix_prepends_to_injected_text_only(tmp_path, fast_poll):
    """The vessel sees prefix+text; events.jsonl keeps the original text and
    carries the prefix in a new injected_prefix field."""
    v = PrefixSteerableVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, {"steer_prefix": "[steer] "})
    drop(r, "20261022T000000-a.json", "first turn")

    async with Feed(r):
        await wait_for(lambda: v.releases, what="turn open")
        drop(r, "20261022T000000-b.json", "status update?")
        await wait_for(lambda: acked_count(r) == 2, what="both acked")
    r._events_fh.close()

    assert v.injected == ["[steer] status update?"]
    evs = events_of(r)
    fed = next(e for e in evs if e.get("type") == "user_message"
               and not e.get("injected"))
    injected = next(e for e in evs if e.get("type") == "user_message"
                    and e.get("injected"))
    assert fed["text"] == "first turn"
    assert "injected_prefix" not in fed
    assert injected["text"] == "status update?", "event text stays original"
    assert injected["injected_prefix"] == "[steer] "


async def test_no_override_means_no_prefix(tmp_path, fast_poll):
    v = PrefixSteerableVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20261022T000001-a.json", "first turn")

    async with Feed(r):
        await wait_for(lambda: v.releases, what="turn open")
        drop(r, "20261022T000001-b.json", "status update?")
        await wait_for(lambda: acked_count(r) == 2, what="both acked")
    r._events_fh.close()

    assert v.injected == ["status update?"]
    injected = next(e for e in events_of(r)
                    if e.get("type") == "user_message" and e.get("injected"))
    assert "injected_prefix" not in injected


async def test_overrides_are_reread_every_turn(tmp_path, fast_poll):
    """A changed file applies on the next turn — not once per process."""
    v = PrefixSteerableVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, {"steer_prefix": "A "})
    drop(r, "20261022T000002-a.json", "turn one")

    async with Feed(r):
        await wait_for(lambda: len(v.releases) == 1, what="turn one open")
        drop(r, "20261022T000002-b.json", "status update?")
        await wait_for(lambda: acked_count(r) == 2, what="turn one done")
        write_overrides(r.home, {"steer_prefix": "B "})
        drop(r, "20261022T000003-c.json", "turn two")
        await wait_for(lambda: len(v.releases) == 2, what="turn two open")
        drop(r, "20261022T000003-d.json", "status update?")
        await wait_for(lambda: acked_count(r) == 4, what="turn two done")
    r._events_fh.close()

    assert v.injected == ["A status update?", "B status update?"]


async def test_malformed_file_yields_today_behaviour(tmp_path, fast_poll):
    v = PrefixSteerableVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, "{broken")
    drop(r, "20261022T000004-a.json", "first turn")

    async with Feed(r):
        await wait_for(lambda: v.releases, what="turn open")
        drop(r, "20261022T000004-b.json", "status update?")
        await wait_for(lambda: acked_count(r) == 2, what="both acked")
    r._events_fh.close()

    assert v.injected == ["status update?"]
    injected = next(e for e in events_of(r)
                    if e.get("type") == "user_message" and e.get("injected"))
    assert injected["text"] == "status update?"
    assert "injected_prefix" not in injected


async def test_steer_nudge_s_override_drives_the_nudge(tmp_path, fast_poll):
    """steer_nudge_s=0.3 in the file, module default untouched (300 s):
    the wedged-bash abort must still fire."""
    v = WedgedBashVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, {"steer_nudge_s": 0.3})
    assert runner_mod.STEER_NUDGE_S == 300.0, "module default must stay put"
    drop(r, "20261022T000005-a.json", "start the server")

    async with Feed(r):
        await wait_for(lambda: any(e.get("type") == "tool_call"
                                   for e in events_of(r)),
                       what="turn wedged in bash tool")
        drop(r, "20261022T000005-b.json", "status update?")
        await wait_for(lambda: v.bash_aborts == 1,
                       what="abort_bash sent after override window")
    r._events_fh.close()

    assert v.interrupts == 0
    assert v.injected == ["status update?"]


async def test_steer_nudge_s_zero_matches_global_zero(tmp_path, fast_poll,
                                                      monkeypatch):
    """steer_nudge_s=0 is honoured as an IMMEDIATE nudge, exactly like the
    existing process-wide knob at zero. Both loops must
    abort the wedged bash within seconds — never the 300 s default."""
    async def run_case(global_s: float, override) -> float:
        monkeypatch.setattr(runner_mod, "STEER_NUDGE_S", global_s)
        v = WedgedBashVessel()
        r = make_runner(tmp_path / f"home-{global_s}", v)
        if override is not ...:
            write_overrides(r.home, {"steer_nudge_s": override})
        drop(r, "20261022T000008-a.json", "start the server")
        t0 = asyncio.get_running_loop().time()
        async with Feed(r):
            await wait_for(lambda: any(e.get("type") == "tool_call"
                                       for e in events_of(r)),
                           what="turn wedged in bash tool")
            drop(r, "20261022T000008-b.json", "status update?")
            await wait_for(lambda: v.bash_aborts == 1,
                           what="abort_bash sent immediately")
        elapsed = asyncio.get_running_loop().time() - t0
        r._events_fh.close()
        assert v.interrupts == 0
        return elapsed

    # Today's behaviour: the process-wide knob at zero nudges immediately.
    global_zero = await run_case(0.0, ...)
    # The per-pulse override at zero must match it, global default untouched.
    assert runner_mod.STEER_NUDGE_S == 0.0  # monkeypatched above
    monkeypatch.setattr(runner_mod, "STEER_NUDGE_S", 300.0)
    override_zero = await run_case(300.0, 0)
    assert override_zero < 5.0, "override 0 must nudge, not take 300 s"
    assert global_zero < 5.0


@pytest.mark.parametrize("raw", [
    pytest.param(float("inf"), id="inf"),
    pytest.param(float("nan"), id="nan"),
    pytest.param(10**400, id="huge-int"),
    pytest.param(True, id="bool"),
    pytest.param(-1, id="negative-int"),
    pytest.param(-0.5, id="negative-float"),
    pytest.param("not-a-number", id="string"),
    pytest.param("0", id="numeric-string"),  # float("0") must not sneak through
    pytest.param(None, id="null"),
    pytest.param([1], id="list"),
])
async def test_bad_steer_nudge_s_falls_back_loudly(tmp_path, fast_poll,
                                                  monkeypatch, raw, caplog):
    """A non-finite/bool/negative/non-number steer_nudge_s must fall back
    to the module default WITH a warning; a huge
    int must not raise on conversion."""
    monkeypatch.setattr(runner_mod, "STEER_NUDGE_S", 0.3)
    v = WedgedBashVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, {"steer_nudge_s": raw})
    drop(r, "20261022T000009-a.json", "start the server")

    with caplog.at_level("WARNING", logger="pulsar.runner"):
        async with Feed(r):
            await wait_for(lambda: any(e.get("type") == "tool_call"
                                       for e in events_of(r)),
                           what="turn wedged in bash tool")
            drop(r, "20261022T000009-b.json", "status update?")
            await wait_for(lambda: v.bash_aborts == 1,
                           what="abort_bash sent after default window")
    r._events_fh.close()

    assert v.interrupts == 0
    assert any("steer_nudge_s" in rec.message and "invalid" in rec.message
               for rec in caplog.records), \
        f"fallback for {raw!r} must be loud"


async def test_json_overflow_number_falls_back(tmp_path, fast_poll,
                                               monkeypatch, caplog):
    """JSON 1e309 parses to float('inf') — it must fall back like inf."""
    monkeypatch.setattr(runner_mod, "STEER_NUDGE_S", 0.3)
    v = WedgedBashVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, '{"steer_nudge_s": 1e309}')
    drop(r, "20261022T000010-a.json", "start the server")

    with caplog.at_level("WARNING", logger="pulsar.runner"):
        async with Feed(r):
            await wait_for(lambda: any(e.get("type") == "tool_call"
                                       for e in events_of(r)),
                           what="turn wedged in bash tool")
            drop(r, "20261022T000010-b.json", "status update?")
            await wait_for(lambda: v.bash_aborts == 1,
                           what="abort_bash sent after default window")
    r._events_fh.close()

    assert v.interrupts == 0
    assert any("steer_nudge_s" in rec.message for rec in caplog.records)


async def test_absent_steer_nudge_s_is_silent(tmp_path, fast_poll, caplog):
    """Recheck R1: an ABSENT key (no file, or a file without the key) must
    select the default SILENTLY — the old code warned 'None invalid' on
    every ordinary injectable turn."""
    v = PrefixSteerableVessel()
    r = make_runner(tmp_path / "no-file", v)
    drop(r, "20261022T000011-a.json", "first turn")

    with caplog.at_level("WARNING", logger="pulsar.runner"):
        async with Feed(r):
            await wait_for(lambda: v.releases, what="turn open")
            drop(r, "20261022T000011-b.json", "status update?")
            await wait_for(lambda: acked_count(r) == 2, what="both acked")
    r._events_fh.close()

    assert v.injected == ["status update?"]
    assert not [rec for rec in caplog.records
                if "steer_nudge_s" in rec.message], \
        "absent key must not warn"

    # A file with other keys but no steer_nudge_s is equally silent.
    v2 = PrefixSteerableVessel()
    r2 = make_runner(tmp_path / "other-keys", v2)
    write_overrides(r2.home, {"steer_prefix": "P "})
    drop(r2, "20261022T000012-a.json", "first turn")

    with caplog.at_level("WARNING", logger="pulsar.runner"):
        async with Feed(r2):
            await wait_for(lambda: v2.releases, what="turn open")
            drop(r2, "20261022T000012-b.json", "status update?")
            await wait_for(lambda: acked_count(r2) == 2, what="both acked")
    r2._events_fh.close()

    assert v2.injected == ["P status update?"]
    assert not [rec for rec in caplog.records
                if "steer_nudge_s" in rec.message]


async def test_wrong_typed_steer_nudge_s_falls_back(tmp_path, fast_poll,
                                                    monkeypatch):
    """A non-number steer_nudge_s must fall back to the module default."""
    monkeypatch.setattr(runner_mod, "STEER_NUDGE_S", 0.3)
    v = WedgedBashVessel()
    r = make_runner(tmp_path, v)
    write_overrides(r.home, {"steer_nudge_s": "not-a-number"})
    drop(r, "20261022T000007-a.json", "start the server")

    async with Feed(r):
        await wait_for(lambda: any(e.get("type") == "tool_call"
                                   for e in events_of(r)),
                       what="turn wedged in bash tool")
        drop(r, "20261022T000007-b.json", "status update?")
        await wait_for(lambda: v.bash_aborts == 1,
                       what="abort_bash sent after default window")
    r._events_fh.close()

    assert v.interrupts == 0


# --------------------------------------------------------------------------- #
# Handoff prompt templates
# --------------------------------------------------------------------------- #

@pytest.fixture()
def ho_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    return root


@pytest.fixture()
def ho_store(ho_root):
    s = Store(str(ho_root / "pulses.db"))
    yield s
    s.close()


def _mk_pulse(ho_store, model="claude-opus-5", vessel="claude-code"):
    return ho_store.create_pulse("t", model, vessel, state="running")


def _home(p):
    from pulsar.daemon import paths
    return paths.pulse_home(p.id)


def test_handoff_prompt_template_replaces_base(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"handoff_prompt": "OPENER {opener}\nwrap up"})
    text = handoff_mod.handoff_prompt_for(p)
    assert text.startswith("OPENER [pulsar] Your context is nearly full.")
    assert "wrap up" in text
    # The notes addendum still appends.
    assert str(handoff_mod.notes_path(p.id)) in text


def test_handoff_prompt_template_gets_reason_opener(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"handoff_prompt": "OPENER {opener}"})
    text = handoff_mod.handoff_prompt_for(p, "model_switch")
    assert text.startswith("OPENER [pulsar] This pulse is switching")
    text = handoff_mod.handoff_prompt_for(p, "manual")
    assert text.startswith("OPENER [pulsar] A fresh session was requested")


def test_interface_model_explicit_template_wins(ho_store):
    """An explicit template wins for EVERY model, interface models included
    (the old interface exception is gone)."""
    p = _mk_pulse(ho_store, model="pi-qwen27-interface", vessel="pi")
    write_overrides(_home(p), {"handoff_prompt": "OPENER {opener}\nwrap up",
                               "successor_prefix": "SEED[{idle_note}]"})
    text = handoff_mod.handoff_prompt_for(p)
    assert text.startswith("OPENER [pulsar] Your context is nearly full.")
    assert "wrap up" in text
    assert "TEAM-PACKET" not in text
    # the notes addendum still appends
    assert str(handoff_mod.notes_path(p.id)) in text
    assert handoff_mod.successor_prefix_for(p) == "SEED[]"
    idle = handoff_mod.successor_prefix_for(p, was_idle=True)
    assert idle == "SEED[" + prompts_mod.IDLE_SEED_NOTE + "]"


def test_interface_model_absent_keys_use_team_packet(ho_store):
    """No overrides on an interface model: today's TEAM-PACKET defaults."""
    p = _mk_pulse(ho_store, model="pi-qwen27-interface", vessel="pi")
    text = handoff_mod.handoff_prompt_for(p)
    assert text.startswith(prompts_mod.TEAM_PACKET_REQUEST)
    assert str(handoff_mod.notes_path(p.id)) in text
    assert handoff_mod.successor_prefix_for(p) == prompts_mod.TEAM_PACKET_PREFIX


def test_empty_handoff_prompt_is_empty_plus_notes_addendum(ho_store):
    """An explicit "" handoff_prompt is honoured as empty — present is not
    truthy. Only the notes addendum survives."""
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"handoff_prompt": ""})
    assert handoff_mod.handoff_prompt_for(p) == \
        prompts_mod.notes_addendum(p.id)
    assert handoff_mod.handoff_prompt_for(p, "model_switch") == \
        prompts_mod.notes_addendum(p.id)


def test_empty_successor_prefix_is_empty(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"successor_prefix": ""})
    assert handoff_mod.successor_prefix_for(p) == ""
    assert handoff_mod.successor_prefix_for(p, was_idle=True) == ""


def test_empty_handoff_warning_keeps_notes_alarm(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"handoff_warning": ""})
    # small notes file: no alarm, the warning text is empty outright
    assert prompts_mod.warning_text(p, 5, 10) == ""
    # an oversized notes file still appends its alarm
    handoff_mod.notes_path(p.id).write_text(
        "x" * (prompts_mod.NOTES_WARN_BYTES + 1024))
    text = prompts_mod.warning_text(p, 5, 10)
    assert text != ""
    assert "condense" in text


def test_bad_placeholder_in_template_does_not_break_handoff(ho_store, caplog):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"handoff_prompt": "no {such} placeholder"})
    with caplog.at_level("WARNING"):
        text = handoff_mod.handoff_prompt_for(p)
    assert text.startswith("no {such} placeholder")
    assert str(handoff_mod.notes_path(p.id)) in text


def test_successor_prefix_template(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {"successor_prefix": "SEED[{idle_note}]"})
    assert handoff_mod.successor_prefix_for(p) == "SEED[]"
    idle = handoff_mod.successor_prefix_for(p, was_idle=True)
    assert idle == "SEED[" + prompts_mod.IDLE_SEED_NOTE + "]"


def test_handoff_warning_template(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), {
        "handoff_warning": "ctx {ctx}/{thr} note={note} pid={pulse_id} "
                           "url={daemon_url} notes={notes_path}"})
    text = prompts_mod.warning_text(p, 123456, 200000)
    assert "ctx 123456/200000" in text
    assert f"pid={p.id}" in text
    assert "url=http://127.0.0.1:8726" in text
    assert f"notes={handoff_mod.notes_path(p.id)}" in text
    assert f"note={_home(p) / 'handoffs' / 'next.md'}" in text


def test_handoff_warning_template_keeps_notes_alarm(ho_store):
    p = _mk_pulse(ho_store)
    _home(p).mkdir(parents=True, exist_ok=True)
    handoff_mod.notes_path(p.id).write_text("x" * (prompts_mod.NOTES_WARN_BYTES
                                                   + 1024))
    write_overrides(_home(p), {"handoff_warning": "W {ctx}/{thr}"})
    text = prompts_mod.warning_text(p, 1, 2)
    assert text.startswith("W 1/2")
    assert "condense" in text, "the notes-size alarm must still append"


def test_malformed_file_yields_default_prompts(ho_store):
    p = _mk_pulse(ho_store)
    write_overrides(_home(p), "{broken")
    assert handoff_mod.handoff_prompt_for(p) == \
        prompts_mod.HANDOFF_REQUEST + prompts_mod.notes_addendum(p.id)
    assert handoff_mod.successor_prefix_for(p) == \
        prompts_mod.SUCCESSOR_PREFIX
    default_warning = prompts_mod.warning_text(p, 5, 10)
    assert "Context is at 5 of 10 tokens" in default_warning


# --------------------------------------------------------------------------- #
# The full HTTP path: POST /pulses -> diet -> materialized file -> consumer
# (fixtures from tests.test_daemon; the local ho_* fixtures above belong to
#  the hand-written-file tests and never mix with these)
# --------------------------------------------------------------------------- #

from tests.test_daemon import (  # noqa: E402,F401 — fixtures
    FakeSock, _SockErr, _write_heartbeat,
    client, data_root, fake_sock, fake_spawn, store,
)


async def _spawn_with_overrides(client, overrides):
    """POST /pulses with harness_overrides; returns the pulse id. Asserts
    the diet stored EXACTLY the keys sent and the spawn materialized the
    sidecar into the pulse home."""
    r = await client.post("/pulses", json={
        "title": "lab-knob", "model": "claude-haiku-4-5",
        "harness_overrides": overrides})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    return pid


def _spawned_home(fake_spawn):
    home = Path(fake_spawn.spawned[-1]["home"])
    assert (home / OVERRIDES_FILE).is_file(), "spawn must materialize it"
    return home


async def test_api_handoff_prompt_key_reaches_the_prompt_builder(
        client, store, fake_spawn):
    pid = await _spawn_with_overrides(
        client, {"handoff_prompt": "OPENER {opener}\nAPI WRAP"})
    want = {"handoff_prompt": "OPENER {opener}\nAPI WRAP"}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = _spawned_home(fake_spawn)
    assert json.loads((home / OVERRIDES_FILE).read_text()) == want
    p = store.get_pulse(pid)
    text = handoff_mod.handoff_prompt_for(p)
    assert text.startswith("OPENER [pulsar] Your context is nearly full.")
    assert "API WRAP" in text


async def test_api_successor_prefix_key_reaches_the_prompt_builder(
        client, store, fake_spawn):
    pid = await _spawn_with_overrides(
        client, {"successor_prefix": "SEED[{idle_note}]"})
    want = {"successor_prefix": "SEED[{idle_note}]"}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = _spawned_home(fake_spawn)
    assert json.loads((home / OVERRIDES_FILE).read_text()) == want
    p = store.get_pulse(pid)
    assert handoff_mod.successor_prefix_for(p) == "SEED[]"
    assert handoff_mod.successor_prefix_for(p, was_idle=True) == \
        "SEED[" + prompts_mod.IDLE_SEED_NOTE + "]"


async def test_api_handoff_warning_key_reaches_the_warning_text(
        client, store, fake_spawn):
    pid = await _spawn_with_overrides(
        client, {"handoff_warning": "APIW ctx {ctx}/{thr} pid={pulse_id}"})
    want = {"handoff_warning": "APIW ctx {ctx}/{thr} pid={pulse_id}"}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = _spawned_home(fake_spawn)
    assert json.loads((home / OVERRIDES_FILE).read_text()) == want
    p = store.get_pulse(pid)
    text = prompts_mod.warning_text(p, 5, 10)
    assert text.startswith("APIW ctx 5/10")
    assert f"pid={pid}" in text


async def test_api_steer_prefix_key_reaches_the_runner(
        client, store, fake_spawn, fast_poll):
    pid = await _spawn_with_overrides(client, {"steer_prefix": "[api] "})
    want = {"steer_prefix": "[api] "}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = _spawned_home(fake_spawn)
    assert json.loads((home / OVERRIDES_FILE).read_text()) == want

    v = PrefixSteerableVessel()
    r = runner_mod.Runner("p-api", home, "m", "claude-code", None)
    r.vessel = v
    for sub in ("inbox/new", "inbox/acked"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    r._events_fh = (home / "events.jsonl").open("a")
    drop(r, "20261022T000020-a.json", "first turn")

    async with Feed(r):
        await wait_for(lambda: v.releases, what="turn open")
        drop(r, "20261022T000020-b.json", "status update?")
        await wait_for(lambda: acked_count(r) == 2, what="both acked")
    r._events_fh.close()

    assert v.injected == ["[api] status update?"]


async def test_api_steer_nudge_s_key_reaches_the_runner(
        client, store, fake_spawn, fast_poll):
    pid = await _spawn_with_overrides(client, {"steer_nudge_s": 0.3})
    want = {"steer_nudge_s": 0.3}
    assert store.get_pulse(pid).settings["diet"]["harness_overrides"] == want
    home = _spawned_home(fake_spawn)
    assert json.loads((home / OVERRIDES_FILE).read_text()) == want

    assert runner_mod.STEER_NUDGE_S == 300.0, "module default must stay put"
    v = WedgedBashVessel()
    r = runner_mod.Runner("p-api", home, "m", "claude-code", None)
    r.vessel = v
    for sub in ("inbox/new", "inbox/acked"):
        (home / sub).mkdir(parents=True, exist_ok=True)
    r._events_fh = (home / "events.jsonl").open("a")
    drop(r, "20261022T000021-a.json", "start the server")

    async with Feed(r):
        await wait_for(lambda: any(e.get("type") == "tool_call"
                                   for e in events_of(r)),
                       what="turn wedged in bash tool")
        drop(r, "20261022T000021-b.json", "status update?")
        # Only the override can fire inside 5 s — the default is 300 s.
        await wait_for(lambda: v.bash_aborts == 1, timeout=5.0,
                       what="abort_bash sent after the override window")
    r._events_fh.close()

    assert v.interrupts == 0
    assert v.injected == ["status update?"]


@pytest.mark.anyio
@pytest.mark.parametrize("value,id_", [
    (True, "bool"),
    (float("inf"), "inf"),
    (float("nan"), "nan"),
    ("0.3", "numeric-string"),
    # An int too large for float() makes math.isfinite
    # itself raise OverflowError — that must be a 422, never a 500.
    (10 ** 400, "oversized-int"),
])
async def test_api_rejects_bad_steer_nudge_s_with_422(client, value, id_):
    # Mirrors the runner's own rules: booleans are not numbers, inf/NaN
    # never elapse, and numeric strings must not sneak through float().
    body = {"title": "bad", "model": "claude-haiku-4-5",
            "harness_overrides": {"steer_nudge_s": value}}
    r = await client.post("/pulses", content=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert "steer_nudge_s" in r.text


async def test_api_rejects_oversized_integer_steer_nudge_s_on_patch_diet(
        client):
    # The same 10**400 probe on PATCH /diet.
    r = await client.post("/pulses", json={"title": "lab",
                                           "model": "claude-haiku-4-5"})
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/diet", content=json.dumps({
        "skills": [],
        "harness_overrides": {"steer_nudge_s": 10 ** 400}}).encode(),
        headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert "steer_nudge_s" in r.text


async def test_api_rejects_nul_in_steer_prefix_with_422(client):
    body = {"title": "bad", "model": "claude-haiku-4-5",
            "harness_overrides": {"steer_prefix": "a\x00b"}}
    r = await client.post("/pulses", content=json.dumps(body).encode(),
                          headers={"Content-Type": "application/json"})
    assert r.status_code == 422, r.text
    assert "steer_prefix" in r.text


async def test_api_unknown_b_key_typo_is_still_422(client):
    # extra="forbid" holds for the runner/handoff keys too: a typo'd knob
    # must never look applied when it was not.
    r = await client.post("/pulses", json={
        "title": "bad", "model": "claude-haiku-4-5",
        "harness_overrides": {"steer_prefix_s": "x"}})
    assert r.status_code == 422, r.text
    assert "steer_prefix_s" in r.text
