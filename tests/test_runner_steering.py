"""Mid-turn steering: inbox messages reach a BUSY pulse between tool calls.

2026-07-27 post-mortem: the auto-handoff request queued behind a long turn
until REPLY_TIMEOUT killed the handoff, and the pulse blew past its context
threshold. Steering is the fix — the runner injects new inbox messages (the
human's and the daemon's handoff request) into the live turn.

Unit-level: a scripted fake vessel (no subprocess) drives Runner.feed_loop.
The end-to-end stream-json path is test_runner_integration's job.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from pathlib import Path

import pytest

from pulsar.runner import runner as runner_mod
from pulsar.runner.runner import Runner
from pulsar.runner.vessel_bridge import BridgedVessel
from pulsar.vessel.events import VesselEvent


class SteerableVessel:
    """One turn that stays open until a message is injected into it."""
    can_inject = True

    def __init__(self):
        self.injected: list[str] = []
        self.release = asyncio.Event()

    async def send(self, text):
        yield {"type": "assistant_text", "text": "working on it"}
        await self.release.wait()
        yield {"type": "assistant_text", "text": "steered, wrapping up"}
        yield {"type": "done", "reason": "result"}

    async def inject(self, text):
        self.injected.append(text)
        self.release.set()


class QueueOnlyVessel:
    """No can_inject — messages must wait their turn (codex/qwen shape)."""

    def __init__(self):
        self.turns: list[str] = []

    async def send(self, text):
        self.turns.append(text)
        yield {"type": "assistant_text", "text": f"turn: {text}"}
        yield {"type": "done", "reason": "result"}


class PumpFailureVessel:
    def __init__(self):
        self.stopped = False

    async def subscribe(self, sid):
        if False:  # make this an async generator for ``async for``
            yield sid
        raise RuntimeError("subscription transport failed")

    async def stop(self, sid):
        self.stopped = True


class TimestampVessel:
    async def subscribe(self, sid):
        yield VesselEvent(kind="assistant_text", payload={"text": "hello"},
                          timestamp="2026-08-07T12:34:56+00:00")


@pytest.fixture
def fast_poll(monkeypatch):
    monkeypatch.setattr(runner_mod, "INBOX_POLL_S", 0.02)


def make_runner(tmp_path: Path, vessel) -> Runner:
    home = tmp_path / "home"
    for sub in ("inbox/new", "inbox/acked"):
        (home / sub).mkdir(parents=True)
    r = Runner("p-steer", home, "m", "claude-code", None)
    r.vessel = vessel
    r._events_fh = (home / "events.jsonl").open("a")
    return r


def drop(r: Runner, name: str, text: str) -> Path:
    p = r.home / "inbox" / "new" / name
    p.write_text(json.dumps({"text": text}))
    return p


def events_of(r: Runner) -> list[dict]:
    return [json.loads(l) for l in
            (r.home / "events.jsonl").read_text().splitlines()]


async def wait_for(cond, timeout=5.0, what="condition"):
    t0 = asyncio.get_running_loop().time()
    while asyncio.get_running_loop().time() - t0 < timeout:
        if cond():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"timed out waiting for {what}")


async def run_feed(r: Runner, until, what: str):
    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(until, what=what)
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


@pytest.mark.anyio
async def test_message_injected_into_live_turn(tmp_path, fast_poll):
    v = SteerableVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260727T000000-a.json", "start the work")
    acked = r.home / "inbox" / "acked"

    async def scenario():
        # the turn is open (vessel parked on release); steer it
        await wait_for(lambda: v.release is not None and not v.release.is_set()
                       and (r.home / "events.jsonl").exists(),
                       what="turn open")
        await asyncio.sleep(0.05)
        drop(r, "20260727T000001-b.json", "steer: stop and report")
        return True

    feed = asyncio.create_task(r.feed_loop())
    try:
        await scenario()
        await wait_for(lambda: len(list(acked.glob("*.json"))) == 2,
                       what="both messages acked at the turn boundary")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.injected == ["steer: stop and report"]

    evs = events_of(r)
    users = [e for e in evs if e["type"] == "user_message"]
    assert [e["msg_file"] for e in users] == [
        "20260727T000000-a.json", "20260727T000001-b.json"]
    assert users[1].get("injected") is True
    # the injected bubble lands BEFORE the turn's closing text
    idx_injected = evs.index(users[1])
    idx_closing = next(i for i, e in enumerate(evs)
                       if e.get("text") == "steered, wrapping up")
    assert idx_injected < idx_closing


@pytest.mark.anyio
async def test_queue_only_vessel_keeps_serial_feed(tmp_path, fast_poll):
    v = QueueOnlyVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260727T000000-a.json", "one")
    drop(r, "20260727T000001-b.json", "two")
    acked = r.home / "inbox" / "acked"
    await run_feed(r, lambda: len(list(acked.glob("*.json"))) == 2,
                   "both messages processed serially")
    assert v.turns == ["one", "two"]
    users = [e for e in events_of(r) if e["type"] == "user_message"]
    assert all("injected" not in e for e in users)


def test_handoff_messages_jump_the_queue(tmp_path):
    r = make_runner(tmp_path, QueueOnlyVessel())
    drop(r, "20260101T000000-user.json", "earlier user message")
    drop(r, "handoff-20991231T000000-x.json", "handoff request")
    nxt = r.next_message()
    r._events_fh.close()
    assert nxt is not None and nxt.name.startswith("handoff-")


def test_bridge_poll_event_preserves_sentinel():
    bv = BridgedVessel("m", Path("/nonexistent"), None)
    assert bv.can_inject is True
    bv._queue.put_nowait({"type": "assistant_text", "text": "stray"})
    bv._queue.put_nowait(None)
    assert bv.poll_event() == {"type": "assistant_text", "text": "stray"}
    assert bv.poll_event() is None
    # the stream-over sentinel must survive the poll for a later send()
    assert bv._queue.get_nowait() is None


def test_stray_session_and_provider_error_are_captured_outside_send(tmp_path):
    """A session / provider_error event that arrives while the runner is
    idle (stray pump) updates the same state the send() drain would."""
    class StrayVessel:
        def __init__(self, events):
            self.events = list(events)

        def poll_event(self):
            return self.events.pop(0) if self.events else None

    r = make_runner(tmp_path, StrayVessel([
        {"type": "session", "session_id": "sess-new"},
        {"type": "usage", "ctx": 1234},
        {"type": "runner_error", "where": "provider_error", "reason": "boom"},
    ]))
    r._pump_stray_events()
    assert r.cli_session_id == "sess-new"
    assert r.last_ctx_tokens == 1234
    assert r.turn_error == "boom"
    assert [e["type"] for e in events_of(r)] == [
        "session", "usage", "runner_error"]


@pytest.mark.anyio
async def test_bridge_stops_provider_before_resolving_pump_failure():
    bv = BridgedVessel("m", Path("/nonexistent"), None,
                       vessel_cls=PumpFailureVessel)
    bv._sid = "sid"
    bv._exited = asyncio.get_running_loop().create_future()

    await bv._pump()

    assert bv._v.stopped is True
    assert bv._exited.done() and bv._exited.result() == 1


@pytest.mark.anyio
async def test_bridge_preserves_vessel_event_timestamp():
    bv = BridgedVessel("m", Path("/nonexistent"), None,
                       vessel_cls=TimestampVessel)
    bv._sid = "sid"
    bv._exited = asyncio.get_running_loop().create_future()

    await bv._pump()

    assert bv._queue.get_nowait()["ts"] == "2026-08-07T12:34:56+00:00"


class InterruptSteerVessel:
    """No native injection, but a turn interrupt (qwen kill-and-resume
    shape): a message arriving mid-turn must abort the turn so it gets
    dispatched immediately instead of waiting for the boundary."""
    can_inject = False
    can_interrupt = True

    def __init__(self):
        self.turns: list[str] = []
        self.interrupts = 0
        self._abort = asyncio.Event()

    async def send(self, text):
        self.turns.append(text)
        if len(self.turns) == 1:
            yield {"type": "assistant_text", "text": "grinding away"}
            await self._abort.wait()
            yield {"type": "done", "reason": "result",
                   "subtype": "interrupted"}
        else:
            yield {"type": "assistant_text", "text": f"turn: {text}"}
            yield {"type": "done", "reason": "result"}

    async def interrupt(self):
        self.interrupts += 1
        self._abort.set()


@pytest.mark.anyio
async def test_message_interrupts_turn_when_no_inject(tmp_path, fast_poll):
    v = InterruptSteerVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260814T000000-a.json", "start the long grind")
    acked = r.home / "inbox" / "acked"

    async def until():
        return len(v.turns) == 2 and len(list(acked.glob("*.json"))) == 2

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: v.turns == ["start the long grind"],
                       what="first turn open")
        await asyncio.sleep(0.05)
        drop(r, "20260814T000001-b.json", "urgent: do the handoff")
        await wait_for(lambda: v.interrupts == 1, what="turn interrupted")
        await wait_for(lambda: len(v.turns) == 2
                       and len(list(acked.glob("*.json"))) == 2,
                       what="steered message dispatched as its own turn")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.interrupts == 1
    assert v.turns == ["start the long grind", "urgent: do the handoff"]


@pytest.mark.anyio
async def test_queued_handoff_interrupts_work_turn(tmp_path, fast_poll):
    """A wrap-up sitting in new/ must abort the current WORK turn.

    aa362e7 skipped handoff-*.json in _interrupt_on_message_loop, so a
    pulse over its context threshold kept grinding until OOM while the
    daemon's request sat queued (2026-09-04, 246k/175k).
    The wrap-up-in-flight guard (test_handoff_turn_is_never_interrupted)
    stays; this is the other direction.
    """
    v = InterruptSteerVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260904T052654-a.json", "keep running the build")
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: v.turns == ["keep running the build"],
                       what="work turn open")
        await asyncio.sleep(0.05)
        drop(r, "handoff-20260904T054924Z-1a2b3c4d.json",
             "wrap up and hand off")
        await wait_for(lambda: v.interrupts == 1, what="work turn interrupted")
        await wait_for(lambda: v.turns == [
                           "keep running the build",
                           "wrap up and hand off"]
                       and len(list(acked.glob("*.json"))) == 2,
                       what="handoff dispatched as its own turn")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.interrupts == 1
    assert v.turns[-1] == "wrap up and hand off"


class InjectFailInterruptVessel:
    """Pi-rpc shape: steer exists but rejects; interrupt is the floor."""
    can_inject = True
    can_interrupt = True

    def __init__(self):
        self.turns: list[str] = []
        self.injected: list[str] = []
        self.interrupts = 0
        self._abort = asyncio.Event()

    async def send(self, text):
        self.turns.append(text)
        if len(self.turns) == 1:
            yield {"type": "assistant_text", "text": "grinding away"}
            await self._abort.wait()
            yield {"type": "done", "reason": "result",
                   "subtype": "interrupted"}
        else:
            yield {"type": "assistant_text", "text": f"turn: {text}"}
            yield {"type": "done", "reason": "result"}

    async def inject(self, text):
        raise RuntimeError("pi rpc steer rejected")

    async def interrupt(self):
        self.interrupts += 1
        self._abort.set()


@pytest.mark.anyio
async def test_failed_inject_interrupts_when_vessel_can(tmp_path, fast_poll):
    """If we cannot send between tool calls, abort so the message is not
    parked behind the rest of the turn (e.g. a handoff must not sit until
    the session runs out of context)."""
    v = InjectFailInterruptVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260904T052654-a.json", "start the long grind")
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: v.turns == ["start the long grind"],
                       what="first turn open")
        await asyncio.sleep(0.05)
        drop(r, "handoff-20260904T054924Z-1a2b3c4d.json",
             "wrap up and hand off")
        await wait_for(lambda: v.interrupts == 1,
                       what="turn interrupted after inject failed")
        await wait_for(lambda: v.turns == [
                           "start the long grind",
                           "wrap up and hand off"]
                       and len(list(acked.glob("*.json"))) == 2,
                       what="handoff dispatched as its own turn")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.injected == []
    assert v.interrupts == 1
    assert v.turns[-1] == "wrap up and hand off"


@pytest.mark.anyio
async def test_failed_inject_does_not_interrupt_claude_shape(tmp_path, fast_poll):
    """Claude has inject and no interrupt: a failed inject requeues and
    waits for the turn boundary (existing contract)."""
    v = SteerableVessel()

    async def boom(text):
        raise RuntimeError("stdin write failed")

    v.inject = boom  # type: ignore[method-assign]
    r = make_runner(tmp_path, v)
    drop(r, "20260904T000000-a.json", "start")

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: any(
            e.get("type") == "assistant_text" for e in events_of(r)),
            what="turn open")
        drop(r, "20260904T000001-b.json", "steer me")
        await asyncio.sleep(0.2)
        queued = [p.name for p in (r.home / "inbox" / "new").glob("*.json")]
        assert queued == ["20260904T000001-b.json"]
        v.release.set()
        await wait_for(lambda: len(list(
            (r.home / "inbox" / "acked").glob("*.json"))) == 2,
                       what="queued message processed after boundary")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


@pytest.mark.anyio
async def test_no_interrupt_without_new_message(tmp_path, fast_poll):
    """The interrupt-steer loop must not fire for the turn's own message."""
    v = InterruptSteerVessel()
    # let the first turn finish on its own
    v._abort.set()
    r = make_runner(tmp_path, v)
    drop(r, "20260814T000000-a.json", "quick job")
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: len(list(acked.glob("*.json"))) == 1,
                       what="turn acked")
        await asyncio.sleep(0.1)
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.interrupts == 0


