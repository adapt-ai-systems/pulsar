"""Claude stream-json line -> normalized VesselEvent classification.

Ported from an earlier project. Pulsar's vessel parses a single stream: the
CLI's stdout NDJSON, which in ``--verbose`` / ``--replay-user-messages`` mode
carries the full ``assistant`` / ``user`` / ``result`` records plus the
``result`` boundary line, so one classifier covers both.
"""

from __future__ import annotations

from typing import Any

from pulsar.vessel.base import as_int
from pulsar.vessel.events import VesselEvent

# Cap for tool_result snippets so a multi-megabyte tool output (a big file read,
# a long Bash log) never balloons a single event; Pulsar's event stream is for
# display, so truncate.
_SNIPPET_MAX = 4000


def _usage_total(usage: dict[str, Any]) -> tuple[int, int, int]:
    """(in, out, ctx) from a Claude usage block.

    ``in`` folds cache-read + cache-creation into input, summing all four
    counters for the running context total while splitting input vs output.
    """
    tin = (
        as_int(usage.get("input_tokens"))
        + as_int(usage.get("cache_read_input_tokens"))
        + as_int(usage.get("cache_creation_input_tokens"))
    )
    tout = as_int(usage.get("output_tokens"))
    return tin, tout, tin + tout


def _tool_result_text(block: dict[str, Any]) -> str | None:
    """Extract the textual body of a tool_result block.

    The content is EITHER a bare string OR a list of ``{"type": "text",
    "text": ...}`` blocks. Anything else -> None.
    """
    content = block.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts: list[str] = []
        for inner in content:
            if isinstance(inner, dict) and inner.get("type") == "text":
                text = inner.get("text")
                if isinstance(text, str):
                    parts.append(text)
        if parts:
            return "\n".join(parts)
    return None


def _snippet(text: str | None) -> str:
    if not text:
        return ""
    text = text.strip()
    if len(text) > _SNIPPET_MAX:
        return text[:_SNIPPET_MAX] + "\n… (truncated)"
    return text


