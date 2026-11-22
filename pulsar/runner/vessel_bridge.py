"""Bridge between the runner's dumb loop and pulsar.vessel's session API.

Runner-owned (lives in runner/ so juniors keep out). The vessel gives one
continuous subscribe() stream per CLI session; the runner wants per-message
iteration plus wait()/stop(). One pump task fans the stream into a queue;
send() drains until the turn-boundary `done`, wait() resolves when the CLI
process is gone. Events cross as plain dicts ({"type": kind, **payload})
because events.jsonl is the durable surface.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
from pathlib import Path

from pulsar.vessel import ClaudeVessel, VesselError
from pulsar.vessel.profiles import profile

log = logging.getLogger("pulsar.runner.vessel")

# Token sync polls every 60s and allows a 20s gateway request timeout.
CLAUDE_AUTH_SYNC_GRACE_SECONDS = 90.0


class BridgedVessel:
    def __init__(self, model: str, workdir: Path, resume: str | None,
                 vessel_cls=ClaudeVessel, diet: dict | None = None,
                 effort: str | None = None, fast: bool = False,
                 env: dict[str, str] | None = None):
        self.model = model
        self.workdir = workdir
        self.resume = resume
        self.diet = diet
        self.effort = effort
        self.fast = fast
        self.env = env
        self._v = vessel_cls()
        self._claude_auth = isinstance(self._v, ClaudeVessel)
        self._credential_id: str | None = None
        self._restarting = False
        self._write_lock = asyncio.Lock()
        self._has_injections = False
        self._send_active = False
        self._sid: str | None = None
        self._queue: asyncio.Queue[dict | None] = asyncio.Queue()
        self._exited: asyncio.Future | None = None
        self._pump_task: asyncio.Task | None = None
        self.pid: int | None = None
        # Mid-turn steering: the claude CLI and the codex app-server slot
        # stream-json user messages written during a turn in between tool
        # calls (codex via the `turn/steer` RPC). Qwen has no proven
        # equivalent yet, so its messages keep waiting their turn. Match by
        # class name so the codex/qwen adapters stay lazily imported (a
        # claude-only install must not import them at module load).
        self.can_inject = bool(getattr(vessel_cls, "supports_inject", False))
        # Turn abort: only vessels whose protocol has a real turn interrupt
        # (codex app-server `turn/interrupt`). Callers gate on this flag.
        self.can_interrupt = bool(getattr(vessel_cls, "supports_interrupt", False))
        # Surgical bash-tool abort (pi rpc `abort_bash`): kills the wedged
        # tool WITHOUT ending the turn, so queued steers get delivered on
        # the same session. The runner's steer nudge gates on this flag.
        self.can_abort_bash = bool(
            getattr(vessel_cls, "supports_abort_bash", False))

    async def start(self) -> None:
        self._credential_id = self._read_credential_id()
        self._has_injections = False
        kwargs = {}
        if self.diet is not None and getattr(self._v, "takes_diet", False):
            kwargs["diet"] = self.diet
        if self.effort:
            # spawn takes effort for claude/codex/qwen/pi. pi-glm-52 maps
            # off/high/max onto `--thinking`; other pi models stay off.
            kwargs["effort"] = self.effort
        if self.fast:
            # make_vessel only sets fast for codex — the one spawn that takes it.
            kwargs["fast"] = True
        if self.env:
            kwargs["env"] = self.env
        self._sid = await self._v.spawn(
            cwd=str(self.workdir), model=self.model,
            resume_cli_session_id=self.resume, **kwargs)
        pid_of = getattr(self._v, "pid_of", None)  # BaseVessel; test fakes may lack it
        self.pid = getattr(self._v, "pid", None) or (pid_of(self._sid) if pid_of else None)
        if self._exited is None:
            self._exited = asyncio.get_running_loop().create_future()
        self._pump_task = asyncio.create_task(self._pump())
        log.info("vessel up sid=%s model=%s effort=%s fast=%s resume=%s",
                 self._sid, self.model, self.effort or "-", self.fast,
                 self.resume or "-")

    async def _pump(self) -> None:
        try:
            async for ev in self._v.subscribe(self._sid):
                d = {"type": ev.kind, **(ev.payload or {}),
                     "ts": ev.timestamp}
                await self._queue.put(d)
                if ev.kind == "done" and (d.get("reason") == "exited" or
                        (d.get("reason") == "error" and not (self._claude_auth and self._send_active))):
                    break
        except Exception:
            log.exception("vessel pump died sid=%s", self._sid)
            # A subscription failure is not proof that the provider process
            # exited. Stop it before resolving wait(), or the runner's CLI-death
            # branch can leave the child alive behind a dead stream.
            try:
                await self._v.stop(self._sid)
            except Exception:
                log.exception("vessel stop after pump failure failed sid=%s",
                              self._sid)
            await self._queue.put({"type": "runner_error", "where": "pump"})
        finally:
            await self._queue.put(None)  # sentinel: stream over
            if not self._restarting and self._exited and not self._exited.done():
                self._exited.set_result(1)
            log.info("vessel stream ended sid=%s", self._sid)

    async def _vision_rewrite(self, text: str) -> str:
        """Vision sidecar hook — the ONE choke point both dispatch paths cross.

        Image-blind local models cannot see images; for them, image attachment
        lines are replaced by <vision_observation> blocks from the vision
        sidecar before the vessel ever sees the message (fail closed —
        vision_sidecar never raises and never leaves a raw image line). All
        other models pass through untouched. Blocking VL HTTP runs in an
        executor so the runner's heartbeat/socket loops keep breathing.
        """
        from pulsar.vessel.vision_sidecar import VISION_MODELS, rewrite_if_needed
        if self.model not in VISION_MODELS or "Attached file: " not in text:
            return text
        import functools
        return await asyncio.get_running_loop().run_in_executor(
            None, functools.partial(
                rewrite_if_needed, text, model=self.model,
                attachments_root=self.workdir.parent / "attachments"))

    def _read_credential_id(self) -> str | None:
        """Compare token generations without retaining or logging credentials."""
        if not self._claude_auth:
            return None
        root = (self.env or {}).get("CLAUDE_CONFIG_DIR") or os.environ.get(
            "CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
        try:
            data = json.loads((Path(root) / ".credentials.json").read_text())
            token = data["claudeAiOauth"]["accessToken"]
            if not isinstance(token, str) or not token:
                return None
            return hashlib.sha256(token.encode()).hexdigest()
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def _credentials_changed(self) -> bool:
        current = self._read_credential_id()
        return bool(current and self._credential_id and current != self._credential_id)

    async def _wait_for_rotated_credentials(self) -> bool:
        """Allow one sync interval after upstream revokes the old token.

        This is asynchronous: heartbeats and stop requests remain responsive.
        No provider calls or refresh-token operations happen here.
        """
        if not self._credential_id:
            return False
        loop = asyncio.get_running_loop()
        deadline = loop.time() + CLAUDE_AUTH_SYNC_GRACE_SECONDS
        while not self._credentials_changed():
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(1.0, remaining))
        return True

    async def _resume_with_current_credentials(self) -> bool:
        """Restart only the CLI, preserving the bridge's lifetime and history."""
        resume = self._v.cli_session_id(self._sid)
        if not resume:
            return False
        self._restarting = True
        try:
            if self._pump_task:
                self._pump_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self._pump_task
            await self._v.stop(self._sid)
            self._queue = asyncio.Queue()
            self.resume = resume
            await self.start()
            log.info("claude credential rotation: resumed existing session")
            return True
        finally:
            self._restarting = False

    async def send(self, text: str):
        self._send_active = True
        try:
            async for event in self._send_with_auth_recovery(text):
                yield event
        finally:
            self._send_active = False

    async def _send_with_auth_recovery(self, text: str):
        """Refresh idle CLI credentials; recover one rotated-token failure."""
        text = await self._vision_rewrite(text)
        async with self._write_lock:
            # A late injected turn may still be running after the previous
            # done event. Never proactively stop a CLI that accepted steers.
            if (self._claude_auth and not self._has_injections
                    and self._queue.empty() and self._credentials_changed()):
                await self._resume_with_current_credentials()
            await self._v.send(self._sid, text)
        retried = False
        auth_error = False
        while True:
            ev = await self._queue.get()
            if ev is None:
                raise VesselError("CLI stream ended mid-turn")
            if ev.get("type") == "assistant_text":
                message = str(ev.get("text") or "").lower()
                auth_error = auth_error or message.startswith("failed to authenticate")
            if ev.get("type") == "done" and ev.get("reason") == "error":
                errors = str(ev.get("errors") or "").lower()
                auth_error = (auth_error or "oauth token revoked" in errors
                              or "oauth access token has expired" in errors)
            if (ev.get("type") == "done" and ev.get("reason") == "error"
                    and self._claude_auth and auth_error and not retried):
                async with self._write_lock:
                    if (await self._wait_for_rotated_credentials()
                            and await self._resume_with_current_credentials()):
                        retried = True
                        auth_error = False
                        yield {"type": "auth_recovery", "reason": "credentials_rotated"}
                        # Resume saved work instead of replaying a request whose
                        # tools may already have performed external actions.
                        await self._v.send(self._sid,
                            "The previous turn stopped because its OAuth token was replaced. "
                            "Authentication is restored. Continue the unfinished request "
                            "from the saved conversation. Do not repeat completed tool "
                            "actions. Check uncertain action results before retrying them.")
                        continue
            yield ev
            if ev.get("type") == "done":
                sid = self._v.cli_session_id(self._sid)
                if sid:
                    yield {"type": "session", "session_id": sid}
                if ev.get("reason") in ("exited", "error"):
                    cause = ev.get("message") or ev.get("subtype") or ev.get("reason")
                    if auth_error:
                        cause = "authentication failed; no further automatic retry"
                    raise VesselError(f"CLI turn ended badly: {cause}")
                return

    async def inject(self, text: str) -> None:
        """Write a user message into the LIVE turn (mid-turn steering).

        The events keep flowing through the one queue send() is draining, so
        ordering in events.jsonl is preserved. Only meaningful while a send()
        is in flight; callers gate on ``can_inject``.
        """
        text = await self._vision_rewrite(text)
        # pi rpc has a real steer command (mid-turn inject WITHOUT respawn —
        # keeps the KV cache warm, no re-prefill). claude/codex deliver a
        # live user message via send(). Prefer the vessel's inject when it
        # exists, fall back to send for the others.
        async with self._write_lock:
            self._has_injections = True
            if hasattr(self._v, "inject"):
                await self._v.inject(self._sid, text)
            else:
                await self._v.send(self._sid, text)

    async def interrupt(self) -> None:
        """Abort the vessel's in-flight turn (no-op when idle).

        The vessel closes the aborted turn with a normal ``done`` on the one
        event queue, so a draining send() reaches its boundary and the runner
        moves on to the next inbox message.
        """
        fn = getattr(self._v, "interrupt", None)
        if fn is None:
            raise VesselError(f"vessel for {self.model} has no turn interrupt")
        await fn(self._sid)

    async def abort_bash(self) -> None:
        """Abort the vessel's RUNNING bash tool, keeping the turn alive.

        The scalpel to interrupt()'s sledgehammer: the tool returns an error
        result, the agent loop continues, and any queued steer is delivered
        before the next LLM call. Callers gate on ``can_abort_bash``.
        """
        fn = getattr(self._v, "abort_bash", None)
        if fn is None:
            raise VesselError(f"vessel for {self.model} has no bash abort")
        await fn(self._sid)

    def last_progress_ts(self) -> float | None:
        """Vessel-side progress clock (monotonic ts), when the vessel has one.

        Sub-event stream chatter (tool partials the event schema drops)
        still moves the pi clock, so the runner can tell a LIVE streaming
        tool from a frozen one. None for vessels without a clock.
        """
        fn = getattr(self._v, "last_progress_ts", None)
        return fn(self._sid) if fn is not None else None

    def poll_event(self) -> dict | None:
        """Non-blocking event fetch for the runner's idle pump.

        Covers the injection race: a message written in the last instants of
        a turn can land after the CLI's turn boundary, so its reply streams
        while no send() is draining. Returns None when empty. The stream-over
        sentinel is put back so a later send() still sees it.
        """
        try:
            ev = self._queue.get_nowait()
        except asyncio.QueueEmpty:
            return None
        if ev is None:
            self._queue.put_nowait(None)
            return None
        return ev

    async def wait(self) -> int:
        return await asyncio.shield(self._exited)

    async def stop(self) -> None:
        try:
            await self._v.stop(self._sid)
        except Exception:
            log.exception("vessel stop failed sid=%s", self._sid)
        if self._pump_task:
            self._pump_task.cancel()


