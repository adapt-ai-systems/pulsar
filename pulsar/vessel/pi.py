"""Standalone Pi (OMP) vessel adapter for Pulsar.

Drives ``@mariozechner/pi-coding-agent`` (the ``pi`` CLI, aka OMP) as the
agentic CLI for a pulse — one short-lived ``pi -p --mode json`` process per user
message, with session continuation so a pulse keeps its conversation across
inbox messages (the same resume-cycle pattern QwenCliVessel uses). This is
PHASE-1: tool use, usage accounting, kill-and-resume interrupt. Live mid-turn
*steer* (inject) is a phase-2 upgrade once pi's ``--mode rpc`` steer protocol is
proven in this process model, so ``can_inject`` must stay off.

``done`` semantics match the rest of Pulsar (claude/codex/qwen): ``done{reason:
result}`` is a TURN boundary that a send()/drain returns past but the subscribe
stream KEEPS ALIVE; ``done{reason: exited}`` is process/session death that ENDS
the stream. ``stop()`` is the only exited source mid-lifecycle; abnormal process
death also emits it. ``interrupt()`` kills the process group and emits
``done{reason: interrupted}`` without ending the stream.

Public surface: ``spawn / send / subscribe / interrupt / cli_session_id / stop``.
Imports nothing outside the stdlib + ``pulsar.*`` (VesselError from
``pulsar.vessel.claude``, matching every other vessel, to avoid a circular import
once this module is registered).
"""

from __future__ import annotations

import asyncio
import functools
import atexit
import contextlib
import json
import logging
import os
import shutil
import signal
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, AsyncIterator

from pulsar.fsutil import atomic_write_json
from pulsar.harness_overrides import read_overrides
from pulsar.vessel.claude import VesselError
from pulsar.vessel.base import (
    BaseVessel, as_int, is_str, snippet, safe_killpg, random_session_uuid, cancel_task,
    drain_until_exited, terminate_group,
)
from pulsar.vessel.events import VesselEvent

log = logging.getLogger("pulsar.vessel.pi")

DEFAULT_CLI_BINARY = "pi"
PI_MODEL = "dspair/deepseek-v4-flash"
PI_GLM52_MODEL = "glmlocal/glm-5.2"
PI_QWEN27_MODEL = "localqwen/qwen3.8-27b"
PI_QWEN38_MODEL = "glmlocal/Qwen3.8-Flash-Next-NVFP4"
# Same GLM family as glm-5.2 — thinking is template-default ON, so it gets
# the GLM thinking dial (off/high/max), not the forced-off non-GLM path.
PI_GLMFLASH_MODEL = "glm53local/glm-5.3-flash"
PI_GLMUNC_MODEL = "glm53pair/glm-5.3-flash-nvfp4"
PI_GLMEXL3_MODEL = "glm53exl3/GLM-5.3-Flash-EXL3"

# Picker ids the daemon may pass as model= -> real pi provider model. We key on
# the PICKER id (e.g. "pi-v4-flash", "pi-v4-flash-b"), never on ds4-*: that id is
# codex's still (the codex-ds4-flash entry rides the codex vessel). Any unknown
# value passes through verbatim so an explicit provider model works too.
# Built from pulsar/models.json (wire_model) — add pi models THERE.
from pulsar import models as _registry
_CLI = {m.id: m.wire_model for m in _registry.MODELS.values()
        if m.vessel == "pi" and m.wire_model}

# Shared thinking profiles keyed off models.json `thinking`:
#   PI_THINKING_LEVELS: Pulsar effort -> Pi CLI `--thinking` slot
#   PI_PROVIDER_THINKING_MAPS: Pi CLI `--thinking` slot -> provider wire effort
PI_THINKING_LEVELS: dict[str, dict[str, str]] = {
    "glm": {"off": "off", "low": "low", "high": "high", "max": "xhigh"},
    "glm52": {"off": "off", "high": "high", "max": "xhigh"},
    "qwen38": {"low": "low", "medium": "medium", "xhigh": "xhigh"},
    # DS41 dial = the number the server gets (80/70/50/30/none); the old
    # names stay for a pulse not yet migrated (models.DS41_EFFORT_ALIASES).
    "ds41": {"none": "off", "30": "minimal", "50": "low", "70": "medium", "80": "high",
             "off": "off", "low": "low", "high": "high", "xhigh": "high", "max": "high"},
}

PI_PROVIDER_THINKING_MAPS: dict[str, dict[str, str | None]] = {
    "glm": {"minimal": None, "low": "low", "medium": None, "high": "high", "xhigh": "max"},
    "glm52": {"minimal": None, "low": None, "medium": None, "high": "high", "xhigh": "max"},
    "qwen38": {"off": "none", "minimal": None, "low": "low", "medium": "medium", "high": None, "xhigh": "xhigh"},
    "qwen38_flash": {"minimal": None, "low": "low", "medium": "medium", "high": "xhigh"},
    # Numeric strings: pi accepts only string|null here; pi_ext/effort.ts
    # sends them as JSON integers (the DS41 server 400s on "80").
    "ds41": {"minimal": "30", "low": "50", "medium": "70", "high": "80", "xhigh": None},
}

# Bare tail aliases and unregistered-provider suffixes accepted alongside
# registered picker ids and wire_models.
_BARE_THINKING_ALIASES = frozenset({
    "glm-5.2", "glm-5.3", "glm53", "glm53f", "glm-5.3-flash",
    "glm-5.3-flash-nvfp4", "GLM-5.3-Flash-EXL3", "qwen3.8-27b",
    "deepseek-v4.1-flash",
})
_SUFFIX_THINKING: tuple[tuple[str, str], ...] = (
    ("/glm-5.2", "glm52"),
    ("/glm-5.3", "glm"),
    ("/glm53", "glm"),
    ("/glm53f", "glm"),
    ("/glm-5.3-flash", "glm"),
    ("/glm-5.3-flash-nvfp4", "glm"),
    ("/GLM-5.3-Flash-EXL3", "glm"),
    ("/Qwen3.8-Flash-Next-NVFP4", "qwen38"),
)


def _is_slot_provider(provider: str) -> bool:
    reg = _registry.model_by_id(provider)
    return reg is not None and reg.slot


def _is_glm53_wire_model(mid: str) -> bool:
    """True for local GLM-5.3 models (``thinking == 'glm'`` or a dynamic slot
    serving ``glm53*`` / ``glm-5.3*``). GLM-5.3 has no non-thinking template
    branch, so ``--thinking off`` runs at low effort (via ``effort.ts``) and
    suppresses ``reasoning_delta`` here so no thinking text enters chat."""
    if not mid:
        return False
    _, profile = _resolve_pi_thinking(mid)
    if profile is not None:
        return profile == "glm"
    if "/" in mid and _is_slot_provider(mid.split("/", 1)[0]):
        tail = mid.split("/", 1)[1].lower()
        return tail.startswith(("glm-5.3", "glm5.3", "glm53"))
    return False


def _resolve_pi_thinking(mid: str) -> tuple[_registry.Model | None, str | None]:
    """Resolve a model id, wire_model, bare alias, or provider suffix to
    ``(registered_model, thinking_profile)`` while preserving the registry
    ``default_effort`` tie-break order."""
    if not mid:
        return None, None
    reg = _registry.model_by_id(mid)
    if reg is not None and reg.vessel == "pi":
        return reg, reg.thinking
    reg = next(
        (m for m in _registry.MODELS.values()
         if m.vessel == "pi" and m.wire_model == mid),
        None,
    )
    if reg is not None:
        return reg, reg.thinking
    if mid in _BARE_THINKING_ALIASES:
        reg = next(
            (m for m in _registry.MODELS.values()
             if m.vessel == "pi" and m.wire_model
             and m.wire_model.rsplit("/", 1)[-1] == mid),
            None,
        )
        if reg is not None:
            return reg, reg.thinking
    for suffix, profile in _SUFFIX_THINKING:
        if mid.endswith(suffix):
            return None, profile
    return None, None


def _wire_model_for(model: str, cwd: str | os.PathLike) -> str:
    """Resolve a dynamic slot id from the pulse's private provider map."""
    mapped = _CLI.get(model)
    if mapped:
        return mapped
    path = Path(cwd).parent / "pi-home" / "agent" / "models.json"
    try:
        providers = json.loads(path.read_text()).get("providers") or {}
        provider = providers.get(model) or {}
        entries = provider.get("models") or []
        served_id = entries[0].get("id") if entries else None
        if isinstance(served_id, str) and served_id:
            return f"{model}/{served_id}"
    except (OSError, json.JSONDecodeError, AttributeError, TypeError):
        pass
    return model


def pi_thinking_level(model: str | None, effort: str | None) -> str:
    """Value for pi ``--thinking``, driven by ``models.json`` ``thinking``."""
    mid = model or ""
    if "/" in mid and _is_slot_provider(mid.split("/", 1)[0]):
        served = mid.split("/", 1)[1].lower()
        if served.startswith("glm"):
            return PI_THINKING_LEVELS["glm"].get(effort or "off", "off")
        if served.startswith("qwen"):
            return PI_THINKING_LEVELS["qwen38"].get(effort or "off", "off")
        if served.startswith("mimo"):
            return {"low": "low", "high": "high", "max": "xhigh"}.get(
                effort or "off", "off")
        if served.startswith("deepseek"):
            return PI_THINKING_LEVELS["ds41"].get(effort or "off", "off")
        return "off"
    registered, thinking = _resolve_pi_thinking(mid)
    default = registered.default_effort if registered else None
    if thinking in ("glm", "glm52", "qwen38", "ds41"):
        return PI_THINKING_LEVELS[thinking].get(effort or default or "off", "off")
    if thinking == "required-low":
        return effort if effort in {"low", "medium", "high"} else (default or "low")
    if thinking == "passthrough":
        if mid in {"pi-grok46", "openrouter/x-ai/grok-4.6"}:
            return effort or "medium"
        return "off"
    return "off"


def _pi_skill_bodies(cwd: Path) -> set[str] | None:
    """Names from ``<home>/pi-skill-bodies.json``; None = no file (every
    body). Malformed content is ignored loudly, like pi-args.json."""
    path = cwd.parent / "pi-skill-bodies.json"
    if not path.is_file():
        return None
    try:
        val = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        log.exception("unreadable pi-skill-bodies.json at %s — ignored", path)
        return None
    if isinstance(val, list) and all(isinstance(x, str) for x in val):
        log.info("vessel_pi skill bodies from %s: %s", path, val)
        return set(val)
    log.warning("pi-skill-bodies.json at %s is not a list of strings — ignored",
                path)
    return None


def effective_skill_bodies(cwd: Path,
                           overrides: dict | None = None) -> set[str] | None:
    """The ONE effective skill-body selection (arms A/B/C), shared by
    pi_skill_args and the context renderer's [loaded] marks: the
    harness-overrides key wins over pi-skill-bodies.json when present — null =
    every selected body (arm A), [] = none (arm B), a list of names = those
    (arm C). A malformed persisted value (not a list of strings) degrades to
    arm A, like the legacy reader does for a malformed file."""
    ov = overrides if isinstance(overrides, dict) else {}
    if "skill_bodies" in ov:
        sb = ov["skill_bodies"]
        if sb is None:
            return None
        if isinstance(sb, list) and all(isinstance(x, str) for x in sb):
            return set(sb)
        log.warning("harness override skill_bodies %r is not a list of "
                    "strings — every selected body is loaded instead", sb)
        return None
    return _pi_skill_bodies(cwd)


