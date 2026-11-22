#!/usr/bin/env python3
"""Pulsar patch: pi bash tool gains an optional `description` arg.

The Pulsar UI shows WHAT a tool call is for; Claude CLI carries a
description on Bash/Task calls, while stock pi bash has no such field,
so pi pulses rendered raw command text instead of intent.
This script applies that patch in place.

Idempotent: checks for the marker first, applies only when missing.
Modes:
  apply   (default) patch if the marker is absent, no-op if present
  check   exit 0 if present, exit 1 if missing (for guards/watches)

Run automatically after every npm install/ci via the postinstall
script in package.json.
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "node_modules" / "@mariozechner" / "pi-coding-agent" / "dist" / "core" / "tools" / "bash.js"

# Marker added by the patch — its presence means "already applied".
MARKER = 'description: Type.Optional(Type.String({ description: "One line on what this command is for'

REPLACEMENTS = [
    # 1. schema: add the optional description arg
    (
        'const bashSchema = Type.Object({\n'
        '    command: Type.String({ description: "Bash command to execute" }),\n'
        '    timeout: Type.Optional(Type.Number({ description: "Timeout in seconds (optional, no default timeout)" })),\n'
        '});',
        'const bashSchema = Type.Object({\n'
        '    command: Type.String({ description: "Bash command to execute" }),\n'
        '    description: Type.Optional(Type.String({ description: "One line on what this command is for and why you are running it (shown to the human watching your session — never skip it)" })),\n'
        '    timeout: Type.Optional(Type.Number({ description: "Timeout in seconds (optional, no default timeout)" })),\n'
        '});',
    ),
    # 2. TUI call display: show the description as a muted line above $ cmd
    (
        'function formatBashCall(args) {\n'
        '    const command = str(args?.command);\n'
        '    const timeout = args?.timeout;\n'
        '    const timeoutSuffix = timeout ? theme.fg("muted", ` (timeout ${timeout}s)`) : "";\n'
        '    const commandDisplay = command === null ? invalidArgText(theme) : command ? command : theme.fg("toolOutput", "...");\n'
        '    return theme.fg("toolTitle", theme.bold(`$ ${commandDisplay}`)) + timeoutSuffix;\n'
        '}',
        'function formatBashCall(args) {\n'
        '    const command = str(args?.command);\n'
        '    const timeout = args?.timeout;\n'
        '    const description = str(args?.description);\n'
        '    const timeoutSuffix = timeout ? theme.fg("muted", ` (timeout ${timeout}s)`) : "";\n'
        '    const commandDisplay = command === null ? invalidArgText(theme) : command ? command : theme.fg("toolOutput", "...");\n'
        '    const descriptionLine = description ? theme.fg("muted", `${description}\\n`) : "";\n'
        '    return descriptionLine + theme.fg("toolTitle", theme.bold(`$ ${commandDisplay}`)) + timeoutSuffix;\n'
        '}',
    ),
    # 3. tool description: tell the model to always fill it
    (
        'Optionally provide a timeout in seconds.`,',
        'Optionally provide a timeout in seconds. ALWAYS provide a short description of what the command is for and why you are running it — it is shown to the human watching your session.`,',
    ),
]


def applied(source: str) -> bool:
    return MARKER in source


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "apply"
    try:
        source = TARGET.read_text(encoding="utf-8")
    except OSError as e:
        print(f"pi bash-description patch: cannot read {TARGET}: {e}", file=sys.stderr)
        return 2

    if applied(source):
        if mode == "check":
            print("pi bash-description patch: present")
        return 0

    if mode == "check":
        print(f"pi bash-description patch: MISSING from {TARGET}", file=sys.stderr)
        return 1

    missing = 0
    for old, new in REPLACEMENTS:
        if old in source:
            source = source.replace(old, new, 1)
        else:
            missing += 1
    if missing:
        # Only the exact-count hard-replace keeps us honest: if the shipped
        # bash.js diverges from what this patch expects, DO NOT half-apply.
        print(
            f"pi bash-description patch: {missing}/{len(REPLACEMENTS)} anchor(s) not found — "
            "pi-coding-agent likely updated past this patch; re-derive the anchors",
            file=sys.stderr,
        )
        return 3
    TARGET.write_text(source, encoding="utf-8")
    print(f"pi bash-description patch: re-applied to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
