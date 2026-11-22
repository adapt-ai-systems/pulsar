"""Auto-handoff engine + monitor.

When a pulse's running context crosses its threshold, hand off to a fresh CLI
session that keeps the pulse's title, tile, grid, loop, and settings. The same
engine powers cross-vessel model switches (claude -> codex needs a context
handoff, not a flag flip).

Ported as a concept from an earlier project, rebuilt small: no event bus —
the pulse itself writes its handoff prose, the daemon does the plumbing. All state on disk:

  <pulse home>/handoffs/<n>.md        the handoff texts, numbered, durable
  <pulse home>/handoff_state.json     in-flight marker {phase, started_at, ...}
                                      so a daemon restart never double-fires

Flow (run_handoff):
  1. mark "requested", drop a handoff-request message into inbox/new
     (handoff- filename prefix, same atomic tmp+rename as every other writer).
     The runner treats handoff- messages as priority AND injects them into a
     live turn mid-stream (runner steering, 2026-07-27), so a busy pulse sees
     the request within seconds, not after its current turn ends.
  2. wait for the pulse's reply by reading events.jsonl forward from its size
     at request time — the assistant text between our user_message and its
     done IS the handoff prompt (re-asked once if it lacks the sections)
  3. stop the runner (socket stop, systemd fallback), save handoffs/<n>.md
  4. respawn FRESH (resume=None) — optionally with a new model/vessel —
     and seed the successor's first message with the handoff text
  5. mark "done", stamp settings.last_handoff_at (the monitor's cooldown)

Every phase logs one timestamped line; every failure path marks the state file
and logs loudly (standing rule — no silent fail/wait/retry).

Threshold precedence: pulse.handoff_tokens (set at creation / settings) beats
the per-model-family defaults table, which beats the fallback.

Package layout: prompts (texts), state (marker, ledger, cooldown, thresholds),
seed (successor seeds), engine (run_handoff), monitor (warn / due / tick).
"""

from __future__ import annotations

# Submodules last-bound: tests and callers patch/read through them
# (handoff.engine.run_handoff, handoff.state.IN_FLIGHT_STALE_S,
# handoff.monitor.tick, ...), so those names are NOT re-exported here — a
# patch on this package would silently miss the module that reads it.
from pulsar.daemon.handoff import engine, monitor, prompts, seed, state
from pulsar.daemon.handoff.engine import extract_reply
from pulsar.daemon.handoff.monitor import (
    TICK_SECONDS,
    drain,
    over_threshold,
    warn_pulses,
)
from pulsar.daemon.handoff.prompts import (
    HANDOFF_REQUEST,
    handoff_prompt_for,
    notes_path,
    successor_prefix_for,
)
from pulsar.daemon.handoff.seed import (
    _is_human,
    carry_over_block,
    forced_seed,
    read_note_file,
)
from pulsar.daemon.handoff.state import (
    ledger_path,
    read_state,
    reserve,
    threshold_for,
)

__all__ = [
    "engine", "monitor", "prompts", "seed", "state",
    "HANDOFF_REQUEST", "TICK_SECONDS",
    "_is_human", "carry_over_block", "drain", "extract_reply", "forced_seed",
    "handoff_prompt_for", "ledger_path", "notes_path",
    "over_threshold", "read_note_file", "read_state", "reserve",
    "successor_prefix_for", "threshold_for", "warn_pulses",
]