def pi_skill_args(cwd: str | os.PathLike,
                  overrides: dict | None = None) -> list[str]:
    """Deliver full bodies of explicitly selected clean-pulse skills.

    Pi's --no-skills deliberately disables ambient discovery. The spawner's
    symlinks and [loaded] catalog labels alone therefore deliver no skill
    instructions. Append selected SKILL.md files explicitly in both launch
    modes; consult diet.json rather than globbing stale/unselected links.
    Resource paths remain relative to the named skill directory, not cwd.

    A name the spawner could not link (it logs ERROR and skips a skill whose
    directory is missing) is skipped the same way here with a WARNING — one
    unlinked name must not kill the whole spawn, unlike an unreadable body.

    Skill delivery arms: an optional ``pi-skill-bodies.json``
    in the pulse home (parent of ``cwd``) holds the names whose bodies are
    appended. Absent = every selected body (arm A, the default); ``[]`` =
    no bodies, one "read <path> before working" line per skill instead
    (arm B, claude parity: the catalog is already in PULSAR.md); a list of
    names = those bodies, read lines for the rest (arm C).

    Harness overrides (an experiment): ``overrides["skill_bodies"]``
    has the same three arms and WINS over pi-skill-bodies.json when the key
    is present (null = arm A, [] = arm B, a list of names = arm C).
    """
    cwd = Path(cwd)
    path = cwd.parent / "diet.json"
    if not path.is_file():
        return []
    try:
        diet = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(diet, dict):
            raise ValueError("diet must be an object")
        if not diet.get("clean"):
            return []  # preserve existing full-fat behavior
        selected = diet.get("skills") or []
        if not isinstance(selected, list):
            raise ValueError("skills must be a list")
        ov = overrides or {}
        bodies = effective_skill_bodies(cwd, ov)
        args: list[str] = []
        seen: set[str] = set()
        for name in selected:
            if (not isinstance(name, str) or not name or "/" in name
                    or name in (".", "..")):
                raise ValueError(f"invalid skill name: {name!r}")
            if name in seen:
                continue
            seen.add(name)
            skill = cwd / ".claude" / "skills" / name / "SKILL.md"
            if not skill.parent.is_dir():
                log.warning("pi skill %r not linked at %s — skipped (the "
                            "spawner should have logged why)", name, skill.parent)
                continue
            # Fail visibly instead of claiming a missing/unreadable body loaded.
            skill.read_text(encoding="utf-8")
            if bodies is not None and name not in bodies:
                args += ["--append-system-prompt",
                         f"Selected Pulsar skill: {name}. Its SKILL.md is NOT "
                         f"loaded: read {skill} before working on its topic. "
                         f"Resolve its relative resource paths from this "
                         f"skill directory: {skill.parent}"]
                continue
            args += ["--append-system-prompt",
                     f"Selected Pulsar skill: {name}. The following SKILL.md "
                     f"is loaded in full. Resolve its relative resource paths "
                     f"from this skill directory: {skill.parent}",
                     "--append-system-prompt", str(skill)]
        return args
    except (OSError, ValueError) as exc:
        raise VesselError(f"Pi selected skill loading failed ({path}): {exc}") from exc


# The todo_write tool for pi: grok, codex, and qwen emit it natively; pi gets
# it from a bundled extension. pi's `-e` loads an explicit extension even under
# --no-extensions. The system-prompt line makes the model use it; the tool
# description carries the update rules.
PI_TODO_EXTENSION = Path(__file__).resolve().parent / "pi_ext" / "todo.ts"
PI_TODO_PROMPT = (
    "Task list: for any job with more than one step, keep your task list "
    "current with the todo_write tool — plan it before you start, mark the "
    "item you are on in_progress, and mark items completed as you finish "
    "them, not all at the end."
)


def pi_todo_args(cwd: str | os.PathLike | None = None,
                 overrides: dict | None = None) -> list[str]:
    """CLI args that give pi the todo_write tool + the prompt line to use it.
    Empty (logged) if the extension file is missing so a bad checkout never
    blocks a spawn. Empty too when the pulse's diet.json (parent of ``cwd``)
    has a ``prompt_parts`` list without "todo" (a lean spawn).

    Harness overrides (an experiment): ``overrides["todo_prompt"]``
    replaces PI_TODO_PROMPT; "" means NO todo append line ONLY — the
    todo_write extension/tool stay (the knob changes instructions, not tool
    availability). A harness-overrides ``prompt_parts`` list wins over the
    diet's with the SAME resolution the context renderer and the
    default-skills merge use, so an experiment knob that excludes "todo"
    removes the tool+line and one that re-includes it restores them on a lean
    diet."""
    ov = overrides or {}
    todo_prompt = ov.get("todo_prompt")
    if isinstance(todo_prompt, str):
        if not PI_TODO_EXTENSION.is_file():
            log.error("vessel_pi todo extension missing at %s — pi runs without "
                      "todo_write", PI_TODO_EXTENSION)
            return []
        args = ["-e", str(PI_TODO_EXTENSION)]
        if todo_prompt:
            args += ["--append-system-prompt", todo_prompt]
        return args
    if cwd is not None:
        try:
            diet = json.loads((Path(cwd).parent / "diet.json").read_text(
                encoding="utf-8"))
        except (OSError, ValueError):
            diet = None
        # ONE effective allowlist: the harness-overrides list wins over
        # the diet's (null/absent/malformed -> the diet's).
        from pulsar.harness_overrides import effective_prompt_parts
        parts = effective_prompt_parts(ov, diet)
        if parts is not None and "todo" not in parts:
            return []
    if not PI_TODO_EXTENSION.is_file():
        log.error("vessel_pi todo extension missing at %s — pi runs without "
                  "todo_write", PI_TODO_EXTENSION)
        return []
    return ["-e", str(PI_TODO_EXTENSION),
            "--append-system-prompt", PI_TODO_PROMPT]


# The delete guard for pi: the extension blocks a bash tool call that
# `pulsar-guard check` refuses. Always wired in (honor guard.enabled inside
# pulsar-guard); not diet-dependent. It also sets the 120 s default bash
# timeout, so the prompt line rides with it.
PI_GUARD_EXTENSION = Path(__file__).resolve().parent / "pi_ext" / "guard.ts"
PI_BASH_PROMPT = (
    "bash stops at 120 s by default; cd persists across bash calls (no need "
    "to repeat it)"
)


# Numeric reasoning effort (DS41 dial 80/70/50/30): pi's thinkingLevelMap
# holds strings only, the server wants a JSON integer. The extension turns a
# numeric-string effort into a number in the outgoing request body; any other
# value passes untouched.
PI_EFFORT_EXTENSION = Path(__file__).resolve().parent / "pi_ext" / "effort.ts"


def pi_effort_args() -> list[str]:
    """``-e <effort.ts>`` for every pi launch; empty (logged) if missing."""
    if not PI_EFFORT_EXTENSION.is_file():
        log.error("vessel_pi effort extension missing at %s — numeric "
                  "efforts go out as strings", PI_EFFORT_EXTENSION)
        return []
    return ["-e", str(PI_EFFORT_EXTENSION)]


def pi_guard_args(overrides: dict | None = None) -> list[str]:
    """``-e <guard.ts>`` + the bash prompt line for every pi launch. Empty
    (logged loudly) only when the file is missing, so a bad checkout never
    blocks a spawn.

    Harness override ``bash_prompt``: replaces the PI_BASH_PROMPT line;
    "" means NO bash prompt line (present != truthy) — the guard extension
    itself always stays."""
    if not PI_GUARD_EXTENSION.is_file():
        log.error("vessel_pi guard extension missing at %s — pi runs WITHOUT "
                  "the delete guard", PI_GUARD_EXTENSION)
        return []
    args = ["-e", str(PI_GUARD_EXTENSION)]
    ov = overrides or {}
    bash_prompt = ov.get("bash_prompt")
    if bash_prompt is None:
        args += ["--append-system-prompt", PI_BASH_PROMPT]
    elif bash_prompt != "":
        args += ["--append-system-prompt", bash_prompt]
    return args


# pi's default active tools are read/bash/edit/write (sdk.js:151), so models
# walk trees with bash `grep -r`/`find`. The native grep/find/ls
# tools use rg/fd, respect .gitignore and cap their output. `--tools` is an
# ALLOWLIST that also filters extension tools, so todo_write (pi_ext/todo.ts)
# must be listed. It goes BEFORE pi_extra_args: pi keeps the LAST `--tools`,
# so a pulse's own pi-args.json allowlist (e.g. a custom MCP bridge) still wins.
# find runs fd; with no fd on PATH pi downloads the latest fd release from
# GitHub into the pulse's agent dir at first use, so find is listed only
# when fd (or Debian's fdfind) is installed. grep runs rg, ls needs nothing.
PI_DEFAULT_TOOLS = ("read", "bash", "edit", "write", "grep", "find", "ls",
                    "todo_write")


def pi_tools_args(overrides: dict | None = None) -> list[str]:
    """The ``--tools`` allowlist. Harness override ``tools`` replaces
    PI_DEFAULT_TOOLS; the fd rule always applies — "find" is dropped when
    neither fd nor fdfind is on PATH, whatever list it came from."""
    has_fd = bool(shutil.which("fd") or shutil.which("fdfind"))
    ov = overrides or {}
    source = ov.get("tools")
    if not (isinstance(source, list) and source):
        source = PI_DEFAULT_TOOLS
    # Normalize each name with pi's own whitespace rule BEFORE the fd filter
    # and the join: a padded " find " must not slip past the fd rule — pi trims
    # argv names, Pulsar must too.
    tools = [t.strip() for t in source]
    tools = [t for t in tools if t != "find" or has_fd]
    if ov.get("ptc") is True and "script" not in tools:
        tools.append("script")
    return ["--tools", ",".join(tools)]


# Programmatic tool calling:
# the `script` tool runs one JavaScript program whose functions are pi's own
# read/bash/edit/write/grep/find/ls, so many calls cost one model request.
# Off unless the harness override ``ptc`` is true; pi_tools_args adds
# "script" to the allowlist under the same knob.
PI_SCRIPT_EXTENSION = Path(__file__).resolve().parent / "pi_ext" / "script.ts"


def pi_script_args(overrides: dict | None = None) -> list[str]:
    """``-e <script.ts>`` when ``overrides["ptc"]`` is true, else []."""
    if (overrides or {}).get("ptc") is not True:
        return []
    if not PI_SCRIPT_EXTENSION.is_file():
        log.error("vessel_pi script extension missing at %s — pi runs "
                  "without the script tool", PI_SCRIPT_EXTENSION)
        return []
    return ["-e", str(PI_SCRIPT_EXTENSION)]


# Spill big tool outputs to a file:
# a bash/grep/find/ls result over ``spill_chars`` is saved whole under
# <cli>/.pi/spill/ and the model gets head + tail + the path. Off unless the
# harness override ``spill_chars`` is set; the extension reads the limit
# from harness-overrides.json itself.
PI_SPILL_EXTENSION = Path(__file__).resolve().parent / "pi_ext" / "spill.ts"


def pi_spill_args(overrides: dict | None = None) -> list[str]:
    """``-e <spill.ts>`` when ``overrides["spill_chars"]`` is set, else []."""
    if not (overrides or {}).get("spill_chars"):
        return []
    if not PI_SPILL_EXTENSION.is_file():
        log.error("vessel_pi spill extension missing at %s — tool outputs "
                  "are not spilled", PI_SPILL_EXTENSION)
        return []
    return ["-e", str(PI_SPILL_EXTENSION)]


