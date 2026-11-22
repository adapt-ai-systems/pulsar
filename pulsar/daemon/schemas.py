"""Request/response bodies for the daemon API.

Responses use the envelope ``{"ok": true, "value": ...}`` on success and
``{"ok": false, "error": {...}}`` on failure (built in ``app.py``); these
models describe request bodies and the ``value`` payloads.
"""

from __future__ import annotations

import math
from typing import Any, Literal, Optional

from pydantic import BaseModel, ConfigDict, Field, field_validator
def _argv_safe_text(text: str) -> bool:
    """Usable as subprocess argv / prompt text: no embedded NUL and no lone
    surrogate — neither can reach a real process (2026-10-23; the vessel's
    disk reader mirrors this check)."""
    if "\x00" in text:
        return False
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


class HarnessOverridesBody(BaseModel):
    """Per-pulse harness knobs for the pi vessel, the runner, the handoff
    prompts and the context renderer (an experiment, 2026-10-22).
    pi-vessel knobs (base_prompt, base_guidelines, todo_prompt,
    skill_bodies, thinking, tools, bash_prompt, snippet_max, compaction,
    ptc, spill_chars)
    are stored in the diet, materialized to
    ``<pulse home>/harness-overrides.json``, and read by the vessel at
    every launch. Runner knobs (steer_nudge_s, steer_prefix) and handoff
    prompt knobs are read at turn start. Context-render knobs
    (prompt_parts, part_text, skill_catalog) apply daemon-side, where
    PULSAR.md is rendered; the pi vessel ignores them and interface models
    bypass them (their prompt is the TEAM-PACKET, not this document).
    Absent key = today's behaviour, byte-for-byte.

    extra="forbid": an unknown key is a 422 that names the key — callers
    must never think a knob applied when it did not (same incident guard
    as CreatePulseBody)."""

    model_config = ConfigDict(extra="forbid")

    # Replaces the text pi_base_prompt() builds for --system-prompt.
    # "" is honoured as an EMPTY replacement (present != truthy), and
    # base_guidelines is suppressed whenever this key is present.
    base_prompt: Optional[str] = None
    # Replaces the Guidelines list inside the default base prompt.
    # Ignored when base_prompt is set. "" entries are not possible: the
    # key accepts only lists of strings; [] = no Guidelines section.
    base_guidelines: Optional[list[str]] = None
    # Replaces PI_TODO_PROMPT. "" = no todo append line ONLY; the
    # todo_write extension and tool stay (tool availability is a separate
    # knob, prompt_parts).
    todo_prompt: Optional[str] = None
    # Same semantics as pi-skill-bodies.json (arms A/B/C) and wins
    # over that file when the key is present: null = every selected body
    # (arm A), [] = no bodies, just read-lines (arm B), a list of names =
    # those bodies (arm C).
    skill_bodies: Optional[list[str]] = None
    # Replaces the --thinking level computed by pi_thinking_level.
    thinking: Optional[str] = None
    # Replaces the tool allowlist pi_tools_args() builds from
    # PI_DEFAULT_TOOLS. The fd rule still applies: "find" is dropped when
    # neither fd nor fdfind is on PATH. Non-empty list required; a pulse's
    # own pi-args.json --tools still wins (it goes after, pi keeps the last).
    tools: Optional[list[str]] = Field(None, min_length=1)
    # Replaces PI_BASH_PROMPT (the --append-system-prompt line riding with
    # the guard extension). "" = no bash prompt line; the guard extension
    # itself always stays.
    bash_prompt: Optional[str] = None
    # Replaces _SNIPPET_MAX (the tool-result snippet cap in classify_record).
    # Must be >= 200 (a smaller cap would eat real tool output).
    snippet_max: Optional[int] = Field(None, ge=200)
    # When true, pi's OWN auto compaction stays enabled for that pulse
    # (disable_pi_auto_compaction does not turn it off — in rpc mode;
    # print mode refuses the knob, see PiVessel.spawn). Absent/false =
    # today's behaviour (compaction disabled).
    compaction: Optional[bool] = None
    # When true, pi loads pi_ext/script.ts and the allowlist gains the
    # `script` tool: programmatic tool calling, one JavaScript program that
    # calls read/bash/edit/... in a loop. Absent/false = no script tool.
    ptc: Optional[bool] = None
    # When set, pi loads pi_ext/spill.ts: a bash/grep/find/ls result longer
    # than this many chars is saved whole to <cli>/.pi/spill/<n>.txt and the
    # model gets head + tail + the path.
    # Absent = outputs go to the model whole, as before.
    spill_chars: Optional[int] = Field(None, ge=500)
    # Same allowlist meaning as CreatePulseBody.prompt_parts (names in
    # pulsar.launch.context.PROMPT_PARTS), but from harness overrides. When both the
    # diet's prompt_parts and this key are set, this key wins at render.
    prompt_parts: Optional[list[str]] = None
    # part name -> markdown that replaces that PULSAR.md part's rendered
    # text, verbatim, where the part was. Valid names: the parts the
    # context renderer produces into PULSAR.md
    # (pulsar.launch.context.PART_TEXT_PARTS). "default_skills"/"todo"
    # are rejected: their text is not rendered by the context module, so a
    # part_text for them could only silently no-op.
    part_text: Optional[dict[str, str]] = None
    # How the ## Skill library index renders in PULSAR.md: "full" = name +
    # one-line description per skill, grouped (today's format); "names" =
    # names only, one line per group; "none" = no catalog section. The
    # [loaded] marks hold in "full" and "names".
    skill_catalog: Optional[Literal["full", "names", "none"]] = None

    @field_validator("prompt_parts")
    @classmethod
    def _known_prompt_parts(cls, v: Optional[list[str]]) -> Optional[list[str]]:
        """422 naming any part not in PROMPT_PARTS (never a silent drop).
        An explicit null stays null = all parts: it must not collapse to []."""
        from pulsar.launch.context import PROMPT_PARTS
        if v is None:
            return None
        bad = [p for p in v if p not in PROMPT_PARTS]
        if bad:
            raise ValueError(f"unknown prompt_parts {bad}; valid: "
                             f"{list(PROMPT_PARTS)}")
        return list(dict.fromkeys(v))

    @field_validator("part_text")
    @classmethod
    def _known_part_text(cls, v: Optional[dict[str, str]]) -> Optional[dict[str, str]]:
        """422 naming any part whose text the renderer cannot replace, and
        any value that could never reach the rendered file (embedded NUL
        byte or lone surrogate: refuse at the API boundary, field-naming,
        instead of failing the spawn)."""
        from pulsar.launch.context import PART_TEXT_PARTS
        bad = [p for p in (v or {}) if p not in PART_TEXT_PARTS]
        if bad:
            raise ValueError(f"unknown part_text parts {bad}; valid: "
                             f"{list(PART_TEXT_PARTS)}")
        unusable = [p for p, t in (v or {}).items()
                    if not _argv_safe_text(t)]
        if unusable:
            raise ValueError(
                f"part_text: values for {unusable} are not usable as "
                "rendered text (embedded NUL byte or lone surrogate)")
        return v

    # Runner knobs (read at turn start, see Runner._feed):
    # Window in seconds before an accepted-but-undelivered steer aborts the
    # wedged tool call. Same rules as the runner's own check: a finite
    # number >= 0, booleans rejected, numeric strings rejected.
    steer_nudge_s: Optional[float] = Field(default=None, ge=0)
    # Prepended to every injected inbox message's text. "" = no prefix.
    steer_prefix: Optional[str] = None
    # Handoff prompt knobs (pulsar/daemon/handoff/prompts.py):
    # handoff_prompt replaces the handoff summary prompt, successor_prefix
    # is prepended to the successor's first message, handoff_warning
    # replaces the context-low warning text. "" is honoured as an explicit
    # empty replacement wherever the consumer distinguishes present from
    # absent.
    handoff_prompt: Optional[str] = None
    successor_prefix: Optional[str] = None
    handoff_warning: Optional[str] = None

    @field_validator(
        "base_prompt", "todo_prompt", "thinking", "bash_prompt",
        "steer_prefix", "handoff_prompt", "successor_prefix",
        "handoff_warning")
    @classmethod
    def _reject_unusable_text(cls, v, info):
        # A 422 that names the field: the string could never reach a real
        # pi process, so refuse it at the API boundary instead of letting
        # the vessel fail at spawn time.
        if v is not None and not _argv_safe_text(v):
            raise ValueError(
                f"{info.field_name}: not usable as prompt/argv text "
                "(embedded NUL byte or lone surrogate)")
        return v

    @field_validator("base_guidelines")
    @classmethod
    def _reject_unusable_guidelines(cls, v, info):
        if v is not None and not all(_argv_safe_text(g) for g in v):
            raise ValueError(
                "base_guidelines: an entry is not usable as prompt text "
                "(embedded NUL byte or lone surrogate)")
        return v

    @field_validator("tools")
    @classmethod
    def _reject_unusable_tools(cls, v, info):
        if v is not None and not all(
                isinstance(t, str) and _argv_safe_text(t) and "," not in t
                for t in v):
            raise ValueError(
                "tools: an entry is not a usable tool name (embedded NUL "
                "byte, lone surrogate, or comma — the allowlist is "
                "comma-joined)")
        return v

    @field_validator("steer_nudge_s", mode="before")
    @classmethod
    def _reject_unusable_nudge(cls, v, info):
        # Mirror the runner's own rules: booleans are not numbers (True would
        # be 1.0s), and inf/NaN never elapse so a wedged turn would never get
        # its nudge. Numeric strings are rejected too — the runner only
        # accepts real numbers, and a 422 at the API beats a loud per-turn
        # warning later. A huge int (10**400) makes isfinite itself raise
        # OverflowError — treat that as non-finite (422), never a 500.
        if v is None:
            return v
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise ValueError(
                "steer_nudge_s: must be a finite number >= 0 "
                "(not a boolean or a string)")
        try:
            finite = math.isfinite(v)
        except OverflowError:
            finite = False
        if finite:
            return v
        raise ValueError(
            "steer_nudge_s: must be a finite number >= 0 "
            "(not a boolean or a string)")


