"""Antigravity CLI (``agy``) ACP vessel adapter for Pulsar.

Drives Google's ``agy acp`` over JSON-RPC 2.0 stdio — one warm, persistent ACP
process per pulse session.

Why ``agy acp`` instead of ``agy --output-format stream-json``:
``printmode.streamJSONEmitter`` in ``agy`` (verified live on 1.5.9, 2026-11-14)
serializes ``thinking_tokens`` and ``text_delta`` on ``step_update`` but never
emits ``thinking_delta`` on stdout. ``agy acp`` streams live
``agent_thought_chunk`` updates as the model reasons, tracks the true context
window via ``usage_update.used`` (rather than a multi-step billed sum), resumes
both ACP and legacy stream-json conversations via ``session/resume``, and
cancels in-flight turns cleanly via ``session/cancel`` while keeping the server
warm.

Auth is the cached Google login in ``~/.gemini/antigravity-cli`` (run ``agy``
once interactively). Advertising empty ``clientCapabilities`` keeps ``agy``
executing its own filesystem and command tools locally rather than delegating
unimplemented ``fs/*`` or ``terminal/*`` RPCs back to Pulsar.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from pulsar.vessel.base import (
    BaseVessel, as_int, cancel_task, drain_until_exited, log_stderr,
    snippet, terminate_group,
)
from pulsar.vessel.claude import VesselError
from pulsar.vessel.events import VesselEvent

log = logging.getLogger("pulsar.vessel.antigravity")

DEFAULT_AGY_BIN = str(Path.home() / ".local" / "bin" / "agy")
STREAM_LINE_LIMIT = 64 * 1024 * 1024
_SNIPPET_MAX = 4000
_snippet = functools.partial(snippet, limit=_SNIPPET_MAX)
_KILL_WAIT = 5.0
_RPC_TIMEOUT = 120.0
_PROMPT_TIMEOUT = 3600.0
_CANCEL_WAIT = 5.0

# Picker id -> the `agy` modelId (`agy models` / ACP `availableModels`).
MODEL_ROUTES: dict[str, str] = {
    "agy-gemini-4-neon": "gemini-4-neon-high",
    "agy-gemini-3.8-flash": "gemini-3.8-flash-high",
    "agy-gemini-4-argon": "gemini-4-argon-high",
    "agy-gemini-4-carbon": "gemini-4-carbon-high",
    "agy-claude-opus-5-5": "claude-opus-5-5-high",
}

# agy effort levels.
EFFORTS = frozenset({"low", "medium", "high", "xhigh", "max"})


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def tool_fields(u: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Extract ``(name, args, description)`` from an ``agy acp`` tool update."""
    raw_args = u.get("args")
    if not isinstance(raw_args, dict):
        raw_args = u.get("rawInput")
    args = dict(raw_args) if isinstance(raw_args, dict) else {}

    locs = u.get("locations")
    if isinstance(locs, list):
        for loc in locs:
            if isinstance(loc, dict) and isinstance(loc.get("path"), str):
                args.setdefault("path", loc["path"])
                break

    name = _text(u.get("name")).strip()
    if not name:
        kind = _text(u.get("kind")).strip()
        if kind and kind != "other":
            name = kind
    if not name:
        name = "tool"

    # Prefer the model's explicit toolSummary when present (e.g. run_command),
    # else ACP's human title when it differs from the bare function name.
    summary = _text(args.get("toolSummary")).strip()
    title = _text(u.get("title")).strip()
    desc = summary or (title if title and title.lower() != name.lower() else None)
    return name, args, desc


def tool_result_fields(u: dict[str, Any]) -> tuple[str, int | None]:
    """Extract ``(snippet, exit_code)`` from a completed/failed tool update."""
    exit_code: int | None = None
    out = u.get("rawOutput")
    if isinstance(out, dict):
        ec = out.get("exit_code")
        if isinstance(ec, int) and not isinstance(ec, bool):
            exit_code = ec
        for key in ("output", "content", "result", "text", "stdout", "error", "snippet"):
            val = out.get(key)
            if isinstance(val, str) and val.strip():
                return _snippet(val), exit_code
        dumped = json.dumps(out)
        if dumped not in ("{}", "null"):
            return _snippet(dumped), exit_code
    elif isinstance(out, str) and out.strip():
        return _snippet(out), exit_code

    content = u.get("content")
    if isinstance(content, list):
        parts: list[str] = []
        for block in content:
            if not isinstance(block, dict):
                continue
            btype = block.get("type")
            if btype == "content":
                inner = block.get("content")
                if isinstance(inner, dict) and isinstance(inner.get("text"), str):
                    parts.append(inner["text"])
                elif isinstance(inner, str):
                    parts.append(inner)
            elif btype == "text" and isinstance(block.get("text"), str):
                parts.append(block["text"])
            elif btype == "diff":
                path = _text(block.get("path"))
                parts.append(f"edited {path}".strip())
        if parts:
            return _snippet("\n".join(parts)), exit_code
    return "", exit_code


