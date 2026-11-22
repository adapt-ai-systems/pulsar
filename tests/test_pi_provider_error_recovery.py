"""Runner + failover regression tests for the provisional provider-error
hold (2026-10-23).

A compaction-recovered turn (context overflow -> error message_end ->
compaction -> retry -> the answer arrives) must NOT count as a provider
error: the runner heartbeat stays at provider_error_turns 0 and the
failover monitor leaves a running pi pulse alone (pre-fix evidence:
provider_error_turns [1,2], failover, message re-sent even though
RECOVERED_AFTER_COMPACTION text was present).
"""

from __future__ import annotations

import json

import pytest

from pulsar.daemon import paths
from tests.test_daemon import (  # noqa: F401
    data_root, fake_sock, fake_spawn, store)
from tests.test_daemon_failover import _mk, _monitor, quick_handoff  # noqa: F401
from tests.test_runner_steering import _run_turns, fast_poll

pytestmark = pytest.mark.anyio

# POST-FIX vessel event stream of a recovered compaction turn (captured
# RPC shape, minus the runner_error the provisional hold now swallows):
# the overflow attempt's usage, the recovered answer, the retry's usage,
# and the single done.
RECOVERED_TURN = [
    {"type": "usage", "in": 0, "out": 0, "ctx": 0},
    {"type": "assistant_delta", "text": "RECOVERED_AFTER_COMPACTION"},
    {"type": "assistant_text", "text": "RECOVERED_AFTER_COMPACTION"},
    {"type": "usage", "in": 1004, "out": 20, "ctx": 1024},
    {"type": "done", "exit_code": 0, "reason": "result"},
]


async def test_recovered_compaction_turns_do_not_count_as_provider_errors(
        tmp_path, fast_poll):
    beats = await _run_turns(tmp_path, [RECOVERED_TURN, RECOVERED_TURN])
    assert [b["provider_error_turns"] for b in beats] == [0, 0]
    assert beats[-1]["provider_error_msg"] is None
    assert beats[-1]["reason"] is None


async def test_recovered_compaction_turns_do_not_trigger_failover(
        tmp_path, fast_poll, store, fake_spawn, fake_sock, quick_handoff):
    beats = await _run_turns(tmp_path / "replay",
                             [RECOVERED_TURN, RECOVERED_TURN])
    assert [b["provider_error_turns"] for b in beats] == [0, 0]
    p = _mk(store, state="running", stop_reason=None, model="pi-glm53-full",
            vessel="pi")
    fake_spawn.units.append(p.id)
    home = paths.pulse_home(p.id)
    home.mkdir(parents=True, exist_ok=True)
    # The heartbeat of a fully recovered turn: no provider-error count and
    # no message name to re-send.
    (home / "heartbeat.json").write_text(json.dumps(beats[-1]))
    moved = await _monitor(store).tick()
    assert moved == 0
    assert store.get_pulse(p.id).model == "pi-glm53-full"
    inbox_new = home / "inbox" / "new"
    assert not inbox_new.exists() or not any(inbox_new.iterdir())
