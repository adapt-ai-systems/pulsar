"""Per-pulse harness overrides: ``<pulse home>/harness-overrides.json``.

A JSON object the daemon writes from the spawn body (experiment plumbing). This
module is the single shared READER — nothing here writes the file. Every
consumer must treat each key as optional and fall back to today's
behaviour when the key is absent or wrongly typed. ``read_overrides``
never raises: a missing or malformed file returns ``{}`` (one warning
logged per path per process).

Callers read it fresh where behaviour is decided — the runner once per
turn start, the daemon's handoff prompt builders per call — so a changed
file applies on the next turn / next handoff without a restart.

Keys honored today (all optional):

``steer_nudge_s`` (number)
    Per-pulse value for the runner's steer-delivery nudge window
    (pulsar/runner/runner.py, module default STEER_NUDGE_S = 300 s).
    Must be a finite number >= 0 — 0 is honoured as an immediate nudge,
    same as the process-wide knob. Booleans, negatives, inf/NaN and
    non-numbers fall back to the default with a warning.

``steer_prefix`` (string)
    Text prepended to a message's text when the runner INJECTS it
    mid-turn as a steer. The events.jsonl record keeps the original
    ``text`` and carries the prefix in a new ``injected_prefix`` field.
    Empty string or non-string = no prefix (today's behaviour).

``handoff_prompt`` (string template)
    Replaces the base handoff request used by
    ``handoff_prompt_for()`` (pulsar/daemon/handoff/prompts.py) for every
    model, interface models included; the interface TEAM-PACKET default
    applies only when the key is absent. An explicitly supplied empty
    string is honoured as empty. ``str.format`` placeholders:

    * ``{opener}`` — the reason-appropriate opener: the context-threshold
      opener, the model-switch opener, or the manual opener.

    Watching addendum and the notes-file addendum still append.
    Literal braces must be doubled (``{{`` / ``}}``).

``successor_prefix`` (string template)
    Replaces the successor seed prefix used by ``successor_prefix_for()``
    for every model (the interface TEAM-PACKET default applies only when
    the key is absent; an explicit empty string is honoured as empty).
    Placeholders:

    * ``{idle_note}`` — the idle seed note when the old session was idle
      at the request, else the empty string.

``handoff_warning`` (string template)
    Replaces the early-warning text from ``warning_text()``. Placeholders:

    * ``{ctx}`` — current context tokens (plain integer)
    * ``{thr}`` — the handoff threshold tokens (plain integer)
    * ``{note}`` — path of the ``handoffs/next.md`` note file
    * ``{notes_path}`` — path of the pulse's NOTES.md
    * ``{pulse_id}`` — the pulse id
    * ``{daemon_url}`` — the daemon's base URL

    The notes-size alarm still appends. Literal braces must be doubled.

The pi-vessel keys (``base_prompt``, ``base_guidelines``, ``todo_prompt``,
``skill_bodies``, ``thinking``, ``tools``, ``bash_prompt``,
``snippet_max``, ``compaction``) and the context-render keys
(``prompt_parts``, ``part_text``, ``skill_catalog``) also ride this file;
their consumers validate and apply them — see
``pulsar.vessel.pi._validated_harness_overrides`` (pi keys),
``effective_prompt_parts`` below (prompt_parts) and
``pulsar.launch.context`` (part_text / skill_catalog, daemon-side).
``compaction`` is RPC-only: print-mode pi refuses it (see PiVessel.spawn).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("pulsar.harness_overrides")

OVERRIDES_FILE = "harness-overrides.json"

# One warning per bad path per process — the file is re-read every turn,
# so per-read warnings would spam the log.
_warned: set[Path] = set()


def effective_prompt_parts(overrides: dict | None,
                           diet: dict | None) -> list[str] | None:
    """The ONE prompt-parts allowlist for a spawn — resolved identically
    everywhere it is used: the context renderer (launch/diet.py), the
    default-skills merge (store/settings.py) and the pi todo extension
    (vessel/pi.py).

    A harness-overrides ``prompt_parts`` LIST wins over the diet's list.
    An explicit ``null`` ALSO wins: it means ALL parts (never []). A
    malformed persisted value (not a list) is logged and degrades to the
    diet's list, never to a crash. Absent from the overrides -> the diet's
    list, and None overall = all parts (today's behaviour).
    """
    ov = overrides if isinstance(overrides, dict) else {}
    if "prompt_parts" in ov:
        v = ov["prompt_parts"]
        if v is None:
            return None  # explicit null = all parts
        if isinstance(v, list):
            return v
        log.warning("harness override prompt_parts %r is not a list "
                    "— using the diet's prompt_parts instead", v)
    d = diet if isinstance(diet, dict) else {}
    v = d.get("prompt_parts")
    if isinstance(v, list):
        return v
    return None


def read_overrides(home: Path) -> dict:
    """Read ``<home>/harness-overrides.json``; ``{}`` when missing/bad.

    Never raises. Malformed JSON or a non-object top level logs one
    warning (per path, per process) and returns ``{}``.
    """
    path = Path(home) / OVERRIDES_FILE
    try:
        data = json.loads(path.read_text())
    except FileNotFoundError:
        return {}
    except Exception as e:
        if path not in _warned:
            _warned.add(path)
            log.warning("harness overrides %s unreadable/malformed (%s) "
                        "— ignoring", path, e)
        return {}
    if not isinstance(data, dict):
        if path not in _warned:
            _warned.add(path)
            log.warning("harness overrides %s is not a JSON object "
                        "— ignoring", path)
        return {}
    return data