def usage_from_prompt_result(
    usage: dict[str, Any] | None,
    window_tokens: int = 0,
) -> tuple[int, int, int, int | None] | None:
    """Map an ``agy acp`` ``session/prompt`` result usage block to
    ``(in, out, ctx, thinking_tokens)``.

    ``session/prompt`` ``result.usage`` sums tokens across every LLM step in a
    multi-tool turn, whereas ``usage_update.used`` reports the live context
    window after the latest step. Handoff keys off ``ctx``, so ``window_tokens``
    always wins when non-zero.
    """
    block = usage if isinstance(usage, dict) else {}
    tin = as_int(block.get("inputTokens")) + as_int(block.get("cachedReadTokens"))
    out_tok = as_int(block.get("outputTokens"))
    raw_think = block.get("thoughtTokens")
    think_tok = raw_think if isinstance(raw_think, int) and not isinstance(raw_think, bool) else None
    tout = out_tok
    if window_tokens > 0:
        in_window = max(0, window_tokens - tout) if window_tokens >= tout else window_tokens
        return (in_window or tin), tout, window_tokens, think_tok
    total = as_int(block.get("totalTokens"))
    if total > 0:
        return (tin or total), tout, total, think_tok
    if tin or tout:
        return tin, tout, tin + tout, think_tok
    return None


class Translator:
    """Legacy ``agy`` stream-json records -> VesselEvents (retained for unit
    compatibility and offline log replay)."""

    def __init__(self) -> None:
        self.conversation_id: str | None = None
        self._seen_tools: set[int] = set()
        self._done_tools: set[int] = set()
        self._text: dict[int, str] = {}

    def feed(self, record: dict[str, Any]) -> list[VesselEvent]:
        cid = record.get("conversation_id")
        if isinstance(cid, str) and cid:
            self.conversation_id = cid
        etype = record.get("event")
        if etype == "step_update":
            step = record.get("step_update")
            return self._step(step if isinstance(step, dict) else {})
        if etype == "result":
            res = record.get("result")
            res = res if isinstance(res, dict) else {}
            rcid = res.get("conversation_id")
            if isinstance(rcid, str) and rcid:
                self.conversation_id = rcid
            events: list[VesselEvent] = []
            status = res.get("status")
            if status not in (None, "SUCCESS"):
                err = res.get("error") or status
                events.append(VesselEvent(kind="runner_error", payload={
                    "where": "agy_turn",
                    "reason": f"agy {status}: {' '.join(str(err).split())[:400]}"}))
            events.append(VesselEvent(kind="done",
                                      payload={"exit_code": 0, "reason": "result"}))
            return events
        if etype == "error":
            return [VesselEvent(kind="runner_error", payload={
                "where": "agy_turn",
                "reason": "agy error: " + " ".join(str(record.get("error")).split())[:400]})]
        return []

    def _step(self, step: dict[str, Any]) -> list[VesselEvent]:
        idx = step.get("step_index")
        idx = idx if isinstance(idx, int) else -1
        stype = step.get("step_type")
        state = step.get("state")
        events: list[VesselEvent] = []
        if stype == "agent_response":
            think = step.get("thinking_delta")
            if isinstance(think, str) and think:
                events.append(VesselEvent(kind="reasoning_delta", payload={"text": think}))
            delta = step.get("text_delta")
            if isinstance(delta, str) and delta:
                self._text[idx] = self._text.get(idx, "") + delta
                events.append(VesselEvent(kind="assistant_delta", payload={"text": delta}))
            if state == "DONE":
                text = self._text.pop(idx, "")
                if text.strip():
                    events.append(VesselEvent(kind="assistant_text", payload={"text": text}))
                usage = step.get("usage")
                if isinstance(usage, dict):
                    events.append(self._usage(usage))
            return events
        if stype == "tool":
            info = step.get("tool_info")
            info = info if isinstance(info, dict) else {}
            name = str(info.get("name") or step.get("tool_name") or "tool")
            args = info.get("parameters")
            if not isinstance(args, dict):
                args = {} if args is None else {"value": args}
            tid = f"agy-step-{idx}"
            if idx not in self._seen_tools:
                self._seen_tools.add(idx)
                events.append(VesselEvent(kind="tool_call", payload={
                    "name": name, "args": args, "description": None, "id": tid}))
            if state in ("DONE", "ERROR", "FAILED") and idx not in self._done_tools:
                self._done_tools.add(idx)
                out = info.get("output")
                if out is None:
                    out = info.get("error") or ""
                events.append(VesselEvent(kind="tool_result", payload={
                    "ok": state == "DONE",
                    "snippet": _snippet(out if isinstance(out, str) else json.dumps(out)),
                    "exit_code": None, "id": tid}))
        return events

    @staticmethod
    def _usage(usage: dict[str, Any]) -> VesselEvent:
        inp = as_int(usage.get("input_tokens")) + as_int(usage.get("cache_read_tokens"))
        out = as_int(usage.get("output_tokens")) + as_int(usage.get("thinking_tokens"))
        return VesselEvent(kind="usage", payload={"in": inp, "out": out, "ctx": inp + out})


