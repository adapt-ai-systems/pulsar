#!/usr/bin/env python3
"""Pulsar patch: GLM local templates honor reasoning_effort.

pi-ai's openai-completions provider sends only `enable_thinking` in
chat_template_kwargs, so GLM-5.2 / GLM-5.3-flash on local vLLM servers
never receive the effort level (high/xhigh) — thinking always runs at
template default. Pulsar's thinking dial (pi.py _GLM52_THINKING -> pi
--thinking xhigh) relies on this reaching the template.

Idempotent, marker-checked, same contract as bash-description.py: run by
postinstall after every npm install/ci; exits non-zero if upstream diverges.

Modes: apply (default) | check (exit 1 when missing)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "node_modules" / "@mariozechner" / "pi-ai" / "dist" / "providers" / "openai-completions.js"

MARKER = 'kwargs.reasoning_effort ='

STOCK = '''    else if (compat.thinkingFormat === "qwen-chat-template" && model.reasoning) {
        params.chat_template_kwargs = {
            enable_thinking: !!options?.reasoningEffort,
            preserve_thinking: true,
        };
    }'''

PATCHED = '''    else if (compat.thinkingFormat === "qwen-chat-template" && model.reasoning) {
        // GLM-5.2 (and other local templates) read both enable_thinking and
        // reasoning_effort from chat_template_kwargs. enable_thinking false
        // is the only off switch; high vs max is the effort string.
        const kwargs = {
            enable_thinking: !!options?.reasoningEffort,
            preserve_thinking: true,
        };
        if (options?.reasoningEffort) {
            kwargs.reasoning_effort =
                model.thinkingLevelMap?.[options.reasoningEffort] ?? options.reasoningEffort;
        }
        params.chat_template_kwargs = kwargs;
    }'''


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "apply"
    try:
        source = TARGET.read_text(encoding="utf-8")
    except OSError as e:
        print(f"pi glm-thinking-effort patch: cannot read {TARGET}: {e}", file=sys.stderr)
        return 2

    if MARKER in source:
        if mode == "check":
            print("pi glm-thinking-effort patch: present")
        return 0
    if mode == "check":
        print(f"pi glm-thinking-effort patch: MISSING from {TARGET}", file=sys.stderr)
        return 1

    if source.count(STOCK) != 1:
        print(
            f"pi glm-thinking-effort patch: qwen-chat-template anchor found "
            f"{source.count(STOCK)}x (want 1) — pi-ai updated past this patch; re-derive",
            file=sys.stderr,
        )
        return 3
    TARGET.write_text(source.replace(STOCK, PATCHED, 1), encoding="utf-8")
    print(f"pi glm-thinking-effort patch: re-applied to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
