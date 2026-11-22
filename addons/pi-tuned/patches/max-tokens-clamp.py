#!/usr/bin/env python3
"""Pulsar patch: remove pi-ai's hidden 32000 output-token clamp.

pi-ai's buildBaseOptions() sends max_tokens = min(model.maxTokens, 32000)
whenever the caller does not pass maxTokens explicitly (pi-coding-agent
never does). So a models.json maxTokens of 65536+ silently becomes 32000
and long reasoning turns can stop on `length` mid-thinking with no reply.

Patch: honor model.maxTokens as configured. Same contract as the other
patches here: idempotent, marker-checked, run by postinstall.

Modes: apply (default) | check (exit 1 when missing)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "node_modules" / "@mariozechner" / "pi-ai" / "dist" / "providers" / "simple-options.js"

MARKER = "pulsar: no 32000 clamp"

STOCK = "        maxTokens: options?.maxTokens ?? (model.maxTokens > 0 ? Math.min(model.maxTokens, 32000) : undefined),"

PATCHED = ("        // pulsar: no 32000 clamp — models.json maxTokens is the cap.\n"
           "        maxTokens: options?.maxTokens ?? (model.maxTokens > 0 ? model.maxTokens : undefined),")


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "apply"
    try:
        source = TARGET.read_text(encoding="utf-8")
    except OSError as e:
        print(f"pi max-tokens-clamp patch: cannot read {TARGET}: {e}", file=sys.stderr)
        return 2

    if MARKER in source:
        if mode == "check":
            print("pi max-tokens-clamp patch: present")
        return 0
    if mode == "check":
        print(f"pi max-tokens-clamp patch: MISSING from {TARGET}", file=sys.stderr)
        return 1

    if source.count(STOCK) != 1:
        print(
            f"pi max-tokens-clamp patch: anchor found {source.count(STOCK)}x (want 1) "
            "— pi-ai updated past this patch; re-derive",
            file=sys.stderr,
        )
        return 3
    TARGET.write_text(source.replace(STOCK, PATCHED, 1), encoding="utf-8")
    print(f"pi max-tokens-clamp patch: re-applied to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