def harness_overrides_json(
        overrides: Optional[HarnessOverridesBody]) -> Optional[dict]:
    """The dict to store in the diet: ONLY the keys the caller actually
    sent (an explicit ``"skill_bodies": null`` is kept — it means arm A).
    None when nothing was sent."""
    if overrides is None:
        return None
    return overrides.model_dump(exclude_unset=True)


def print_mode_compaction_error(
        vessel: Optional[str], model: Optional[str],
        overrides_json: Optional[dict]) -> Optional[str]:
    """Why ``harness_overrides.compaction`` cannot apply to this pulse, or
    None when it can.

    The compaction retry is RPC-only: in print mode pi's overflow recovery
    runs as a ~100 ms setTimeout continuation whose answer never reaches
    stdout, so the knob would be accepted but never effective (2026-10-23).
    The API knows the mode at store time — pi_rpc is a
    property of the model — so refuse the key up front with a 422.
    """
    from pulsar import models
    if (overrides_json or {}).get("compaction") is True \
            and vessel == "pi" and not models.is_pi_rpc(model):
        return (f"harness_overrides.compaction is RPC-only: model "
                f"'{model}' runs pi in print mode, where a compaction "
                f"retry can never deliver its answer — use an RPC pi model "
                f"(pi_rpc) or drop the compaction key")
    return None