# --------------------------------------------------------------------------- #
# In-turn claim (inbox/feeding/) — the reset-redelivery fix (2026-08-15:
# resetting a pulse right after it reported done re-fed the whole brief,
# because messages sat in new/ until the turn-boundary ack).
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_in_turn_message_lives_in_feeding_until_ack(tmp_path, fast_poll):
    """From the moment a message enters a turn it is claimed into feeding/,
    so the daemon's reset can retire consumed work instead of redelivering
    it; the turn-boundary ack moves it feeding/ -> acked/ as before."""
    v = SteerableVessel()
    r = make_runner(tmp_path, v)
    p = drop(r, "20260815T000000-a.json", "the brief")
    feeding = r.home / "inbox" / "feeding"
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: (feeding / p.name).exists(),
                       what="message claimed into feeding/ at turn start")
        assert not p.exists(), "claimed message must leave new/"
        assert not (acked / p.name).exists()
        v.release.set()  # close the turn
        await wait_for(lambda: (acked / p.name).exists(),
                       what="turn-boundary ack")
        assert not (feeding / p.name).exists()
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


@pytest.mark.anyio
async def test_vessel_error_unclaims_back_to_new(tmp_path, fast_poll):
    """A failed turn is not consumed: on vessel error the claimed messages
    return to new/ so a restored pulse gets every one of them back."""
    class ExplodingVessel:
        async def send(self, text):
            yield {"type": "assistant_text", "text": "dying"}
            raise RuntimeError("boom")

    r = make_runner(tmp_path, ExplodingVessel())
    p = drop(r, "20260815T000000-a.json", "doomed turn")

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(r._stopping.is_set,
                       what="feed loop stopped on vessel error")
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(feed, timeout=5)
    finally:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert p.exists(), "errored message must be back in new/"
    assert list((r.home / "inbox" / "feeding").glob("*.json")) == []
    assert list((r.home / "inbox" / "acked").glob("*.json")) == []


