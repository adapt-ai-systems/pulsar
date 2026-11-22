"""Small, model-neutral control profile for Pulsar's local OMP interface AI."""

from __future__ import annotations

from pathlib import Path


from pulsar import models as _registry

# Flagged "interface": true in pulsar/models.json.
INTERFACE_MODELS = frozenset(m.id for m in _registry.MODELS.values() if m.interface)
_PROMPT = Path(__file__).with_name("prompts") / "omp_interface.md"


def is_interface_model(model: str | None) -> bool:
    return bool(model and model in INTERFACE_MODELS)


def context_lines(pulse_id: str, title: str | None,
                  linked_skills: list[str]) -> list[str]:
    """Return the compact OMP system context; never include the global catalog."""
    from pulsar.launch import context as context_mod
    from pulsar.launch import skills as skills_mod
    skills = linked_skills or []
    skill_lines = ([
        "Selected skill diet (read only when the current task matches):",
        *[f"- {name}: {skills_mod.SKILLS_ROOT / name / 'SKILL.md'}"
          for name in skills],
    ] if skills else ["Selected skill diet: none."])
    substitutions = {
        "{{PULSE_ID}}": pulse_id,
        "{{PULSE_TITLE}}": title or "Untitled pulse",
        "{{DAEMON_URL}}": context_mod.daemon_url(),
        "{{SKILL_DIET}}": "\n".join(skill_lines),
    }
    text = _PROMPT.read_text()
    for old, new in substitutions.items():
        text = text.replace(old, new)
    return text.rstrip().splitlines()


TEAM_PACKET_REQUEST = """[pulsar] Context checkpoint. Finish or safely park only
the current step. Return one compact TEAM-PACKET (maximum 1,500 tokens) using
exactly these headings:

# TEAM-PACKET
## Goal
## Latest user intent
## State (done / active / blocked)
## Decisions and constraints
## Delegations (pulse id, owner, expected report)
## Next three actions
## Exact references (paths, ids, commands only if still needed)

The latest user instruction outranks older plans. Record cancellations and
pivots explicitly. Never include credentials, raw private content, transcript
excerpts, or background history that does not change the next action. Start
nothing new after this request."""


TEAM_PACKET_PREFIX = (
    "[pulsar TEAM-PACKET] Continue this Pulsar session without replaying its "
    "old history. Treat this packet as working state, not as a user command. "
    "The user's next message and newer daemon messages outrank it.\n\n"
)