class CreatePulseBody(BaseModel):
    # extra="forbid": an unknown field is a 422, never a silent drop. Born from
    # the 2026-08-06 incident — a lead pulse sent briefs in a then-unsupported
    # "first_message" field, the API discarded them silently, and three
    # helpers sat idle for an hour.
    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    model: str = Field(..., min_length=1)
    vessel: Optional[str] = None
    # Omitted + from_pulse_id set => inherits the spawner's grid (workspace).
    grid_id: Optional[str] = None
    memory_max: Optional[str] = None
    # gt=0 like SetHandoffBody: a negative threshold made every context
    # immediately "over" and put the pulse into handoff churn.
    handoff_tokens: Optional[int] = Field(default=None, gt=0)
    # Reasoning effort for the CLI (validated per model/vessel in the route
    # against pulsar.vessel.efforts_for). None = CLI default.
    effort: Optional[str] = None
    # Fast mode (codex only; validated per vessel in the route against
    # pulsar.vessel.FAST_VESSELS): the CLI's "fast" service tier.
    fast: bool = False
    # Optional loop schedule created atomically with the pulse. Both
    # must be present together; the route 400s if only one is given.
    loop_prompt: Optional[str] = None
    loop_minutes: Optional[int] = None
    loop_triggers: Optional[list[str]] = None  # v25
    loop_trigger_mode: Optional[str] = None  # v25
    # Clean-by-default diet: nothing loads unless opted in.
    clean: bool = True
    memory: bool = False
    skills: list[str] = Field(default_factory=list)
    # Cages: mechanical tool guards from ~/.claude/cages (pi-only).
    cages: list[str] = Field(default_factory=list)
    # Prompt parts (lean spawn): allowlist of the system-prompt parts this
    # pulse gets — names in pulsar.launch.context.PROMPT_PARTS. Omitted/null =
    # every part (the standard prompt); [] = none. Stored in the diet, so it
    # survives restore, handoff, model switch and scheduler wakes.
    prompt_parts: Optional[list[str]] = None
    # Harness overrides (an experiment): per-pulse pi-vessel knobs
    # (HarnessOverridesBody). Stored in the diet like prompt_parts, written
    # to <pulse home>/harness-overrides.json at every materialize. Omitted =
    # today's behaviour.
    harness_overrides: Optional[HarnessOverridesBody] = None
    # Delegation: from_pulse_id names the spawning pulse — the
    # daemon records a delegation watch and reports back to it (working, or
    # loudly NOT). first_message is the brief, delivered to the new pulse's
    # inbox atomically with the spawn — the one-call delegate recipe.
    from_pulse_id: Optional[str] = None
    first_message: Optional[str] = None
    # Role of the context file: "helper" = spawned by a
    # lead for one assignment; its context file gains a "your lead" prefix
    # (questions + the finished report go to the lead first). Explicit and
    # opt-in — NEVER inferred from from_pulse_id (CI, triage, advisors and
    # watchers are all spawned by pulses and must keep the full lead file).
    # None/"lead" = the standard file. "helper" requires from_pulse_id.
    role: Optional[str] = None