@pytest.mark.anyio
async def test_vessel_error_during_stop_is_not_a_runner_error(tmp_path, fast_poll):
    """A stop kills the CLI mid-turn; that is not a crash row."""
    r = make_runner(tmp_path, None)

    class StoppedVessel:
        async def send(self, text):
            yield {"type": "assistant_text", "text": "working"}
            r._stopping.set()
            raise RuntimeError("CLI killed by stop")

    r.vessel = StoppedVessel()
    p = drop(r, "20260815T000000-a.json", "stopped turn")
    feed = asyncio.create_task(r.feed_loop())
    try:
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(feed, timeout=5)
    finally:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert not [e for e in events_of(r) if e["type"] == "runner_error"]
    # Live's rule (test_stop_induced_failure_leaves_turn_in_feeding): a
    # stop-killed turn stays in feeding/ for the daemon's handoff/reset.
    assert (r.home / "inbox" / "feeding" / p.name).exists()
    assert not (r.home / "inbox" / "new" / p.name).exists()


# --------------------------------------------------------------------------- #
# A handoff wrap-up is the one turn nothing may steer
# --------------------------------------------------------------------------- #
# 2026-08-23: a pulse message landed 43s into the wrap-up turn.
# _interrupt_on_message_loop killed it, so the turn closed with
# done{interrupted} and no assistant_text; handoff.extract_reply returns None
# for exactly that shape, and the daemon then sat in _await_reply for the full
# REPLY_TIMEOUT_S before marking the handoff failed — leaving the pulse running
# on the session that was already over its context threshold.

