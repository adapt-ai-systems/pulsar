"""Grok Build ACP vessel.

Grok's supported machine interface is ``grok agent stdio`` (ACP), not the
interactive TUI.  This adapter keeps one ACP process per pulse and translates
ACP updates into Pulsar's small vessel event vocabulary.
"""
from __future__ import annotations

import asyncio, json, logging, os, shutil, signal, uuid
from dataclasses import dataclass, field
from typing import Any
from pulsar.vessel.claude import VesselError
from pulsar.vessel.base import BaseVessel, as_int, is_str, snippet, safe_killpg, random_session_uuid, cancel_task, drain_until_exited
from pulsar.vessel.events import VesselEvent

log = logging.getLogger("pulsar.vessel.grok")
_LINE_LIMIT = 16 * 1024 * 1024
_SNIPPET_MAX = 4000
_as_int = as_int

def _text(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _from_bytes(val: Any) -> str | None:
    """Decode Grok ACP bash stdout when it arrives as a 0-255 int list."""
    if isinstance(val, str):
        return val
    if isinstance(val, (bytes, bytearray, memoryview)):
        return bytes(val).decode("utf-8", errors="replace")
    if isinstance(val, list) and all(type(x) is int and 0 <= x <= 255 for x in val):
        return bytes(val).decode("utf-8", errors="replace")
    return None


def _snippet(v: Any) -> str:
    s = _text(v).strip()
    return s if len(s) <= _SNIPPET_MAX else s[:_SNIPPET_MAX] + "\n… (truncated)"



def _window_from_meta(blob: Any) -> int:
    if not isinstance(blob, dict):
        return 0
    return _as_int(blob.get("contextTokensUsed")) or _as_int(blob.get("totalTokens"))


def _as_dict(v: Any) -> dict[str, Any]:
    return v if isinstance(v, dict) else {}


def _bare_ident(v: Any) -> str:
    """ACP title is sometimes the function name, sometimes a human sentence."""
    if not isinstance(v, str):
        return ""
    s = v.strip()
    if not s or not all(c.isalnum() or c in "_-." for c in s):
        return ""
    return s


def tool_fields(u: dict[str, Any]) -> tuple[str, dict[str, Any], str | None]:
    """Pull (name, args, description) out of a Grok ACP tool_call update.

    First updates often have ``name``/``title`` = ``read_file`` plus
    ``rawInput``. Later updates add a human title. Status-only updates have
    neither — those must not become a new ``tool`` line.
    """
    args = dict(_as_dict(u.get("rawInput")))
    locs = u.get("locations")
    if isinstance(locs, list):
        for loc in locs:
            if isinstance(loc, dict) and isinstance(loc.get("path"), str):
                args.setdefault("path", loc["path"])
                break

    name = _text(u.get("name")).strip() or _bare_ident(u.get("title"))
    if not name:
        kind = _text(u.get("kind")).strip()
        if kind and kind != "other":
            name = kind
    if not name:
        name = "tool"

    title = _text(u.get("title")).strip()
    desc = title if title and title != name else None
    return name, args, desc


def tool_useful(name: str, args: dict[str, Any], desc: str | None) -> bool:
    return bool(desc) or bool(args) or (name != "tool")


def tool_snippet(u: dict[str, Any]) -> str:
    out = u.get("rawOutput")
    if isinstance(out, str):
        return _snippet(out)
    if isinstance(out, dict):
        for key in ("content", "output", "result", "text", "stdout", "snippet"):
            val = out.get(key)
            text = _from_bytes(val)
            if text is None:
                continue
            if text.strip() or isinstance(val, list):
                return _snippet(text)
        dumped = json.dumps(out)
        return _snippet(dumped) if dumped not in ("{}", "null") else ""
    decoded = _from_bytes(out)
    if decoded is not None:
        return _snippet(decoded)
    content = u.get("content")
    if not isinstance(content, list):
        return ""
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
        elif btype == "diff":
            path = block.get("path") or ""
            parts.append(f"edited {path}".strip())
    return _snippet("\n".join(parts)) if parts else ""


def usage_from_turn(
    usage: dict[str, Any] | None,
    window_tokens: int = 0,
) -> tuple[int, int, int] | None:
    """Map a Grok ``turn_completed.usage`` block to ``(in, out, ctx)``.

    ``inputTokens`` / ``totalTokens`` on a multi-call turn are a billed sum
    (every inner prompt resent). Handoff keys off ``ctx``, so ``ctx`` must
    be the live window from session-update ``_meta.totalTokens``, not that
    sum. Fall back to in+out only when the turn was a single model call.
    """
    block = usage or {}
    tin = _as_int(block.get("inputTokens", block.get("input_tokens")))
    tout = _as_int(block.get("outputTokens", block.get("output_tokens")))
    calls = _as_int(block.get("modelCalls", block.get("numTurns")))
    if window_tokens > 0:
        return tin, tout, window_tokens
    if calls <= 1 and (tin or tout):
        return tin, tout, tin + tout
    return None


@dataclass
class _Session:
    sid: str
    cwd: str
    process: asyncio.subprocess.Process
    queue: asyncio.Queue[VesselEvent] = field(default_factory=asyncio.Queue)
    pending: dict[int, asyncio.Future] = field(default_factory=dict)
    rpc_id: int = 0
    pump: asyncio.Task | None = None
    waiter: asyncio.Task | None = None
    exited: bool = False
    prompt_task: asyncio.Task | None = None
    turn_completed: bool = False
    text_buffer: str = ""
    window_tokens: int = 0
    seen_tools: set[str] = field(default_factory=set)
    finished_tools: set[str] = field(default_factory=set)

class GrokVessel(BaseVessel):
    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    async def spawn(self, *, cwd: str, initial_prompt: str = "", model: str | None = None,
                    effort: str | None = None, env: dict[str, str] | None = None,
                    session_id: str | None = None, resume_cli_session_id: str | None = None,
                    **_: Any) -> str:
        binary = (os.environ.get("PULSAR_GROK_BIN") or shutil.which("grok")
                  or str(os.path.expanduser("~/.local/bin/grok")))
        if not os.path.isfile(binary):
            binary = None
        if not binary:
            raise VesselError("`grok` binary not found on PATH")
        proc = await asyncio.create_subprocess_exec(
            # Pulsar is the headless supervisor; without always-approve Grok
            # can wait forever for an interactive permission dialog ACP does
            # not surface in the chat UI.
            binary, "agent", "--always-approve", "stdio", cwd=cwd,
            env={**os.environ, **(env or {})}, stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE,
            limit=_LINE_LIMIT, start_new_session=True)
        s = _Session(session_id or "grok-" + uuid.uuid4().hex[:12], cwd, proc)
        self._sessions[s.sid] = s
        s.pump = asyncio.create_task(self._pump(s))
        s.waiter = asyncio.create_task(self._wait(s))
        try:
            init = await self._request(s, "initialize", {
                "protocolVersion": 1,
                # Grok executes its own filesystem and terminal tools when the
                # ACP client advertises no handlers.  Claiming these capabilities
                # makes Grok send fs/* and terminal/* RPCs back to us; Pulsar does
                # not implement them, so any tool-using turn hangs until timeout.
                "clientCapabilities": {},
            })
            methods = {m.get("id") for m in (init.get("authMethods") or []) if isinstance(m, dict)}
            auth = "xai.api_key" if os.environ.get("XAI_API_KEY") and "xai.api_key" in methods else "cached_token"
            await self._request(s, "authenticate", {"methodId": auth, "_meta": {"headless": True}})
            params: dict[str, Any] = {"cwd": cwd, "mcpServers": []}
            if model:
                params["model"] = model
            if effort:
                params["reasoningEffort"] = effort
            if resume_cli_session_id:
                params["sessionId"] = resume_cli_session_id
            result = await self._request(s, "session/new", params)
            real = result.get("sessionId")
            if not isinstance(real, str):
                raise VesselError("Grok ACP did not return a session id")
            self._sessions.pop(s.sid, None); s.sid = real; self._sessions[real] = s
            if initial_prompt:
                await self.send(real, initial_prompt)
            return real
        except Exception:
            await self.stop(s.sid)
            raise

    async def _request(self, s: _Session, method: str, params: dict[str, Any], timeout: float = 180) -> dict:
        if s.process.stdin is None: raise VesselError("Grok ACP stdin is closed")
        s.rpc_id += 1; rid = s.rpc_id
        fut = asyncio.get_running_loop().create_future(); s.pending[rid] = fut
        s.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": rid, "method": method, "params": params}) + "\n").encode())
        await s.process.stdin.drain()
        result = await asyncio.wait_for(fut, timeout)
        return result if isinstance(result, dict) else {}

    async def send(self, sid: str, text: str) -> None:
        s = self._sessions.get(sid)
        if not s: raise VesselError(f"unknown Grok session {sid}")
        if s.prompt_task and not s.prompt_task.done():
            # ACP sends turn_completed immediately before the session/prompt
            # response.  The bridge returns at turn_completed, so a queued
            # follow-up can arrive during that tiny RPC cleanup gap.  It is
            # safe to wait only after Grok has explicitly ended the turn;
            # otherwise this really is an overlapping prompt and must fail.
            if not s.turn_completed:
                raise VesselError(f"Grok session {sid} already has an active prompt")
            await s.prompt_task
        s.turn_completed = False
        # session/prompt is a long-lived RPC: its response arrives only after
        # Grok finishes every inference/tool loop.  Do not await it here or
        # BridgedVessel cannot begin draining the session/update stream and the
        # Pulsar UI appears dead for the entire turn.
        s.prompt_task = asyncio.create_task(self._run_prompt(s, text))

    async def _run_prompt(self, s: _Session, text: str) -> None:
        try:
            await self._request(
                s, "session/prompt",
                {"sessionId": s.sid,
                 "prompt": [{"type": "text", "text": text or " "}]},
                timeout=3600,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            log.exception("Grok prompt RPC failed sid=%s", s.sid)
            await s.queue.put(VesselEvent(
                kind="done",
                payload={"reason": "error", "message": str(exc)},
            ))
        finally:
            s.prompt_task = None

    def cli_session_id(self, sid: str) -> str | None:
        return sid if sid in self._sessions else None

    def subscribe(self, sid: str):
        return drain_until_exited(self._sessions[sid].queue)

    async def _pump(self, s: _Session) -> None:
        assert s.process.stdout
        try:
            async for raw in s.process.stdout:
                try: msg = json.loads(raw)
                except Exception: continue
                rid = msg.get("id")
                if isinstance(rid, int) and rid in s.pending:
                    fut = s.pending.pop(rid)
                    if not fut.done():
                        if msg.get("error"): fut.set_exception(VesselError(str(msg["error"])))
                        else: fut.set_result(msg.get("result") or {})
                    continue
                if msg.get("method") == "session/update": self._update(s, msg.get("params") or {})
                elif msg.get("method") == "_x.ai/session_notification": self._update(s, msg.get("params") or {})
        except asyncio.CancelledError: raise
        finally:
            s.exited = True
            for fut in s.pending.values():
                if not fut.done(): fut.set_exception(VesselError("Grok ACP exited"))
            await s.queue.put(VesselEvent(kind="done", payload={"reason": "exited", "exit_code": s.process.returncode}))

    def _note_window(self, s: _Session, params: dict[str, Any], update: dict[str, Any]) -> None:
        # Live window lives on session-update ``_meta.totalTokens``. Never
        # read ``usage.totalTokens`` — that field is a billed sum.
        n = _window_from_meta(params.get("_meta")) or _window_from_meta(update.get("_meta"))
        if n:
            s.window_tokens = n

    @staticmethod
    def _looks_complete(text: str) -> bool:
        # Enough of a thought to persist (not a single streamed token).
        s = text.rstrip()
        if len(s) < 12:
            return False
        return s.endswith((".", "!", "?", ":", "…")) or text.endswith("\n")

    def _flush_text(self, s: _Session) -> None:
        # Fable/Claude persist each narration block as assistant_text mid-turn.
        # Grok streams the same commentary as agent_message_chunk; flush it
        # as thoughts complete, before tools, and at turn end — otherwise the
        # chat is silent until session/prompt returns.
        text = s.text_buffer
        if not text or not text.strip():
            s.text_buffer = ""
            return
        s.text_buffer = ""
        s.queue.put_nowait(VesselEvent(kind="assistant_text", payload={"text": text}))

    def _on_tool(self, s: _Session, u: dict[str, Any], typ: str) -> None:
        cid = u.get("toolCallId")
        cid = cid if isinstance(cid, str) and cid else None
        name, args, desc = tool_fields(u)
        status = _text(u.get("status")).strip()

        if cid and cid not in s.seen_tools:
            if tool_useful(name, args, desc) or typ == "tool_call":
                self._flush_text(s)
                s.seen_tools.add(cid)
                s.queue.put_nowait(VesselEvent(
                    kind="tool_call",
                    payload={"id": cid, "name": name, "args": args,
                             "description": desc},
                ))
        elif not cid:
            if tool_useful(name, args, desc) or typ == "tool_call":
                self._flush_text(s)
                s.queue.put_nowait(VesselEvent(
                    kind="tool_call",
                    payload={"id": None, "name": name, "args": args,
                             "description": desc},
                ))

        if cid and status in ("completed", "failed") and cid not in s.finished_tools:
            s.finished_tools.add(cid)
            s.queue.put_nowait(VesselEvent(
                kind="tool_result",
                payload={"id": cid, "ok": status != "failed",
                         "snippet": tool_snippet(u)},
            ))

    def _update(self, s: _Session, params: dict[str, Any]) -> None:
        u = params.get("update") if isinstance(params.get("update"), dict) else params
        typ = u.get("sessionUpdate")
        if typ != "turn_completed":
            self._note_window(s, params, u if isinstance(u, dict) else {})
        content = u.get("content") if isinstance(u, dict) else None
        if typ == "agent_message_chunk" and isinstance(content, dict):
            t = content.get("text")
            if t:
                s.text_buffer += t
                s.queue.put_nowait(VesselEvent(kind="assistant_delta", payload={"text": t}))
                if self._looks_complete(s.text_buffer):
                    self._flush_text(s)
        elif typ == "agent_thought_chunk":
            # The model's thinking trace. Display-only (the reasoning panel);
            # grok streams it in text chunks like the visible narration.
            t = content.get("text") if isinstance(content, dict) else None
            if t:
                s.queue.put_nowait(VesselEvent(kind="reasoning_delta",
                                               payload={"text": t}))
            return
        elif typ == "tool_call" or typ == "tool_call_update":
            self._on_tool(s, u if isinstance(u, dict) else {}, typ)
        elif typ == "turn_completed":
            self._flush_text(s)
            mapped = usage_from_turn(u.get("usage") if isinstance(u, dict) else None, s.window_tokens)
            if mapped:
                tin, tout, ctx = mapped
                s.queue.put_nowait(VesselEvent(kind="usage", payload={"in": tin, "out": tout, "ctx": ctx}))
            s.turn_completed = True
            s.queue.put_nowait(VesselEvent(kind="done", payload={"reason": "result", "subtype": u.get("stop_reason", "success")}))

    async def _wait(self, s: _Session) -> None:
        await s.process.wait()

    async def stop(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if not s: return
        if s.process.returncode is None:
            try: self._safe_killpg(s.process.pid, signal.SIGTERM); await asyncio.wait_for(s.process.wait(), 5)
            except Exception:
                self._safe_killpg(s.process.pid, signal.SIGKILL)
        for t in (s.prompt_task, s.pump, s.waiter):
            if t and not t.done(): t.cancel()