# Pulsar's base prompt replaces pi's: pi opens every request
# with "You are an expert coding assistant operating inside pi" plus a pi
# docs block. With --system-prompt pi drops its own text AND every tool
# snippet/guideline, but still appends --append-system-prompt (PULSAR.md,
# todo, bash, skills), cage text, date and cwd. So the base carries the
# tool lines and the read/edit/write guidelines pi would have sent, for
# the tools that are really active. Tool lines match pi's names.
PI_BASE_TOOL_LINES = {
    "read": "Read file contents",
    "bash": "Execute bash commands",
    "edit": ("Make precise file edits with exact text replacement, "
             "including multiple disjoint edits in one call"),
    "write": "Create or overwrite files",
    "grep": "Search file contents for patterns (respects .gitignore)",
    "find": "Find files by glob pattern (respects .gitignore)",
    "ls": "List directory contents",
    "todo_write": "Keep your task list current",
    "script": ("Run a JavaScript program that calls the other tools as "
               "async functions (many calls in one step; only what it "
               "prints comes back)"),
}
PI_BASE_GUIDELINES = {
    "read": ["Use read to examine files instead of cat or sed."],
    "edit": [
        "Use edit for precise changes (edits[].oldText must match exactly)",
        "When changing multiple separate locations in one file, use one edit "
        "call with multiple entries in edits[] instead of multiple edit calls",
        "Each edits[].oldText is matched against the original file, not after "
        "earlier edits are applied. Do not emit overlapping or nested edits. "
        "Merge nearby changes into one edit.",
        "Keep edits[].oldText as small as possible while still being unique "
        "in the file. Do not pad with large unchanged regions.",
    ],
    "write": ["Use write only for new files or complete rewrites."],
    "script": ["Batch work with script: when you know the next several tool "
               "calls, run them in one script and print only what you need."],
}


def _last_tools(args: list[str]) -> list[str] | None:
    """The allowlist pi will use: the value of the LAST --tools/-t."""
    tools = None
    for flag, val in zip(args, args[1:]):
        if flag in ("--tools", "-t"):
            tools = [t.strip() for t in val.split(",") if t.strip()]
    return tools


def pi_base_prompt(tools: list[str],
                   guidelines: list[str] | None = None) -> str:
    listed = [t for t in tools if t in PI_BASE_TOOL_LINES]
    lines = [
        "You are a Pulsar pulse: an agent session that does the work it is "
        "given by reading files, running commands, and editing and writing "
        "files with the tools below. Your pulse context follows this section.",
        "",
        "Available tools:",
        *(f"- {t}: {PI_BASE_TOOL_LINES[t]}" for t in listed),
    ]
    if len(listed) < len(tools):
        lines.append("You also have other tools; their descriptions come "
                     "with the tool list.")
    # Harness overrides (an experiment): a caller-supplied guidelines
    # list replaces the computed one verbatim ([] = no Guidelines section).
    if guidelines is None:
        guidelines = []
        if "bash" in tools and {"grep", "find", "ls"} & set(tools):
            guidelines.append("Prefer grep/find/ls tools over bash for file "
                              "exploration (faster, respects .gitignore)")
        for t in listed:
            guidelines += PI_BASE_GUIDELINES.get(t, [])
        guidelines.append(
            "Tool output is cut at 2,000 lines or 50 KB, whichever comes first. "
            "Use read offset/limit for large files; bash saves its full output "
            "to a temp file and names it.")
        guidelines += ["Be concise in your responses",
                       "Show file paths clearly when working with files"]
    if guidelines:
        lines += ["", "Guidelines:", *(f"- {g}" for g in guidelines)]
    return "\n".join(lines)


def pi_base_args(extra: list[str],
                 overrides: dict | None = None) -> list[str]:
    """``--tools`` default + ``--system-prompt`` base for one launch. Goes
    BEFORE ``extra`` (the pulse's pi-args.json): pi keeps the last
    --tools and --system-prompt, so a pulse's own value still wins, and
    the base lists the tools that pulse will really have.

    Harness overrides (an experiment): ``overrides["base_prompt"]``
    replaces the whole base prompt text — "" is honoured as an EMPTY
    replacement, not treated as absent (present != truthy);
    ``overrides["base_guidelines"]`` replaces only the
    Guidelines list and is suppressed whenever base_prompt is set;
    ``overrides["tools"]`` replaces the default allowlist (fd rule kept),
    and the base prompt lists exactly the tools that result."""
    args = pi_tools_args(overrides)
    tools = _last_tools(args + extra) or []
    ov = overrides or {}
    base_prompt = ov.get("base_prompt")
    if isinstance(base_prompt, str):
        prompt = base_prompt
    elif isinstance(ov.get("base_guidelines"), list):
        prompt = pi_base_prompt(tools, ov["base_guidelines"])
    else:
        prompt = pi_base_prompt(tools)
    return args + ["--system-prompt", prompt]


HARNESS_OVERRIDES_FILENAME = "harness-overrides.json"


def _utf8_argv_safe(text: str) -> bool:
    """Usable as subprocess argv / prompt text: no embedded NUL and no lone
    surrogate (neither can reach a real process; the API rejects both with a
    422 — the disk reader must fail open the same way)."""
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _validated_harness_overrides(val: dict, path: Path) -> dict:
    """Field-by-field validation of a loaded sidecar object: a malformed value
    is DROPPED with a loud log and the default behaviour applies — a bad knob
    must never prevent a launch in either mode. Accepted shapes: base_prompt /
    todo_prompt / bash_prompt / thinking = utf-8-safe str; base_guidelines =
    list of utf-8-safe str; skill_bodies = null or list of str; tools =
    non-empty list of utf-8-safe tool names (no commas — the allowlist is
    comma-joined); snippet_max = int >= 200; spill_chars = int >= 500;
    compaction / ptc = bool; prompt_parts = null or list of utf-8-safe str
    (consumed by pi_todo_args). The runner/handoff
    knobs (steer_nudge_s, steer_prefix, handoff_prompt,
    successor_prefix, handoff_warning) and the context-render knobs
    (part_text, skill_catalog — applied daemon-side where PULSAR.md is
    rendered) are RECOGNIZED but passed through without a shape check —
    pi does not consume them; their consumers validate their own.
    Unknown keys are dropped too."""
    out: dict = {}
    for key, v in val.items():
        if key in ("base_prompt", "todo_prompt", "bash_prompt", "thinking"):
            if not isinstance(v, str) or not _utf8_argv_safe(v):
                log.warning("harness override %r at %s is not a usable "
                            "string — ignored", key, path)
                continue
        elif key == "base_guidelines":
            if not (isinstance(v, list)
                    and all(isinstance(g, str) and _utf8_argv_safe(g)
                            for g in v)):
                log.warning("harness override base_guidelines at %s is not "
                            "a list of usable strings — ignored", path)
                continue
        elif key == "tools":
            if not (isinstance(v, list) and v
                    and all(isinstance(t, str) and _utf8_argv_safe(t)
                            and "," not in t for t in v)):
                log.warning("harness override tools at %s is not a "
                            "non-empty list of usable tool names "
                            "(no commas) — ignored", path)
                continue
        elif key == "skill_bodies":
            if v is not None and not (
                    isinstance(v, list)
                    and all(isinstance(s, str) and _utf8_argv_safe(s)
                            for s in v)):
                log.warning("harness override skill_bodies at %s is not "
                            "null or a list of strings — ignored", path)
                continue
        elif key == "snippet_max":
            if not (isinstance(v, int) and not isinstance(v, bool)
                    and v >= 200):
                log.warning("harness override snippet_max at %s is not an "
                            "int >= 200 — ignored", path)
                continue
        elif key == "spill_chars":
            if not (isinstance(v, int) and not isinstance(v, bool)
                    and v >= 500):
                log.warning("harness override spill_chars at %s is not an "
                            "int >= 500 — ignored", path)
                continue
        elif key in ("compaction", "ptc"):
            if not isinstance(v, bool):
                log.warning("harness override %s at %s is not a "
                            "bool — ignored", key, path)
                continue
        elif key == "prompt_parts":
            # Consumed by pi_todo_args: the ONE effective allowlist the
            # context renderer and the default-skills merge also use.
            if v is not None and not (
                    isinstance(v, list)
                    and all(isinstance(p, str) and _utf8_argv_safe(p)
                            for p in v)):
                log.warning("harness override prompt_parts at %s is not "
                            "null or a list of usable strings — ignored",
                            path)
                continue
        elif key in ("steer_nudge_s", "steer_prefix", "handoff_prompt",
                     "successor_prefix", "handoff_warning",
                     "part_text", "skill_catalog"):
            # Recognized pass-through: consumed by the runner / the daemon's
            # handoff prompt builders, not by pi. No shape check here —
            # warning on every pi launch for a runner knob would be noise.
            pass
        else:
            log.warning("unknown harness override key %r at %s — ignored",
                        key, path)
            continue
        out[key] = v
    return out


def pi_harness_overrides(cwd: str | os.PathLike) -> dict:
    """Per-pulse harness knobs from ``<home>/harness-overrides.json``
    (parent of the cli ``cwd``), written by the spawner from
    diet["harness_overrides"] at every materialize (an experiment,
    2026-10-22). Absent file = {} = today's behaviour, byte-for-byte.
    Malformed content is ignored loudly, like pi-args.json.

    Parsing goes through the shared reader (pulsar.harness_overrides.
    read_overrides): its own ad-hoc parse could raise RecursionError on a
    deeply-nested file and stop pi before launch.
    The shared reader never raises and warns once per path per process."""
    path = Path(cwd).parent / HARNESS_OVERRIDES_FILENAME
    val = read_overrides(Path(cwd).parent)
    validated = _validated_harness_overrides(val, path)
    log.info("vessel_pi harness overrides from %s: %s", path, validated)
    return validated


def pi_extra_args(cwd: str | os.PathLike) -> list[str]:
    """Caller-owned per-pulse CLI args: a ``pi-args.json`` in the pulse home
    (parent of the cli cwd) holding a JSON array of strings is appended to
    every pi invocation — e.g. ``["-e", "<path>/mcp-bridge.ts", "--tools",
    "read,mcp__custom__…"]``. Same pattern as the qwen vessel's
    qwen-endpoint.json: one pulse gets extensions/cages without touching
    shared vessel code. Malformed or non-list content is ignored loudly."""
    path = Path(cwd).parent / "pi-args.json"
    if not path.is_file():
        return []
    try:
        val = json.loads(path.read_text())
    except Exception:
        log.exception("unreadable pi-args.json at %s — ignored", path)
        return []
    if isinstance(val, list) and all(isinstance(x, str) for x in val):
        log.info("vessel_pi extra args from %s: %s", path, val)
        return val
    log.warning("pi-args.json at %s is not a list of strings — ignored", path)
    return []


_STREAM_LINE_LIMIT = 1 << 24
_SNIPPET_MAX = 8000
_random_session_uuid = random_session_uuid


def _snippet_max_from(overrides: dict) -> int:
    """The tool-result snippet cap for this launch: harness override
    ``snippet_max`` when it is a valid int >= 200, else the default
    ``_SNIPPET_MAX``. (The loader drops malformed values, so this only
    guards against direct callers.)"""
    v = (overrides or {}).get("snippet_max")
    if isinstance(v, int) and not isinstance(v, bool) and v >= 200:
        return v
    return _SNIPPET_MAX


def disable_pi_auto_compaction(cwd: str | os.PathLike,
                               *, compaction_enabled: bool = False) -> None:
    """Disable both threshold and overflow compaction for Pulsar Pi sessions.

    ``compaction_enabled=True`` (harness override ``compaction``): pi's OWN
    auto compaction stays ENABLED for that pulse — the setting is actively
    written true so a stale disable from an earlier spawn cannot survive.

    Also turns OFF the provider SDK's own retries (``retry.provider.maxRetries
    = 0``) — always, whatever the compaction override says. The Anthropic SDK
    pi uses sleeps for the full ``retry-after`` a 429 sends, unbounded: Z.ai's
    usage-limit 429 (code 1308) says ``retry-after: 6843`` and pi went silent
    until the 1200 s stall kill (2026-10-10). pi's own auto-retry (3 tries,
    2/4/8 s, visible as ``auto_retry_*`` records) still covers transient
    errors.
    """
    path = Path(cwd) / ".pi" / "settings.json"
    settings = json.loads(path.read_text()) if path.exists() else {}
    settings.setdefault("compaction", {})["enabled"] = bool(compaction_enabled)
    settings.setdefault("retry", {}).setdefault("provider", {})["maxRetries"] = 0
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_json(path, settings, indent=2, newline=True)