@pytest.mark.anyio
async def test_handoff_turn_is_never_interrupted(tmp_path, fast_poll):
    v = InterruptSteerVessel()
    r = make_runner(tmp_path, v)
    drop(r, "handoff-20260823T151204Z-2b3c4d5e.json", "wrap up and hand off")

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: v.turns == ["wrap up and hand off"],
                       what="handoff turn open")
        drop(r, "20260823T151247Z-3c4d5e6f.json", "a message from a babysitter")
        await asyncio.sleep(0.2)          # generous next to INBOX_POLL_S=0.02
        assert v.interrupts == 0, "handoff wrap-up must not be interrupted"
        # and the interloper is still queued for the successor, not consumed
        assert [p.name for p in (r.home / "inbox" / "new").glob("*.json")] == \
            ["20260823T151247Z-3c4d5e6f.json"]
        # the wrap-up still finishes normally once the model is done
        v._abort.set()
        await wait_for(lambda: any(
            e.get("type") == "assistant_text" for e in events_of(r)),
            what="handoff reply emitted")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


@pytest.mark.anyio
async def test_handoff_turn_is_never_injected(tmp_path, fast_poll):
    """Injection is milder than a kill, but still wrong: the model would
    answer the interloper instead of writing the handoff its successor needs.
    """
    v = SteerableVessel()
    r = make_runner(tmp_path, v)
    drop(r, "handoff-20260823T151204Z-2b3c4d5e.json", "wrap up and hand off")

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: any(
            e.get("type") == "assistant_text" for e in events_of(r)),
            what="handoff turn open")
        drop(r, "20260823T151247Z-3c4d5e6f.json", "a message from a babysitter")
        await asyncio.sleep(0.2)
        assert v.injected == [], "handoff wrap-up must not be injected into"
        assert [p.name for p in (r.home / "inbox" / "new").glob("*.json")] == \
            ["20260823T151247Z-3c4d5e6f.json"]
        v.release.set()
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


# --------------------------------------------------------------------------- #
# requeue — every un-claim path moves the message back to inbox/new/
# unchanged (at-least-once).
# --------------------------------------------------------------------------- #
class ExplodingVessel:
    """A turn that dies mid-stream — the crash-requeue path."""

    async def send(self, text):
        yield {"type": "assistant_text", "text": "starting"}
        raise RuntimeError("CLI turn ended badly: error")