@dataclass
class _Session:
    sid: str
    cwd: str
    model_ref: str
    env: dict[str, str]
    cli_session_id: str | None = None
    effort: str | None = None
    process: asyncio.subprocess.Process | None = None
    events: asyncio.Queue[VesselEvent] = field(default_factory=asyncio.Queue)
    pending: dict[int, asyncio.Future[dict[str, Any]]] = field(default_factory=dict)
    rpc_id: int = 0
    pump_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    waiter_task: asyncio.Task[None] | None = None
    prompt_task: asyncio.Task[None] | None = None
    stderr_tail: list[str] = field(default_factory=list)
    text_buffer: str = ""
    window_tokens: int = 0
    seen_tools: set[str] = field(default_factory=set)
    finished_tools: set[str] = field(default_factory=set)
    turn_done: bool = True
    interrupted: bool = False
    stopped: bool = False
    exited: bool = False
    killed_by: str | None = None  # "interrupt" | "stop"
    restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)


def resolve_model_and_effort(model: str | None, effort: str | None) -> tuple[str, str | None]:
    """Resolve Pulsar picker id + effort dial into ``(agy_model_id, extra_effort)``."""
    model_ref = MODEL_ROUTES.get(model or "") or (model or "")
    if not model_ref:
        raise VesselError("AntigravityVessel.spawn requires a model")
    if effort and effort not in EFFORTS:
        log.warning("vessel_agy effort=%r ignored", effort)
        effort = None
    # agy encodes low/medium/high directly in the modelId (e.g. gemini-4-carbon-medium).
    if effort in ("low", "medium", "high") and model_ref.endswith("-high"):
        model_ref = model_ref[:-len("high")] + effort
        effort = None
    elif effort in ("xhigh", "max") and model_ref.endswith("-high"):
        # Carbon and Argon top out at -high; Neon and Hydrogen offer -xhigh.
        if "hydrogen" in model_ref or "neon" in model_ref:
            model_ref = model_ref[:-len("high")] + "xhigh"
        effort = None
    return model_ref, effort


