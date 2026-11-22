"""Standalone Claude CLI adapter for Pulsar.

Drives the real ``claude`` CLI in **headless stream-json mode** (``-p`` with
``--input-format stream-json --output-format stream-json``) and yields
normalized :class:`~pulsar.vessel.events.VesselEvent` objects parsed from the
subprocess's stdout NDJSON.

Ported from an earlier project and stripped to a standalone adapter. This
module imports nothing outside the stdlib + ``pulsar.*``.

Why stream-json (not the interactive TUI): the Ink TUI path is fragile — it
needs $TERM, a sized pty, clean env, a constantly-drained pty master, and
claude releases can hang the spawn. Stream-json is the stable contract
Anthropic intends for programmatic use.

Why read stdout directly (not the on-disk session JSONL): in ``--verbose
--replay-user-messages`` mode stdout carries the full assistant/user/result
records plus the turn-boundary ``result`` line, so Pulsar parses the one
stream and needs no projects-dir tailer.
"""

from __future__ import annotations

import asyncio
import functools
import json
import logging
import os
import re
import shutil
import uuid
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from pulsar.vessel.base import (
    BaseVessel, as_int, is_str, snippet, safe_killpg, random_session_uuid, cancel_task,
    drain_until_exited, log_stderr, terminate_group,
)
from pulsar.vessel.events import VesselEvent
from pulsar.vessel.parse import classify_record

log = logging.getLogger(__name__)

# Claude emits large JSON lines (a full tool_result can be MBs); the default
# asyncio StreamReader 64 KB line limit would raise LimitOverrunError and wedge
# the pump. 10 MB keeps headroom for large tool outputs.
_STREAM_LINE_LIMIT = 10 * 1024 * 1024


def projects_root() -> Path:
    """Where the claude CLI keeps per-project session history on disk.

    The CLI derives it from HOME; production leaves the override unset. Tests
    point ``PULSAR_CLAUDE_PROJECTS`` at a tmp dir (read live, not cached —
    same pattern as paths.data_root).
    """
    env = os.environ.get("PULSAR_CLAUDE_PROJECTS")
    return Path(env) if env else Path.home() / ".claude" / "projects"


def session_history_file(cwd: str | Path, session_id: str) -> Path:
    """The CLI's on-disk session JSONL for ``session_id`` run from ``cwd``.

    The CLI munges the project cwd into a directory name by replacing every
    non-alphanumeric character with ``-`` (e.g.
    ``/home/user/pulsar/data/pulses/<id>/cli`` →
    ``-home-user-pulsar-data-pulses-<id>-cli``). Session files are looked up
    per-project, so forking a pulse means copying this file into the target
    cwd's munged directory — ``--resume`` cannot see across projects.
    """
    slug = re.sub(r"[^A-Za-z0-9]", "-", str(Path(cwd)))
    return projects_root() / slug / f"{session_id}.jsonl"