def _msg(path: Path) -> dict:
    return json.loads(path.read_text())


class StoppedUnderTurnVessel:
    """A requested stop shuts the CLI while the turn runs: the stream errors."""

    def __init__(self, runner_ref: list):
        self.runner_ref = runner_ref

    async def send(self, text):
        yield {"type": "assistant_text", "text": "working"}
        self.runner_ref[0]._stopping.set()      # socket stop arrived
        raise RuntimeError("CLI stream ended mid-turn")


@pytest.mark.anyio
async def test_stop_induced_failure_leaves_turn_in_feeding(tmp_path,
                                                           fast_poll):
    """A turn the stop itself broke stays in feeding/ (not requeued to new/):
    the handoff/reset can then tell in-turn work from queued work, and the
    next boot still requeues it (at-least-once unchanged)."""
    ref: list = []
    r = make_runner(tmp_path, StoppedUnderTurnVessel(ref))
    ref.append(r)
    drop(r, "20260825T000000-a.json", "the brief")
    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: feed.done(), what="feed loop exits on stop")
    finally:
        r._events_fh.close()
    assert (r.home / "inbox" / "feeding" / "20260825T000000-a.json").exists()
    assert not (r.home / "inbox" / "new" / "20260825T000000-a.json").exists()


@pytest.mark.anyio
async def test_crashed_turn_requeues_unchanged(tmp_path, fast_poll):
    r = make_runner(tmp_path, ExplodingVessel())
    drop(r, "20260825T000000-a.json", "do the thing")
    new = r.home / "inbox" / "new"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: r._stopping.is_set(), what="turn crashed")
        await asyncio.sleep(0.05)
    finally:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    back = new / "20260825T000000-a.json"
    assert back.exists(), "a failed turn is not consumed — it goes back to new/"
    assert _msg(back) == {"text": "do the thing"}
    assert list(new.glob("*.tmp")) == []


def test_boot_requeue_of_stale_feeding_is_unchanged(tmp_path):
    """The predecessor died holding a claimed message; the fresh runner sweeps
    feeding/ back to new/ exactly as it was."""
    r = make_runner(tmp_path, QueueOnlyVessel())
    r._events_fh.close()
    feeding = r.home / "inbox" / "feeding"
    feeding.mkdir(parents=True, exist_ok=True)
    stale = feeding / "20260825T000000-a.json"
    stale.write_text(json.dumps({"text": "mid-flight when it died"}))

    runner_mod.requeue_message(stale, r.home / "inbox" / "new" / stale.name)

    assert not stale.exists()
    back = r.home / "inbox" / "new" / "20260825T000000-a.json"
    assert _msg(back) == {"text": "mid-flight when it died"}


def test_requeue_of_an_unreadable_message_still_moves_it(tmp_path):
    """An unparseable message is moved as-is — never lost."""
    r = make_runner(tmp_path, QueueOnlyVessel())
    r._events_fh.close()
    feeding = r.home / "inbox" / "feeding"
    feeding.mkdir(parents=True, exist_ok=True)
    stale = feeding / "20260825T000000-a.json"
    stale.write_text("{not json at all")

    runner_mod.requeue_message(stale, r.home / "inbox" / "new" / stale.name)

    assert not stale.exists()
    assert (r.home / "inbox" / "new" / stale.name).read_text() == \
        "{not json at all"


# --------------------------------------------------------------------------- #
# Steer-delivery nudge — get through to a WEDGED agent (2026-09-18:
# an ssh that never exited parked the turn inside a bash tool; three steers
# were ACCEPTED by pi (queued) but never delivered, because pi delivers a
# steer only before the next LLM call. Each steer's queue_update also petted
# the vessel stall watchdog, so nothing ever fired.)
# --------------------------------------------------------------------------- #

@pytest.fixture
def fast_nudge(monkeypatch):
    monkeypatch.setattr(runner_mod, "STEER_NUDGE_S", 0.3)