class AntigravityVessel(BaseVessel):
    """Drive ``agy acp`` as a warm, persistent pulse vessel."""

    supports_interrupt = True

    def __init__(self, *, agy_bin: str | None = None) -> None:
        self._bin = agy_bin or os.environ.get("PULSAR_AGY_BIN") or DEFAULT_AGY_BIN
        self._sessions: dict[str, _Session] = {}
        self.pid: int | None = None

    async def spawn(self, *, cwd: str, initial_prompt: str = "",
                    model: str | None = None, effort: str | None = None,
                    env: dict[str, str] | None = None, session_id: str | None = None,
                    resume_cli_session_id: str | None = None, **_: Any) -> str:
        if not cwd:
            raise VesselError("AntigravityVessel.spawn requires a cwd")
        if not os.path.isfile(self._bin):
            raise VesselError(f"`agy` binary not found at {self._bin}")
        model_ref, rem_effort = resolve_model_and_effort(model, effort)
        Path(cwd).mkdir(parents=True, exist_ok=True)
        sid = session_id or f"agy-{uuid.uuid4().hex[:16]}"
        sess = _Session(
            sid=sid, cwd=cwd, model_ref=model_ref,
            env={**os.environ, **(env or {})},
            cli_session_id=resume_cli_session_id or None,
            effort=rem_effort,
        )
        self._sessions[sid] = sess
        try:
            await self._start_acp(sess)
            if initial_prompt:
                await self.send(sid, initial_prompt)
            return sid
        except Exception:
            await self.stop(sid)
            raise

    def _build_command(self) -> list[str]:
        return [self._bin, "acp", "--dangerously-skip-permissions"]

    async def _start_acp(self, sess: _Session) -> None:
        log.info("vessel_agy_spawn sid=%s cwd=%s model=%s resume=%s",
                 sess.sid, sess.cwd, sess.model_ref, sess.cli_session_id or "-")
        try:
            proc = await asyncio.create_subprocess_exec(
                *self._build_command(),
                cwd=sess.cwd, env=sess.env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=STREAM_LINE_LIMIT,
                start_new_session=True,
            )
        except Exception as e:  # noqa: BLE001
            log.exception("vessel_agy_spawn_failed sid=%s error=%s", sess.sid, e)
            raise VesselError(f"failed to spawn agy acp: {e}") from e

        sess.process = proc
        sess.exited = False
        sess.killed_by = None
        self.pid = proc.pid
        sess.pump_task = asyncio.create_task(
            self._pump_stdout(sess, proc), name=f"pulsar-agy-acp-pump-{sess.sid}")
        sess.stderr_task = asyncio.create_task(
            self._pump_stderr(sess, proc), name=f"pulsar-agy-acp-stderr-{sess.sid}")
        sess.waiter_task = asyncio.create_task(
            self._wait_for_exit(sess, proc), name=f"pulsar-agy-acp-wait-{sess.sid}")

        await self._request(sess, "initialize", {
            "protocolVersion": 1,
            # Keep clientCapabilities empty so agy executes its own tools
            # locally instead of sending fs/* or terminal/* RPCs back to Pulsar.
            "clientCapabilities": {},
            "clientInfo": {"name": "pulsar", "version": "1.0"},
        })

        s_res: dict[str, Any] = {}
        if sess.cli_session_id:
            try:
                s_res = await self._request(sess, "session/resume", {
                    "sessionId": sess.cli_session_id,
                    "cwd": sess.cwd,
                    "mcpServers": [],
                })
            except VesselError as e:
                log.warning("vessel_agy_resume_fallback sid=%s resume=%s err=%s",
                            sess.sid, sess.cli_session_id, e)
                sess.cli_session_id = None

        if not sess.cli_session_id:
            s_res = await self._request(sess, "session/new", {
                "cwd": sess.cwd,
                "mcpServers": [],
            })
            real_sid = s_res.get("sessionId")
            if not isinstance(real_sid, str) or not real_sid:
                raise VesselError("agy acp session/new did not return a sessionId")
            sess.cli_session_id = real_sid

        await self._request(sess, "session/set_model", {
            "sessionId": sess.cli_session_id,
            "modelId": sess.model_ref,
        })
        if sess.effort:
            try:
                await self._request(sess, "session/set_config_option", {
                    "sessionId": sess.cli_session_id,
                    "configId": "thought_level",
                    "value": sess.effort,
                })
            except VesselError as e:
                log.warning("vessel_agy_effort_rejected sid=%s effort=%s err=%s",
                            sess.sid, sess.effort, e)

    async def _request(self, sess: _Session, method: str, params: dict[str, Any],
                       timeout: float = _RPC_TIMEOUT) -> dict[str, Any]:
        proc = sess.process
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise VesselError("agy acp process is not running")
        sess.rpc_id += 1
        rid = sess.rpc_id
        fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        sess.pending[rid] = fut
        frame = (json.dumps({
            "jsonrpc": "2.0",
            "id": rid,
            "method": method,
            "params": params,
        }, ensure_ascii=False) + "\n").encode("utf-8")
        try:
            proc.stdin.write(frame)
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError) as e:
            sess.pending.pop(rid, None)
            raise VesselError(f"failed to write {method} to agy acp: {e}") from e
        try:
            res = await asyncio.wait_for(fut, timeout=timeout)
        except Exception:
            sess.pending.pop(rid, None)
            raise
        return res if isinstance(res, dict) else {}

    async def _reply_error(self, sess: _Session, req_id: Any, code: int, message: str) -> None:
        proc = sess.process
        if proc is None or proc.returncode is not None or proc.stdin is None:
            return
        frame = (json.dumps({
            "jsonrpc": "2.0",
            "id": req_id,
            "error": {"code": code, "message": message},
        }) + "\n").encode("utf-8")
        try:
            proc.stdin.write(frame)
            await proc.stdin.drain()
        except OSError:
            pass

    async def send(self, sid: str, text: str) -> None:
        sess = self._require(sid)
        async with sess.restart_lock:
            if sess.prompt_task is not None and not sess.prompt_task.done():
                if not sess.turn_done:
                    raise VesselError("previous agy turn is still active")
                await sess.prompt_task
            if sess.process is None or sess.process.returncode is not None:
                # If an escalation interrupt had to kill a wedged child, reboot
                # ACP on the same cli_session_id so the next turn resumes seamlessly.
                if sess.killed_by == "interrupt" and not sess.stopped:
                    await self._start_acp(sess)
                else:
                    raise VesselError("agy acp process has exited")
            sess.turn_done = False
            sess.interrupted = False
            sess.killed_by = None
            sess.text_buffer = ""
            sess.seen_tools.clear()
            sess.finished_tools.clear()
            log.info("vessel_agy_send sid=%s conv=%s prompt=%.120s",
                     sid, sess.cli_session_id or "-", text)
            sess.prompt_task = asyncio.create_task(
                self._run_prompt(sess, text), name=f"pulsar-agy-prompt-{sid}")

    async def _run_prompt(self, sess: _Session, text: str) -> None:
        try:
            res = await self._request(
                sess, "session/prompt",
                {
                    "sessionId": sess.cli_session_id,
                    "prompt": [{"type": "text", "text": text or " "}],
                },
                timeout=_PROMPT_TIMEOUT,
            )
            if sess.stopped or sess.killed_by is not None or sess.turn_done:
                return
            self._flush_text(sess)
            stop_reason = _text(res.get("stopReason"))
            mapped = usage_from_prompt_result(res.get("usage"), sess.window_tokens)
            think_tok: int | None = None
            if mapped is not None:
                tin, tout, ctx, think_tok = mapped
                sess.events.put_nowait(VesselEvent(
                    kind="usage", payload={"in": tin, "out": tout, "ctx": ctx}))
            sess.turn_done = True
            if sess.interrupted or stop_reason == "cancelled":
                sess.events.put_nowait(VesselEvent(
                    kind="done", payload={"exit_code": 0, "reason": "interrupted"}))
            else:
                payload: dict[str, Any] = {"exit_code": 0, "reason": "result"}
                if stop_reason:
                    payload["subtype"] = stop_reason
                if think_tok is not None and think_tok > 0:
                    payload["thinking_tokens"] = think_tok
                sess.events.put_nowait(VesselEvent(kind="done", payload=payload))
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            if sess.stopped or sess.killed_by is not None or sess.turn_done:
                return
            self._flush_text(sess)
            sess.turn_done = True
            if sess.interrupted:
                sess.events.put_nowait(VesselEvent(
                    kind="done", payload={"exit_code": -15, "reason": "interrupted"}))
                return
            log.exception("vessel_agy_prompt_failed sid=%s error=%s", sess.sid, e)
            tail = " | ".join(sess.stderr_tail[-3:])
            detail = f"{e}" + (f" ({tail[:300]})" if tail else "")
            sess.events.put_nowait(VesselEvent(kind="runner_error", payload={
                "where": "provider_error",
                "reason": " ".join(detail.split())[:400],
            }))
            sess.events.put_nowait(VesselEvent(kind="done", payload={
                "exit_code": None, "reason": "error",
                "message": " ".join(str(e).split())[:400],
            }))
        finally:
            sess.prompt_task = None

    def _flush_text(self, sess: _Session) -> None:
        text = sess.text_buffer
        sess.text_buffer = ""
        if text and text.strip():
            sess.events.put_nowait(
                VesselEvent(kind="assistant_text", payload={"text": text}))

    def _on_update(self, sess: _Session, params: dict[str, Any]) -> None:
        if sess.stopped or sess.killed_by is not None or sess.turn_done:
            return
        u = params.get("update")
        u = u if isinstance(u, dict) else params
        typ = u.get("sessionUpdate")

        if typ == "usage_update":
            used = as_int(u.get("used"))
            if used > 0:
                sess.window_tokens = used
            return

        if typ == "agent_thought_chunk":
            content = _as_dict(u.get("content"))
            t = _text(content.get("text"))
            if t:
                sess.events.put_nowait(
                    VesselEvent(kind="reasoning_delta", payload={"text": t}))
            return

        if typ == "agent_message_chunk":
            content = _as_dict(u.get("content"))
            t = _text(content.get("text"))
            if t:
                sess.text_buffer += t
                sess.events.put_nowait(
                    VesselEvent(kind="assistant_delta", payload={"text": t}))
            return

        if typ in ("tool_call", "tool_call_update"):
            self._on_tool(sess, u, typ)

    def _on_tool(self, sess: _Session, u: dict[str, Any], typ: str) -> None:
        cid = _text(u.get("toolCallId")).strip() or None
        name, args, desc = tool_fields(u)
        status = _text(u.get("status")).strip()

        if cid and cid not in sess.seen_tools:
            if typ == "tool_call" or name != "tool" or args or desc:
                self._flush_text(sess)
                sess.seen_tools.add(cid)
                sess.events.put_nowait(VesselEvent(
                    kind="tool_call",
                    payload={"id": cid, "name": name, "args": args, "description": desc},
                ))
        elif not cid and typ == "tool_call":
            self._flush_text(sess)
            sess.events.put_nowait(VesselEvent(
                kind="tool_call",
                payload={"id": None, "name": name, "args": args, "description": desc},
            ))

        if cid and status in ("completed", "failed") and cid not in sess.finished_tools:
            sess.finished_tools.add(cid)
            snip, exit_code = tool_result_fields(u)
            ok = (status != "failed") and (exit_code in (None, 0))
            sess.events.put_nowait(VesselEvent(
                kind="tool_result",
                payload={"id": cid, "ok": ok, "snippet": snip, "exit_code": exit_code},
            ))

    async def _pump_stdout(self, sess: _Session, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        try:
            async for raw in proc.stdout:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("vessel_agy_unparsed sid=%s line=%.200s", sess.sid, line)
                    continue
                if not isinstance(msg, dict):
                    continue
                rid = msg.get("id")
                method = msg.get("method")
                if rid is not None and method is None:
                    fut = sess.pending.pop(rid, None) if isinstance(rid, int) else None
                    if fut is not None and not fut.done():
                        err = msg.get("error")
                        if err:
                            err_msg = err.get("message") if isinstance(err, dict) else str(err)
                            fut.set_exception(VesselError(f"agy acp error: {err_msg}"))
                        else:
                            res = msg.get("result")
                            fut.set_result(res if isinstance(res, dict) else {})
                    continue
                if method == "session/update":
                    self._on_update(sess, _as_dict(msg.get("params")))
                elif rid is not None and isinstance(method, str):
                    # Headless pulse has no interactive UI for unsolicited server->client
                    # RPCs; reply immediately so agy never blocks waiting on stdin.
                    log.warning("vessel_agy_unsupported_server_rpc sid=%s method=%s",
                                sess.sid, method)
                    await self._reply_error(
                        sess, rid, -32601,
                        f"Pulsar is headless; client method {method!r} is not supported")
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("vessel_agy_pump_failed sid=%s", sess.sid)
        finally:
            for fut in sess.pending.values():
                if not fut.done():
                    fut.set_exception(VesselError("agy acp stdout closed"))
            sess.pending.clear()

    async def _pump_stderr(self, sess: _Session, proc: asyncio.subprocess.Process) -> None:
        if proc.stderr is None:
            return
        try:
            async for raw in proc.stderr:
                text = raw.decode("utf-8", "replace").strip()
                if not text:
                    continue
                sess.stderr_tail.append(text)
                del sess.stderr_tail[:-40]
                log.info("vessel_agy_stderr sid=%s %.300s", sess.sid, text)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass

    async def _wait_for_exit(self, sess: _Session, proc: asyncio.subprocess.Process) -> None:
        try:
            rc = await proc.wait()
            if sess.pump_task is not None and not sess.pump_task.done():
                try:
                    await asyncio.wait_for(sess.pump_task, timeout=1.0)
                except (asyncio.TimeoutError, TimeoutError):
                    sess.pump_task.cancel()
            sess.exited = True
            if sess.stopped or sess.killed_by == "interrupt":
                return
            log.warning("vessel_agy_exited sid=%s rc=%s", sess.sid, rc)
            if not sess.turn_done:
                sess.turn_done = True
                tail = " | ".join(sess.stderr_tail[-3:])
                sess.events.put_nowait(VesselEvent(kind="runner_error", payload={
                    "where": "agy_exit",
                    "reason": f"agy acp exited rc={rc}" + (f": {tail[:400]}" if tail else ""),
                }))
            sess.events.put_nowait(VesselEvent(
                kind="done", payload={"exit_code": rc, "reason": "exited"}))
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("vessel_agy_wait_failed sid=%s", sess.sid)

    def subscribe(self, sid: str) -> AsyncIterator[VesselEvent]:
        return drain_until_exited(self._require(sid).events)

    def cli_session_id(self, sid: str) -> str | None:
        sess = self._sessions.get(sid)
        return sess.cli_session_id if sess else None

    async def interrupt(self, sid: str) -> None:
        sess = self._sessions.get(sid)
        proc = sess.process if sess else None
        if sess is None or proc is None or proc.returncode is not None:
            return
        async with sess.restart_lock:
            if sess.turn_done or proc.returncode is not None:
                return
            sess.interrupted = True
            log.info("vessel_agy_interrupt sid=%s pid=%s", sid, proc.pid)
            if sess.prompt_task is not None and not sess.prompt_task.done() and sess.cli_session_id:
                try:
                    if proc.stdin is not None:
                        sess.rpc_id += 1
                        cancel_frame = (json.dumps({
                            "jsonrpc": "2.0",
                            "id": sess.rpc_id,
                            "method": "session/cancel",
                            "params": {"sessionId": sess.cli_session_id},
                        }) + "\n").encode("utf-8")
                        proc.stdin.write(cancel_frame)
                        await proc.stdin.drain()
                    await asyncio.wait_for(asyncio.shield(sess.prompt_task), timeout=_CANCEL_WAIT)
                    if sess.turn_done:
                        return
                except Exception:  # noqa: BLE001
                    log.warning("vessel_agy_cancel_escalate sid=%s pid=%s", sid, proc.pid)

            # Fallback when session/cancel times out or no ACP prompt_task is running:
            # terminate the process group cleanly; the next send() restarts warm ACP
            # on sess.cli_session_id.
            sess.killed_by = "interrupt"
            self._flush_text(sess)
            await terminate_group(proc, sid=sid, term_timeout=_KILL_WAIT,
                                  kill_timeout=None, log=None)
            for task in (sess.prompt_task, sess.pump_task, sess.stderr_task, sess.waiter_task):
                cancel_task(task)
            sess.turn_done = True
            sess.events.put_nowait(VesselEvent(
                kind="done", payload={"exit_code": -15, "reason": "interrupted"}))

    async def stop(self, sid: str) -> None:
        sess = self._sessions.pop(sid, None)
        if sess is None:
            return
        sess.stopped = True
        sess.killed_by = "stop"
        if sess.process is not None and sess.process.returncode is None:
            await terminate_group(sess.process, sid=sid, term_timeout=_KILL_WAIT,
                                  kill_timeout=2.0, log=None)
        sess.events.put_nowait(VesselEvent(
            kind="done", payload={"exit_code": None, "reason": "exited"}))
        for task in (sess.prompt_task, sess.pump_task, sess.stderr_task, sess.waiter_task):
            if task is not None and not task.done():
                task.cancel()

    def _require(self, sid: str) -> _Session:
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"unknown agy session: {sid!r}")
        return sess


__all__ = [
    "AntigravityVessel",
    "Translator",
    "MODEL_ROUTES",
    "resolve_model_and_effort",
    "tool_fields",
    "tool_result_fields",
    "usage_from_prompt_result",
]