def resolve_resume_session(session_dir: Path,
                           requested: str | None) -> str | None:
    """Turn a requested ``--session`` id into one pi will actually accept.

    pi exit(1)s ("No session found matching") on an id with no session file,
    which killed the pulse on every wake/restore/model-switch whenever the
    heartbeat carried a stale or made-up id (rpc mode used to record a
    Pulsar-minted uuid, never pi's real one). ``pi-sessions/`` under a pulse
    cwd belongs to that pulse alone, so the fallback is safe: the requested
    id if its file exists, else the NEWEST session file's id (that IS this
    pulse's latest conversation), else None (fresh spawn). Never raise.
    """
    if not requested:
        return None
    try:
        files = sorted(Path(session_dir).glob("*.jsonl"),
                       key=lambda f: f.stat().st_mtime)
    except OSError:
        files = []
    if any(f.stem == requested or f.stem.endswith(f"_{requested}")
           for f in files):
        return requested
    for f in reversed(files):
        candidate = f.stem.rsplit("_", 1)[-1]
        if candidate:
            log.warning("pi resume id %s has no session file in %s — "
                        "resuming newest session %s instead",
                        requested, session_dir, candidate)
            return candidate
    log.warning("pi resume id %s has no session file in %s and the dir is "
                "empty — spawning fresh", requested, session_dir)
    return None
_cancel = cancel_task

# Watchdog. pi's bash tool takes an OPTIONAL timeout and defaults to none, so a
# model that shells out to something unbounded (e.g. `find /`) can wedge the
# turn forever. Every stdout line counts as progress, and pi streams
# message_update tokens while generating. However, local models under
# concurrent load can go >300s silent during prefill + thinking with nothing
# streamed, so a tight timeout kills healthy sessions. Silence is not a
# reliable short-interval hang signal for local models, so 1200s stays;
# runaway disk walks are handled upstream by `pulsar-search`.
_STALL_TIMEOUT = float(os.environ.get("PULSAR_PI_STALL_TIMEOUT", "1200"))
_STALL_POLL = 15.0

# pids we spawned whose children could outlive us — reaped at interpreter exit,
# so a daemon restart never leaks pi orphans (parity with qwen_cli's reaper).
_OWNED_PIDS: set[int] = set()
_atexit_registered = False


def _register_reaper() -> None:
    global _atexit_registered
    if _atexit_registered:
        return
    _atexit_registered = True

    def _reap() -> None:
        for pid in list(_OWNED_PIDS):
            safe_killpg(pid, signal.SIGKILL)  # guarded: never a recycled pid
    atexit.register(_reap)


async def _wait_owned(proc: asyncio.subprocess.Process) -> None:
    """Wait for ``proc`` to exit, then drop its pid from ``_OWNED_PIDS`` (also
    when the wait raises or the waiter task is cancelled)."""
    try:
        await proc.wait()
    finally:
        _OWNED_PIDS.discard(proc.pid)








def _usage_of(turn_end: dict[str, Any]) -> dict[str, int]:
    """Real pi (0.73.1) carries usage on turn_end.MESSAGE.usage, not turn_end."""
    msg = turn_end.get("message") or {}
    usage = msg.get("usage") or {}
    tin = int(usage.get("input") or 0)
    tout = int(usage.get("output") or 0)
    tin += int(usage.get("cacheRead") or 0) + int(usage.get("cacheWrite") or 0)
    return {"in": tin, "out": tout, "ctx": tin + tout}


def _output_cap_payload(msg: dict[str, Any], text: str) -> dict[str, Any]:
    """runner_error payload for a pi assistant message that stopped on
    ``length`` (max_tokens). Says where the budget went so the fix is obvious:
    raise the provider's maxTokens or lower the thinking effort."""
    usage = msg.get("usage") or {}
    out = int(usage.get("output") or 0)
    thinking = sum(len(c.get("thinking") or c.get("text") or "")
                   for c in (msg.get("content") or [])
                   if isinstance(c, dict) and c.get("type") in ("thinking", "reasoning"))
    tools = sum(1 for c in (msg.get("content") or [])
                if isinstance(c, dict) and c.get("type") == "toolCall")
    where = ("mid-reasoning" if thinking and not text.strip()
             else "mid-reply" if text.strip() else "mid-turn")
    reason = (f"output cap hit {where}: stop_reason=length after {out} output tokens"
              f" ({thinking} chars of thinking, {tools} tool calls,"
              f" {'partial' if text.strip() else 'no'} reply)"
              " — raise the provider maxTokens or lower the thinking effort")
    return {"where": "output_cap", "reason": reason, "output_tokens": out,
            "thinking_chars": thinking}


def _provider_error_payload(msg: dict[str, Any]) -> dict[str, Any]:
    """runner_error payload for a pi assistant message that stopped on
    ``error`` (the provider rejected or failed the request). Carries the
    provider's own message so the cause is readable in the pane."""
    err = str(msg.get("errorMessage") or "provider returned an error")
    err = " ".join(err.split())
    reason = (f"provider error: stop_reason=error from "
              f"{msg.get('provider') or '?'}/{msg.get('model') or '?'} — {err[:400]}")
    return {"where": "provider_error", "reason": reason, "error": err[:2000]}


def _partial_block(update: dict[str, Any]) -> dict[str, Any]:
    """The content block an AssistantMessageEvent's ``contentIndex`` points
    at in its ``partial`` message, or {} when the shape is missing."""
    idx = update.get("contentIndex")
    content = (update.get("partial") or {}).get("content")
    if (isinstance(idx, int) and isinstance(content, list)
            and 0 <= idx < len(content) and isinstance(content[idx], dict)):
        return content[idx]
    return {}


