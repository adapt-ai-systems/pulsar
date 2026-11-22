"""Normalized vessel event schema (Vessel contract).

The vessel yields exactly these event kinds. They are provider-neutral: a
consumer (runner/daemon/UI) never parses raw Claude stream-json — it iterates
``VesselEvent`` objects.

Event kinds and their payloads:

- ``assistant_text``   {"text": str}
      A chunk of visible assistant prose. Emitted for each assistant message
      block that carries text (whether or not the same turn also calls tools).
- ``assistant_delta``  {"text": str}
      A streaming chunk of visible assistant prose. Ephemeral: the runner
      folds these into ``partial.json`` (the pane's typing ghost) and they
      NEVER reach ``events.jsonl``.
- ``reasoning_delta``  {"text": str}
      A streaming chunk of the model's reasoning / thinking trace. Ephemeral:
      the runner folds these into ``reasoning.json`` (the pane's reasoning
      panel) and they NEVER reach ``events.jsonl``. Only vessels whose model
      emits a reasoning channel produce these. The runner (not the vessel)
      writes one durable ``reasoning`` {"text": str} event per finished
      thinking block — the chat's collapsed thinking card — capped at
      ``Runner.REASONING_EVENT_MAX_CHARS`` and emitted just before the
      assistant_text / tool_call / done that closed the block.
- ``hidden_delta``     {"text": str}
      Generated tokens that are neither visible prose nor a readable
      reasoning trace. Fed to the speed meter only; never displayed.
- ``code_delta``       {"text": str, "name": str | None, "id": str | None}
      A streaming chunk of a tool call's argument JSON (the code a Write /
      Edit is typing, a Bash command, ...). Ephemeral: the runner meters it
      and folds it into ``code.json`` (the pane's live code card); it NEVER
      reaches ``events.jsonl`` — the finished ``tool_call`` supersedes it.
      ``id`` / ``name`` name the call when the vessel knows them; a chunk
      with a new ``id`` starts a new call, one without continues the last.

- ``tool_call``        {"name": str, "args": dict, "description": str | None,
                        "id": str | None}
      The model invoked a tool. ``description`` is preserved verbatim from the
      tool_use block when present (Bash/Task carry it; Pulsar's UI renders it).
- ``tool_result``      {"ok": bool, "snippet": str, "exit_code": int | None,
                        "id": str | None}
      A tool returned. ``ok`` is False when the CLI marked the result an error.
      ``snippet`` is the (truncated) textual result. ``exit_code`` is parsed
      from Bash results when the CLI exposes it, else None.
- ``usage``            {"in": int, "out": int, "ctx": int}
      Token accounting for the latest assistant message. ``in`` = input +
      cache-read + cache-creation tokens, ``out`` = output tokens, ``ctx`` =
      their sum (the running context total).
- ``runner_error``     {"where": str, "reason": str}
      A loud, non-fatal failure the vessel can name (e.g. pi "output_cap":
      the model hit max_tokens mid-reasoning and the turn ended with no
      reply). Rendered red in the chat pane and activity feed.
- ``done``             {"exit_code": int | None, "reason": str}
      The turn/session reached a boundary. ``reason`` is "result" for a normal
      per-turn completion (stream-json ``result`` line), "exited" when the CLI
      process terminated, or "error" for a CLI-reported error result.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

VesselEventKind = Literal[
    "assistant_text",
    "assistant_delta",
    "reasoning_delta",
    "hidden_delta",
    "code_delta",
    "tool_call",
    "tool_result",
    "usage",
    "metrics",
    "done",
    "runner_error",
]


@dataclass
class VesselEvent:
    kind: VesselEventKind
    payload: dict[str, Any]
    timestamp: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
