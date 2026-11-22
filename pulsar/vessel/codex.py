"""Standalone Codex CLI adapter for Pulsar.

Drives a persistent ``codex app-server --listen stdio://`` child over JSON-RPC
and yields normalized :class:`~pulsar.vessel.events.VesselEvent` objects. Like
:mod:`pulsar.vessel.claude`, this vessel contains NO agent loop, MCP client, or
tool dispatcher — the ``codex`` CLI owns those. Pulsar owns one long-lived
app-server process per session, drives ``thread``/``turn`` JSON-RPC requests over
stdin, and observes notifications on stdout.

Ported from an earlier project and stripped to a standalone adapter. This
module imports nothing outside the stdlib + ``pulsar.*``.

Isolation is NOT this module's job: a clean pulse's ``CODEX_HOME`` (and the
empty ~/.agents bind that goes with it) is built by
:mod:`pulsar.launch.diet` and arrives here as ``env`` on :meth:`spawn`. This
module just passes it to the child.

Why the app-server (not ``codex exec``): the app-server is a warm, persistent
process. ``spawn`` starts a session + first turn; ``send`` starts later turns on
the SAME process; ``turn/completed`` reopens the input boundary. That mirrors the
Claude vessel's long-lived-process lifecycle instead of a fresh subprocess per
turn.

Why normalize app-server v2 notifications first (``_normalize_method``): the
newer app-server speaks camelCase JSON-RPC methods; the pure classification code
below maps the older snake_case ``codex --json`` event shape. Adapting method
names / casing / token paths once keeps the classifier unchanged.
"""

from __future__ import annotations

import asyncio
import functools
import atexit
import json
import logging
import os
import urllib.error
import urllib.request
import shutil
import signal
import uuid
from pathlib import Path
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from typing import Any

from pulsar.vessel.claude import VesselError
from pulsar.vessel.base import (
    BaseVessel, as_int, is_str, snippet, safe_killpg, random_session_uuid, cancel_task,
    drain_until_exited, log_stderr, terminate_group,
)
from pulsar.vessel.events import VesselEvent

log = logging.getLogger(__name__)

DEFAULT_CLI_BINARY = "codex"

# --- v4 command one-liner summarizer (display-only) -------------------------
# Codex's app-server protocol (0.144.0) sends NO model-authored per-tool text on
# command items, so v4 tool lines would otherwise fall back to the raw command.
# This post-hoc step asks a local OpenAI-compatible model to write ONE plain-
# English sentence of what the command is DOING, then attaches it as the tool's
# description. Purely presentational; never blocks the agent fatally on failure.
#
# Config (env): URL/model of the optional local summarizer, timeout, and an
# off-switch. Unset PULSAR_CODEX_DESC_URL disables command summarization.
SUMMARIZER_ENABLED = os.environ.get("PULSAR_CODEX_DESC_DISABLED", "").lower() not in ("1", "true", "yes")
SUMMARIZER_URL = os.environ.get("PULSAR_CODEX_DESC_URL", "").rstrip("/")
SUMMARIZER_MODEL = os.environ.get("PULSAR_CODEX_DESC_MODEL", "qwen3.8-27b")
SUMMARIZER_TIMEOUT = float(os.environ.get("PULSAR_CODEX_DESC_TIMEOUT", "3.0"))

_SUMMARIZER_PROMPT = (
    "You write terse display labels for an AI agent's tool calls. "
    "Given a shell command, write ONE short plain-English sentence (at most "
    "two clauses) saying what the command is DOING or INTENDING — never its "
    "output, never meta-commentary. Reply with only that sentence."
)

_SUMMARY_META_MARKERS = (
    "you write terse display labels",
    "user's request",
    "users request",
    "produce final",
    "reply only",
    "plain-english sentence",
    "plain english sentence",
    "at most two clauses",
    "given a shell command",
    "never meta-commentary",
)


def _clean_summary(text: str) -> str | None:
    """Accept only a short display label, never summarizer scratch/prompt text."""
    one_line = " ".join(text.split())
    lower = one_line.lower()
    if not one_line or len(one_line) > 180:
        return None
    if lower.startswith(("we need ", "i need ", "need ", "command:",
                         "analysis:", "reasoning:", "<think")):
        return None
    if any(marker in lower for marker in _SUMMARY_META_MARKERS):
        return None
    return one_line


