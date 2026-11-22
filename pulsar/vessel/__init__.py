"""Pulsar Claude CLI vessel.

Public surface: the ``ClaudeVessel`` adapter plus the normalized event
dataclass it yields. Ported from an earlier project, stripped to a standalone
adapter. Imports nothing outside the stdlib + ``pulsar.*``.
"""

from pulsar.vessel.claude import (
    ClaudeVessel,
    VesselError,
)
from pulsar.vessel.codex import CodexVessel
from pulsar.vessel.events import VesselEvent, VesselEventKind
from pulsar.vessel.grok import GrokVessel
from pulsar.vessel.pi import PiVessel

# Effort dials and fast-mode capability live in pulsar/models.json (ONE
# registry for daemon validation, handoff sanitize and the web picker). These
# names are kept for callers; they are views over the registry.
from pulsar import models as _registry

EFFORT_LEVELS_BY_MODEL: dict[str, tuple[str, ...]] = {
    m.id: m.efforts for m in _registry.MODELS.values() if m.efforts is not None
}
efforts_for = _registry.efforts_for
FAST_VESSELS: tuple[str, ...] = _registry.fast_vessels()

__all__ = [
    "ClaudeVessel",
    "CodexVessel",
    "GrokVessel",
    "PiVessel",
    "VesselError",
    "VesselEvent",
    "VesselEventKind",
    "EFFORT_LEVELS_BY_MODEL",
    "efforts_for",
    "FAST_VESSELS",
]