class WedgedBashVessel:
    """pi-rpc wedge shape: a turn parked inside a bash tool that never ends.
    inject() is accepted (queued in the CLI) but delivers only before the
    next LLM call — which the wedge never lets happen."""
    can_inject = True
    can_abort_bash = True
    can_interrupt = True

    def __init__(self, abort_bash_raises: bool = False):
        self.abort_bash_raises = abort_bash_raises
        self.injected: list[str] = []
        self.turns: list[str] = []
        self.bash_aborts = 0
        self.interrupts = 0
        self._freed = asyncio.Event()

    async def send(self, text):
        self.turns.append(text)
        if len(self.turns) == 1:
            yield {"type": "tool_call", "name": "bash", "id": "w1",
                   "args": {"command": "ssh box 'start server'"}}
            await self._freed.wait()            # wedged: silence
            if self.interrupts:
                yield {"type": "done", "reason": "result",
                       "subtype": "interrupted"}
            else:
                yield {"type": "tool_result", "ok": False, "id": "w1",
                       "snippet": "command aborted"}
                yield {"type": "assistant_text",
                       "text": "tool died, answering the steer"}
                yield {"type": "done", "reason": "result"}
        else:
            yield {"type": "assistant_text", "text": f"turn: {text}"}
            yield {"type": "done", "reason": "result"}

    async def inject(self, text):
        self.injected.append(text)              # accepted, parked in the queue

    async def abort_bash(self):
        self.bash_aborts += 1
        if self.abort_bash_raises:
            raise RuntimeError("pi rpc abort_bash rejected: "
                               "no bash command running")
        self._freed.set()

    async def interrupt(self):
        self.interrupts += 1
        self._freed.set()


@pytest.mark.anyio
async def test_steer_nudge_aborts_wedged_bash(tmp_path, fast_poll, fast_nudge):
    """Silence after an accepted steer → abort_bash (NOT a turn interrupt):
    the tool dies, the steer is delivered, the same turn answers it."""
    v = WedgedBashVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260918T043200-a.json", "restart the server")
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: any(e.get("type") == "tool_call"
                                   for e in events_of(r)),
                       what="turn wedged in bash tool")
        drop(r, "20260918T050051-b.json", "status update?")
        await wait_for(lambda: v.bash_aborts == 1,
                       what="abort_bash sent after silent window")
        await wait_for(lambda: len(list(acked.glob("*.json"))) == 2,
                       what="both messages acked at the boundary")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.interrupts == 0, "the scalpel must come before the sledgehammer"
    assert v.injected == ["status update?"]
    evs = events_of(r)
    idx_steer = next(i for i, e in enumerate(evs)
                     if e.get("type") == "user_message" and e.get("injected"))
    idx_reply = next(i for i, e in enumerate(evs)
                     if e.get("text") == "tool died, answering the steer")
    assert idx_steer < idx_reply, "the freed steer must land before the reply"


@pytest.mark.anyio
async def test_nudge_escalates_to_interrupt_when_abort_bash_fails(
        tmp_path, fast_poll, fast_nudge):
    """abort_bash rejected (wedge is not bash / unsupported build) → full
    turn interrupt, and the injected steer is REQUEUED so it gets its own
    turn instead of a silent boundary ack (message loss is the wrong
    failure mode)."""
    v = WedgedBashVessel(abort_bash_raises=True)
    r = make_runner(tmp_path, v)
    drop(r, "20260918T043200-a.json", "restart the server")
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: any(e.get("type") == "tool_call"
                                   for e in events_of(r)),
                       what="turn wedged in bash tool")
        drop(r, "20260918T050051-b.json", "status update?")
        await wait_for(lambda: v.interrupts == 1,
                       what="escalated to turn interrupt")
        await wait_for(lambda: v.turns == ["restart the server",
                                           "status update?"]
                       and len(list(acked.glob("*.json"))) == 2,
                       what="requeued steer dispatched as its own turn")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.bash_aborts == 1, "abort_bash was tried exactly once"
    assert v.injected == ["status update?"]
    assert v.turns[-1] == "status update?"


@pytest.mark.anyio
async def test_no_nudge_while_the_turn_streams(tmp_path, fast_poll, fast_nudge):
    """A LIVE turn (events flowing — model thinking, tool partials) must not
    be nudged: the steer will be delivered at the next LLM call anyway."""
    class StreamingVessel:
        can_inject = True
        can_abort_bash = True

        def __init__(self):
            self.injected = []
            self.bash_aborts = 0

        async def send(self, text):
            for i in range(20):
                await asyncio.sleep(0.05)
                yield {"type": "assistant_delta", "text": "x"}
            yield {"type": "assistant_text", "text": "streamed the reply"}
            yield {"type": "done", "reason": "result"}

        async def inject(self, text):
            self.injected.append(text)

        async def abort_bash(self):
            self.bash_aborts += 1

    v = StreamingVessel()
    r = make_runner(tmp_path, v)
    drop(r, "20260918T043200-a.json", "long but healthy turn")
    acked = r.home / "inbox" / "acked"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: any(e.get("type") == "user_message"
                                   for e in events_of(r)),
                       what="turn streaming")
        drop(r, "20260918T044616-b.json", "we should keep latency low too")
        await asyncio.sleep(1.0)   # >> STEER_NUDGE_S=0.3, but events flow
        assert v.bash_aborts == 0, "a streaming turn must not be nudged"
        await wait_for(lambda: len(list(acked.glob("*.json"))) == 2,
                       what="turn finished and both messages acked")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