class ForkPulseBody(BaseModel):
    """POST /pulses/{id}/fork — branch the conversation into a new pulse.

    Everything else (model, vessel, grid, diet, effort, handoff threshold) is
    inherited from the source pulse; only the title is choosable.
    """

    title: Optional[str] = None


class DietBody(BaseModel):
    """PATCH /pulses/{id}/diet — stored now, applies on the NEXT spawn
    (restore, model switch, handoff, or scheduler wake)."""

    clean: bool = True
    memory: bool = False
    skills: list[str] = Field(default_factory=list)
    cages: list[str] = Field(default_factory=list)
    # Omitted = keep the stored prompt_parts; null = every part.
    prompt_parts: Optional[list[str]] = None
    # Omitted = keep the stored harness_overrides; explicit null = none.
    # Same model as CreatePulseBody.harness_overrides.
    harness_overrides: Optional[HarnessOverridesBody] = None


class CreateScheduleBody(BaseModel):
    """POST /pulses/{id}/schedules — one loop/once/daily schedule.

    Per-kind field requirements are validated in the route (400 on nonsense):
      loop  needs interval_minutes (> 0)
      once  needs at_iso (parseable ISO instant)
      daily needs cron_hhmm ("HH:MM"), optional days (mon..sun csv)
    """

    kind: str = Field(..., min_length=1)
    prompt: str = Field(..., min_length=1)
    interval_minutes: Optional[int] = None
    target_pulse_id: Optional[str] = None  # scheduled cross-pulse message
    triggers: Optional[list[str]] = None  # loop only: timer|handoff|message|replies:N|tools:N (v25)
    trigger_mode: Optional[str] = None  # loop only: any|all (v25)
    at_iso: Optional[str] = None
    cron_hhmm: Optional[str] = None
    days: Optional[str] = None


class UpdateScheduleBody(BaseModel):
    """PATCH /schedules/{id} — enable/disable a schedule and/or edit a loop's
    prompt/interval."""

    enabled: Optional[bool] = None
    prompt: Optional[str] = None
    interval_minutes: Optional[int] = None
    triggers: Optional[list[str]] = None
    trigger_mode: Optional[str] = None


