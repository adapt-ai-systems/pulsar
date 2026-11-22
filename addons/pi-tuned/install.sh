#!/usr/bin/env bash
set -euo pipefail

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
TARGET_DIR="${1:-$HOME/.local/share/pulsar/pi-tuned}"

for tool in node npm python3; do
  if ! command -v "$tool" >/dev/null 2>&1; then
    echo "ERROR: required tool '$tool' not found on PATH." >&2
    exit 1
  fi
done

mkdir -p "$TARGET_DIR"
TARGET_DIR="$(cd "$TARGET_DIR" && pwd)"

if [ "$TARGET_DIR" != "$SRC_DIR" ]; then
  cp "$SRC_DIR/package.json" "$SRC_DIR/package-lock.json" "$TARGET_DIR/"
  mkdir -p "$TARGET_DIR/patches"
  cp "$SRC_DIR"/patches/*.py "$TARGET_DIR/patches/"
fi

cd "$TARGET_DIR"
npm ci --ignore-scripts

PATCHES=(
  bash-description.py
  glm-thinking-effort.py
  max-tokens-clamp.py
  context-fit-max-tokens.py
)

for patch in "${PATCHES[@]}"; do
  python3 "patches/$patch"
done

failed=0
echo ""
echo "Patch status:"
for patch in "${PATCHES[@]}"; do
  if out="$(python3 "patches/$patch" check 2>&1)"; then
    echo "  [applied] $patch ($out)"
  else
    echo "  [FAILED]  $patch ($out)" >&2
    failed=1
  fi
done

if [ "$failed" -ne 0 ]; then
  echo "ERROR: one or more patches did not apply." >&2
  exit 1
fi

PI_BIN="$TARGET_DIR/node_modules/.bin/pi"
if [ ! -x "$PI_BIN" ]; then
  echo "ERROR: pi binary not found at $PI_BIN" >&2
  exit 1
fi

echo ""
echo "pi binary: $PI_BIN"
