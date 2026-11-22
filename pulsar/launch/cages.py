"""Cages: the per-pulse pi tool-call guards a diet can switch on.

Daemon-side (pulsar.launch): runners never import this module.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from pulsar.fsutil import atomic_write_json

log = logging.getLogger("pulsar.spawn")

# Cages: mechanical tool-call guards, one dir per cage holding a
# ``cage.ts`` pi extension. Toggled per pulse like skills (diet["cages"]).
CAGES_ROOT = Path.home() / ".claude" / "cages"


def list_cages(root: Path | None = None) -> list[str]:
    """Cage names for the UI/API: dirs under CAGES_ROOT holding a cage.ts."""
    root = CAGES_ROOT if root is None else root
    try:
        return sorted(p.name for p in root.iterdir()
                      if p.is_dir() and not p.name.startswith((".", "_"))
                      and (p / "cage.ts").is_file())
    except FileNotFoundError:
        return []


CAGES_MARKER = "pi-args.cages.json"


def _materialize_cages(pulse_id: str, home: Path, cages: list[str],
                       vessel: str) -> list[str]:
    """Turn diet["cages"] into ``<home>/pi-args.json`` (``-e <cage.ts>`` per
    cage) so the pi vessel loads them on every invocation (pi_extra_args).

    Ownership: a sidecar ``pi-args.cages.json`` records that WE wrote
    pi-args.json. A hand-written pi-args.json (no sidecar) is never touched
    when the diet has no cages, so per-pulse bridges keep working. Cages are
    pi-only today: on another vessel a non-empty list logs a warning and
    nothing is written. Missing cage dirs are skipped loudly, never fatal.
    Returns the cage names actually installed.
    """
    args_path = home / "pi-args.json"
    marker = home / CAGES_MARKER
    if not cages:
        if marker.exists():
            args_path.unlink(missing_ok=True)
            marker.unlink()
            log.info("cages pulse=%s cleared pi-args.json (no cages)", pulse_id)
        return []
    if vessel != "pi":
        log.warning("cages pulse=%s %s requested on vessel=%s — cages are "
                    "pi-only, ignored", pulse_id, cages, vessel)
        return []
    installed: list[str] = []
    args: list[str] = []
    for name in cages:
        if "/" in name or name in (".", ".."):
            log.error("cages pulse=%s cage name %r invalid — skipped",
                      pulse_id, name)
            continue
        src = CAGES_ROOT / name / "cage.ts"
        if not src.is_file():
            log.error("cages pulse=%s cage %r missing at %s — skipped",
                      pulse_id, name, src)
            continue
        args += ["-e", str(src)]
        installed.append(name)
    home.mkdir(parents=True, exist_ok=True)
    atomic_write_json(args_path, args)
    marker.write_text(json.dumps(installed))
    log.info("cages pulse=%s installed=%s", pulse_id, installed)
    return installed