def _classify_record(record: dict[str, Any]) -> list[VesselEvent]:
    """Classify one validated stream-json record into normalized events."""
    events: list[VesselEvent] = []
    rtype = record.get("type")

    if rtype == "assistant":
        msg = record.get("message")
        if msg is None:
            msg = {}
        if not isinstance(msg, dict):
            raise TypeError("assistant message is not an object")
        content = msg.get("content")
        blocks = content if isinstance(content, list) else []

        for block in blocks:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "text":
                text = block.get("text")
                # Guard on truthy text — empty text blocks are not surfaced.
                if isinstance(text, str) and text.strip():
                    events.append(
                        VesselEvent(kind="assistant_text", payload={"text": text})
                    )
            elif btype == "tool_use":
                # Preserve the tool_use "description" field verbatim. Bash/Task
                # calls carry it and Pulsar's UI renders it (vessel contract).
                # It is NOT part of "input"; it's a sibling key on the tool_use
                # block, so read it from the block, not args.
                name = block.get("name")
                args = block.get("input")
                events.append(
                    VesselEvent(
                        kind="tool_call",
                        payload={
                            "name": name if isinstance(name, str) else "",
                            "args": args if isinstance(args, dict) else {},
                            "description": block.get("description"),
                            "id": block.get("id"),
                        },
                    )
                )

        # Usage: emit whenever the assistant message carries a usage block.
        usage = msg.get("usage")
        if isinstance(usage, dict) and usage:
            tin, tout, ctx = _usage_total(usage)
            events.append(
                VesselEvent(kind="usage", payload={"in": tin, "out": tout, "ctx": ctx})
            )
        return events

    if rtype == "user":
        # Tool results come back as user-role records whose content is a list of
        # tool_result blocks. One event per block.
        msg = record.get("message")
        if msg is None:
            msg = {}
        if not isinstance(msg, dict):
            raise TypeError("user message is not an object")
        content = msg.get("content")
        if not isinstance(content, list):
            return events
        for block in content:
            if not isinstance(block, dict) or block.get("type") != "tool_result":
                continue
            is_error = block.get("is_error") is True
            text = _tool_result_text(block)
            payload: dict[str, Any] = {
                "ok": not is_error,
                "snippet": _snippet(text),
                "exit_code": _bash_exit_code(block),
                "id": block.get("tool_use_id"),
            }
            events.append(VesselEvent(kind="tool_result", payload=payload))
        return events

    if rtype == "stream_event":
        # --include-partial-messages partials: the CLI wraps raw Anthropic SSE
        # events. Visible text becomes assistant_delta (the pane's typing ghost);
        # thinking_delta becomes reasoning_delta (the reasoning panel); tool-arg
        # JSON becomes code_delta (the live code card). Deltas never reach events.jsonl — the runner folds
        # them into the ephemeral partial.json / reasoning.json instead (see
        # runner.Runner._on_delta / _on_reasoning_delta).
        inner = record.get("event")
        if (isinstance(inner, dict)
                and inner.get("type") == "content_block_start"):
            # A tool_use block opening names the call its input_json_delta
            # chunks belong to (the pane's live code card).
            cb = inner.get("content_block")
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                name = cb.get("name")
                events.append(VesselEvent(kind="code_delta", payload={
                    "text": "",
                    "name": name if isinstance(name, str) else None,
                    "id": cb.get("id") if isinstance(cb.get("id"), str) else None,
                }))
            return events
        if isinstance(inner, dict) and inner.get("type") == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = delta.get("text")
                if isinstance(text, str) and text:
                    events.append(
                        VesselEvent(kind="assistant_delta", payload={"text": text})
                    )
            elif (isinstance(delta, dict)
                  and delta.get("type") == "thinking_delta"):
                think = delta.get("thinking")
                if isinstance(think, str) and think:
                    events.append(
                        VesselEvent(kind="reasoning_delta", payload={"text": think})
                    )
            elif (isinstance(delta, dict)
                  and delta.get("type") == "input_json_delta"):
                chunk = delta.get("partial_json")
                if isinstance(chunk, str) and chunk:
                    events.append(VesselEvent(
                        kind="code_delta",
                        payload={"text": chunk, "name": None, "id": None}))
        return events

    if rtype == "result":
        # Per-turn / session boundary line the CLI prints on stdout after each
        # user turn completes. subtype "success" is a
        # clean turn end; an error subtype or is_error=True is a CLI-reported
        # failure.
        subtype = str(record.get("subtype", ""))
        is_error = bool(record.get("is_error")) or subtype.startswith("error")
        payload = {
            "exit_code": None,
            "reason": "error" if is_error else "result",
            "subtype": subtype,
            "errors": record.get("errors") or [],
        }
        # The result's usage sums the whole turn; thinking_tokens is the
        # billed hidden reasoning. Assistant records only
        # carry the stream's opening usage, so this is the one exact count.
        usage = record.get("usage")
        details = usage.get("output_tokens_details") if isinstance(usage, dict) else None
        think = details.get("thinking_tokens") if isinstance(details, dict) else None
        if isinstance(think, int) and not isinstance(think, bool):
            payload["thinking_tokens"] = think
        events.append(VesselEvent(kind="done", payload=payload))
        return events

    return events


def _classification_error(error: Exception) -> VesselEvent:
    message = str(error).strip() or type(error).__name__
    return VesselEvent(
        kind="done",
        payload={
            "exit_code": None,
            "reason": "error",
            "subtype": "malformed_record",
            "message": message[:500],
        },
    )


def classify_record(record: Any) -> list[VesselEvent]:
    """Classify one record without allowing malformed input to escape.

    Claude's stream is provider input, not a trusted internal object. A valid
    JSON object with an unexpected nested shape must end the current turn with a
    normalized error, while the stdout pump remains alive to drain later data.
    """
    if not isinstance(record, dict):
        return [_classification_error(TypeError("record is not an object"))]
    try:
        return _classify_record(record)
    except Exception as exc:
        return [_classification_error(exc)]


def _bash_exit_code(block: dict[str, Any]) -> int | None:
    """Best-effort exit code for a Bash tool_result.

    The stream-json tool_result block does not carry a structured exit
    code; Claude appends it into the text tail. We parse it opportunistically and
    return None when absent — the boring, documented-as-optional path per the
    contract (exit_code?).
    """
    text = _tool_result_text(block)
    if not text:
        return None
    # Claude's Bash tool appends a line like "Exit code: 1" on non-zero exits.
    # Walk newline boundaries backwards without retaining every preceding line.
    end = len(text)
    while end:
        newline = text.rfind("\n", 0, end)
        line = text[newline + 1:end]
        line = line.strip()
        low = line.lower()
        if low.startswith("exit code:"):
            tail = line.split(":", 1)[1].strip()
            try:
                return int(tail)
            except ValueError:
                return None
        if newline < 0:
            break
        end = newline
    return None
