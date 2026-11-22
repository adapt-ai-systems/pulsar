"""The model registry — ONE table for every picker id Pulsar knows.

``pulsar/models.json`` is the source of truth. This module loads it once and
answers the questions the daemon, the handoff engine and the vessels used to
answer from six hand-mirrored tables (models.js, handoff DEFAULT_THRESHOLDS,
pi._CLI, EFFORT_LEVELS_BY_MODEL, INTERFACE_MODELS, vessel_bridge). The web
picker imports the same JSON file, so the two sides cannot drift.

Unknown ids still resolve: ``vessel_for`` falls back to the historical prefix
rules and ``default_handoff_for`` to the family prefixes, so a hand-set or
retired model on an old pulse keeps working.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

logger = logging.getLogger("pulsar.models")

SHIPPED_REGISTRY_PATH = Path(__file__).with_name("models.json")
REGISTRY_PATH = Path(os.environ.get("PULSAR_MODELS_PATH") or SHIPPED_REGISTRY_PATH)

DEFAULT_VESSEL = "claude-code"

_warned_missing_local: set[str] = set()


@dataclass(frozen=True)
class Model:
    id: str
    label: str
    vessel: str
    default_handoff: int
    efforts: Optional[tuple[str, ...]] = None  # None = the vessel's dial
    # Retired effort name -> current level (a stored old name keeps working).
    effort_aliases: tuple[tuple[str, str], ...] = ()
    wire_model: Optional[str] = None
    pi_rpc: bool = False
    interface: bool = False
    hidden: bool = False
    default_effort: Optional[str] = None
    thinking: Optional[str] = None
    slot: bool = False
    local: bool = False
    base_url: Optional[str] = None
    # Slot only: every address the slot may be served on (a model swap can
    # move the server between ports). First live one wins; base_url first.
    base_urls: tuple = ()
    # Standing prompt notes added to the context document (see
    # pulsar/launch/context.py): ds4_clarity | vision | qwen.
    prompt_notes: tuple[str, ...] = ()
    # Spawn cap: how many pulses on this model may be running at once
    # (POST /pulses is a 409 model_busy past it). None = no cap. Set for
    # local serves whose KV pool a second session can evict.
    max_concurrent: Optional[int] = None


@dataclass(frozen=True)
class Vessel:
    id: str
    label: str
    efforts: tuple[str, ...] = ()
    fast: bool = False


def _load(path: Path = REGISTRY_PATH) -> dict:
    raw = json.loads(path.read_text(encoding="utf-8"))
    vessels = {
        vid: Vessel(id=vid, label=v["label"], efforts=tuple(v.get("efforts", ())),
                    fast=bool(v.get("fast", False)))
        for vid, v in raw["vessels"].items()
    }
    models: dict[str, Model] = {}
    for m in raw["models"]:
        if m["id"] in models:
            raise ValueError(f"models.json: duplicate id {m['id']!r}")
        if m["vessel"] not in vessels:
            raise ValueError(f"models.json: {m['id']!r} names unknown vessel "
                             f"{m['vessel']!r}")
        raw_local = m.get("local")
        if isinstance(raw_local, bool):
            local = raw_local
        else:
            local = False
            if m["id"] not in _warned_missing_local:
                _warned_missing_local.add(m["id"])
                logger.warning(
                    "models.json: %r missing boolean 'local' flag (got %r); "
                    "defaulting to local=False",
                    m["id"], raw_local,
                )
        models[m["id"]] = Model(
            id=m["id"], label=m["label"], vessel=m["vessel"],
            default_handoff=int(m["default_handoff"]),
            efforts=tuple(m["efforts"]) if "efforts" in m else None,
            effort_aliases=tuple((m.get("effort_aliases") or {}).items()),
            wire_model=m.get("wire_model"), pi_rpc=bool(m.get("pi_rpc", False)),
            interface=bool(m.get("interface", False)),
            hidden=bool(m.get("hidden", False)),
            default_effort=m.get("default_effort"),
            thinking=m.get("thinking"),
            slot=bool(m.get("slot", False)),
            local=local,
            base_url=m.get("base_url"),
            base_urls=tuple(u for u in [m.get("base_url"), *m.get("base_urls", [])]
                            if u),
            prompt_notes=tuple(m.get("prompt_notes", ())),
            max_concurrent=(int(m["max_concurrent"])
                            if m.get("max_concurrent") is not None else None))
    return {"raw": raw, "vessels": vessels, "models": models}


_REG = _load()
VESSELS: dict[str, Vessel] = _REG["vessels"]
MODELS: dict[str, Model] = _REG["models"]
DEFAULT_MODEL_ID: str = _REG["raw"]["default_model"]
DEFAULT_EFFORT: str = _REG["raw"]["default_effort"]

# Family fallbacks for ids that are not in the registry (hand-set / retired).
_VESSEL_PREFIXES: tuple[tuple[str, str], ...] = (
    ("grok-", "grok"), ("agy-", "antigravity"),
    ("gpt-", "codex"), ("ds4-", "codex"), ("pi-", "pi"),
)
_HANDOFF_PREFIXES: tuple[tuple[str, int], ...] = (
    ("pi-qwen", 100_000), ("claude-opus-5", 300_000), ("claude-fable", 200_000),
    ("gpt", 225_000), ("codex", 225_000), ("ds4", 175_000), ("qwen", 190_000),
)


# Prompt-note flags for ids that are not in the registry: the old model-id
# prefix rules. Registered ids use their ``prompt_notes`` list only.
_PROMPT_NOTES_PREFIXES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("ds4", ("ds4_clarity",)), ("qwen", ("qwen",)),
)


def model_by_id(model: Optional[str]) -> Optional[Model]:
    return MODELS.get(model or "")


def vessel_for(model: str) -> str:
    """The CLI for a model id. Registry first, then the historical prefixes,
    then Claude (unknown/custom ids keep the original default)."""
    m = model_by_id(model)
    if m is not None:
        return m.vessel
    for prefix, vessel in _VESSEL_PREFIXES:
        if model.startswith(prefix):
            return vessel
    return DEFAULT_VESSEL


def default_handoff_for(model: Optional[str]) -> Optional[int]:
    """Per-model handoff default; family prefix for unknown ids; None if
    nothing matches (caller applies its own floor)."""
    m = model_by_id(model)
    if m is not None:
        return m.default_handoff
    low = (model or "").lower()
    for prefix, tokens in _HANDOFF_PREFIXES:
        if low.startswith(prefix):
            return tokens
    return None


def prompt_notes_for(model: Optional[str]) -> tuple[str, ...]:
    """Prompt-note flags for a model id: the registry's ``prompt_notes`` list,
    or the old prefix rules for an id the registry does not know."""
    m = model_by_id(model)
    if m is not None:
        return m.prompt_notes
    for prefix, notes in _PROMPT_NOTES_PREFIXES:
        if (model or "").startswith(prefix):
            return notes
    return ()


def efforts_for(model: Optional[str], vessel: Optional[str]) -> tuple[str, ...]:
    """Levels the picker/daemon accept for this pulse. Model override wins."""
    m = model_by_id(model)
    if m is not None and m.efforts is not None:
        return m.efforts
    v = VESSELS.get(vessel or "")
    return v.efforts if v else ()


# DeepSeek V4.1's server takes reasoning_effort as an integer 1..100; the
# dial shows the five levels ("80", "70", "50", "30", "none").
# Old names map to the nearest level so stored pulses keep working.
DS41_EFFORTS: tuple[str, ...] = ("80", "70", "50", "30", "none")
DS41_EFFORT_ALIASES: dict[str, str] = {
    "off": "none", "low": "50", "high": "80", "xhigh": "80", "max": "80"}


def canonical_effort(model: Optional[str], effort: Optional[str],
                     served_id: Optional[str] = None) -> Optional[str]:
    """``effort`` with a retired name replaced by its current level
    (registry ``effort_aliases``; a slot serving DeepSeek uses the DS41
    table). Anything else comes back unchanged."""
    if not effort:
        return effort
    if served_id is not None:
        if served_id.lower().startswith("deepseek"):
            return DS41_EFFORT_ALIASES.get(effort, effort)
        return effort
    m = model_by_id(model)
    if m is not None:
        return dict(m.effort_aliases).get(effort, effort)
    return effort


def slot_efforts(served_id: Optional[str]) -> tuple[str, ...]:
    """The effort dial for the model currently served by a dynamic slot."""
    value = (served_id or "").lower()
    if value.startswith("glm"):
        return ("off", "low", "high", "max")
    if value.startswith("qwen"):
        return ("low", "medium", "xhigh")
    if value.startswith("mimo"):
        return ("low", "high", "max")
    if value.startswith("deepseek"):
        return DS41_EFFORTS
    return ()


def has_model_efforts(model: Optional[str]) -> bool:
    m = model_by_id(model)
    return m is not None and m.efforts is not None


def fast_vessels() -> tuple[str, ...]:
    return tuple(v.id for v in VESSELS.values() if v.fast)


def wire_model_for(model: Optional[str]) -> Optional[str]:
    m = model_by_id(model)
    return m.wire_model if m else None


def is_pi_rpc(model: Optional[str]) -> bool:
    m = model_by_id(model)
    return bool(m and m.pi_rpc)


def is_local(model: Optional[str]) -> bool:
    """Whether ``model`` is served locally rather than from a cloud provider."""
    m = model_by_id(model)
    return bool(m and m.local)


def registry_payload(slot_statuses: Optional[dict[str, dict]] = None,
                     vessel_tools: Optional[dict[str, dict]] = None) -> dict:
    """What GET /models serves, with optional live slot and tool decorations."""
    if not slot_statuses and not vessel_tools:
        return _REG["raw"]
    payload = dict(_REG["raw"])
    if slot_statuses:
        entries = []
        for raw in _REG["raw"]["models"]:
            entry = dict(raw)
            if raw.get("slot"):
                status = dict(slot_statuses.get(raw["id"], {
                    "live": False, "served_id": None,
                    "checked_at": None, "error": "not_checked",
                }))
                entry["slot"] = True
                entry["status"] = status
                served_id = status.get("served_id") if status.get("live") else None
                entry["label_live"] = (f"{raw['label']}: {served_id}"
                                        if served_id else f"{raw['label']}: unavailable")
            entries.append(entry)
        payload["models"] = entries
    if vessel_tools:
        payload["tools"] = vessel_tools
    return payload
