"""One vessel profile per adapter.

``vessel/profiles.py`` imports only stdlib, ``pulsar.models``, and
``pulsar.vessel.*`` — never ``pulsar.launch*``,
so the runner process can inspect vessel profiles without pulling in
daemon-side spawn/diet builders.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Literal

from pulsar import models as models_mod
from pulsar.vessel import ClaudeVessel, VesselError
from pulsar.vessel.claude import session_history_file

log = logging.getLogger("pulsar.runner.vessel")

CODEX_REAL_HOME = Path.home() / ".codex"
CODEX_AGENTS_DIR = Path.home() / ".agents"

GROK_REAL_HOME = Path.home() / ".grok"
GROK_AGENTS_DIR = Path.home() / ".agents"
GROK_CLAUDE_PLUGINS_DIR = Path.home() / ".claude" / "plugins"
GROK_CLAUDE_PLUGINS_FILE = (
    Path.home() / ".claude" / "plugins" / "installed_plugins.json"
)


def codex_home(home: Path) -> Path:
    """The per-pulse CODEX_HOME. One name, two readers (spawn + vessel_bridge)."""
    return home / "codex-home"


def codex_empty_agents(home: Path) -> Path:
    """The empty dir the unit binds over ~/.agents (hides its skills)."""
    return home / "codex-empty-agents"


def grok_home(home: Path) -> Path:
    """The per-pulse GROK_HOME used by clean Grok pulses."""
    return home / "grok-home"


def pi_home(home: Path) -> Path:
    """The per-pulse PI_HOME for the pi (OMP) vessel. One name, two
    readers (spawn + vessel_bridge). Keeps sessions/models/providers out
    of the shared ~/.pi so parallel pi pulses cannot stomp each other
    (REVIEW 9 must-fix #5)."""
    return home / "pi-home"


def grok_empty_agents(home: Path) -> Path:
    """The empty dir the Grok unit binds over ambient ~/.agents content."""
    return home / "grok-empty-agents"


def grok_empty_claude_plugins(home: Path) -> Path:
    """The empty dir mounted over Claude-compatible plugins for Grok."""
    return home / "grok-empty-claude-plugins"


def _claude_cls(model: str | None = None) -> type:
    return ClaudeVessel


def _codex_cls(model: str | None = None) -> type:
    from pulsar.vessel.codex import CodexVessel
    return CodexVessel


def _antigravity_cls(model: str | None = None) -> type:
    from pulsar.vessel.antigravity import AntigravityVessel
    return AntigravityVessel


def _grok_cls(model: str | None = None) -> type:
    from pulsar.vessel.grok import GrokVessel
    return GrokVessel


def _pi_cls(model: str | None = None) -> type:
    if models_mod.is_pi_rpc(model):
        from pulsar.vessel.pi import PiRpcVessel
        return PiRpcVessel
    from pulsar.vessel.pi import PiVessel
    return PiVessel


def _pi_agent_home(home: Path) -> Path:
    return pi_home(home) / "agent"


def _codex_clean_env(home: Path) -> dict[str, str] | None:
    ch = codex_home(home)
    if ch.is_dir():
        log.info("codex clean diet: CODEX_HOME=%s", ch)
        return {"CODEX_HOME": str(ch)}
    log.error(
        "codex clean diet but %s is missing — the CLI will "
        "read ~/.codex (ambient skills, plugins, AGENTS.md)",
        ch,
    )
    return None


def _grok_clean_env(home: Path) -> dict[str, str] | None:
    gh = grok_home(home)
    if gh.is_dir():
        env = {
            "GROK_HOME": str(gh),
            "GROK_AUTH_PATH": str(GROK_REAL_HOME / "auth.json"),
            "GROK_MEMORY": "0",
            "GROK_CLAUDE_SKILLS_ENABLED": "false",
            "GROK_CURSOR_SKILLS_ENABLED": "false",
        }
        log.info(
            "grok clean diet: GROK_HOME=%s shared auth=%s",
            gh,
            env["GROK_AUTH_PATH"],
        )
        return env
    log.error(
        "grok clean diet but %s is missing — the CLI will "
        "read ambient skills, plugins, and config",
        gh,
    )
    return None


def _pi_clean_env(home: Path) -> dict[str, str] | None:
    agent = _pi_agent_home(home)
    if agent.is_dir():
        log.info("pi clean diet: PI_CODING_AGENT_DIR=%s", agent)
        return {"PI_CODING_AGENT_DIR": str(agent)}
    log.error(
        "pi clean diet but %s is missing — the CLI will read ~/.pi (shared)",
        agent,
    )
    return None


def _codex_bind_mounts(home: Path) -> list[str]:
    return [f"{codex_empty_agents(home)}:{CODEX_AGENTS_DIR}"]


def _grok_bind_mounts(home: Path) -> list[str]:
    return [
        f"{grok_empty_agents(home)}:{GROK_AGENTS_DIR}",
        f"{grok_empty_claude_plugins(home)}:{GROK_CLAUDE_PLUGINS_DIR}",
    ]


def _claude_history_source(
    cli_dir: Path, sid: str
) -> tuple[Path, Literal["dir", "jsonl"]]:
    return session_history_file(cli_dir, sid), "jsonl"


def _codex_history_source(
    cli_dir: Path, sid: str
) -> tuple[Path, Literal["dir", "jsonl"]]:
    return codex_home(cli_dir.parent) / "sessions", "dir"


def _pi_history_source(
    cli_dir: Path, sid: str
) -> tuple[Path, Literal["dir", "jsonl"]]:
    return cli_dir / "pi-sessions", "dir"


HistorySource = tuple[Path, Literal["dir", "jsonl"]]


@dataclass(frozen=True)
class VesselProfile:
    kind: str
    diet_builder: str | None
    context_file: str | None
    fast: bool
    _private_home_fn: Callable[[Path], Path] | None
    _clean_env_fn: Callable[[Path], dict[str, str] | None] | None
    _bind_mounts_fn: Callable[[Path], list[str]] | None
    _vessel_cls_fn: Callable[[str | None], type]
    _history_source_fn: Callable[[Path, str], HistorySource] | None
    local_helper: bool = False
    warn_fast_ignored: bool = True

    def private_home(self, home: Path) -> Path | None:
        if self._private_home_fn is None:
            return None
        return self._private_home_fn(home)

    def clean_env(self, home: Path) -> dict[str, str] | None:
        if self._clean_env_fn is None:
            return None
        return self._clean_env_fn(home)

    def bind_mounts(self, home: Path) -> list[str]:
        if self._bind_mounts_fn is None:
            return []
        return self._bind_mounts_fn(home)

    def vessel_cls(self, model: str | None = None) -> type:
        return self._vessel_cls_fn(model)

    def history_source(self, cli_dir: Path, sid: str) -> HistorySource | None:
        if self._history_source_fn is None:
            return None
        return self._history_source_fn(cli_dir, sid)


def _fast_for(kind: str) -> bool:
    v = models_mod.VESSELS.get(kind)
    return bool(v and v.fast)


PROFILES: dict[str, VesselProfile] = {
    "claude-code": VesselProfile(
        kind="claude-code",
        diet_builder=None,
        context_file=None,
        fast=_fast_for("claude-code"),
        _private_home_fn=None,
        _clean_env_fn=None,
        _bind_mounts_fn=None,
        _vessel_cls_fn=_claude_cls,
        _history_source_fn=_claude_history_source,
        local_helper=False,
    ),
    "codex": VesselProfile(
        kind="codex",
        diet_builder="codex",
        context_file="AGENTS.md",
        fast=_fast_for("codex"),
        _private_home_fn=codex_home,
        _clean_env_fn=_codex_clean_env,
        _bind_mounts_fn=_codex_bind_mounts,
        _vessel_cls_fn=_codex_cls,
        _history_source_fn=_codex_history_source,
        local_helper=False,
        warn_fast_ignored=False,
    ),
    "antigravity": VesselProfile(
        kind="antigravity",
        diet_builder=None,
        context_file="AGENTS.md",
        fast=_fast_for("antigravity"),
        _private_home_fn=None,
        _clean_env_fn=None,
        _bind_mounts_fn=None,
        _vessel_cls_fn=_antigravity_cls,
        _history_source_fn=None,
        local_helper=False,
        warn_fast_ignored=False,
    ),
    "grok": VesselProfile(
        kind="grok",
        diet_builder="grok",
        context_file="AGENTS.md",
        fast=_fast_for("grok"),
        _private_home_fn=grok_home,
        _clean_env_fn=_grok_clean_env,
        _bind_mounts_fn=_grok_bind_mounts,
        _vessel_cls_fn=_grok_cls,
        _history_source_fn=None,
        local_helper=False,
        warn_fast_ignored=False,
    ),
    "pi": VesselProfile(
        kind="pi",
        diet_builder="pi",
        context_file=None,
        fast=_fast_for("pi"),
        _private_home_fn=_pi_agent_home,
        _clean_env_fn=_pi_clean_env,
        _bind_mounts_fn=None,
        _vessel_cls_fn=_pi_cls,
        _history_source_fn=_pi_history_source,
        local_helper=True,
    ),
}


def profile(kind: str) -> VesselProfile:
    """Return the VesselProfile for ``kind`` or raise VesselError."""
    try:
        return PROFILES[kind]
    except KeyError:
        raise VesselError(f"unknown vessel kind: {kind!r}") from None
