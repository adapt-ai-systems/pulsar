"""Per-call speed meter for a pulse (tokens/s, prefill speed, and per-call stats).

A CALL is one model request inside a turn: it starts when the user message is
sent or when a tool result goes back to the model, and ends at the next
tool_call or the turn's done. The meter watches the runner's own event stream
(no vessel-specific hooks) and emits one ``metrics`` event per call::

    {"type": "metrics", "ttft_s": 0.9, "gen_s": 12.4, "call_s": 13.3,
     "in_tok": 41200, "out_tok": 480, "out_est": false, "out_chars": 1900}

- ``ttft_s``  first streamed token after the call started (None when the
              vessel does not stream deltas — codex, qwen http, deepseek)
- ``gen_s``   first token → end of call (tool-call JSON is generated but not
              streamed, so the span runs to the boundary, not the last delta)
- ``in_tok``  the call's input as the vessel reported it (context re-read;
              for Claude this includes cache reads)
- ``out_tok`` the vessel's own output count when it reported exactly one
              usable usage for the call; otherwise estimated from streamed
              characters (~4 per token) and flagged ``out_est``

Speeds are left to the UI: gen t/s = out_tok / gen_s, prefill t/s =
in_tok / ttft_s.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Optional

CHARS_PER_TOKEN = 4.0
MIN_REAL_OUT = 8  # a usage "out" this small is a message-start stub (Claude emits out=3)

Clock = Callable[[], float]


class CallMeter:
    """Vessels disagree on WHEN usage arrives: Claude reports it at message
    start (before any output), pi reports it after the tool RESULT, Codex
    trails it after done. So a finished call ("prev") stays open until the
    next call shows its first output: a usage that arrives before that
    belongs to prev. prev is closed (metrics emitted) on the next call's
    first delta, on a usage, on the next boundary, or on start().

    A parallel batch (pi runs every tool call of one message at once) ends
    at its LAST result: the next call starts then, not at the first result.
    Ending it at the first result timed the slowest tool as a phantom model
    call and gave that phantom the step's usage, which pi sends after all
    results — so the real call got in_tok=None (G9; DS off R1: 28 of 160)."""

    def __init__(self, clock: Clock = time.monotonic) -> None:
        self._clock = clock
        self._live = None  # the call in flight
        self._prev = None  # a finished call waiting for trailing usage
        self._pending: list = []  # tool_call ids of the open batch, no result yet
        self._batch_result = False  # a result of the open batch arrived

    # -- lifecycle ---------------------------------------------------------- #
    def start(self) -> Optional[dict[str, Any]]:
        """A model call begins now (message sent). Returns the metrics of a
        call held open since the last done, if any."""
        m = self._close_prev()
        self._live = self._new_call()
        self._pending, self._batch_result = [], False
        return m

    def delta(self, text: str) -> Optional[dict[str, Any]]:
        """Streamed output. Returns prev's metrics when this is the new
        call's first output (prev can take no more usage)."""
        if self._live is None:
            return None
        m = None
        if self._live["first"] is None:
            self._live["first"] = self._clock()
            m = self._close_prev()
        self._live["chars"] += len(text or "")
        return m

    def observe(self, event: dict[str, Any]) -> Optional[dict[str, Any]]:
        """Feed every emitted event BEFORE it is written. Returns a metrics
        event when a call closes, else None."""
        t = event.get("type")
        if t == "usage":
            target = self._prev if (self._prev is not None and (self._live is None or self._live["chars"] == 0)) else self._live
            if target is None:
                return None
            tin, tout = event.get("in"), event.get("out")
            if isinstance(tin, int):
                target["in"] = tin
            if isinstance(tout, int) and tout >= MIN_REAL_OUT:
                target["out"] += tout
                target["out_n"] += 1
            return self._close_prev() if target is self._prev else None
        if t == "tool_call":
            if self._batch_result:  # a new message: the old batch is over
                self._pending, self._batch_result = [], False
            if event.get("id") is not None:
                self._pending.append(event.get("id"))
            m = self._close_prev()
            if self._live is not None and self._live["end"] is None:
                self._live["end"] = self._clock()
            return m
        if t == "tool_result":
            rid = event.get("id")
            if rid is not None and rid in self._pending:
                self._pending.remove(rid)
                self._batch_result = True
                if self._pending:
                    return None  # the batch still runs: the model waits too
            self._pending, self._batch_result = [], False
            # The model reads this next: the finished call becomes prev and a
            # new call starts. (prev may still be waiting: close it first.)
            m = self._close_prev()
            if self._live is not None:
                if self._live["end"] is None:
                    self._live["end"] = self._clock()
                self._prev = self._live
            self._live = self._new_call()
            return m
        if t == "done":
            self._pending, self._batch_result = [], False
            m = self._close_prev()
            if self._live is not None:
                if self._live["end"] is None:
                    self._live["end"] = self._clock()
                self._prev = self._live  # held for trailing usage / closed at next start
                self._live = None
                if self._has_content(self._prev):
                    m2 = self._close_prev()
                    return m2 if m is None else m  # (m is None here in practice)
            return m
        return None

    # -- internals ---------------------------------------------------------- #
    def _new_call(self) -> dict[str, Any]:
        return {"start": self._clock(), "first": None, "end": None, "chars": 0,
                "in": None, "out": 0, "out_n": 0}

    @staticmethod
    def _has_content(c: dict[str, Any]) -> bool:
        return c["chars"] > 0 or c["out_n"] > 0

    def _close_prev(self) -> Optional[dict[str, Any]]:
        c = self._prev
        self._prev = None
        if c is None or not self._has_content(c):
            return None
        end = c["end"] if c["end"] is not None else self._clock()
        call_s = max(0.0, end - c["start"])
        ttft = None if c["first"] is None else max(0.0, c["first"] - c["start"])
        gen_s = call_s if c["first"] is None else max(0.0, end - c["first"])
        if c["out_n"] == 1:
            out_tok, out_est = c["out"], False
        elif c["chars"] > 0:
            out_tok, out_est = max(1, round(c["chars"] / CHARS_PER_TOKEN)), True
        else:
            out_tok, out_est = c["out"], False
        return {
            "type": "metrics",
            "ttft_s": None if ttft is None else round(ttft, 3),
            "gen_s": round(gen_s, 3),
            "call_s": round(call_s, 3),
            "in_tok": c["in"],
            "out_tok": out_tok,
            "out_est": out_est,
            "out_chars": c["chars"],
        }
