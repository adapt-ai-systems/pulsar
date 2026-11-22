"""Detect which supported agent CLI binaries are available on this host.

Each vessel uses the same env-var override and default lookup path documented
in README.md ("Agent tools and how Pulsar finds them") and implemented in the
vessel adapters and ``pulsar.launch.units``.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from typing import Any, Optional

# User-level tool directories the runner adds to PATH (pulsar/runner/runner.py).
_USER_TOOL_DIRS = (".local/bin", ".cargo/bin")


def _search_path() -> str:
    """Build PATH matching what pulse runners search."""
    raw = os.environ.get("PATH", "")
    parts = [p for p in raw.split(os.pathsep) if p]
    home = Path.home()
    for rel in _USER_TOOL_DIRS:
        d = str(home / rel)
        if d not in parts and (home / rel).is_dir():
            parts.append(d)
    return os.pathsep.join(parts)


def _executable(path: Optional[str | Path]) -> Optional[str]:
    if not path:
        return None
    expanded = os.path.expanduser(str(path))
    if os.path.isfile(expanded) and os.access(expanded, os.X_OK):
        return expanded
    return None


def _find_on_path(binary: str, search_path: str) -> Optional[str]:
    return shutil.which(binary, path=search_path)


def _resolve_vessel(vessel_id: str, search_path: str, home: Path) -> dict[str, Any]:
    if vessel_id == "claude-code":
        binary = "claude"
        env = os.environ.get("PULSAR_CLAUDE_BIN")
        found = _executable(env) if env else _find_on_path(binary, search_path)
    elif vessel_id == "codex":
        binary = "codex"
        env = os.environ.get("PULSAR_CODEX_BIN")
        found = _executable(env) if env else _find_on_path(binary, search_path)
    elif vessel_id == "pi":
        binary = "pi"
        env = os.environ.get("PULSAR_PI_BIN")
        default_pi = (
            home / ".local" / "share" / "pulsar" / "pi-tuned"
            / "node_modules" / ".bin" / "pi"
        )
        if env:
            found = _executable(env)
        else:
            found = _executable(default_pi) or _find_on_path(binary, search_path)
    elif vessel_id == "antigravity":
        binary = "agy"
        env = os.environ.get("PULSAR_AGY_BIN")
        default_agy = home / ".local" / "bin" / "agy"
        found = _executable(env) if env else _executable(default_agy)
    elif vessel_id == "grok":
        binary = "grok"
        env = os.environ.get("PULSAR_GROK_BIN")
        default_grok = home / ".local" / "bin" / "grok"
        if env:
            found = _executable(env)
        else:
            found = _find_on_path(binary, search_path) or _executable(default_grok)
    else:
        return {
            "available": False,
            "binary": vessel_id,
            "path": None,
            "reason": f"{vessel_id} not found",
        }

    return {
        "available": found is not None,
        "binary": binary,
        "path": found,
        "reason": None if found is not None else f"{binary} not found",
    }


VESSEL_ORDER: tuple[str, ...] = (
    "claude-code",
    "codex",
    "pi",
    "antigravity",
    "grok",
)


def detect_tools() -> dict[str, dict[str, Any]]:
    """Return availability status per vessel id."""
    search_path = _search_path()
    home = Path.home()
    return {vid: _resolve_vessel(vid, search_path, home) for vid in VESSEL_ORDER}


def tools_summary() -> dict[str, Any]:
    """Return the read-only payload for ``GET /tools``."""
    vessels = detect_tools()
    return {
        "any_available": any(v["available"] for v in vessels.values()),
        "vessels": vessels,
    }
