#!/bin/sh
# Pulsar CI gate. Exit 0 = pass.
# Deterministic suites only — nothing here reaches an external host
# (the real_qwen_smoke and real_codex_smoke tests are excluded;
# svelte-check runs with --no-install so npx never touches the registry).
set -eu
# Pin UTC so timestamp-sensitive tests are deterministic regardless of host TZ.
export TZ=UTC
cd "$(dirname "$0")"
venv/bin/python -m pytest tests -q -k 'not real_qwen_smoke and not real_codex_smoke'
( cd web && node --test src/lib/*.test.mjs && npx --no-install svelte-check --threshold error )
