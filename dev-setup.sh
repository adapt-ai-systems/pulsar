#!/usr/bin/env bash
# Prepare a fresh clone for development and for ./ci.sh.
#
# It makes ./venv, installs the pinned Python packages plus pytest, and
# installs the web packages from the lock file. It does not use sudo, does
# not install or start a service, and does not change anything outside the
# clone. Safe to run twice.
#
# Needs: Python >= 3.12 (with venv + pip, or uv), Node.js, pnpm >= 9.
# To install and run Pulsar itself, use ./install.sh instead.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$REPO_DIR/venv"
cd "$REPO_DIR"

fail() { echo "dev-setup: $*" >&2; exit 1; }

command -v node >/dev/null 2>&1 || fail "node not found (see README.md, Install prerequisites)"
command -v pnpm >/dev/null 2>&1 || fail "pnpm not found (see README.md, Install prerequisites)"

PYTHON_BIN=""
for candidate in python3.13 python3.12 python3; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' 2>/dev/null; then
    PYTHON_BIN="$(command -v "$candidate")"
    break
  fi
done

echo "== Python virtualenv at $VENV_DIR =="
if command -v uv >/dev/null 2>&1; then
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    uv venv --seed --python "${PYTHON_BIN:-3.12}" "$VENV_DIR"
  fi
  uv pip install --python "$VENV_DIR/bin/python" -r requirements-dev.txt
  uv pip install --python "$VENV_DIR/bin/python" --no-deps -e .
else
  [ -n "$PYTHON_BIN" ] || fail "Python >= 3.12 not found (see README.md, Install prerequisites)"
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR" || fail "could not make the virtualenv (install python3-venv or uv)"
  fi
  "$VENV_DIR/bin/python" -m pip install --quiet -r requirements-dev.txt
  "$VENV_DIR/bin/python" -m pip install --quiet --no-deps -e .
fi

echo "== web packages =="
( cd web && pnpm install --frozen-lockfile )

echo
echo "Ready. Run ./ci.sh"