def classify_record(record: dict[str, Any],
                     snippet_max: int = _SNIPPET_MAX) -> list[VesselEvent]:
    """Map ONE pi print-mode JSONL record into VesselEvents (schema VERIFIED
    against live bench transcripts 2026-08-19: toolCallId on start+end, usage on
    turn_end.message.usage, agent_end = process-over not session-over).

    ``snippet_max`` is the tool-result snippet cap; the harness override
    ``snippet_max`` replaces the default ``_SNIPPET_MAX`` for the launch."""
    snippet_of = functools.partial(snippet, limit=snippet_max)
    etype = record.get("type")
    if etype == "message_update":
        # Pi's json + rpc modes stream the current assistant message as an
        # AssistantMessageEvent.  The completed text still arrives later in
        # message_end; deltas belong only on the runner's ephemeral
        # partial.json surface so the durable log stays one block per line.
        update = record.get("assistantMessageEvent") or {}
        if update.get("type") == "text_delta" and update.get("delta"):
            return [VesselEvent(kind="assistant_delta",
                                payload={"text": update["delta"]})]
        # Thinking and tool-call JSON are generated tokens too. The speed
        # meter needs them so output tokens are timed over the full generation
        # window including hidden thinking. Reasoning text is preserved for the
        # pane's live reasoning panel (reasoning_delta → reasoning.json); the
        # tool-call argument JSON feeds the live code card (code_delta →
        # code.json), metered like hidden_delta.
        if update.get("type") == "thinking_delta" and update.get("delta"):
            return [VesselEvent(kind="reasoning_delta",
                                payload={"text": update["delta"]})]
        if update.get("type") == "toolcall_delta" and update.get("delta"):
            # The live code card: name/id come from the partial message's
            # tool-call block (pi-ai ToolCall {id, name, arguments}).
            call = _partial_block(update)
            name, cid = call.get("name"), call.get("id")
            return [VesselEvent(kind="code_delta", payload={
                "text": update["delta"],
                "name": name if isinstance(name, str) and name else None,
                "id": cid if isinstance(cid, str) and cid else None})]
        return []
    if etype == "message_end":
        msg = record.get("message") or {}
        if msg.get("role") == "assistant":
            chunks = []
            for c in (msg.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text" and c.get("text"):
                    chunks.append(c["text"])
            text = "\n".join(chunks)
            events: list[VesselEvent] = []
            if chunks and text.strip():
                events.append(VesselEvent(kind="assistant_text",
                                          payload={"text": text}))
            # Output cap hit: if a model spends all max_tokens inside its
            # thinking block, pi ends the turn with stopReason=length and no
            # visible reply — surface the failure loudly in the pane.
            if msg.get("stopReason") == "length":
                events.append(VesselEvent(kind="runner_error",
                                          payload=_output_cap_payload(msg, text)))
            # Provider error: when the backend rejects a request (e.g. context
            # overflow), pi records an empty assistant message with
            # stopReason=error — surface the provider error in the pane.
            elif msg.get("stopReason") == "error":
                events.append(VesselEvent(kind="runner_error",
                                          payload=_provider_error_payload(msg)))
            return events
        return []
    if etype == "tool_execution_start":
        return [VesselEvent(kind="tool_call",
                            payload={"name": record.get("toolName") or "",
                                     "args": record.get("args") or {},
                                     "description": None,
                                     "id": record.get("toolCallId")})]
    if etype == "tool_execution_end":
        res = record.get("result")
        text_snippet = ""
        if isinstance(res, dict):
            for c in (res.get("content") or []):
                if isinstance(c, dict) and c.get("type") == "text":
                    text_snippet = snippet_of(c.get("text"))
                    break
            if not text_snippet:
                text_snippet = snippet_of(res.get("output")
                                          or res.get("details"))
        return [VesselEvent(kind="tool_result",
                            payload={"ok": not bool(record.get("isError")),
                                     "snippet": text_snippet,
                                     "exit_code": None,
                                     "id": record.get("toolCallId")})]
    if etype == "turn_end":
        # A pi ``turn_end`` is ONE agent STEP (pi increments turnIndex on each
        # one), not the end of the user's turn: a single message routinely
        # produces a dozen. Carry its usage — the running ctx meter wants every
        # step — but NEVER a done, or the runner acks the message and flips the
        # pulse idle while pi is still working (seen live 2026-08-23: 13 step
        # boundaries after the ack, all invisible, plus an orphan pi per
        # follow-up message).
        return [VesselEvent(kind="usage", payload=_usage_of(record))]
    if etype == "agent_end":
        # The agent loop is over -> the user's turn is over. `_read_stdout`
        # backstops this on clean process exit, so a pi that retries past an
        # agent_end (auto-retry / auto-compaction) still ends exactly once.
        return [VesselEvent(kind="done",
                            payload={"exit_code": 0, "reason": "result"})]
    # session / agent_start / turn_start / message_start /
    # tool_execution_update -> no discrete VesselEvent
    return []


# How long a done from an errored agent_end is held to see whether pi
# auto-retries it. pi writes auto_retry_start right after that agent_end
# (same event-loop tick), so 2 s is ample.
_RETRY_GRACE_S = float(os.environ.get("PULSAR_PI_RETRY_GRACE_S", "2"))


def _is_error_agent_end(record: dict[str, Any]) -> bool:
    """agent_end whose last assistant message stopped on ``error``."""
    if record.get("type") != "agent_end":
        return False
    for m in reversed(record.get("messages") or []):
        if isinstance(m, dict) and m.get("role") == "assistant":
            return m.get("stopReason") == "error"
    return False


def _provisional_provider_error_gate(sess: "_Session",
                                     record: dict[str, Any],
                                     deliver, snippet_max: int) -> bool:
    """Hold the runner_error of a provider error PROVISIONALLY while
    recovery may still win (2026-10-23). Returns True when
    the record is consumed here and the read loop must not classify it.

    On context overflow pi records an empty assistant message with
    stopReason=error, then runs compaction and retries — the turn can still
    SUCCEED. classify_record maps that message_end to runner_error
    immediately, the runner sets _turn_provider_error, and the failover
    path re-sends the message even though compaction recovered and the turn
    completed.

    So ONLY the runner_error is held on the session; the record's other
    content events (assistant text the provider streamed before the error)
    are delivered exactly as the base does.
    Recovery signals (auto_retry_start / compaction) set recovery_started,
    the retry's own assistant text sets recovered_text, and the turn's final
    done decides — _deliver drops the held error on verified recovery,
    _flush_prov_error delivers it on any other turn end.
    """
    if record.get("type") != "message_end":
        return False
    msg = record.get("message") or {}
    if msg.get("role") != "assistant":
        return False
    if msg.get("stopReason") == "error":
        sess.prov_error = VesselEvent(
            kind="runner_error", payload=_provider_error_payload(msg))
        sess.recovery_started = False
        sess.recovered_text = False
        for ev in classify_record(record, snippet_max):
            if ev.kind != "runner_error":
                deliver(ev)
        log.info("vessel_pi_provider_error_provisional sid=%s err=%.200s",
                 sess.sid, msg.get("errorMessage"))
        return True
    if sess.prov_error is not None and sess.recovery_started:
        # A message from the RETRY: non-empty text means recovery worked.
        for c in (msg.get("content") or []):
            if isinstance(c, dict) and c.get("type") == "text" \
                    and c.get("text") and c["text"].strip():
                sess.recovered_text = True
                break
    return False


def _flush_prov_error(sess: "_Session") -> None:
    """Deliver a still-held provisional provider error (the turn is ending
    WITHOUT verified recovery) and clear the recovery state."""
    pe, sess.prov_error = sess.prov_error, None
    sess.recovery_started = False
    sess.recovered_text = False
    if pe is not None:
        sess.event_queue.put_nowait(pe)


def _flush_held_done(sess: "_Session", deliver) -> None:
    if sess.held_timer is not None:
        sess.held_timer.cancel()
        sess.held_timer = None
    held, sess.held_done = sess.held_done, None
    if held is not None:
        deliver(held)


def _retry_gate(sess: "_Session", record: dict[str, Any], deliver) -> bool:
    """Keep pi's auto-retry inside ONE turn. Returns True when ``record`` is
    fully handled here.

    pi ends every failed attempt with agent_end, THEN decides to retry
    (auto_retry_start). Mapping that agent_end straight to done ended the
    turn on attempt 1 while pi kept retrying (and kept working) behind the
    runner's back. So the done of an errored agent_end is held: dropped when
    auto_retry_start follows, delivered on anything else or after
    _RETRY_GRACE_S. auto_retry_end{success:false} (retries exhausted or
    cancelled by abort) closes the turn if nothing else did.

    Compaction (2026-10-23) needs the same hold: on
    context overflow pi ends the attempt with an error agent_end, then
    runs compaction_start/compaction_end{willRetry:true} and retries the
    turn. compaction_start swallows the held done; the retry's own normal
    agent_end then closes the turn with exactly one done. compaction_end
    WITHOUT willRetry closes the turn only when a done was actually
    swallowed (a mid-turn threshold compaction holds nothing and must not
    end the turn early).
    """
    rtype = record.get("type")
    if rtype == "compaction_start":
        if sess.held_timer is not None:
            sess.held_timer.cancel()
            sess.held_timer = None
        # An error agent_end's done may be held; compaction owns the turn
        # boundary now. Remember whether a done was swallowed so
        # compaction_end can close the turn if recovery does NOT retry.
        sess.comp_held = sess.held_done is not None
        sess.held_done = None
        # Compaction is a recovery attempt for a held provider error too.
        if sess.prov_error is not None:
            sess.recovery_started = True
        log.info("vessel_pi_compaction_start sid=%s reason=%s held_done=%s",
                 sess.sid, record.get("reason"), sess.comp_held)
        return True
    if rtype == "compaction_end":
        comp_held, sess.comp_held = sess.comp_held, False
        log.info("vessel_pi_compaction_end sid=%s reason=%s willRetry=%s "
                 "err=%.200s", sess.sid, record.get("reason"),
                 record.get("willRetry"), record.get("errorMessage"))
        if record.get("willRetry"):
            # Recovery retries the turn; its own normal agent_end delivers
            # the single final done. The retry is recovery for a held
            # provider error as well.
            if sess.prov_error is not None:
                sess.recovery_started = True
            return True
        if comp_held and not sess.turn_done:
            # Recovery failed or was aborted after the error agent_end's
            # done was swallowed — close the turn here (same shape as
            # auto_retry_end{success:false}).
            deliver(VesselEvent(kind="done",
                                payload={"exit_code": 0, "reason": "result"}))
        return True
    if rtype == "auto_retry_start":
        if sess.held_timer is not None:
            sess.held_timer.cancel()
            sess.held_timer = None
        sess.held_done = None
        # An auto retry is a recovery attempt for a held provider error.
        if sess.prov_error is not None:
            sess.recovery_started = True
        log.info("vessel_pi_auto_retry sid=%s attempt=%s/%s delay=%sms err=%.200s",
                 sess.sid, record.get("attempt"), record.get("maxAttempts"),
                 record.get("delayMs"), record.get("errorMessage"))
        return True
    _flush_held_done(sess, deliver)
    if rtype == "auto_retry_end":
        if not record.get("success") and not sess.turn_done:
            deliver(VesselEvent(kind="done",
                                payload={"exit_code": 0, "reason": "result"}))
        return True
    if _is_error_agent_end(record):
        sess.held_done = VesselEvent(kind="done",
                                     payload={"exit_code": 0, "reason": "result"})
        sess.held_timer = asyncio.get_running_loop().call_later(
            _RETRY_GRACE_S, _flush_held_done, sess, deliver)
        return True
    return False


@dataclass
class _Session:
    sid: str
    cwd: str
    session_dir: Path
    cli_session_id: str
    model: str
    env: dict[str, str]
    process: asyncio.subprocess.Process | None = None
    event_queue: asyncio.Queue[VesselEvent] = field(default_factory=asyncio.Queue)
    reader_task: asyncio.Task | None = None
    stderr_task: asyncio.Task | None = None
    waiter_task: asyncio.Task | None = None
    resumed: bool = False
    stopped: bool = False
    killed_by: str | None = None  # "interrupt" | "stop" | "stall"
    turn_done: bool = False  # a done{result} was already emitted for this turn
    turn_in_flight: bool = False  # rpc: a prompt is running; watchdog gates on it
    watchdog_task: asyncio.Task | None = None
    last_progress: float = 0.0  # monotonic ts of the last stdout line
    restart_lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    # Serializes interrupt() (kill+cleanup) against the next send()
    # launching a fresh pi process, so the interrupted turn’s reader
    # can never race a successor on the shared event queue (grok-low
    # REVIEW 9 must-fix #3).
    # rpc: futures for response acks that inject() awaits. Keyed by request
    # id; _read_stdout resolves/raises them when the matching response lands.
    pending_responses: dict = field(default_factory=dict)
    effort: str | None = None
    suppress_reasoning: bool = False
    # done of an errored agent_end, held while pi may auto-retry (_retry_gate)
    held_done: VesselEvent | None = None
    held_timer: asyncio.TimerHandle | None = None
    # compaction swallowed a held done (_retry_gate): compaction_end without
    # willRetry must close the turn
    comp_held: bool = False
    # Provisional provider error: the runner_error a stopReason=error
    # message_end would emit, held while recovery (auto-retry / compaction)
    # may still win the turn. Decided at the turn's final done; delivered
    # by _flush_prov_error on every other turn end.
    prov_error: VesselEvent | None = None
    recovery_started: bool = False
    recovered_text: bool = False


class PiVessel(BaseVessel):
    """Drive ``pi -p --mode json`` as a pulse vessel (phase-1)."""

    supports_interrupt = True  # kill + resume

    def __init__(self, *, model: str | None = None) -> None:
        self._sessions: dict[str, _Session] = {}
        self._model = model or PI_MODEL
        self._bin = os.environ.get("PULSAR_PI_BIN") or shutil.which(DEFAULT_CLI_BINARY)
        if not self._bin:
            raise VesselError("`pi` binary not found on PATH (set PULSAR_PI_BIN)")
        _register_reaper()

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
    ) -> str:
        if not cwd:
            raise VesselError("PiVessel.spawn requires a cwd")
        Path(cwd).mkdir(parents=True, exist_ok=True)
        # Harness override ``compaction``: REFUSED in print mode (2026-10-23).
        # The pinned CLI (dist/modes/print-mode.js)
        # unsubscribes from the session emitter as soon as session.prompt()
        # returns — and on overflow prompt() returns on the ERROR agent_end,
        # before the compaction retry (a ~100 ms setTimeout continuation)
        # runs — so the recovered answer never reaches stdout and the vessel
        # would wait for a done that can never arrive. PiRpcVessel's
        # long-lived subscription supports the knob; this mode keeps pi's own
        # auto compaction disabled (the retry fix still applies).
        _ov_spawn = pi_harness_overrides(cwd)
        if _ov_spawn.get("compaction") is True:
            log.warning("vessel_pi compaction override refused in print mode "
                        "(cwd=%s): `pi -p --mode json` detaches its output "
                        "before overflow recovery finishes, so the recovered "
                        "answer would never reach stdout — pi auto-compaction "
                        "stays disabled; use --mode rpc for compaction", cwd)
        disable_pi_auto_compaction(cwd, compaction_enabled=False)
        session_dir = Path(cwd) / "pi-sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        resume_cli_session_id = resolve_resume_session(
            session_dir, resume_cli_session_id)
        cli_sid = resume_cli_session_id or _random_session_uuid()
        sid = session_id or cli_sid
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        child_env.pop("OPENAI_API_KEY", None)
        sess = _Session(sid=sid, cwd=cwd, session_dir=session_dir,
                        cli_session_id=cli_sid,
                        model=model or self._model,
                        env=child_env,
                        resumed=resume_cli_session_id is not None,
                        effort=effort)
        self._sessions[sid] = sess
        log.info("vessel_pi_spawn sid=%s cwd=%s model=%s effort=%s resume=%s",
                 sid, cwd, sess.model, effort or "-",
                 resume_cli_session_id is not None)
        if initial_prompt:
            await self.send(sid, initial_prompt)
        return sid

    def _build_command(self, sess: _Session, text: str) -> list[str]:
        # Harness overrides (an experiment): per-pulse knobs from
        # <home>/harness-overrides.json; absent file = today's command.
        ov = pi_harness_overrides(sess.cwd)
        wire_model = _wire_model_for(sess.model, sess.cwd)
        thinking = ov.get("thinking")
        if not (isinstance(thinking, str) and thinking):
            thinking = pi_thinking_level(wire_model, sess.effort)
        sess.suppress_reasoning = (
            thinking == "off"
            and _is_glm53_wire_model(wire_model or sess.model)
        )
        cmd = [self._bin, "-p", "--mode", "json",
               "--session-dir", str(sess.session_dir),
               "-nc", "--no-skills", "--no-extensions",
               "--thinking", thinking,
               "--model", wire_model or PI_MODEL]
        extra = pi_extra_args(sess.cwd)
        cmd += pi_base_args(extra, ov)
        cmd += extra
        if sess.resumed:
            cmd += ["--session", sess.cli_session_id]
        # Pulse context: the spawner writes <cwd>/PULSAR.md (own id
        # + pulse-messaging recipe so a pulse is never mute). -nc disables
        # AGENTS.md/CLAUDE.md discovery, so deliver the identity as an explicit
        # system-prompt append. pi's --append-system-prompt accepts a FILE PATH
        # (resolvePromptInput reads it if it exists), so pass the path rather
        # than inlining ~29KB into argv on every send.
        pulsar_md = Path(sess.cwd) / "PULSAR.md"
        if pulsar_md.is_file():
            cmd += ["--append-system-prompt", str(pulsar_md)]
            log.info("vessel_pi pulsar-context appended (%s)",
                     pulsar_md)
        cmd += pi_todo_args(sess.cwd, ov)  # after PULSAR.md: the first append stays identity
        cmd += pi_guard_args(ov)
        cmd += pi_effort_args()
        cmd += pi_script_args(ov)
        cmd += pi_spill_args(ov)
        cmd += pi_skill_args(sess.cwd, ov)
        cmd.append(text)
        return cmd

    async def send(self, sid: str, text: str) -> None:
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"PiVessel.send: unknown sid {sid!r}")
        # A fresh short-lived `pi -p` process per message. Serialize behind any
        # in-flight interrupt cleanup AND wait out the previous turn’s tasks
        # before overwriting sess.process/reader_task: if the previous turn was
        # interrupted and its reader had not yet drained the shared event queue,
        # a new reader would race the old one on the SAME queue (REVIEW 9 #3).
        async with sess.restart_lock:
            _cancel(sess.watchdog_task)
            for t in (sess.reader_task, sess.stderr_task, sess.waiter_task):
                if t and not t.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(t), timeout=10)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        t.cancel()
            # Each send starts a FRESH process owned by this turn. Clear any
            # stale kill reason from a prior interrupt/stop so an abnormal exit
            # of THIS process correctly emits done{exited} (REVIEW 9 #3 — the
            # killed_by residue would otherwise swallow a real crash).
            sess.killed_by = None
            sess.turn_done = False
            sess.comp_held = False
            # Fresh turn: no provisional provider error carries over.
            sess.prov_error = None
            sess.recovery_started = False
            sess.recovered_text = False
            cmd = self._build_command(sess, text)
            log.info("vessel_pi_send sid=%s resume=%s prompt=%.120s",
                     sid, sess.resumed, text)
            try:
                proc = await asyncio.create_subprocess_exec(
                    *cmd, cwd=sess.cwd, env=sess.env,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    limit=_STREAM_LINE_LIMIT, start_new_session=True)
            except Exception as e:  # noqa: BLE001
                log.exception("vessel_pi_spawn_failed sid=%s error=%s", sid, e)
                raise VesselError(f"failed to spawn pi: {e}") from e
            _OWNED_PIDS.add(proc.pid)
            sess.process = proc
            sess.resumed = True
            sess.reader_task = asyncio.create_task(
                self._read_stdout(sess), name=f"pulsar-pi-read-{sid}")
            sess.stderr_task = asyncio.create_task(
                self._pump_stderr(sess), name=f"pulsar-pi-stderr-{sid}")
            sess.waiter_task = asyncio.create_task(
                _wait_owned(proc), name=f"pulsar-pi-wait-{sid}")
            sess.last_progress = time.monotonic()
            sess.watchdog_task = asyncio.create_task(
                self._watchdog(sess, proc), name=f"pulsar-pi-watchdog-{sid}")

    async def _read_stdout(self, sess: _Session) -> None:
        assert sess.process and sess.process.stdout
        # Harness override ``snippet_max``: the tool-result cap for this
        # launch's classify_record calls.
        snippet_max = _snippet_max_from(pi_harness_overrides(sess.cwd))
        try:
            async for raw in sess.process.stdout:
                sess.last_progress = time.monotonic()
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("vessel_pi_unparsed_line sid=%s line=%.200s",
                                sess.sid, line)
                    continue
                if record.get("type") == "session" and record.get("id"):
                    sess.cli_session_id = record["id"]
                    continue
                deliver = functools.partial(self._deliver, sess)
                if _retry_gate(sess, record, deliver):
                    continue
                if _provisional_provider_error_gate(sess, record, deliver,
                                                    snippet_max):
                    continue
                for ev in classify_record(record, snippet_max):
                    deliver(ev)
        finally:
            _flush_held_done(sess, functools.partial(self._deliver, sess))
            _OWNED_PIDS.discard(sess.process.pid)
            # stdout EOF can beat the reap, and the returncode decides which
            # boundary this is — settle it before choosing (bounded so a wedged
            # child can never strand the reader).
            try:
                await asyncio.wait_for(
                    asyncio.shield(sess.process.wait()), timeout=5)
            except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                pass
            rc = sess.process.returncode
            if sess.killed_by == "stall" and not sess.stopped:
                # The turn is dead but the SESSION is fine: pi resumes from
                # --session, so end the turn cleanly and let the next message
                # carry on rather than tearing the pulse down.
                if not sess.turn_done:
                    sess.turn_done = True
                    _flush_prov_error(sess)
                    sess.event_queue.put_nowait(
                        VesselEvent(kind="done",
                                    payload={"exit_code": rc,
                                             "reason": "result"}))
            elif not sess.stopped and sess.killed_by is None:
                if rc not in (0, None):
                    # Abnormal exit with no deliberate kill -> session death.
                    _flush_prov_error(sess)
                    sess.event_queue.put_nowait(
                        VesselEvent(kind="done",
                                    payload={"exit_code": rc,
                                             "reason": "exited"}))
                elif not sess.turn_done:
                    # One short-lived process per message: the process going
                    # away IS the turn boundary. Covers a pi that exits without
                    # agent_end (--mode json header-only runs, killed tool, a
                    # future schema change) so send() can never hang forever.
                    sess.turn_done = True
                    _flush_prov_error(sess)
                    sess.event_queue.put_nowait(
                        VesselEvent(kind="done",
                                    payload={"exit_code": 0,
                                             "reason": "result"}))

    @staticmethod
    def _deliver(sess: _Session, ev: VesselEvent) -> None:
        if sess.suppress_reasoning and ev.kind == "reasoning_delta":
            ev = VesselEvent(kind="hidden_delta", payload=ev.payload)
        if ev.kind == "done" and ev.payload.get("reason") == "result":
            # Decide the provisional provider error AT the turn
            # boundary. Verified recovery (a retry/compaction ran AND produced
            # assistant text) drops the held error; anything else delivers it
            # just before the done. Never decide on the done alone — the
            # recovered turn also ends with a done.
            pe, sess.prov_error = sess.prov_error, None
            if pe is not None \
                    and not (sess.recovery_started and sess.recovered_text):
                sess.event_queue.put_nowait(pe)
            sess.recovery_started = False
            sess.recovered_text = False
            if sess.turn_done:
                return  # one boundary per turn, whatever pi does
            sess.turn_done = True
        sess.event_queue.put_nowait(ev)

    async def _watchdog(self, sess: _Session,
                        proc: asyncio.subprocess.Process) -> None:
        """Kill a turn whose pi process has gone silent for _STALL_TIMEOUT.

        Guards the one hole pi leaves open: its bash tool has no default
        timeout, so an unbounded command (`find /`) blocks the agent loop with
        no output and nothing else ever ends the turn. Kills the process GROUP
        — node's deep children have shrugged off a bare SIGTERM before.
        """
        try:
            while True:
                # Wake on process exit, not just on the poll tick — a finished
                # turn must not leave a watchdog dozing for another 15s.
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()),
                                           timeout=_STALL_POLL)
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                if proc.returncode is not None or sess.stopped:
                    return
                if sess.process is not proc:  # a later turn owns the session
                    return
                stalled = time.monotonic() - sess.last_progress
                if stalled < _STALL_TIMEOUT:
                    continue
                log.error("vessel_pi_turn_stalled sid=%s pid=%s stalled=%.0fs "
                          "timeout=%.0fs — killing process group",
                          sess.sid, proc.pid, stalled, _STALL_TIMEOUT)
                sess.killed_by = "stall"
                self._killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, TimeoutError):
                    self._killpg(proc.pid, signal.SIGKILL)
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("vessel_pi_watchdog_error sid=%s error=%s",
                          sess.sid, e)

    async def _pump_stderr(self, sess: _Session) -> None:
        if not sess.process or not sess.process.stderr:
            return
        try:
            async for raw in sess.process.stderr:
                log.debug("vessel_pi_stderr sid=%s: %.300s", sess.sid,
                          raw.decode("utf-8", "replace").rstrip())
        except Exception:  # noqa: BLE001
            pass

    def cli_session_id(self, sid: str) -> str | None:
        s = self._sessions.get(sid)
        return s.cli_session_id if s else None

    def subscribe(self, sid: str) -> AsyncIterator[VesselEvent]:
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"PiVessel.subscribe: unknown sid {sid!r}")
        return drain_until_exited(sess.event_queue)

    async def interrupt(self, sid: str) -> None:
        s = self._sessions.get(sid)
        if not s or s.process is None or s.process.returncode is not None:
            return
        async with s.restart_lock:
            _cancel(s.watchdog_task)
            # Capture the process NOW — kill only the process that launched this
            # turn, never a later turn that may have replaced s.process by the
            # time the SIGKILL fires (grok-low REVIEW 6).
            proc = s.process
            s.killed_by = "interrupt"
            # No cap after KILL, and silent: interrupt never logged vessel_stop*.
            await terminate_group(proc, sid=sid, kill_timeout=None,
                                  signal_fn=self._killpg, log=None)
            # Let the killed turn’s reader/waiter fully drain before releasing
            # the lock, so the next send() cannot race a live reader on the
            # shared event queue (REVIEW 9 #3).
            for t in (s.reader_task, s.stderr_task, s.waiter_task):
                if t and not t.done():
                    try:
                        await asyncio.wait_for(asyncio.shield(t), timeout=5.0)
                    except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                        t.cancel()
            s.event_queue.put_nowait(VesselEvent(
                kind="done", payload={"exit_code": -15, "reason": "interrupted"}))

    async def stop(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if not s:
            return
        s.stopped = True
        s.killed_by = "stop"
        if s.process and s.process.returncode is None:
            self._killpg(s.process.pid, signal.SIGTERM)
        s.event_queue.put_nowait(VesselEvent(
            kind="done", payload={"exit_code": None, "reason": "exited"}))
        # Let the reader see process exit + transport close before we return
        # (mirrors qwen_cli), so no transport is left dangling at loop teardown.
        _cancel(s.watchdog_task)
        for t in (s.reader_task, s.stderr_task, s.waiter_task):
            if t:
                try:
                    await asyncio.wait_for(t, timeout=5)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    t.cancel()

    @staticmethod
    def _killpg(pid: int | None, sig: int) -> None:
        # Guarded (getpgid == pid) like every other vessel — pi spawns with
        # start_new_session=True, so a live child IS its own group leader and
        # a recycled pid is refused instead of SIGKILLed.
        safe_killpg(pid, sig)
        if pid:
            _OWNED_PIDS.discard(pid)


# PiRpcVessel.abort_bash writes <cwd>/.pi/<this> before it kills a bash
# tool call; guard.ts reads it (keep the two names in step).
ABORT_BASH_STAMP = "abort-bash.stamp"


def _bash_tool_groups(pid: int) -> list[int]:
    """Process groups of pi's running bash tool calls. pi spawns every bash
    command detached (tools/bash.js: its own session), so a child of ``pid``
    that leads its own session is a bash call — never an extension, an MCP
    bridge, or the guard's check, which stay in pi's session."""
    groups: list[int] = []
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            fields = (d / "stat").read_text().rsplit(")", 1)[1].split()
        except (OSError, IndexError):
            continue
        if int(fields[1]) == pid and int(fields[3]) == int(d.name):
            groups.append(int(d.name))
    return groups


class PiRpcVessel(BaseVessel):
    """Drive ``pi --mode rpc`` as a long-lived pulse vessel (phase-2).

    ONE ``pi --mode rpc`` process per SESSION (not per message): stdin takes
    JSONL commands, stdout streams JSONL events + ``response`` acks. This is
    what unlocks ``can_inject`` — pi's ``steer`` command queues a user message
    delivered after the current assistant turn's tool calls, before the next
    LLM call, WITHOUT respawning the process. The json vessel's
    kill-and-resume interrupt costs a full re-prefill every interjection
    (~2.5 min TTFT on an 80k prompt); rpc steer keeps the KV cache warm, so an
    interjection lands in seconds with ``prompt_tokens_by_source_total
    {source=local_compute}`` barely moving.

    Public surface mirrors PiVessel: ``spawn / send / subscribe / interrupt /
    cli_session_id / stop`` PLUS ``inject`` (the steer path). ``send`` writes a
    ``prompt`` command; ``inject`` writes a ``steer`` command; ``interrupt``
    writes ``abort`` (SIGKILL fallback if the process ignores it).

    TWO INVersions from PiVessel, both forced by the long-lived process model:
      1. The stall watchdog MUST gate on ``turn_in_flight``. In PiVessel the
         process dies at end of turn, so 'alive + silent' means 'wedged'. Here
         the process lives across turns, so an IDLE pulse is silent by
         definition — ungated, the watchdog would SIGKILL a healthy idle
         session 20 minutes after it goes quiet. Parity with qwen_cli's guard.
      2. Process exit is SESSION DEATH and emits ``done{reason: exited}`` —
         the event that ends the subscribe stream. In PiVessel a clean exit IS
         the turn boundary (``done{result}``); here emitting ``result`` on exit
         would keep the stream open, lie to the runner that the turn ended
         fine, and let it feed the next message into a dead process.
    """

    supports_inject = True     # rpc `steer`
    supports_interrupt = True  # rpc `abort`
    supports_abort_bash = True  # rpc `abort_bash` (wedged-tool steer delivery)

    def __init__(self, *, model: str | None = None) -> None:
        self._sessions: dict[str, _Session] = {}
        self._model = model or PI_MODEL
        self._bin = os.environ.get("PULSAR_PI_BIN") or shutil.which(
            DEFAULT_CLI_BINARY)
        if not self._bin:
            raise VesselError(
                "`pi` binary not found on PATH (set PULSAR_PI_BIN)")
        _register_reaper()

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
    ) -> str:
        if not cwd:
            raise VesselError("PiRpcVessel.spawn requires a cwd")
        Path(cwd).mkdir(parents=True, exist_ok=True)
        # Harness override ``compaction``: when true, pi's OWN auto
        # compaction stays enabled for this pulse (the retry fix still
        # applies). The overrides are loaded once here and reused below.
        ov = pi_harness_overrides(cwd)
        disable_pi_auto_compaction(
            cwd, compaction_enabled=ov.get("compaction") is True)
        session_dir = Path(cwd) / "pi-sessions"
        session_dir.mkdir(parents=True, exist_ok=True)
        resume_cli_session_id = resolve_resume_session(
            session_dir, resume_cli_session_id)
        cli_sid = resume_cli_session_id or _random_session_uuid()
        sid = session_id or cli_sid
        child_env = dict(os.environ)
        if env:
            child_env.update(env)
        child_env.pop("OPENAI_API_KEY", None)
        sess = _Session(sid=sid, cwd=cwd, session_dir=session_dir,
                        cli_session_id=cli_sid,
                        model=model or self._model,
                        env=child_env,
                        resumed=resume_cli_session_id is not None,
                        effort=effort)
        self._sessions[sid] = sess
        # ``--session <id>`` IS honored in rpc mode: dist/main.js runs
        # createSessionManager BEFORE the rpc mode dispatch, so session
        # selection is mode-independent and the restore path is identical to
        # PiVessel's. (switch_session{sessionPath} is the fallback if a future
        # pi build regresses this.)
        # Harness overrides (an experiment): per-pulse knobs from
        # <home>/harness-overrides.json; absent file = today's command.
        # (Loaded once at the top of spawn for the compaction knob.)
        wire_model = _wire_model_for(sess.model, cwd)
        thinking = ov.get("thinking")
        if not (isinstance(thinking, str) and thinking):
            thinking = pi_thinking_level(wire_model, sess.effort)
        sess.suppress_reasoning = (
            thinking == "off"
            and _is_glm53_wire_model(wire_model or sess.model)
        )
        cmd = [self._bin, "--mode", "rpc",
               "--session-dir", str(session_dir),
               "-nc", "--no-skills", "--no-extensions",
               "--thinking", thinking,
               "--model", wire_model or PI_MODEL]
        extra = pi_extra_args(cwd)
        cmd += pi_base_args(extra, ov)
        cmd += extra
        # ONLY on resume. `--session <id>` for an id that does not exist yet is
        # fatal, not a create: pi prints "No session found matching '<id>'" and
        # exit(1)s (dist/main.js:196). A fresh spawn mints its own id and the
        # `session` record on stdout corrects sess.cli_session_id — same
        # contract as PiVessel. Passing it unconditionally killed every FRESH
        # pi-glm pulse, and every reset and handoff (handoff respawns with
        # resume=None) — verified against the real binary 2026-08-23.
        if sess.resumed:
            cmd += ["--session", cli_sid]
        # Pulse identity. -nc disables AGENTS.md/CLAUDE.md
        # discovery, so PULSAR.md — the pulse's own id and the pulse-messaging
        # recipe — only reaches the model as an explicit system-prompt append.
        # Without it a pulse cannot say who it is or message anyone.
        pulsar_md = Path(cwd) / "PULSAR.md"
        if pulsar_md.is_file():
            cmd += ["--append-system-prompt", str(pulsar_md)]
            log.info("vessel_pi_rpc pulsar-context appended (%s)", pulsar_md)
        cmd += pi_todo_args(cwd, ov)  # after PULSAR.md: the first append stays identity
        cmd += pi_guard_args(ov)
        cmd += pi_effort_args()
        cmd += pi_script_args(ov)
        cmd += pi_spill_args(ov)
        cmd += pi_skill_args(cwd, ov)
        log.info("vessel_pi_rpc_spawn sid=%s cwd=%s model=%s effort=%s resume=%s",
                 sid, cwd, sess.model, effort or "-",
                 resume_cli_session_id is not None)
        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd, cwd=cwd, env=child_env,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                limit=_STREAM_LINE_LIMIT, start_new_session=True)
        except Exception as e:  # noqa: BLE001
            log.exception("vessel_pi_rpc_spawn_failed sid=%s error=%s", sid, e)
            raise VesselError(f"failed to spawn pi rpc: {e}") from e
        _OWNED_PIDS.add(proc.pid)
        sess.process = proc
        sess.last_progress = time.monotonic()
        sess.reader_task = asyncio.create_task(
            self._read_stdout(sess), name=f"pulsar-pi-rpc-read-{sid}")
        sess.stderr_task = asyncio.create_task(
            self._pump_stderr(sess), name=f"pulsar-pi-rpc-stderr-{sid}")
        sess.waiter_task = asyncio.create_task(
            _wait_owned(proc), name=f"pulsar-pi-rpc-wait-{sid}")
        sess.watchdog_task = asyncio.create_task(
            self._watchdog(sess, proc), name=f"pulsar-pi-rpc-watch-{sid}")
        # rpc mode never prints a `session` record on stdout, so the minted
        # uuid above would be reported as the CLI session id forever — and a
        # later `--session <uuid>` (model switch, restore) exits 1 with "No
        # session found". The REAL id is only in get_state's `sessionId`;
        # ask for it once at boot (_read_stdout stores it from the response).
        await self._learn_session_id(sess)
        if initial_prompt:
            await self.send(sid, initial_prompt)
        return sid

    async def _learn_session_id(self, sess: _Session) -> None:
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req_id = f"get_state-{id(fut):x}"
        sess.pending_responses[req_id] = fut
        try:
            await self._write_cmd(sess, {"type": "get_state", "id": req_id})
            await asyncio.wait_for(fut, timeout=10.0)
            log.info("vessel_pi_rpc_session sid=%s cli_session_id=%s",
                     sess.sid, sess.cli_session_id)
        except Exception as e:  # noqa: BLE001 — best effort, never fatal
            log.warning("vessel_pi_rpc_get_state_failed sid=%s err=%s",
                        sess.sid, e)
        finally:
            sess.pending_responses.pop(req_id, None)

    # ---- commands over stdin ------------------------------------------

    async def _write_cmd(self, sess: _Session, cmd: dict[str, Any]) -> None:
        proc = sess.process
        if proc is None or proc.returncode is not None or proc.stdin is None:
            raise VesselError(f"pi rpc session {sess.sid!r} has no live stdin")
        line = json.dumps(cmd, separators=(",", ":")) + "\n"
        try:
            proc.stdin.write(line.encode("utf-8"))
            await proc.stdin.drain()
        except (OSError, ConnectionResetError, RuntimeError) as e:
            raise VesselError(f"failed to write to pi rpc stdin: {e}") from e

    async def send(self, sid: str, text: str) -> None:
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"PiRpcVessel.send: unknown sid {sid!r}")
        # A new prompt starts a turn. The watchdog now cares about silence;
        # agent_end clears the flag and the done{result} boundary closes it.
        sess.turn_in_flight = True
        sess.turn_done = False
        sess.comp_held = False
        sess.killed_by = None
        # Fresh turn: no provisional provider error carries over.
        sess.prov_error = None
        sess.recovery_started = False
        sess.recovered_text = False
        sess.last_progress = time.monotonic()
        log.info("vessel_pi_rpc_send sid=%s resume=%s prompt=%.120s",
                 sid, sess.resumed, text)
        # The prompt carries an id and we await pi's `response` ack: a
        # rejected prompt (success:false) used to be only logged while the
        # runner sat out the full stall timeout waiting for a turn that never
        # started. A slow ack is not an error — log and let the turn proceed.
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req_id = f"prompt-{id(fut):x}"
        sess.pending_responses[req_id] = fut
        try:
            await self._write_cmd(sess, {"type": "prompt", "message": text,
                                         "id": req_id})
            await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            log.warning("vessel_pi_rpc_prompt_ack_timeout sid=%s — proceeding",
                        sid)
        finally:
            sess.pending_responses.pop(req_id, None)

    async def inject(self, sid: str, text: str) -> None:
        """Queue a steering message into the LIVE turn (mid-turn inject).

        pi's ``steer`` is delivered after the current assistant turn's tool
        calls, before the next LLM call — NO respawn, so the KV cache stays
        warm and there is no re-prefill (the win over kill-and-resume). Only
        valid while a turn is in flight; the runner gates on ``can_inject``.

        Awaits the matching ``response`` ack: a rejected steer (success:false)
        raises VesselError so _inject_loop's except path un-queues the message
        instead of acking a message the model never saw (silent message loss
        is the wrong failure mode).
        """
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"PiRpcVessel.inject: unknown sid {sid!r}")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req_id = f"steer-{id(fut):x}"
        sess.pending_responses[req_id] = fut
        log.info("vessel_pi_rpc_steer sid=%s steer=%.120s", sid, text)
        try:
            await self._write_cmd(sess, {"type": "steer",
                                         "message": text, "id": req_id})
            await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            raise VesselError("pi rpc steer timed out waiting for response")
        finally:
            sess.pending_responses.pop(req_id, None)

    async def abort_bash(self, sid: str) -> None:
        """Abort the RUNNING bash tool without ending the turn.

        The surgical steer-delivery fix (seen live 2026-09-18: an ssh that
        never exited wedged the turn, and three accepted steers sat
        undelivered — pi delivers a steer only before the NEXT LLM call,
        which a wedged tool never lets happen). ``abort_bash`` kills the
        wedged tool; its partial output returns as an error result, the
        agent loop CONTINUES on the same session, and every queued steer
        is delivered. Acks like steer; ``success:false`` raises so the
        runner can escalate to a full turn interrupt.

        pi 0.73.1's rpc ``abort_bash`` stops only a user ``bash`` rpc
        command (session.executeBash), never the agent's bash TOOL, so the
        ack alone freed nothing. After the ack
        the running bash tool calls are killed here: the tool returns an
        error result and the loop goes on, the same as pi's own timeout.
        """
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"PiRpcVessel.abort_bash: unknown sid {sid!r}")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future = loop.create_future()
        req_id = f"abort-bash-{id(fut):x}"
        sess.pending_responses[req_id] = fut
        log.info("vessel_pi_rpc_abort_bash sid=%s — aborting wedged tool", sid)
        try:
            await self._write_cmd(sess, {"type": "abort_bash", "id": req_id})
            await asyncio.wait_for(fut, timeout=10.0)
        except asyncio.TimeoutError:
            raise VesselError("pi rpc abort_bash timed out waiting for response")
        finally:
            sess.pending_responses.pop(req_id, None)
        if sess.process is not None and sess.process.returncode is None:
            groups = _bash_tool_groups(sess.process.pid)
            if groups:
                # A killed command exits on a signal (code null), which pi
                # reports as success with "(no output)". guard.ts turns a
                # bash result that ends after this stamp into an error with
                # a note, so the model knows the command did not finish.
                stamp = Path(sess.cwd) / ".pi" / ABORT_BASH_STAMP
                stamp.parent.mkdir(parents=True, exist_ok=True)
                stamp.write_text(str(time.time()))
            for pgid in groups:
                safe_killpg(pgid, signal.SIGKILL)
            log.info("vessel_pi_rpc_abort_bash sid=%s killed %d bash tool "
                     "group(s) %s", sid, len(groups), groups)

    def last_progress_ts(self, sid: str) -> float | None:
        """Monotonic ts of the last REAL stream progress on sid (or None).

        The runner's steer nudge uses this to tell a wedged tool (frozen)
        from a live one streaming tool partials. Chatter (response /
        queue_update) deliberately does not move it — see ``_read_stdout``.
        """
        sess = self._sessions.get(sid)
        return sess.last_progress if sess is not None else None

    # ---- stdout / events ----------------------------------------------

    async def _read_stdout(self, sess: _Session) -> None:
        assert sess.process and sess.process.stdout
        # Harness override ``snippet_max``: the tool-result cap for this
        # launch's classify_record calls.
        snippet_max = _snippet_max_from(pi_harness_overrides(sess.cwd))
        try:
            async for raw in sess.process.stdout:
                line = raw.decode("utf-8", "replace").rstrip("\n")
                if not line:
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("vessel_pi_rpc_unparsed_line sid=%s line=%.200s",
                                sess.sid, line)
                    sess.last_progress = time.monotonic()
                    continue
                # Protocol chatter is NOT agent progress: every accepted
                # steer emits a queue_update + response pair, and each one
                # was petting the stall watchdog's 20-min clock (live
                # 2026-09-18: a wedged ssh tool survived three
                # steers because every ack reset last_progress). Only real
                # stream records — events, tool partials — count as progress.
                if record.get("type") not in ("response", "queue_update"):
                    sess.last_progress = time.monotonic()
                # rpc interleaves `response` acks (prompt/steer/abort accepted)
                # with `event` records. Responses carry an `id` for correlation
                # and are NOT agent events. A failed response (success: false)
                # is delivered to the waiting caller (inject) so it can raise;
                # unmatched failures are logged so a rejected prompt surfaces.
                if record.get("type") == "response":
                    rid = record.get("id")
                    real = (record.get("data") or {}).get("sessionId") \
                        if isinstance(record.get("data"), dict) else None
                    if real:
                        sess.cli_session_id = real
                    fut = sess.pending_responses.pop(rid, None) if rid else None
                    if fut is not None and not fut.done():
                        if record.get("success", True):
                            fut.set_result(None)
                        else:
                            fut.set_exception(VesselError(
                                f"pi rpc {record.get('command', '?')} rejected: "
                                f"{record.get('error', 'unknown')}"))
                    elif not record.get("success", True):
                        log.error("vessel_pi_rpc_cmd_rejected sid=%s cmd=%s "
                                  "error=%s", sess.sid,
                                  record.get("command"), record.get("error"))
                    continue
                # session record carries the CLI session id (for --resume).
                if record.get("type") == "session" and record.get("id"):
                    sess.cli_session_id = record["id"]
                    continue
                deliver = functools.partial(self._deliver, sess)
                if _retry_gate(sess, record, deliver):
                    continue
                if _provisional_provider_error_gate(sess, record, deliver,
                                                    snippet_max):
                    continue
                for ev in classify_record(record, snippet_max):
                    deliver(ev)
        finally:
            _flush_held_done(sess, functools.partial(self._deliver, sess))
            _OWNED_PIDS.discard(sess.process.pid)
            if not sess.stopped and not sess.turn_done:
                # Process exit is SESSION DEATH — emit done{exited} (the event
                # that ends the subscribe stream). Now that interrupt() never
                # kills the process (it writes abort and returns; pi's own
                # agent_end -> done{result} closes the drain), ANY exit here is
                # unexpected death: a broken abort that produced no agent_end,
                # a crash, OOM, or an external kill. The runner is blocked in
                # `async for ev in vessel.send(...)` waiting for a terminal
                # done; without this emission it hangs forever.
                #
                # `not sess.turn_done` suppresses the double-done after a
                # clean agent_end (turn_done is set when agent_end ->
                # done{result} is emitted above). `not sess.stopped` suppresses
                # for deliberate teardown (stop() sets stopped and emits its
                # own done{exited}). No killed_by guard — REVIEW 9 #3 (the
                # killed_by residue swallows a real crash) applies here too:
                # interrupt()'s killed_by="interrupt" must NOT suppress a
                # post-abort crash, or the hang a613c13 opened returns.
                _flush_prov_error(sess)
                sess.event_queue.put_nowait(VesselEvent(
                    kind="done",
                    payload={"exit_code": sess.process.returncode,
                             "reason": "exited"}))

    @staticmethod
    def _deliver(sess: _Session, ev: VesselEvent) -> None:
        if sess.suppress_reasoning and ev.kind == "reasoning_delta":
            ev = VesselEvent(kind="hidden_delta", payload=ev.payload)
        if ev.kind == "done" and ev.payload.get("reason") == "result":
            # Decide the provisional provider error AT the turn
            # boundary. Verified recovery (a retry/compaction ran AND produced
            # assistant text) drops the held error; anything else delivers it
            # just before the done. Never decide on the done alone — the
            # recovered turn also ends with a done.
            pe, sess.prov_error = sess.prov_error, None
            if pe is not None \
                    and not (sess.recovery_started and sess.recovered_text):
                sess.event_queue.put_nowait(pe)
            sess.recovery_started = False
            sess.recovered_text = False
            # agent_end → turn over. Clear the in-flight flag so the
            # watchdog stops timing silence (an idle session is
            # legitimately quiet between turns).
            sess.turn_in_flight = False
            sess.turn_done = True
        sess.event_queue.put_nowait(ev)

    async def _pump_stderr(self, sess: _Session) -> None:
        if not sess.process or not sess.process.stderr:
            return
        try:
            async for raw in sess.process.stderr:
                log.debug("vessel_pi_rpc_stderr sid=%s: %.300s", sess.sid,
                          raw.decode("utf-8", "replace").rstrip())
        except Exception:  # noqa: BLE001
            pass

    def cli_session_id(self, sid: str) -> str | None:
        s = self._sessions.get(sid)
        return s.cli_session_id if s else None

    def subscribe(self, sid: str) -> AsyncIterator[VesselEvent]:
        sess = self._sessions.get(sid)
        if sess is None:
            raise VesselError(f"PiRpcVessel.subscribe: unknown sid {sid!r}")
        return drain_until_exited(sess.event_queue)

    # ---- watchdog (MUST-FIX 1) ----------------------------------------

    async def _watchdog(self, sess: _Session,
                        proc: asyncio.subprocess.Process) -> None:
        """Kill a turn that has gone silent for _STALL_TIMEOUT.

        Gated on ``turn_in_flight``: the rpc process is long-lived, so an IDLE
        pulse is silent by definition. Without this gate the watchdog would
        SIGKILL a healthy idle session 20 minutes after it goes quiet. Parity
        with qwen_cli's ``turn_in_flight`` guard. Only fires when a turn is
        actually running AND the process is alive AND nothing has streamed for
        the timeout — i.e. a wedged tool call (the ``find /`` shape).
        """
        try:
            while True:
                try:
                    await asyncio.wait_for(asyncio.shield(proc.wait()),
                                           timeout=_STALL_POLL)
                    return
                except (asyncio.TimeoutError, TimeoutError):
                    pass
                if proc.returncode is not None or sess.stopped:
                    return
                if sess.process is not proc:
                    return
                # MUST-FIX 1: skip when no turn is in flight. An idle rpc
                # session is legitimately silent between turns.
                if not sess.turn_in_flight:
                    continue
                stalled = time.monotonic() - sess.last_progress
                if stalled < _STALL_TIMEOUT:
                    continue
                log.error("vessel_pi_rpc_turn_stalled sid=%s pid=%s "
                          "stalled=%.0fs timeout=%.0fs — killing process group",
                          sess.sid, proc.pid, stalled, _STALL_TIMEOUT)
                sess.killed_by = "stall"
                self._killpg(proc.pid, signal.SIGTERM)
                try:
                    await asyncio.wait_for(proc.wait(), timeout=5.0)
                except (asyncio.TimeoutError, TimeoutError):
                    self._killpg(proc.pid, signal.SIGKILL)
                return
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001
            log.exception("vessel_pi_rpc_watchdog_error sid=%s error=%s",
                          sess.sid, e)

    # ---- control -------------------------------------------------------

    async def interrupt(self, sid: str) -> None:
        s = self._sessions.get(sid)
        if not s or s.process is None or s.process.returncode is not None:
            return
        s.killed_by = "interrupt"
        # abort ends the TURN, not the process. Real pi emits
        # tool_execution_end{isError} + turn_end + message_end{stopReason:
        # aborted} + turn_end + agent_end, THEN the response ack (verified
        # live 2026-08-23). classify_record maps agent_end -> done{result},
        # which closes the runner's send() drain naturally. The process stays
        # alive waiting for the next prompt -- the whole point of rpc.
        #
        # Do NOT wait for process exit (a long-lived rpc process does NOT
        # exit on abort) and do NOT signal (that kills the session -- the
        # kill-and-resume 4c eliminates, reintroduced through the stop
        # button if you do). The watchdog stays armed: if pi's abort fails
        # to produce agent_end, turn_in_flight is still True and the
        # watchdog kills the stuck process after _STALL_TIMEOUT (session
        # dies, but that's the right fallback for a broken abort).
        try:
            await self._write_cmd(s, {"type": "abort"})
        except VesselError:
            pass
        log.info("vessel_pi_rpc_abort sid=%s — abort sent, turn ends via "
                 "agent_end; process stays alive", sid)

    async def stop(self, sid: str) -> None:
        s = self._sessions.pop(sid, None)
        if not s:
            return
        s.stopped = True
        s.killed_by = "stop"
        s.turn_in_flight = False
        if s.process and s.process.returncode is None:
            self._killpg(s.process.pid, signal.SIGTERM)
        s.event_queue.put_nowait(VesselEvent(
            kind="done", payload={"exit_code": None, "reason": "exited"}))
        _cancel(s.watchdog_task)
        for t in (s.reader_task, s.stderr_task, s.waiter_task):
            if t:
                try:
                    await asyncio.wait_for(t, timeout=5)
                except (asyncio.TimeoutError, Exception):  # noqa: BLE001
                    t.cancel()

    _killpg = staticmethod(PiVessel._killpg)