def test_user_event_carries_reply_to_and_display_text():
    """Reply button: the chat event shows the user's clean text and the quote
    (reply_to), never the quote block the vessel received."""
    msg = {"text": "[The user is replying…]\n> old\n\nyes", "display_text": "yes",
           "reply_to": {"text": "old", "ts": "2026-09-30T10:00:00Z"}}
    ev = Runner._user_event(None, msg, "m.json")
    assert ev == {"type": "user_message", "text": "yes", "msg_file": "m.json",
                  "reply_to": {"text": "old", "ts": "2026-09-30T10:00:00Z"}}
    assert "reply_to" not in Runner._user_event(None, {"text": "hi"}, "n.json")


def test_user_event_copies_r26_origin_keys_when_present():
    """The runner copies origin/via/from_* as written; no key, no change."""
    from pulsar.daemon import mailbox
    keys = mailbox.origin_keys("peer", "pulse", from_pulse_id="p1",
                               from_title="Boss")
    ev = Runner._user_event(None, {"text": "hi", **keys}, "m.json")
    assert ev == {"type": "user_message", "text": "hi", "msg_file": "m.json",
                  "origin": "peer", "via": "pulse",
                  "from_pulse_id": "p1", "from_title": "Boss"}
    # Old files without the keys: byte-identical to today's event.
    assert Runner._user_event(None, {"text": "hi"}, "m.json") == {
        "type": "user_message", "text": "hi", "msg_file": "m.json"}


def test_user_event_origin_keys_copy_on_presence_not_truthiness():
    msg = {"text": "hi", "origin": "peer", "via": "pulse",
           "from_pulse_id": "p1", "from_title": ""}
    ev = Runner._user_event(None, msg, "m.json")
    assert ev["from_title"] == ""


@pytest.mark.parametrize("origin,via", [("user", "inbox_reply"),
                                        ("user", "question")])
def test_user_event_keeps_origin_and_via_for_user_producers(origin, via):
    """Only `via` separates an Inbox reply from a question answer."""
    from pulsar.daemon import mailbox
    msg = {"text": "yes", **mailbox.origin_keys(origin, via)}
    ev = Runner._user_event(None, msg, "m.json")
    assert (ev["origin"], ev["via"]) == (origin, via)
    assert "from_pulse_id" not in ev and "from_title" not in ev


@pytest.mark.anyio
async def test_handoff_seed_turn_is_steerable(tmp_path, fast_poll):
    """Only the wrap-up REQUEST is unsteerable. The successor's seed turn is
    ordinary work: a human message during it must interrupt as usual
    (handoff review 2026-10-02: seed turns delayed messages ~4.5 min)."""
    from pulsar.runner.runner import Runner
    assert Runner._is_handoff(Path("handoff-20260823T151204Z-2b3c4d5e.json"))
    assert not Runner._is_handoff(Path("handoff-seed-20260823T151204Z-e6.json"))
    assert not Runner._is_handoff(Path("20260823T151204Z-2b3c4d5e.json"))

    v = InterruptSteerVessel()
    r = make_runner(tmp_path, v)
    drop(r, "handoff-seed-20260823T151204Z-2b3c4d5e.json", "seed: continue")

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: v.turns == ["seed: continue"],
                       what="seed turn open")
        drop(r, "20260823T151247Z-3c4d5e6f.json", "human message")
        await wait_for(lambda: v.interrupts >= 1, what="seed turn interrupted")
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


@pytest.mark.anyio
async def test_nudge_requeue_then_send_failure_requeues_once(
        tmp_path, fast_poll, fast_nudge):
    """Live 2026-10-09 pulse 4d5e6f70: the nudge requeued 4 injected steers
    to new/ and interrupted, pi ignored the abort (stuck in a provider retry
    sleep), and the 1200 s stall kill then failed vessel.send. The failure
    path requeued the same 4 AGAIN from feeding/ — "requeue ... unreadable
    ([Errno 2] ...)" — because they were still in the turn's pending set.
    Each message must be requeued exactly once and end up in new/."""
    class IgnoresAbortVessel(WedgedBashVessel):
        async def interrupt(self):
            self.interrupts += 1               # accepted, turn never ends

        async def send(self, text):
            self.turns.append(text)
            yield {"type": "tool_call", "name": "bash", "id": "w1",
                   "args": {"command": "ssh box 'start server'"}}
            await self._freed.wait()
            raise RuntimeError("CLI turn ended badly: exited")

    v = IgnoresAbortVessel(abort_bash_raises=True)
    r = make_runner(tmp_path, v)
    drop(r, "20261010T000000-a.json", "do the work")
    new = r.home / "inbox" / "new"

    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: any(e.get("type") == "tool_call"
                                   for e in events_of(r)),
                       what="turn wedged")
        drop(r, "20261010T000001-b.json", "steer one")
        drop(r, "20261010T000002-c.json", "steer two")
        await wait_for(lambda: v.interrupts == 1, what="nudge interrupt")
        await wait_for(lambda: (new / "20261010T000002-c.json").exists(),
                       what="steers requeued by the nudge")
        v._freed.set()                         # the stall kill
        await wait_for(lambda: r._stopping.is_set(), what="send failed")
        await asyncio.sleep(0.05)
    finally:
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()

    assert v.injected == ["steer one", "steer two"]
    assert sorted(p.name for p in new.glob("*.json")) == [
        "20261010T000000-a.json", "20261010T000001-b.json",
        "20261010T000002-c.json"]
    assert list((r.home / "inbox" / "feeding").glob("*.json")) == []