def make_vessel(kind: str, *, model: str, workdir: Path,
                resume: str | None = None,
                diet: dict | None = None,
                effort: str | None = None,
                fast: bool = False) -> BridgedVessel:
    """Vessel dispatch by kind via ``pulsar.vessel.profiles``. The diet applies
    to EVERY vessel (2026-07-27): claude consumes it at spawn (skills/memory/
    CLAUDE.md flags); other clean vessels get theirs on disk via
    materialize_diet before the runner boots."""
    p = profile(kind)
    if fast and not p.fast and p.warn_fast_ignored:
        # The daemon validates fast per vessel (FAST_VESSELS), so this
        # only fires on a stale stored value — drop it loudly.
        log.warning("fast mode ignored: only the codex CLI takes a "
                    "service-tier switch")
    if kind == "claude-code":
        return BridgedVessel(model, workdir, resume,
                             vessel_cls=p.vessel_cls(model),
                             diet=diet, effort=effort)
    if diet and diet.get("clean"):
        log.info("diet for vessel kind=%s delivered via materialize_diet "
                 "(on-disk workspace config), not spawn kwargs", kind)
    home = workdir.parent
    env: dict[str, str] | None = None
    # A dynamic-slot pi pulse needs its private agent dir too, with
    # or without the clean diet: that dir holds its per-pulse provider map.
    if diet and (diet.get("clean") or (kind == "pi" and diet.get("slot"))):
        env = p.clean_env(home)
    return BridgedVessel(model, workdir, resume,
                         vessel_cls=p.vessel_cls(model),
                         effort=effort,
                         fast=fast and p.fast,
                         env=env)


def _pi_vessel_cls(model: str):
    """Pick the pi vessel by model (delegates to the pi VesselProfile)."""
    return profile("pi").vessel_cls(model)