class AskQuestionBody(BaseModel):
    """POST /questions — a pulse asks the user something."""

    pulse_id: str = Field(..., min_length=1)
    text: str = Field(..., min_length=1, max_length=2000)
    choices: list[str] = Field(default_factory=list, max_length=8)
    default_choice: Optional[str] = None
    context: Optional[str] = Field(default=None, max_length=500)


class AnswerQuestionBody(BaseModel):
    """POST /questions/{id}/answer — the user's answer text."""

    text: str = Field(..., min_length=1, max_length=4000)


class CreateConditionWatchBody(BaseModel):
    """POST /watches/conditions — register a daemon-evaluated condition watch.
    The daemon checks it every interval and messages ``pulse_id``
    when the condition trips."""

    kind: str = Field(..., min_length=1)   # pulse_state|file|log|http|cmd
    pulse_id: str = Field(..., min_length=1)  # owner — gets the notification
    spec: dict = Field(..., min_length=1)  # kind-specific condition
    mode: str = "edge"                     # once|edge
    interval_seconds: int = 60
    note: Optional[str] = None


class SwitchModelBody(BaseModel):
    """PATCH /pulses/{id}/model — mid-session model switch.

    ``vessel`` omitted = keep the current CLI. A vessel change triggers a
    context handoff (fresh session seeded with the predecessor's handoff).

    ``effort`` omitted (None) = keep the stored effort; empty string = clear
    back to the CLI default; a value = validated against the target vessel's
    levels. An effort-only change (same model) rides the same-vessel
    respawn-with-resume path so it applies immediately.
    """

    model: str = Field(..., min_length=1)
    vessel: Optional[str] = None
    effort: Optional[str] = None
    force: bool = False  # fresh transcript-seeded session; never ask/resume the old CLI


class SetHandoffBody(BaseModel):
    """PATCH /pulses/{id}/handoff — auto-handoff threshold; null = family default."""

    handoff_tokens: Optional[int] = Field(default=None, gt=0)


class SetBackupBody(BaseModel):
    """PATCH /pulses/{id}/backup — the per-pulse backup model, the pulse's
    failover target (daemon/failover.py).

    One field carries both states, so there is no second "enabled" bit to
    disagree with the stored config: ``model`` naming a model enables backup,
    ``model: null`` disables it (clears ``settings.backup``). ``model`` is
    required in the body — an absent one is a 422, not a silent disable.

    The vessel is DERIVED from the model (``models.vessel_for``) at validation
    time, never stored: a stored vessel can only ever drift out of sync with
    the model beside it.

    ``effort`` absent/null/"" = the CLI's default; a value is validated
    against the backup vessel's levels. ``extra="forbid"`` per the 2026-08-06
    silent-drop incident.
    """

    model_config = ConfigDict(extra="forbid")

    model: Optional[str] = Field(...)
    effort: Optional[str] = None


class SetStreamBody(BaseModel):
    """PATCH /pulses/{id}/stream, /reasoning and /code — per-pulse display
    gates (live typing, reasoning trace, live code)."""

    enabled: bool


class ReplyRef(BaseModel):
    """The earlier pulse message the user is replying to (the pane's Reply
    button): its text is quoted to the pulse and shown on their bubble."""

    text: str = Field(..., min_length=1)
    ts: Optional[str] = None


class MessageBody(BaseModel):
    """POST /pulses/{id}/messages — from_pulse_id marks a pulse-to-pulse
    send: the sender is named in the delivered text and a stopped
    target is woken."""

    text: str = Field(..., min_length=1)
    from_pulse_id: Optional[str] = None
    reply_to: Optional[ReplyRef] = None


class CreateGridBody(BaseModel):
    name: str = Field(..., min_length=1)
    position: Optional[int] = None
    # Sub-workspace: the top-level workspace this one sits in (one layer only).
    parent_id: Optional[str] = None


class ReorderPulsesBody(BaseModel):
    """POST /pulses/reorder — new left-to-right order (may be a grid subset)."""

    ids: list[str] = Field(..., min_length=1)


class ReorderGridsBody(BaseModel):
    """POST /grids/reorder — new left-to-right workspace order."""

    ids: list[str] = Field(..., min_length=1)