@pytest.mark.anyio
async def test_provider_error_is_the_heartbeat_reason_until_next_turn(
        tmp_path, fast_poll):
    """A pi turn that ends on a provider error (Z.ai 429 usage limit) shows
    the provider's text as the heartbeat reason; the next turn clears it."""
    reason = ("provider error: stop_reason=error from zai/glm-5.3 — 429 "
              "[1308][Usage limit reached for 5 hour.]")

    class ProviderErrorVessel:
        def __init__(self):
            self.turns: list[str] = []

        async def send(self, text):
            self.turns.append(text)
            if len(self.turns) == 1:
                yield {"type": "runner_error", "where": "provider_error",
                       "reason": reason, "error": "429 ..."}
            else:
                yield {"type": "assistant_text", "text": "ok"}
            yield {"type": "done", "reason": "result"}

    v = ProviderErrorVessel()
    r = make_runner(tmp_path, v)
    beat = lambda: json.loads((r.home / "heartbeat.json").read_text())
    drop(r, "20261010T000000-a.json", "first")
    feed = asyncio.create_task(r.feed_loop())
    try:
        await wait_for(lambda: len(v.turns) == 1 and r.state == "idle",
                       what="first turn over")
        assert beat()["reason"] == reason
        assert any(e.get("where") == "provider_error" for e in events_of(r))
        drop(r, "20261010T000001-b.json", "second")
        await wait_for(lambda: len(v.turns) == 2 and r.state == "idle",
                       what="second turn over")
        assert beat()["reason"] is None
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()


# --------------------------------------------------------------------------- #
# Provider-error turn counter — the heartbeat input of daemon/failover.py
# --------------------------------------------------------------------------- #
class ScriptedTurnsVessel:
    """Turn N yields ``script[N]`` (a list of events), then done/result
    unless the script already ended the turn."""

    def __init__(self, script):
        self.script = script
        self.turns: list[str] = []

    async def send(self, text):
        self.turns.append(text)
        evs = self.script[len(self.turns) - 1]
        for ev in evs:
            yield ev
        if not any(ev.get("type") == "done" for ev in evs):
            yield {"type": "done", "reason": "result"}


PI_ERR = {"type": "runner_error", "where": "provider_error",
          "reason": "provider error: 503", "error": "503"}
CLAUDE_ERR = {"type": "done", "reason": "error", "subtype": "success"}
GOOD = {"type": "assistant_text", "text": "ok"}


async def _run_turns(tmp_path, script):
    v = ScriptedTurnsVessel(script)
    r = make_runner(tmp_path, v)
    beats = []
    feed = asyncio.create_task(r.feed_loop())
    try:
        for i in range(len(script)):
            drop(r, f"20261014T00000{i}-m.json", f"msg {i}")
            await wait_for(lambda: len(v.turns) == i + 1 and r.state == "idle",
                           what=f"turn {i} over")
            beats.append(json.loads((r.home / "heartbeat.json").read_text()))
    finally:
        r._stopping.set()
        feed.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await feed
        r._events_fh.close()
    return beats


@pytest.mark.anyio
async def test_two_provider_error_turns_in_a_row_reach_the_heartbeat(
        tmp_path, fast_poll):
    beats = await _run_turns(tmp_path, [[PI_ERR], [CLAUDE_ERR]])
    assert beats[0]["provider_error_turns"] == 1
    assert beats[0]["provider_error_msg"] == "20261014T000000-m.json"
    assert beats[1]["provider_error_turns"] == 2
    assert beats[1]["provider_error_msg"] == "20261014T000001-m.json"


@pytest.mark.anyio
async def test_a_good_turn_resets_the_provider_error_count(tmp_path,
                                                          fast_poll):
    beats = await _run_turns(tmp_path, [[PI_ERR], [GOOD], [PI_ERR]])
    assert [b["provider_error_turns"] for b in beats] == [1, 0, 1]
    assert beats[1]["provider_error_msg"] is None


@pytest.mark.anyio
async def test_interrupted_and_local_parse_errors_are_not_provider_errors(
        tmp_path, fast_poll):
    beats = await _run_turns(tmp_path, [
        [{"type": "done", "reason": "error", "subtype": "interrupted"}],
        [{"type": "done", "reason": "error", "subtype": "malformed_record"}],
    ])
    assert [b["provider_error_turns"] for b in beats] == [0, 0]