def _summarize_blocking(command: str) -> str | None:
    """Blocking chat-completions call to the local summarizer; never raises.
    Returns a one-line description or None on any failure/empty reply."""
    if not SUMMARIZER_ENABLED or not SUMMARIZER_URL or not command.strip():
        return None
    body = {
        "model": SUMMARIZER_MODEL,
        "messages": [
            {"role": "system", "content": _SUMMARIZER_PROMPT},
            {"role": "user", "content": command.strip()},
        ],
        "temperature": 0,
        "max_tokens": 64,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    try:
        req = urllib.request.Request(
            SUMMARIZER_URL + "/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=SUMMARIZER_TIMEOUT) as resp:
            raw = resp.read()
        data = json.loads(raw.decode("utf-8", errors="replace"))
        text = (data.get("choices") or [{}])[0].get("message", {}).get("content")
        if isinstance(text, str):
            return _clean_summary(text)
    except Exception:
        pass
    return None


async def summarize_command(command: str) -> str | None:
    """Async wrapper: run the summarizer off the event loop with a hard timeout.
    Any failure or slowness -> None (agent flow never hangs or breaks)."""
    if not SUMMARIZER_ENABLED or not SUMMARIZER_URL or not command or not command.strip():
        return None
    try:
        return await asyncio.wait_for(
            asyncio.to_thread(_summarize_blocking, command.strip()),
            timeout=SUMMARIZER_TIMEOUT + 1.0,
        )
    except Exception:
        return None

# app-server initialize + per-request timeouts; the app-server can take a while
# to warm on the first request.
APP_SERVER_INITIALIZE_TIMEOUT = 120.0
APP_SERVER_REQUEST_TIMEOUT = 120.0

# codex emits large command-output lines (a full aggregated shell output can be
# MBs); the default asyncio StreamReader 64 KB line limit would wedge the pump.
_STREAM_LINE_LIMIT = 16 * 1024 * 1024

# Cap for tool_result snippets, matching parse.py's _SNIPPET_MAX — a multi-MB
# aggregated command output must never balloon a single event.
_SNIPPET_MAX = 4000
_as_int, _is_str = as_int, is_str
_snippet = functools.partial(snippet, limit=_SNIPPET_MAX)

# Local-provider routes: pulsar model ids that ride the codex CLI against a
# local OpenAI-compatible Responses server. Each maps the picker id to the
# real model slug plus the ``-c`` overrides that carry the provider, so the
# route works under ANY CODEX_HOME (per-pulse clean home or ambient ~/.codex).
# Values are TOML fragments (a ``-c`` value is parsed as TOML — strings need
# their quotes).
_DS4_CATALOG = Path(__file__).resolve().parent / "ds4-models.json"
DEFAULT_DS4_BASE_URL = "http://127.0.0.1:8000/v1"


def _ds4_route(base_url: str = DEFAULT_DS4_BASE_URL,
               wire_model: str = "deepseek-v4-flash") -> dict[str, Any]:
    """Build a Codex local DeepSeek V4 route backed by ``ds4-models.json``."""
    return {
        "model": wire_model,
        "config": {
            "model_provider": '"ds4vllm"',
            "model_providers.ds4vllm.name": '"DS4 vLLM (local)"',
            "model_providers.ds4vllm.base_url": f'"{base_url}"',
            "model_providers.ds4vllm.wire_api": '"responses"',
            "model_providers.ds4vllm.stream_idle_timeout_ms": "1000000",
            "model_catalog_json": f'"{_DS4_CATALOG}"',
            # Disable codex auto-compact so Pulsar's handoff engine manages
            # context rollover (0/negative would compact constantly).
            "model_auto_compact_token_limit": "100000000",
        },
    }


MODEL_ROUTES: dict[str, dict[str, Any]] = {
    "codex-ds4-flash": _ds4_route(),
}


def _model_routes() -> dict[str, dict[str, Any]]:
    """Resolve local Codex routes from ``pulsar/models.json`` + env override."""
    from pulsar import models as models_mod

    routes: dict[str, dict[str, Any]] = {}
    env_url = os.environ.get("PULSAR_DS4_BASE_URL")
    ds4_entry = models_mod.model_by_id("codex-ds4-flash")
    ds4_url = env_url or (ds4_entry.base_url if ds4_entry and ds4_entry.base_url else DEFAULT_DS4_BASE_URL)
    ds4_wire = (ds4_entry.wire_model if ds4_entry and ds4_entry.wire_model else "deepseek-v4-flash")
    routes["codex-ds4-flash"] = _ds4_route(base_url=ds4_url, wire_model=ds4_wire)
    for m in models_mod.MODELS.values():
        if m.vessel == "codex" and m.local and m.base_url and m.id not in routes:
            routes[m.id] = _ds4_route(
                base_url=env_url or m.base_url,
                wire_model=m.wire_model or "deepseek-v4-flash",
            )
    return routes

# Item ``type`` values that represent a tool the model ran (as opposed to a
# visible assistant message or internal reasoning).
_TOOL_ITEM_TYPES = frozenset(
    {
        "command_execution",
        "local_shell_call",
        "mcp_tool_call",
        "file_change",
        "patch_apply",
        "web_search",
    }
)


# --------------------------------------------------------------------------- #
# Pure helpers: total functions (never raise) so a single malformed line can
# never crash the pump.
# --------------------------------------------------------------------------- #








def usage_from_turn(usage: dict[str, Any]) -> tuple[int, int, int]:
    """(in, out, ctx) from a codex ``turn.completed.usage`` block.

    codex ``input_tokens`` is the FULL prompt size *including* the cached portion
    (``cached_input_tokens``), so ``in`` = input (the cached part is already
    folded in) and ``out`` = output + reasoning (reasoning is billed as output).
    ``ctx`` = in + out, the running context total, matching claude.py's usage
    semantics.
    """
    inp = _as_int(usage.get("input_tokens"))
    out = _as_int(usage.get("output_tokens"))
    reasoning = _as_int(usage.get("reasoning_output_tokens"))
    tin = inp
    tout = out + reasoning
    return tin, tout, tin + tout


def thread_id_of(record: Any) -> str | None:
    """Return the ``thread_id`` carried by a ``thread.started`` event, else None."""
    if (
        isinstance(record, dict)
        and record.get("type") == "thread.started"
        and _is_str(record.get("thread_id"))
    ):
        return record["thread_id"]
    return None


def _tool_call_view(item: dict[str, Any]) -> tuple[str, dict[str, Any], str, bool] | None:
    """For a tool-like item, return ``(name, input, output, is_error)``.

    A codex tool item bundles the call AND its result in one ``item.completed``
    event.
    """
    itype = item.get("type")

    if itype in ("command_execution", "local_shell_call"):
        cmd = item.get("command")
        out = item.get("aggregated_output")
        ec = item.get("exit_code")
        is_err = isinstance(ec, int) and not isinstance(ec, bool) and ec != 0
        return (
            "command_execution",
            {"command": cmd if isinstance(cmd, str) else ""},
            out if isinstance(out, str) else "",
            is_err,
        )

    if itype == "mcp_tool_call":
        server = item.get("server")
        tool = item.get("tool")
        name = ".".join(p for p in (server, tool) if _is_str(p)) or "mcp_tool_call"
        args = item.get("arguments")
        if not isinstance(args, dict):
            args = {}
        result = item.get("result")
        if isinstance(result, str):
            output = result
        elif result is None:
            output = ""
        else:
            output = json.dumps(result, ensure_ascii=False)
        return (name, args, output, item.get("status") == "failed")

    if itype in ("file_change", "patch_apply"):
        changes = item.get("changes")
        status = item.get("status")
        return (
            "file_change",
            {"changes": changes if isinstance(changes, list) else []},
            status if isinstance(status, str) else "",
            status == "failed",
        )

    if itype == "web_search":
        q = item.get("query")
        return ("web_search", {"query": q if isinstance(q, str) else ""}, "", False)

    return None


def _command_exit_code(item: dict[str, Any]) -> int | None:
    """Best-effort exit code for a completed command item, else None."""
    ec = item.get("exit_code")
    if isinstance(ec, int) and not isinstance(ec, bool):
        return ec
    return None


def _tool_desc(item: dict[str, Any]) -> str | None:
    """Best-effort model-authored one-liner for a tool item, else None.

    Codex items may carry a human "summary"/"description"/"instruction"
    written by the model. Pulsar shows it as the tool's one-line label
    (display-only). If the value just echoes the raw command or the tool
    name, treat it as absent so the caller falls back gracefully.
    """
    if not isinstance(item, dict):
        return None
    for key in ("summary", "description", "instruction"):
        val = item.get(key)
        if not isinstance(val, str) or not val.strip():
            continue
        s = val.strip()
        cmd = item.get("command")
        if cmd and s == cmd:
            continue
        if s == item.get("type") or s == "tool":
            continue
        return s
    return None


def _reasoning_text(item: dict[str, Any]) -> str:
    """Text of a codex reasoning item, or "" when there is none to show.

    codex emits reasoning items with a ``summary`` list of
    ``{type: "summary_text", text}`` blocks (``model_reasoning_summary="auto"``
    — see the argv builder); older/other paths carry a plain string in
    ``summary``/``text``/``content``. Total function: any shape yields "".
    """
    if not isinstance(item, dict):
        return ""
    parts: list[str] = []
    for key in ("text", "summary", "content"):
        val = item.get(key)
        if isinstance(val, str):
            parts.append(val)
        elif isinstance(val, list):
            for block in val:
                if isinstance(block, str):
                    parts.append(block)
                elif isinstance(block, dict) and _is_str(block.get("text")):
                    parts.append(block["text"])
    return "\n".join(p for p in parts if p and p.strip()).strip()


def classify_record(record: Any) -> list[VesselEvent]:
    """Map ONE normalized codex event into zero or more ``VesselEvent``s.

    Total function: any shape that is not a well-formed, known event yields
    either a ``done`` (reason="error") for surfaced failures or an empty list
    for ignored types. It never raises — a single malformed line must not crash
    the turn loop. Maps events to Pulsar's normalized kinds (assistant_text /
    tool_call / tool_result / usage / done).
    """
    events: list[VesselEvent] = []
    if not isinstance(record, dict):
        return [
            VesselEvent(
                kind="done", payload={"exit_code": None, "reason": "error",
                                      "subtype": "non_object_event"}
            )
        ]

    etype = record.get("type")

    if etype == "turn.completed":
        usage = record.get("usage")
        if isinstance(usage, dict) and usage:
            tin, tout, ctx = usage_from_turn(usage)
            events.append(
                VesselEvent(kind="usage", payload={"in": tin, "out": tout, "ctx": ctx})
            )
        subtype = ("interrupted" if record.get("status") == "interrupted"
                   else "success")
        events.append(
            VesselEvent(kind="done", payload={"exit_code": None, "reason": "result",
                                              "subtype": subtype})
        )
        return events

    if etype == "turn.failed":
        err = record.get("error")
        message = err.get("message") if isinstance(err, dict) else err
        return [
            VesselEvent(
                kind="done",
                payload={"exit_code": None, "reason": "error",
                         "subtype": "turn_failed", "error": err,
                         "message": message},
            )
        ]

    if etype == "error":
        # App-server error notifications are also retry progress. Let Codex
        # finish reconnecting/refreshing auth; a done(error) here makes the
        # runner kill the CLI while it is still recovering. Only the explicit
        # protocol boolean permits recovery, never wording like "Reconnecting".
        if record.get("willRetry") is True:
            return []
        # The message nests differently across app-server versions/paths
        # (top-level, under "error", or under "params") — dig for it rather
        # than emit message=None, which buried the real cause of a live
        # failure once (invalid model name, 2026-07-26).
        err = record.get("error") if isinstance(record.get("error"), dict) else {}
        params = record.get("params") if isinstance(record.get("params"), dict) else {}
        message = (record.get("message") or err.get("message")
                   or params.get("message") or json.dumps(record)[:500])
        return [
            VesselEvent(
                kind="done",
                payload={"exit_code": None, "reason": "error", "subtype": "cli_error",
                         "message": message},
            )
        ]

    if etype in ("item.started", "item.completed"):
        item = record.get("item")
        if not isinstance(item, dict):
            return []
        itype = item.get("type")

        if itype == "agent_message" and etype == "item.completed":
            # Visible assistant text mid-session (mirrors claude's
            # assistant_text). The real turn boundary is turn.completed.
            text = item.get("text")
            if _is_str(text):
                return [VesselEvent(kind="assistant_text", payload={"text": text})]
            return []

        if itype == "reasoning":
            # The model's thinking summary. Display-only (the reasoning panel);
            # it arrives completed (item.completed), never as deltas here.
            if etype == "item.completed":
                text = _reasoning_text(item)
                if text:
                    return [VesselEvent(kind="reasoning_delta",
                                        payload={"text": text})]
            return []

        if itype in _TOOL_ITEM_TYPES:
            # A codex tool item bundles the call and its result in ONE
            # item.completed. On item.started we only know the call is beginning;
            # on item.completed we have both. Emit tool_call at started and the
            # tool_result at completed so consumers see the same call/result
            # split the claude stream produces.
            view = _tool_call_view(item)
            if view is None:
                return []
            name, tool_input, output, is_err = view
            tid = item.get("id")
            tid = tid if _is_str(tid) else "item_" + uuid.uuid4().hex[:12]
            if etype == "item.started":
                return [
                    VesselEvent(
                        kind="tool_call",
                        payload={"name": name, "args": tool_input,
                                 "description": _tool_desc(item), "id": tid},
                    )
                ]
            # item.completed → the result.
            return [
                VesselEvent(
                    kind="tool_result",
                    payload={
                        "ok": not is_err,
                        "snippet": _snippet(output),
                        "exit_code": _command_exit_code(item),
                        "id": tid,
                    },
                )
            ]

        if itype == "error" and etype == "item.completed":
            return [
                VesselEvent(
                    kind="done",
                    payload={"exit_code": None, "reason": "error",
                             "subtype": "item_error"},
                )
            ]
        # *.started for messages / todo_list / unknown → no event.
        return []

    # thread.started / turn.started / unknown → no VesselEvent (thread.started is
    # consumed for its thread_id, not surfaced).
    return events


def _snake_item_type(value: Any) -> Any:
    """Map an app-server v2 camelCase item type to the snake_case classifier form."""
    if not isinstance(value, str):
        return value
    mapping = {
        "userMessage": "user_message",
        "agentMessage": "agent_message",
        "commandExecution": "command_execution",
        "localShellCall": "local_shell_call",
        "mcpToolCall": "mcp_tool_call",
        "fileChange": "file_change",
        "patchApply": "patch_apply",
        "webSearch": "web_search",
    }
    return mapping.get(value, value)


def _normalize_item(item: Any) -> Any:
    """Rename app-server v2 camelCase item fields to the snake_case classifier form."""
    if not isinstance(item, dict):
        return item
    out = dict(item)
    out["type"] = _snake_item_type(out.get("type"))
    field_map = {
        "aggregatedOutput": "aggregated_output",
        "exitCode": "exit_code",
        "serverName": "server",
        "toolName": "tool",
    }
    for src, dst in field_map.items():
        if src in out and dst not in out:
            out[dst] = out[src]
    return out


def _legacy_usage_from_appserver(value: Any) -> dict[str, Any] | None:
    """Map an app-server camelCase tokenUsage block to the snake_case usage the
    classifier reads."""
    if not isinstance(value, dict):
        return None
    return {
        "input_tokens": _as_int(value.get("inputTokens")),
        "cached_input_tokens": _as_int(value.get("cachedInputTokens")),
        "output_tokens": _as_int(value.get("outputTokens")),
        "reasoning_output_tokens": _as_int(value.get("reasoningOutputTokens")),
    }


def _normalize_method(notification: Any) -> dict[str, Any] | None:
    """Map one app-server v2 JSON-RPC notification into the legacy event shape.

    Intentionally thin: it adapts method names, casing, and token paths so the
    pure ``classify_record`` helper above stays unchanged. Drops rate-limit and
    agent-message-delta frames (visible assistant text arrives on
    item/completed).
    """
    if not isinstance(notification, dict):
        return None
    method = notification.get("method")
    params = notification.get("params")
    if not isinstance(method, str):
        return None
    if not isinstance(params, dict):
        params = {}

    if method == "thread/started":
        thread = params.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        return {"type": "thread.started", "thread_id": thread_id}

    if method == "turn/started":
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        return {"type": "turn.started", "turn_id": turn_id}

    if method in ("item/started", "item/completed"):
        item = _normalize_item(params.get("item"))
        etype = "item.started" if method == "item/started" else "item.completed"
        record = {"type": etype, "item": item}
        turn_id = params.get("turnId")
        if _is_str(turn_id):
            record["turn_id"] = turn_id
        return record

    if method == "thread/tokenUsage/updated":
        token_usage = params.get("tokenUsage")
        usage = (
            _legacy_usage_from_appserver(token_usage.get("last"))
            if isinstance(token_usage, dict)
            else None
        )
        return {
            "type": "thread.token_usage.updated",
            "turn_id": params.get("turnId"),
            "usage": usage,
        }

    if method == "turn/completed":
        turn = params.get("turn")
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        status = turn.get("status") if isinstance(turn, dict) else None
        if status == "failed":
            return {"type": "turn.failed", "turn_id": turn_id,
                    "error": turn.get("error")}
        return {"type": "turn.completed", "turn_id": turn_id, "status": status}

    if method in ("turn/failed", "error"):
        return {"type": "turn.failed" if method == "turn/failed" else "error", **params}

    # Drop rate-limit and approval frames (Pulsar auto-denies approvals in the pump).
    if method == "account/rateLimits/updated" or method.startswith(
        "item/autoApprovalReview/"
    ):
        return None

    return {"type": method.replace("/", ".")}


class _JsonRpcError(RuntimeError):
    def __init__(self, error: Any):
        self.error = error if isinstance(error, dict) else {"message": str(error)}
        code = self.error.get("code")
        message = self.error.get("message")
        super().__init__(f"JSON-RPC error {code}: {message}")


@dataclass
class _Session:
    sid: str
    cwd: str
    thread_id: str | None = None
    model: str | None = None
    effort: str | None = None
    fast: bool = False
    process: asyncio.subprocess.Process | None = None
    event_queue: asyncio.Queue[VesselEvent] = field(default_factory=asyncio.Queue)
    rpc_id: int = 0
    pending: dict[int, asyncio.Future[Any]] = field(default_factory=dict)
    pending_methods: dict[int, str] = field(default_factory=dict)
    active_turn_id: str | None = None
    initialized: bool = False
    pump_task: asyncio.Task[None] | None = None
    waiter_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    # Last usage fingerprint (in, out, ctx) surfaced per turn, so a trailing
    # tokenUsage/updated after turn/completed (observed on ~50% of turns)
    # doesn't double-emit an identical usage event.
    # Keyed per turn (not boolean) because long tool-heavy turns emit many
    # increasing tokenUsage snapshots that must all reach the runner.
    usage_last_emitted: dict[str, tuple[int, int, int]] = field(default_factory=dict)
    exited: bool = False
    # True between an interrupt() request and the turn's close — lets the
    # notification handler treat a turn/failed caused by our own abort as a
    # normal turn boundary instead of a fatal CLI error.
    interrupt_requested: bool = False


class CodexVessel(BaseVessel):
    """Drives one ``codex app-server`` session over JSON-RPC stdio.

    Lifecycle mirrors :class:`~pulsar.vessel.claude.ClaudeVessel`::

        v = CodexVessel()
        sid = await v.spawn(cwd=..., initial_prompt=..., model=...)
        async for event in v.subscribe(sid):
            ...                       # assistant_text / tool_call / tool_result / usage / done
        await v.send(sid, "next message")
        cli_id = v.cli_session_id(sid)   # codex thread id, for resume
        await v.stop(sid)
        sid2 = await v.spawn(cwd=..., resume_cli_session_id=cli_id)

    The ``sid`` returned by :meth:`spawn` is the codex thread id itself (captured
    from ``thread/start``), which is also what ``--resume``/``thread/resume``
    needs. ``cli_session_id`` returns the same value for API symmetry with the
    Claude vessel.
    """

    supports_inject = True     # turn/steer
    supports_interrupt = True  # turn/interrupt

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}
        # A process left running past interpreter exit would leak.
        atexit.register(self._kill_all_on_exit)

    # ---- lifecycle -----------------------------------------------------

    async def spawn(
        self,
        *,
        cwd: str,
        initial_prompt: str = "",
        model: str | None = None,
        effort: str | None = None,
        fast: bool = False,
        env: dict[str, str] | None = None,
        session_id: str | None = None,
        resume_cli_session_id: str | None = None,
    ) -> str:
        """Spawn (or resume) a ``codex app-server`` session.

        Returns a local session handle (the codex thread id once known). Pass
        ``resume_cli_session_id`` (a value previously read from
        :meth:`cli_session_id`) to relaunch via ``thread/resume`` so the
        conversation continues.
        """
        if not cwd:
            raise VesselError("CodexVessel.spawn requires a cwd")
        binary = os.environ.get("PULSAR_CODEX_BIN") or shutil.which(DEFAULT_CLI_BINARY)
        if not binary:
            log.error("vessel_spawn_no_binary cwd=%s", cwd)
            raise VesselError("`codex` binary not found on PATH")

        # Provisional handle until thread.started/thread-result gives the real id.
        provisional = (
            resume_cli_session_id or session_id or "codex-" + uuid.uuid4().hex[:12]
        )
        sess = _Session(
            sid=provisional,
            cwd=cwd,
            thread_id=resume_cli_session_id,
            model=model,
            effort=effort,
            fast=fast,
        )
        resume = resume_cli_session_id is not None
        log.info(
            "vessel_spawn sid=%s cwd=%s model=%s effort=%s fast=%s resume=%s",
            provisional, cwd, model, effort, fast, resume_cli_session_id,
        )
        try:
            await self._ensure_started(
                sess, binary=binary, env=env, resume=resume,
                first_input=initial_prompt if not resume else None,
            )
        except Exception as e:
            await self._cleanup_failed_start(sess)
            log.exception("vessel_spawn_failed sid=%s cwd=%s error=%s", provisional, cwd, e)
            raise VesselError(f"failed to spawn codex: {e}") from e

        if not _is_str(sess.thread_id):
            await self._cleanup_failed_start(sess)
            raise VesselError("codex app-server did not return a thread id")
        sess.sid = sess.thread_id
        self._sessions[sess.sid] = sess

        # On resume there is no first_input queued in _ensure_started; deliver the
        # initial_prompt as the first post-resume turn.
        if resume and initial_prompt:
            try:
                await self.send(sess.sid, initial_prompt)
            except Exception:
                await self.stop(sess.sid)
                raise
        log.info(
            "vessel_spawned sid=%s pid=%s",
            sess.sid, sess.process.pid if sess.process else None,
        )
        return sess.sid

    async def send(self, sid: str, text: str) -> None:
        """Deliver a user message as a new (or steering) turn."""
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"unknown session {sid}")
        if sess.process is None or sess.process.returncode is not None:
            log.error(
                "vessel_send_dead sid=%s returncode=%s",
                sid, sess.process.returncode if sess.process else None,
            )
            raise VesselError(f"session {sid} has no live process")
        try:
            if sess.active_turn_id is not None:
                await self._steer_turn(sess, text)
            else:
                await self._start_turn(sess, text)
        except Exception as e:
            log.error("vessel_send_failed sid=%s error=%s", sid, e)
            raise VesselError(f"failed to send to session {sid}: {e}") from e
        log.info("vessel_sent sid=%s bytes=%d", sid, len(text))

    async def interrupt(self, sid: str) -> None:
        """Abort the in-flight turn (``turn/interrupt``). No-op when idle.

        The app-server closes the turn after the abort (turn/completed with
        status "interrupted", or turn/failed on some versions — both are
        mapped to a normal turn boundary), so the subscriber's stream reaches
        its ``done`` and the input gate reopens for the next message.
        """
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"unknown session {sid}")
        if not _is_str(sess.thread_id) or not _is_str(sess.active_turn_id):
            log.info("vessel_interrupt_noop sid=%s (no active turn)", sid)
            return
        sess.interrupt_requested = True
        try:
            await self._send_request(
                sess,
                "turn/interrupt",
                {"threadId": sess.thread_id, "turnId": sess.active_turn_id},
            )
        except Exception:
            sess.interrupt_requested = False
            raise
        log.info("vessel_interrupted sid=%s turn=%s", sid, sess.active_turn_id)

    def cli_session_id(self, sid: str) -> str | None:
        """The codex thread id for ``sid`` (== ``sid`` once known), for resume."""
        sess = self._sessions.get(sid)
        return sess.thread_id if sess is not None else None

    def subscribe(self, sid: str) -> AsyncIterator[VesselEvent]:
        """Async-iterate normalized events for a session until it exits."""
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"unknown session {sid}")
        return drain_until_exited(sess.event_queue)

    async def stop(self, sid: str) -> None:
        """Terminate the app-server process (SIGTERM, then SIGKILL) and reap tasks."""
        sess = self._sessions.get(sid)
        if sess is None:
            return
        proc = sess.process
        if proc is not None:
            await terminate_group(proc, sid=sid)
        self._reject_pending(sess, VesselError("codex session stopped"))
        for task in (sess.pump_task, sess.stderr_task, sess.waiter_task):
            if task is not None and not task.done():
                task.cancel()
        self._sessions.pop(sid, None)

    def _kill_all_on_exit(self) -> None:
        try:
            sessions = list(self._sessions.values())
        except Exception:
            return
        for sess in sessions:
            proc = sess.process
            if proc is not None and proc.returncode is None:
                self._safe_killpg(proc.pid, signal.SIGKILL)

    # ---- app-server start + RPC ----------------------------------------

    def _build_argv(self, binary: str, sess: _Session) -> list[str]:
        """Assemble the app-server argv.

        Key options:

        - model carried on the SESSION via ``-c model="..."`` (a ``-c`` value is
          parsed as TOML → it must be quoted).
        - reasoning effort via ``-c model_reasoning_effort="..."``.
        - ``-c sandbox_mode="danger-full-access"`` — Pulsar runs the CLI
          unsandboxed, matching the Claude vessel's ``--dangerously-skip-
          permissions``; a confined codex worker hit a cascade of read-only stalls
          on legitimate writes outside the cwd.
        - ``-c model_reasoning_summary="auto"`` — WITHOUT it a turn's reasoning
          record is ``{"summary": [], "encrypted_content": "gAAAA…"}``: retained
          in the rollout for API replay, unreadable by us forever (probed
          2026-07-29). With it the summary text lands in ``summary``. The model
          reasons either way and the tokens are billed either way
          (usage_from_turn folds reasoning into ``out``), so this buys a record
          of something already paid for. Raw chain-of-thought is not on offer
          from the API at any setting; "auto" is the readable summary.
        """
        argv = [binary, "app-server", "--listen", "stdio://"]
        routes = _model_routes()
        route = routes.get(sess.model or "")
        model = route["model"] if route else sess.model
        if model:
            argv += ["-c", f'model="{model}"']
        if route:
            for key, val in route["config"].items():
                argv += ["-c", f"{key}={val}"]
        else:
            # A session recorded under a local-route provider must still load
            # after a model switch to a cloud model: codex validates the
            # resumed rollout's model_provider against the spawn config, so
            # every local provider TABLE stays defined (selection does not —
            # model_provider/model_catalog_json ride only with their route).
            for r in routes.values():
                for key, val in r["config"].items():
                    if key.startswith("model_providers."):
                        argv += ["-c", f"{key}={val}"]
        if sess.effort:
            argv += ["-c", f'model_reasoning_effort="{sess.effort}"']
        if sess.fast:
            # Fast mode = the "fast" service tier (verified live 2026-07-31:
            # accepted for gpt-6-sol; an unsupported tier only draws an
            # "omitted from requests" warning, so this can't kill a spawn).
            argv += ["-c", 'service_tier="fast"']
        argv += ["-c", 'sandbox_mode="danger-full-access"']
        argv += ["-c", 'model_reasoning_summary="auto"']
        # 2026-08-25 bloat pass: the CLI injects a ~10k-token <recommended_plugins>
        argv += ["-c", "features.plugins=false"]
        return argv

    def _child_env(self, extra: dict[str, str] | None) -> dict[str, str]:
        """Inherited environment plus ``extra`` — which is how a clean pulse's
        private ``CODEX_HOME`` reaches the CLI (runner/vessel_bridge sets it).
        Without it the child reads ~/.codex: its plugins, its global AGENTS.md,
        its skills."""
        env = dict(os.environ)
        env.setdefault("TERM", "xterm-256color")
        if extra:
            env.update(extra)
        return env

    async def _ensure_started(
        self,
        sess: _Session,
        *,
        binary: str,
        env: dict[str, str] | None,
        resume: bool,
        first_input: str | None,
    ) -> None:
        if sess.process is not None and sess.process.returncode is None:
            return

        argv = self._build_argv(binary, sess)
        log.info("vessel_appserver_start sid=%s argv=%r", sess.sid, argv)
        sess.process = await asyncio.create_subprocess_exec(
            *argv,
            cwd=sess.cwd,
            env=self._child_env(env),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            limit=_STREAM_LINE_LIMIT,
            start_new_session=True,  # own process group, so stop() can killpg
        )
        sess.pump_task = asyncio.create_task(
            self._pump_stdout(sess), name=f"pulsar-codex-pump-{sess.sid}"
        )
        sess.stderr_task = asyncio.create_task(
            log_stderr(sess.process.stderr, sess.sid), name=f"pulsar-codex-stderr-{sess.sid}"
        )
        sess.waiter_task = asyncio.create_task(
            self._wait_for_exit(sess), name=f"pulsar-codex-wait-{sess.sid}"
        )

        await self._initialize(sess)

        if resume and _is_str(sess.thread_id):
            result = await self._send_request(
                sess, "thread/resume", {"threadId": sess.thread_id}
            )
            self._capture_thread_result(sess, result)
            return

        thread_result = await self._send_request(
            sess, "thread/start", self._thread_start_params(sess, first_input)
        )
        self._capture_thread_result(sess, thread_result)
        if first_input is not None and first_input.strip():
            await self._start_turn(sess, first_input)

    async def _initialize(self, sess: _Session) -> None:
        result = await asyncio.wait_for(
            self._send_request(
                sess,
                "initialize",
                {
                    "clientInfo": {"name": "pulsar", "version": "1"},
                    "capabilities": {"experimentalApi": True},
                },
            ),
            timeout=APP_SERVER_INITIALIZE_TIMEOUT,
        )
        sess.initialized = True
        del result
        await self._send_notification(sess, "initialized", {})

    def _thread_start_params(self, sess: _Session, first_input: str | None) -> dict[str, Any]:
        instructions = first_input or ""
        # approvalPolicy "never" (non-interactive: no surface to answer prompts)
        # and sandbox matching the argv sandbox_mode -c override EXACTLY — a
        # mismatch lets the more-specific thread/start field silently override
        # argv and cap the session at workspace-write.
        return {
            "cwd": sess.cwd,
            "approvalPolicy": "never",
            "sandbox": "danger-full-access",
            "baseInstructions": instructions,
            "developerInstructions": instructions,
        }

    def _capture_thread_result(self, sess: _Session, result: Any) -> None:
        if not isinstance(result, dict):
            return
        thread = result.get("thread")
        thread_id = thread.get("id") if isinstance(thread, dict) else None
        if _is_str(thread_id):
            if sess.thread_id is None:
                log.info("vessel_cli_session_id sid=%s thread_id=%s", sess.sid, thread_id)
            sess.thread_id = thread_id

    def _turn_input(self, body: str) -> list[dict[str, str]]:
        # Never hand codex an empty/whitespace-only prompt — an empty turn is
        # meaningless and has historically triggered brittle CLI behavior.
        return [{"type": "text", "text": body if body and body.strip() else " "}]

    async def _start_turn(self, sess: _Session, body: str) -> None:
        if sess.active_turn_id is not None:
            raise VesselError(f"codex session {sess.sid} turn already in flight")
        if not _is_str(sess.thread_id):
            raise VesselError(f"codex session {sess.sid} has no thread id yet")
        result = await self._send_request(
            sess,
            "turn/start",
            {"threadId": sess.thread_id, "input": self._turn_input(body)},
        )
        turn = result.get("turn") if isinstance(result, dict) else None
        turn_id = turn.get("id") if isinstance(turn, dict) else None
        if not _is_str(turn_id):
            raise VesselError("codex turn/start response did not include turn.id")
        sess.active_turn_id = turn_id

    async def _steer_turn(self, sess: _Session, body: str) -> None:
        if not _is_str(sess.thread_id) or not _is_str(sess.active_turn_id):
            raise VesselError(f"codex session {sess.sid} has no active turn to steer")
        await self._send_request(
            sess,
            "turn/steer",
            {
                "threadId": sess.thread_id,
                "input": self._turn_input(body),
                "expectedTurnId": sess.active_turn_id,
            },
        )

    def _next_rpc_id(self, sess: _Session) -> int:
        sess.rpc_id += 1
        return sess.rpc_id

    async def _write_json(self, sess: _Session, payload: dict[str, Any]) -> None:
        proc = sess.process
        if proc is None or proc.stdin is None:
            raise VesselError(f"codex session {sess.sid} has no stdin")
        line = json.dumps(payload, ensure_ascii=False).encode("utf-8") + b"\n"
        try:
            proc.stdin.write(line)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError, RuntimeError) as e:
            log.error("vessel_stdin_write_failed sid=%s error=%s", sess.sid, e)
            raise VesselError(f"codex session {sess.sid} stdin write failed: {e}") from e

    async def _send_request(
        self,
        sess: _Session,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        timeout: float = APP_SERVER_REQUEST_TIMEOUT,
    ) -> Any:
        rpc_id = self._next_rpc_id(sess)
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[Any] = loop.create_future()
        sess.pending[rpc_id] = fut
        sess.pending_methods[rpc_id] = method
        try:
            await self._write_json(
                sess,
                {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params or {}},
            )
            return await asyncio.wait_for(fut, timeout=timeout)
        except (asyncio.TimeoutError, TimeoutError) as e:
            log.error("vessel_rpc_timeout sid=%s method=%s timeout=%.0f", sess.sid, method, timeout)
            raise VesselError(f"codex {method} timed out after {timeout}s") from e
        finally:
            sess.pending.pop(rpc_id, None)
            sess.pending_methods.pop(rpc_id, None)

    async def _send_notification(
        self, sess: _Session, method: str, params: dict[str, Any] | None = None
    ) -> None:
        await self._write_json(sess, {"jsonrpc": "2.0", "method": method, "params": params or {}})

    async def _send_response(self, sess: _Session, rpc_id: Any, result: Any) -> None:
        await self._write_json(sess, {"jsonrpc": "2.0", "id": rpc_id, "result": result})

    # ---- pumps ---------------------------------------------------------

    async def _pump_stdout(self, sess: _Session) -> None:
        """Read JSON-RPC frames from stdout: resolve responses, answer server
        requests, classify notifications into events."""
        proc = sess.process
        if proc is None or proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except (asyncio.LimitOverrunError, ValueError) as e:
                    log.error("vessel_stdout_line_too_long sid=%s error=%s", sess.sid, e)
                    continue
                if not line:
                    return  # EOF; _wait_for_exit emits the terminal done.
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    msg = json.loads(text)
                except json.JSONDecodeError as e:
                    log.warning(
                        "vessel_stdout_malformed_line sid=%s error=%s line=%.200r",
                        sess.sid, e, text,
                    )
                    continue
                if not isinstance(msg, dict):
                    log.warning(
                        "vessel_stdout_nondict_line sid=%s type=%s",
                        sess.sid, type(msg).__name__,
                    )
                    continue

                has_id = "id" in msg
                method = msg.get("method")
                if has_id and method is None:
                    self._resolve_response(sess, msg)
                elif has_id and isinstance(method, str):
                    await self._handle_server_request(sess, msg)
                elif isinstance(method, str):
                    await self._handle_notification(sess, msg)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("vessel_stdout_pump_error sid=%s error=%s", sess.sid, e)

    def _resolve_response(self, sess: _Session, msg: dict[str, Any]) -> None:
        rpc_id = msg.get("id")
        fut = sess.pending.get(rpc_id) if isinstance(rpc_id, int) else None
        if fut is None or fut.done():
            return
        if "error" in msg:
            fut.set_exception(_JsonRpcError(msg.get("error")))
        else:
            result = msg.get("result")
            # Set active_turn_id from the turn/start RESPONSE here, in the pump —
            # NOT after the awaiting _start_turn resumes. The app-server can emit
            # this turn's notifications (item/completed, turn/completed) on stdout
            # BEFORE the awaiting coroutine wakes; if active_turn_id were still the
            # prior value/None, turn.completed would be filtered out and the turn
            # would never reach its boundary.
            if sess.pending_methods.get(rpc_id) == "turn/start":
                turn = result.get("turn") if isinstance(result, dict) else None
                turn_id = turn.get("id") if isinstance(turn, dict) else None
                if _is_str(turn_id):
                    sess.active_turn_id = turn_id
            fut.set_result(result)

    async def _handle_server_request(self, sess: _Session, msg: dict[str, Any]) -> None:
        """Answer app-server → client requests. Non-interactive: deny every
        approval/elicitation and decline auth-token refresh. No approval can be
        answered by a headless vessel."""
        method = msg.get("method")
        rpc_id = msg.get("id")
        denial_methods = {
            "item/commandExecution/requestApproval",
            "item/fileChange/requestApproval",
            "item/permissions/requestApproval",
            "item/tool/requestUserInput",
            "mcpServer/elicitation/request",
        }
        if method in denial_methods:
            log.info("vessel_approval_denied sid=%s method=%s", sess.sid, method)
            await self._send_response(sess, rpc_id, {"decision": "denied"})
            return
        if method == "account/chatgptAuthTokens/refresh":
            await self._send_response(sess, rpc_id, {"error": "declined"})
            return
        log.warning("vessel_unsupported_server_request sid=%s method=%s", sess.sid, method)
        await self._send_response(sess, rpc_id, {"error": "unsupported"})

    async def _handle_notification(self, sess: _Session, msg: dict[str, Any]) -> None:
        record = _normalize_method(msg)
        if record is None:
            return

        tid = thread_id_of(record)
        if tid is not None:
            sess.thread_id = tid
            if sess.sid.startswith("codex-"):
                sess.sid = tid

        rtype = record.get("type")
        if rtype == "error" and record.get("willRetry") is True:
            log.warning("vessel_retrying sid=%s turn=%s error=%s",
                        sess.sid, record.get("turnId"), record.get("error"))

        # A turn we interrupted must close as a normal boundary. Depending on
        # app-server version the abort surfaces as turn/completed with status
        # "interrupted" (already a clean boundary) or as turn/failed — rewrite
        # the latter so classify_record doesn't turn our own abort into a
        # fatal cli error that kills the pulse.
        if rtype == "turn.failed" and sess.interrupt_requested:
            record = {"type": "turn.completed",
                      "turn_id": record.get("turn_id") or sess.active_turn_id,
                      "status": "interrupted"}
            rtype = "turn.completed"
        if rtype in ("turn.completed", "turn.failed"):
            sess.interrupt_requested = False

        # Codex emits tokenUsage/updated repeatedly DURING a turn (once per
        # sampling/tool cycle) and sometimes once more AFTER turn/completed
        # (observed on ~50% of turns). Surface
        # every new snapshot so live context growth reaches the runner;
        # suppress only an exact repeat of the last-emitted fingerprint.
        if rtype == "thread.token_usage.updated":
            usage = record.get("usage")
            turn_id = record.get("turn_id")
            if isinstance(usage, dict) and usage:
                tin, tout, ctx = usage_from_turn(usage)
                if _is_str(turn_id):
                    if sess.usage_last_emitted.get(turn_id) == (tin, tout, ctx):
                        return
                    sess.usage_last_emitted[turn_id] = (tin, tout, ctx)
                await sess.event_queue.put(
                    VesselEvent(kind="usage", payload={"in": tin, "out": tout, "ctx": ctx})
                )
            return

        # Record the inline usage fingerprint so a trailing identical
        # tokenUsage notification cannot duplicate it.
        if rtype == "turn.completed":
            turn_id = record.get("turn_id")
            usage = record.get("usage")
            if _is_str(turn_id) and isinstance(usage, dict) and usage:
                sess.usage_last_emitted[turn_id] = usage_from_turn(usage)

        for event in classify_record(record):
            # Post-hoc one-liner for codex commands that carried no description:
            # ask the local summarizer for a display line before emitting.
            if (event.kind == "tool_call"
                    and not event.payload.get("description")
                    and (event.payload.get("args") or {}).get("command")):
                cmd = event.payload["args"]["command"]
                if isinstance(cmd, str) and cmd.strip():
                    desc = await summarize_command(cmd)
                    if desc:
                        event.payload["description"] = desc
            if event.kind == "done" and event.payload.get("reason") == "result":
                # turn.completed is a per-turn boundary, not process exit; only the
                # matching active turn reopens the input gate.
                if record.get("turn_id") not in (None, sess.active_turn_id):
                    continue
                sess.active_turn_id = None
            await sess.event_queue.put(event)

    async def _wait_for_exit(self, sess: _Session) -> None:
        """Await process exit, flush stdout, reject pending RPCs, emit terminal done."""
        proc = sess.process
        if proc is None:
            return
        try:
            exit_code = await proc.wait()
            if sess.pump_task is not None and not sess.pump_task.done():
                try:
                    await asyncio.wait_for(sess.pump_task, timeout=1.0)
                except (asyncio.TimeoutError, TimeoutError):
                    sess.pump_task.cancel()
            if sess.exited:
                return
            sess.exited = True
            level = log.info if exit_code == 0 else log.warning
            level("vessel_exited sid=%s exit_code=%s", sess.sid, exit_code)
            self._reject_pending(
                sess, VesselError(f"codex app-server exited: {exit_code}")
            )
            await sess.event_queue.put(
                VesselEvent(kind="done", payload={"exit_code": exit_code, "reason": "exited"})
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("vessel_wait_error sid=%s error=%s", sess.sid, e)

    def _reject_pending(self, sess: _Session, exc: Exception) -> None:
        for fut in list(sess.pending.values()):
            if not fut.done():
                fut.set_exception(exc)
        sess.pending.clear()
        sess.pending_methods.clear()

    async def _cleanup_failed_start(self, sess: _Session) -> None:
        proc = sess.process
        if proc is not None and proc.returncode is None:
            self._safe_killpg(proc.pid, signal.SIGKILL)
            try:
                await asyncio.wait_for(proc.wait(), timeout=2.0)
            except (asyncio.TimeoutError, TimeoutError):
                pass
        self._reject_pending(sess, VesselError("codex session start failed"))
        for task in (sess.pump_task, sess.waiter_task, sess.stderr_task):
            if task is not None and not task.done():
                task.cancel()
        self._sessions.pop(sess.sid, None)