class UpdateGridBody(BaseModel):
    """PATCH /grids/{id} — rename and/or reorder. Both fields optional."""

    name: Optional[str] = Field(default=None, min_length=1)
    position: Optional[int] = None
    # Present-and-null lifts a sub-workspace to top level; absent = unchanged.
    parent_id: Optional[str] = None


class ArchiveIdleBody(BaseModel):
    """POST /pulses/archive-idle — archive every non-archived pulse whose last
    activity (events.jsonl mtime or store updated_at, whichever is later) is
    older than ``idle_hours``. Fractional hours are allowed (0.5 = 30 min)."""

    model_config = ConfigDict(extra="forbid")

    idle_hours: float = Field(..., gt=0, le=24 * 365)


class MovePulseBody(BaseModel):
    """PATCH /pulses/{id}/grid — move a pulse to a grid (null = ungridded).

    ``grid_id`` is required in the body but may be explicitly null; use the
    sentinel default to tell "absent" apart from an explicit null.
    """

    grid_id: Optional[str] = None


class SetModeBody(BaseModel):
    """PATCH /pulses/{id}/mode — the pulse's organizational mode (Watchers,
    2026-09-07). ``watching`` = a finished project whose pulse now sleeps
    between watch/schedule wakes and keeps an eye on things; ``paused`` = a
    project the user set aside and means to come back to. Both leave the
    workspace grid and live on the Watchers shelf; ``null`` clears the mode.
    Orthogonal to the running/stopped/archived lifecycle."""

    model_config = ConfigDict(extra="forbid")

    mode: Optional[Literal["watching", "paused"]] = Field(...)


class ArchivePulseBody(BaseModel):
    """POST /pulses/{id}/archive — optional body. A pulse that owns live
    condition watches or enabled schedules cannot be archived without a
    choice: ``monitoring: "keep"`` switches it to watching mode instead of
    archiving; ``monitoring: "stop"`` closes the watches, disables the
    schedules, then archives. No choice on such a pulse = 409
    ``monitoring_choice_required``."""

    model_config = ConfigDict(extra="forbid")

    monitoring: Optional[Literal["keep", "stop"]] = None


class RenamePulseBody(BaseModel):
    """PATCH /pulses/{id}/title — rename a pulse (title must be non-blank)."""

    title: str


class CreateInboxItemBody(BaseModel):
    """POST /inbox/items — produce one inbox item.

    ``from_pulse_id`` present → pulse-sourced (source_kind=pulse, label from
    the sender's title). Absent + not an internal producer → script-sourced.
    Caps (title ≤200, body ≤64KB) and channel-slug rules are enforced in the
    route; ``extra="forbid"`` rejects unknown fields (same incident guard as
    CreatePulseBody).
    """

    model_config = ConfigDict(extra="forbid")

    title: str = Field(..., min_length=1)
    body: str = Field(..., min_length=1)
    channel: str = Field(..., min_length=1)
    thread_key: Optional[str] = None
    priority: Optional[str] = None           # notify|quiet (default notify)
    needs_response: bool = False
    from_pulse_id: Optional[str] = None
    source_label: Optional[str] = Field(default=None, min_length=1)


class InboxReadAllBody(BaseModel):
    """POST /inbox/read-all — requires a ``before`` cutoff."""

    model_config = ConfigDict(extra="forbid")

    before: str = Field(..., min_length=1)
    unread_only: bool = False
    channel: Optional[str] = None
    thread_key: Optional[str] = None
    starred: bool = False
    needs_response: bool = False
    q: Optional[str] = None


class InboxScoreBody(BaseModel):
    """POST /inbox/items/{id}/score — 1 (don't want), 2 (ok), 3 (notify),
    or null to unlearn the rule for this kind of message."""

    model_config = ConfigDict(extra="forbid")

    score: Optional[int] = None


class InboxReplyBody(BaseModel):
    """POST /inbox/items/{id}/reply — the user's async reply to a pulse item."""

    model_config = ConfigDict(extra="forbid")

    text: str = Field(..., min_length=1)


class PatchChannelBody(BaseModel):
    """PATCH /inbox/channels/{name} — channel mode + mute_until."""

    model_config = ConfigDict(extra="forbid")

    mode: Optional[str] = None
    mute_until: Optional[str] = None

