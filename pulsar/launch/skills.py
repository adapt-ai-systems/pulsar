"""The opt-in skill library: where it lives, the tier-0 catalog every context
file carries, and the skill directories qwen-code would load on its own.

Daemon-side (pulsar.launch): runners never import this module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

log = logging.getLogger("pulsar.spawn")

# Where opt-in skills live; symlinked per-pulse so a clean spawn with
# --setting-sources project loads exactly the opted-in set.
SKILLS_ROOT = Path.home() / ".claude" / "skills"

# Tier-0 skill discovery: every context file carries a one-line-per-skill
# catalog built from SKILL.md frontmatter, grouped by SKILLS_ROOT/catalog.json.
# The diet still decides what is pre-loaded; the catalog makes the rest
# findable by path mid-conversation. Regenerated every spawn so it cannot
# drift. A missing catalog.json or bad frontmatter degrades to fewer/ungrouped
# lines, never a failed spawn.
# Routing text per skill in the catalog: the optional ``index:`` frontmatter
# line wins (curated, variable-length, keeps aliases and exclusions); otherwise
# the description, cut at a word boundary.
_CATALOG_DESC_MAX = 160

# Only the skills checked in the pulse's diet are pre-loaded — nothing is
# forced in; the catalog makes every skill findable on demand.


def _skill_description(skill_md: Path) -> str | None:
    """Frontmatter description folded to one line, or None if unparsable.

    Hand-rolled on purpose — no yaml dependency in the runner, and the only
    shapes in the library are plain scalars and ``>``/``|`` blocks.
    """
    try:
        with skill_md.open(encoding="utf-8") as fh:
            if fh.readline().strip() != "---":
                return None
            parts: list[str] = []
            in_block = False
            index: str | None = None
            for raw in fh:
                line = raw.rstrip("\r\n")
                if line.strip() == "---":
                    break
                if line.startswith("index:"):
                    val = line[len("index:"):].strip().strip('"').strip("'")
                    if val:
                        index = val
                    continue
                if in_block:
                    if not line.strip():
                        continue
                    if line[:1] in (" ", "\t"):
                        parts.append(line.strip())
                        continue
                    in_block = False  # block ended; keep scanning for index:
                if line.startswith("description:") and not parts:
                    val = line[len("description:"):].strip()
                    if val in ("", ">", "|", ">-", "|-", ">+", "|+"):
                        in_block = True
                    else:
                        parts.append(val)
    except OSError:
        return None
    if index:
        return index
    desc = " ".join(parts).strip().strip('"').strip("'")
    if not desc:
        return None
    if len(desc) > _CATALOG_DESC_MAX:
        desc = desc[:_CATALOG_DESC_MAX].rsplit(" ", 1)[0] + " …"
    return desc


def _skill_catalog_lines(linked: list[str], style: str = "full") -> list[str]:
    """The ``## Skill library`` section of the context file.

    ``style`` is the harness_overrides ``skill_catalog`` knob: "full" = name +
    one-line description per skill, grouped; "names" = one line per group
    listing only the skill names. The [loaded] marks (only the skills actually
    pre-loaded) hold in both styles.
    """
    root = SKILLS_ROOT
    if not root.is_dir():
        return []
    on_disk: dict[str, str] = {}
    for d in sorted(root.iterdir()):
        if not d.is_dir() or d.name.startswith(("_", ".")):
            continue
        desc = _skill_description(d / "SKILL.md")
        if desc is not None:
            on_disk[d.name] = desc
    if not on_disk:
        return []
    groups: list[tuple[str, list[str]]] = []
    try:
        cat = json.loads((root / "catalog.json").read_text())
        groups = [(title, [n for n in names if n in on_disk])
                  for title, names in cat.items() if isinstance(names, list)]
    except (OSError, ValueError, AttributeError):
        log.warning("catalog: %s missing/malformed — flat skill list",
                    root / "catalog.json")
    in_groups = {n for _, names in groups for n in names}
    other = [n for n in on_disk if n not in in_groups]
    if other:
        groups.append(("Other", other))
    out = [
        "## Skill library",
        "",
        "Skills are how-to files at ~/.claude/skills/<name>/SKILL.md —",
        "endpoints, auth, rituals, and gotchas earlier sessions learned the",
        "hard way. Ones marked [loaded] are already in your context via this",
        "pulse's diet. For ANY other skill below, the moment the conversation",
        "turns to its topic, Read its SKILL.md before working — do not",
        "re-derive what it already documents.",
        "",
    ]
    pinned = set(linked)
    for title, names in groups:
        if not names:
            continue
        out.append(f"### {title}")
        if style == "names":
            out.append(", ".join(f"{n} [loaded]" if n in pinned else n
                                 for n in names))
        else:
            for n in names:
                mark = " [loaded]" if n in pinned else ""
                out.append(f"- {n}{mark} — {on_disk[n]}")
        out.append("")
    return out