# --- Clean diet (pulses are clean by default) -------------------------------
# Switch-by-switch isolation: --safe-mode reaches the same end state but kills
# MCP config (verified 2026-07-26); --bare drops memory but
# forces API-key billing over the subscription. So each thing is turned off
# individually:
#   --disable-slash-commands   all skills (only when NO skills are opted in)
#   --setting-sources ""       no user/project/local settings, so no hooks
#                              ("project" instead when opted-in skills are
#                              symlinked into <cwd>/.claude/skills)
#   disableBundledSkills       belt-and-braces with the flag above
#   autoMemoryEnabled          the per-pulse memory-file opt-in
# Env belts the settings braces: the DISABLE vars are
# checked before any settings lookup, so no settings file can re-enable them.
_CLEAN_ENV_NO_CLAUDE_MDS = {"CLAUDE_CODE_DISABLE_CLAUDE_MDS": "1"}
_CLEAN_ENV_NO_AUTO_MEMORY = {"CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


# Background jobs outlive the pulse. A pulse's unit is stopped on handoff/reset
# and systemd kills its whole cgroup, so a Bash run_in_background job would die
# with it. This PreToolUse hook rewrites every run_in_background Bash call into
# `pulsar-job run --follow`, which starts the command in its own systemd unit.
# --settings hooks load even under --setting-sources "", so clean and full-fat
# pulses both get it.
#
# The guard hook: pulsar-guard refuses rm/rmdir/find -delete/rmtree aimed at a
# protected root, a direct child of one, a `..` path, or an unexpanded $VAR —
# see pulsar/cli/guard.py. A PreToolUse deny holds under
# --dangerously-skip-permissions, so every Claude pulse gets it, clean or
# full-fat.
_JOB_BIN = Path(__file__).resolve().parents[2] / "ops" / "bin" / "pulsar-job"
_GUARD_BIN = Path(__file__).resolve().parents[2] / "ops" / "bin" / "pulsar-guard"
JOB_HOOKS = {"PreToolUse": [{"matcher": "Bash", "hooks": [
    {"type": "command", "command": f"{_GUARD_BIN} hook"},
    {"type": "command", "command": f"{_JOB_BIN} hook"}]}]}


def diet_flags_env(diet: dict | None) -> tuple[list[str], dict[str, str]]:
    """(extra argv, extra env) for a pulse diet.

    ``diet`` = {"clean": bool, "memory": bool, "skills": [names]} — absent or
    clean=False means legacy full-fat spawn (only the job hook). Skills opt-in flips
    setting-sources to "project" so ONLY the skills symlinked into the pulse's
    <cwd>/.claude/skills load (the daemon owns the symlinks).
    """
    if not diet or not diet.get("clean", False):
        return ["--settings", json.dumps({"hooks": JOB_HOOKS})], {}
    memory = bool(diet.get("memory", False))
    skills = list(diet.get("skills") or [])
    settings = json.dumps({"disableBundledSkills": True, "autoMemoryEnabled": memory,
                           "hooks": JOB_HOOKS}, separators=(",", ":"))
    flags = ["--setting-sources", ("project" if skills else ""),
             "--settings", settings]
    if not skills:
        flags.append("--disable-slash-commands")
    env = dict(_CLEAN_ENV_NO_CLAUDE_MDS)
    if not memory:
        env.update(_CLEAN_ENV_NO_AUTO_MEMORY)
    log.info("diet_applied clean=True memory=%s skills=%s flags=%r env=%s",
             memory, skills, flags, sorted(env))
    return flags, env

# Env-var prefixes/keys that make a freshly spawned claude think it is nested
# inside another Claude Code session (IDE-attach mode), which breaks the agent
# even in stream-json mode.
_NESTED_ENV_PREFIXES = (
    "CLAUDE_CODE_",
    "CLAUDE_AGENT_",
    "VSCODE_",
    "TERM_PROGRAM",
)
_NESTED_ENV_KEYS = frozenset(
    {
        "CLAUDECODE",
        "AI_AGENT",
        "CLAUDE_EFFORT",
        "ELECTRON_RUN_AS_NODE",
        "CLAUDE_IDE_PORT",
    }
)


class VesselError(RuntimeError):
    """Raised for unrecoverable vessel setup failures (missing binary, etc.)."""


def _child_env(extra: dict[str, str] | None) -> dict[str, str]:
    """Parent env with nested-Claude markers stripped, plus a sane TERM."""
    env = {
        k: v
        for k, v in os.environ.items()
        if not any(k.startswith(p) for p in _NESTED_ENV_PREFIXES)
        and k not in _NESTED_ENV_KEYS
    }
    # stream-json renders no TUI, but some claude internals still consult TERM.
    env.setdefault("TERM", "xterm-256color")
    # Enable TaskCreate/TaskUpdate tools (the pane's TODO card); the prefix
    # strip above would drop a parent copy, so set it here.
    env["CLAUDE_CODE_ENABLE_TODO_TOOLS"] = "1"
    if extra:
        env.update(extra)
    return env


def _format_user_message(text: str) -> bytes:
    """Format a plain-text user message as a stream-json NDJSON line.

    The ``text if text.strip() else " "`` guard avoids sending an
    all-whitespace body, which would produce an empty content block the CLI rejects.
    """
    message = {
        "type": "user",
        "message": {
            "role": "user",
            "content": [{"type": "text", "text": text if text.strip() else " "}],
        },
    }
    return (json.dumps(message) + "\n").encode("utf-8")


def _build_command(
    binary: str,
    *,
    model: str | None,
    effort: str | None,
    session_id: str,
    resume_session_id: str | None,
) -> list[str]:
    """Assemble the stream-json spawn/resume argv.

    Key flags:

    - ``--verbose --include-partial-messages --replay-user-messages`` so stdout
      carries full assistant/user/result records.
    - ``--dangerously-skip-permissions`` — headless mode has no interactive
      surface to answer per-tool prompts; the caller controls the cwd.
    - Resume pins ``--resume <id> --session-id <id> --fork-session`` together, or
      claude mints a fresh random session on ``--resume`` and writes to a phantom
      file nothing tracks.
    """
    cmd = [
        binary,
        "-p",
        "--input-format", "stream-json",
        "--output-format", "stream-json",
        "--verbose",
        "--include-partial-messages",
        "--replay-user-messages",
        "--dangerously-skip-permissions",
        # Summarized thinking for the pane's thinking cards.
        # Headless claude requests display "omitted" (empty thinking text);
        # --thinking-display alone is not sent, it needs --thinking beside it.
        "--thinking", "adaptive",
        "--thinking-display", "summarized",
    ]
    if resume_session_id is not None:
        cmd += [
            "--resume", resume_session_id,
            "--session-id", resume_session_id,
            "--fork-session",
        ]
    else:
        cmd += ["--session-id", session_id]
    if model:
        cmd += ["--model", model]
    # Reasoning effort survives isolation because it's an explicit flag; ""/None
    # means inherit claude's own default.
    if effort:
        cmd += ["--effort", effort]
    return cmd


@dataclass
class _Session:
    sid: str
    cwd: str
    process: asyncio.subprocess.Process
    event_queue: asyncio.Queue[VesselEvent] = field(default_factory=asyncio.Queue)
    stdout_task: asyncio.Task[None] | None = None
    stderr_task: asyncio.Task[None] | None = None
    waiter_task: asyncio.Task[None] | None = None
    # CLI-reported session id, captured from the first stream-json line that
    # carries one (system/init or result). Used for --resume.
    cli_session_id: str | None = None
    exited: bool = False


class ClaudeVessel(BaseVessel):
    """Drives one Claude CLI session in stream-json mode.

    Lifecycle::

        v = ClaudeVessel()
        sid = await v.spawn(cwd=..., initial_prompt=..., model=...)
        async for event in v.subscribe(sid):
            ...                       # assistant_text / tool_call / tool_result / usage / done
        await v.send(sid, "next message")
        cli_id = v.cli_session_id(sid)
        await v.stop(sid)
        # later, resume the CLI-side conversation:
        sid2 = await v.spawn(cwd=..., resume_cli_session_id=cli_id)

    One instance can hold several concurrent sessions keyed by the returned
    ``sid`` (a local handle). ``cli_session_id`` is Claude's own id, captured
    from the stream, and is what ``--resume`` needs.
    """

    supports_inject = True  # stream-json user messages land between tool calls
    takes_diet = True

    def __init__(self) -> None:
        self._sessions: dict[str, _Session] = {}

    # ---- lifecycle -----------------------------------------------------

    async def spawn(
        self,
        *,
        cwd: str,
        initial_prompt: str = "",
        model: str | None = None,
        effort: str | None = None,
        env: dict[str, str] | None = None,
        session_id: str | None = None,
        resume_cli_session_id: str | None = None,
        diet: dict | None = None,
    ) -> str:
        """Spawn (or resume) a ``claude`` stream-json session.

        Returns a local session handle. Pass ``resume_cli_session_id`` (a value
        previously read from :meth:`cli_session_id`) to relaunch with
        ``--resume`` so the CLI-side conversation continues.
        """
        if not cwd:
            raise VesselError("ClaudeVessel.spawn requires a cwd")
        binary = os.environ.get("PULSAR_CLAUDE_BIN") or shutil.which("claude")
        if not binary:
            log.error("vessel_spawn_no_binary cwd=%s", cwd)
            raise VesselError("`claude` binary not found on PATH")

        sid = session_id or resume_cli_session_id or str(uuid.uuid4())
        cmd = _build_command(
            binary,
            model=model,
            effort=effort,
            session_id=sid,
            resume_session_id=resume_cli_session_id,
        )
        diet_argv, diet_env = diet_flags_env(diet)
        cmd += diet_argv
        if diet_env:
            env = {**(env or {}), **diet_env}
        # Pulse context: the spawner writes <cwd>/PULSAR.md (own id
        # + pulse-messaging recipe). Delivered as a system-prompt append — a
        # cwd CLAUDE.md would be dropped by the clean diet's
        # CLAUDE_CODE_DISABLE_CLAUDE_MDS.
        pulsar_md = Path(cwd) / "PULSAR.md"
        if pulsar_md.is_file():
            cmd += ["--append-system-prompt", pulsar_md.read_text()]
            log.info("vessel_spawn pulsar-context appended (%d bytes)",
                     pulsar_md.stat().st_size)
        log.info(
            "vessel_spawn sid=%s cwd=%s model=%s effort=%s resume=%s cmd=%r",
            sid, cwd, model, effort, resume_cli_session_id, cmd,
        )
        try:
            process = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                env=_child_env(env),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LINE_LIMIT,
                start_new_session=True,  # own process group, so stop() can killpg
            )
        except Exception as e:
            log.exception("vessel_spawn_failed sid=%s cwd=%s error=%s", sid, cwd, e)
            raise VesselError(f"failed to spawn claude: {e}") from e

        sess = _Session(sid=sid, cwd=cwd, process=process)
        # If we launched with --resume, the CLI session id is already known.
        if resume_cli_session_id is not None:
            sess.cli_session_id = resume_cli_session_id
        self._sessions[sid] = sess

        sess.stdout_task = asyncio.create_task(
            self._pump_stdout(sess), name=f"pulsar-vessel-stdout-{sid}"
        )
        sess.stderr_task = asyncio.create_task(
            log_stderr(sess.process.stderr, sid), name=f"pulsar-vessel-stderr-{sid}"
        )
        sess.waiter_task = asyncio.create_task(
            self._wait_for_exit(sess), name=f"pulsar-vessel-wait-{sid}"
        )

        if initial_prompt:
            try:
                await self.send(sid, initial_prompt)
            except Exception:
                await self.stop(sid)
                raise
        log.info("vessel_spawned sid=%s pid=%s", sid, process.pid)
        return sid

    async def send(self, sid: str, text: str) -> None:
        """Deliver a user message to the session's stdin."""
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"unknown session {sid}")
        proc = sess.process
        if proc.returncode is not None or proc.stdin is None:
            log.error("vessel_send_dead sid=%s returncode=%s", sid, proc.returncode)
            raise VesselError(f"session {sid} has no live process")
        try:
            proc.stdin.write(_format_user_message(text))
            await proc.stdin.drain()
        except (OSError, ConnectionResetError, RuntimeError) as e:
            log.error("vessel_send_failed sid=%s error=%s", sid, e)
            raise VesselError(f"failed to send to session {sid}: {e}") from e
        log.info("vessel_sent sid=%s bytes=%d", sid, len(text))

    def cli_session_id(self, sid: str) -> str | None:
        """The Claude-reported session id for ``sid``, or None if not yet seen.

        Pass this to ``spawn(resume_cli_session_id=...)`` to resume.
        """
        sess = self._sessions.get(sid)
        return sess.cli_session_id if sess is not None else None

    def subscribe(self, sid: str) -> AsyncIterator[VesselEvent]:
        """Async-iterate normalized events for a session until it exits."""
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"unknown session {sid}")
        return drain_until_exited(sess.event_queue)

    async def stop(self, sid: str) -> None:
        """Terminate the CLI process (SIGTERM, then SIGKILL) and reap tasks."""
        sess = self._sessions.get(sid)
        if sess is None:
            return
        await terminate_group(sess.process, sid=sid)
        for task in (sess.stdout_task, sess.stderr_task, sess.waiter_task):
            if task is not None and not task.done():
                task.cancel()
        self._sessions.pop(sid, None)

    # ---- pumps ---------------------------------------------------------

    async def _pump_stdout(self, sess: _Session) -> None:
        """Read NDJSON from stdout, classify each line, enqueue events.

        Also captures the CLI session id from any line that carries a
        ``session_id`` field (the ``system``/init line and the ``result`` line
        both do), so resume works.
        """
        proc = sess.process
        if proc.stdout is None:
            return
        stdout = proc.stdout
        try:
            while True:
                try:
                    line = await stdout.readline()
                except (asyncio.LimitOverrunError, ValueError) as e:
                    # A line exceeded the 10 MB limit, or the buffer overran.
                    # Do NOT fail silently: log and skip past to the next newline
                    # so the pump keeps going.
                    log.error(
                        "vessel_stdout_line_too_long sid=%s error=%s", sess.sid, e
                    )
                    continue
                if not line:
                    return  # EOF; _wait_for_exit emits the terminal `done`.
                text = line.decode("utf-8", errors="replace").strip()
                if not text:
                    continue
                try:
                    record = json.loads(text)
                except json.JSONDecodeError as e:
                    log.warning(
                        "vessel_stdout_malformed_line sid=%s error=%s line=%.200r",
                        sess.sid, e, text,
                    )
                    continue
                if not isinstance(record, dict):
                    log.warning(
                        "vessel_stdout_nondict_line sid=%s type=%s",
                        sess.sid, type(record).__name__,
                    )
                    continue

                self._capture_session_id(sess, record)

                try:
                    events = classify_record(record)
                except Exception as e:
                    # Keep this guard even though classify_record is total: a
                    # future classifier regression must still terminate this
                    # turn with a normalized event, not kill stdout draining.
                    log.exception(
                        "vessel_stdout_record_classification_error sid=%s error=%s",
                        sess.sid, e,
                    )
                    events = [
                        VesselEvent(
                            kind="done",
                            payload={
                                "exit_code": None,
                                "reason": "error",
                                "subtype": "record_classification",
                                "message": str(e)[:500],
                            },
                        )
                    ]
                for event in events:
                    await sess.event_queue.put(event)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("vessel_stdout_pump_error sid=%s error=%s", sess.sid, e)
            await sess.event_queue.put(
                VesselEvent(
                    kind="done",
                    payload={
                        "exit_code": None,
                        "reason": "error",
                        "subtype": "stdout_stream",
                        "message": str(e)[:500],
                    },
                )
            )

    def _capture_session_id(self, sess: _Session, record: dict) -> None:
        candidate = record.get("session_id")
        if isinstance(candidate, str) and candidate:
            if sess.cli_session_id is None:
                log.info(
                    "vessel_cli_session_id sid=%s cli_session_id=%s",
                    sess.sid, candidate,
                )
            sess.cli_session_id = candidate

    async def _wait_for_exit(self, sess: _Session) -> None:
        """Await process exit, flush stdout, then emit the terminal ``done``."""
        proc = sess.process
        try:
            exit_code = await proc.wait()
            # Let the stdout pump finish draining buffered lines before we post
            # the terminal event.
            if sess.stdout_task is not None and not sess.stdout_task.done():
                try:
                    await asyncio.wait_for(sess.stdout_task, timeout=1.0)
                except (asyncio.TimeoutError, TimeoutError):
                    sess.stdout_task.cancel()
            level = log.info if exit_code == 0 else log.warning
            level("vessel_exited sid=%s exit_code=%s", sess.sid, exit_code)
            sess.exited = True
            await sess.event_queue.put(
                VesselEvent(
                    kind="done",
                    payload={"exit_code": exit_code, "reason": "exited"},
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.exception("vessel_wait_error sid=%s error=%s", sess.sid, e)
