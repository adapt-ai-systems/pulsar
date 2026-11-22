"""Atomic file writes: write a temp file beside the target, then rename it
over the target, so a reader never sees a half-written file.

Stdlib only, so the runner process can import it without pulling in daemon
code. The helpers do what every hand-rolled ``tmp.write_text(...);
tmp.replace(path)`` site did: no fsync, default file mode, no mkdir of the
parent (the caller makes it), and no cleanup of the temp file on failure —
an ``OSError`` propagates unchanged and the target is untouched.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


def atomic_write_text(path: Path, text: str, *, encoding: str | None = None,
                      tmp: Path | None = None) -> None:
    """Write ``text`` to ``path`` through a temp file (mode 0600).

    ``tmp`` is the temp file's path; the default is ``<path>.tmp`` beside the
    target. Readers that skip ``*.tmp`` names (inbox scanners) rely on it, so
    a caller whose temp name was different keeps passing it.
    """
    if tmp is None:
        tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding=encoding) as fh:
            fd = -1
            fh.write(text)
    finally:
        if fd >= 0:
            os.close(fd)
    tmp.replace(path)


def atomic_write_json(path: Path, value: Any, *, indent: int | None = None,
                      ensure_ascii: bool = True, newline: bool = False,
                      encoding: str | None = None,
                      tmp: Path | None = None) -> None:
    """``json.dumps(value, indent=..., ensure_ascii=...)`` written atomically,
    plus one trailing ``"\\n"`` when ``newline`` is true."""
    text = json.dumps(value, indent=indent, ensure_ascii=ensure_ascii)
    if newline:
        text += "\n"
    atomic_write_text(path, text, encoding=encoding, tmp=tmp)
