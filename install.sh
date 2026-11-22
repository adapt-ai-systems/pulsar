#!/usr/bin/env bash
# Install Pulsar from a fresh clone on a Linux machine.
#
# Requirements (from pyproject.toml, web/pnpm-lock.yaml, addons/pi-tuned/README.md):
#   - Linux with systemd user services (systemctl, systemd-run)
#   - Python >= 3.12 (with venv + pip, or uv)
#   - Node.js >= 18.13 (>= 20.6.0 with --with-pi-tuned)
#   - pnpm >= 9
#
# Safe to run twice. Asks nothing it can detect.

set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

DAEMON_PORT="${PULSAR_DAEMON_PORT:-8726}"
WEB_PORT="${PULSAR_WEB_PORT:-5292}"
DATA_DIR="${PULSAR_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/pulsar}"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/pulsar"
UNIT_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user"
BIN_DIR="$HOME/.local/bin"
VENV_DIR="$REPO_DIR/venv"

WITH_PI_TUNED=0
NO_START=0

usage() {
  cat <<'EOF'
Usage: ./install.sh [options]

Install Pulsar (Python venv, web UI build, ops/bin CLI symlinks, and systemd
user units for pulsar-daemon and pulsar-web). Safe to run more than once.

Options:
  --with-pi-tuned          Also run addons/pi-tuned/install.sh and configure PULSAR_PI_BIN
  --daemon-port PORT       Daemon listen port on 127.0.0.1 (default: 8726, env: PULSAR_DAEMON_PORT)
  --web-port PORT          Web UI listen port on 127.0.0.1 (default: 5292, env: PULSAR_WEB_PORT)
  --data-dir DIR           Runtime data directory (default: ~/.local/share/pulsar, env: PULSAR_DATA)
  --no-start               Write unit files without enabling or starting systemd user services
  -h, --help               Show this help text
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --with-pi-tuned) WITH_PI_TUNED=1; shift ;;
    --daemon-port)
      [ $# -ge 2 ] || { echo "error: --daemon-port requires a value" >&2; exit 2; }
      DAEMON_PORT="$2"; shift 2 ;;
    --web-port)
      [ $# -ge 2 ] || { echo "error: --web-port requires a value" >&2; exit 2; }
      WEB_PORT="$2"; shift 2 ;;
    --data-dir)
      [ $# -ge 2 ] || { echo "error: --data-dir requires a value" >&2; exit 2; }
      DATA_DIR="$2"; shift 2 ;;
    --no-start|--no-systemd) NO_START=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "error: unknown option '$1'" >&2; usage >&2; exit 2 ;;
  esac
done

# --- Port validation ---------------------------------------------------------
validate_port() {
  local label="$1" val="$2"
  if ! [[ "$val" =~ ^[0-9]+$ ]] || [ "$val" -lt 1 ] || [ "$val" -gt 65535 ]; then
    echo "error: $label must be an integer between 1 and 65535 (got '$val')" >&2
    exit 2
  fi
}
validate_port "--daemon-port" "$DAEMON_PORT"
validate_port "--web-port" "$WEB_PORT"
if [ "$DAEMON_PORT" = "$WEB_PORT" ]; then
  echo "error: --daemon-port and --web-port must differ (both are $DAEMON_PORT)" >&2
  exit 2
fi

# --- Prerequisite checks (all in one pass) -----------------------------------
version_ge() {
  # Returns 0 if dotted version $1 >= $2.
  local IFS=.
  local -a a b
  read -r -a a <<<"$1"
  read -r -a b <<<"$2"
  local len=${#a[@]}
  [ ${#b[@]} -gt "$len" ] && len=${#b[@]}
  local i ai bi
  for ((i = 0; i < len; i++)); do
    ai="${a[i]:-0}"
    bi="${b[i]:-0}"
    ai="${ai%%[^0-9]*}"; ai="${ai:-0}"
    bi="${bi%%[^0-9]*}"; bi="${bi:-0}"
    if ((10#$ai > 10#$bi)); then return 0; fi
    if ((10#$ai < 10#$bi)); then return 1; fi
  done
  return 0
}

PREREQ_FAILED=0

prereq_ok() {
  echo "  [OK]   $1: $2"
}

prereq_fail() {
  echo "  [FAIL] $1: $2"
  PREREQ_FAILED=1
}

echo "== checking prerequisites =="

# 1. Linux OS
OS_NAME="$(uname -s 2>/dev/null || echo unknown)"
if [ "$OS_NAME" = "Linux" ]; then
  OS_REL="$(uname -r 2>/dev/null || true)"
  prereq_ok "OS" "Linux${OS_REL:+ ($OS_REL)}"
else
  prereq_fail "OS" "found $OS_NAME; Linux is required (use a Linux VM on macOS or Windows)"
fi

# 2. systemd tools and user session
SYSTEMCTL_BIN="$(command -v systemctl 2>/dev/null || true)"
SYSTEMD_RUN_BIN="$(command -v systemd-run 2>/dev/null || true)"
if [ -n "$SYSTEMCTL_BIN" ] && [ -n "$SYSTEMD_RUN_BIN" ]; then
  SYSTEMD_VER="$("$SYSTEMCTL_BIN" --version 2>/dev/null | sed -n '1p' || true)"
  prereq_ok "systemd" "${SYSTEMD_VER:-installed} ($SYSTEMCTL_BIN, $SYSTEMD_RUN_BIN)"
else
  missing_sd=()
  [ -z "$SYSTEMCTL_BIN" ] && missing_sd+=(systemctl)
  [ -z "$SYSTEMD_RUN_BIN" ] && missing_sd+=(systemd-run)
  prereq_fail "systemd" "missing ${missing_sd[*]} (systemd user services are required)"
fi

REAL_HOME="$(getent passwd "$(id -u)" 2>/dev/null | cut -d: -f6 || true)"
if [ -z "$REAL_HOME" ] && [ -r /etc/passwd ]; then
  REAL_HOME="$(awk -F: -v u="$(id -u)" '$3==u {print $6; exit}' /etc/passwd 2>/dev/null || true)"
fi

CAN_SYSTEMCTL=1
SKIP_REASON=""
if [ "$NO_START" -eq 1 ]; then
  CAN_SYSTEMCTL=0
  SKIP_REASON="--no-start was passed"
  prereq_ok "systemd user session" "skipped (--no-start)"
elif [ -n "$REAL_HOME" ] && [ -d "$HOME" ] && [ -d "$REAL_HOME" ] && \
     [ "$(cd "$HOME" && pwd)" != "$(cd "$REAL_HOME" && pwd)" ]; then
  CAN_SYSTEMCTL=0
  SKIP_REASON="HOME ($HOME) differs from login home ($REAL_HOME); refusing to touch live user systemd"
  prereq_ok "systemd user session" "skipped (HOME differs from login home)"
elif [ -z "$SYSTEMCTL_BIN" ]; then
  CAN_SYSTEMCTL=0
  SKIP_REASON="systemctl not found"
  prereq_fail "systemd user session" "cannot check without systemctl"
elif "$SYSTEMCTL_BIN" --user show-environment >/dev/null 2>&1; then
  prereq_ok "systemd user session" "active"
else
  CAN_SYSTEMCTL=0
  SKIP_REASON="no active systemd --user session bus detected"
  prereq_fail "systemd user session" "not active (run inside a systemd user login session, or pass --no-start)"
fi

# 3. Python >= 3.12 (plus uv or venv/ensurepip)
PYTHON_BIN=""
PYTHON_VER=""
PYTHON_FOUND_BIN=""
PYTHON_FOUND_VER=""

find_python() {
  local cand path ver
  local -a candidates=()
  if [ -n "${PYTHON:-}" ]; then
    candidates=("$PYTHON")
  else
    candidates=(python3 python3.15 python3.14 python3.13 python3.12)
  fi
  for cand in "${candidates[@]}"; do
    if path="$(command -v "$cand" 2>/dev/null)"; then
      ver="$("$path" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")' 2>/dev/null || true)"
      if [ -n "$ver" ]; then
        if [ -z "$PYTHON_FOUND_VER" ]; then
          PYTHON_FOUND_BIN="$path"
          PYTHON_FOUND_VER="$ver"
        fi
        if version_ge "$ver" "3.12.0"; then
          PYTHON_BIN="$path"
          PYTHON_VER="$ver"
          return 0
        fi
      fi
    fi
  done
  return 1
}

if find_python; then
  prereq_ok "Python" "$PYTHON_VER ($PYTHON_BIN)"
  UV_BIN="$(command -v uv 2>/dev/null || true)"
  if [ -n "$UV_BIN" ]; then
    UV_VER="$("$UV_BIN" --version 2>/dev/null | sed -n '1p' || true)"
    prereq_ok "Python venv" "${UV_VER:-uv} ($UV_BIN)"
  elif "$PYTHON_BIN" -c 'import venv, ensurepip' >/dev/null 2>&1; then
    prereq_ok "Python venv" "stdlib venv and pip available"
  else
    prereq_fail "Python venv" "missing venv or ensurepip for $PYTHON_BIN (install python3-venv or uv)"
  fi
elif [ -n "$PYTHON_FOUND_VER" ]; then
  prereq_fail "Python" "found $PYTHON_FOUND_VER at $PYTHON_FOUND_BIN (need >= 3.12)"
else
  prereq_fail "Python" "not found on PATH (need >= 3.12)"
fi

# 4. Node.js
MIN_NODE="18.13.0"
if [ "$WITH_PI_TUNED" -eq 1 ]; then
  MIN_NODE="20.6.0"
fi
NODE_BIN="$(command -v node 2>/dev/null || true)"
NODE_VER=""
if [ -z "$NODE_BIN" ]; then
  prereq_fail "Node.js" "not found on PATH (need >= $MIN_NODE)"
else
  NODE_VER="$("$NODE_BIN" -v 2>/dev/null | sed 's/^v//' || true)"
  if [ -n "$NODE_VER" ] && version_ge "$NODE_VER" "$MIN_NODE"; then
    prereq_ok "Node.js" "v$NODE_VER ($NODE_BIN)"
  else
    prereq_fail "Node.js" "found v${NODE_VER:-unknown} at $NODE_BIN (need >= $MIN_NODE)"
  fi
fi

# 5. pnpm >= 9
PNPM_BIN="$(command -v pnpm 2>/dev/null || true)"
PNPM_VER=""
if [ -z "$PNPM_BIN" ]; then
  prereq_fail "pnpm" "not found on PATH (need >= 9)"
else
  PNPM_VER="$("$PNPM_BIN" --version 2>/dev/null | sed -n '1p' || true)"
  if [ -n "$PNPM_VER" ] && version_ge "$PNPM_VER" "9.0.0"; then
    prereq_ok "pnpm" "$PNPM_VER ($PNPM_BIN)"
  else
    prereq_fail "pnpm" "found ${PNPM_VER:-unknown} at $PNPM_BIN (need >= 9)"
  fi
fi

# 6. Helper tools needed by the install and build scripts
FLOCK_BIN="$(command -v flock 2>/dev/null || true)"
if [ -n "$FLOCK_BIN" ]; then
  FLOCK_VER="$("$FLOCK_BIN" --version 2>/dev/null | sed -n '1p' || true)"
  prereq_ok "flock" "${FLOCK_VER:-installed} ($FLOCK_BIN)"
else
  prereq_fail "flock" "not found on PATH (install util-linux)"
fi

if [ "$WITH_PI_TUNED" -eq 1 ]; then
  NPM_BIN="$(command -v npm 2>/dev/null || true)"
  if [ -n "$NPM_BIN" ]; then
    NPM_VER="$("$NPM_BIN" --version 2>/dev/null | sed -n '1p' || true)"
    prereq_ok "npm" "${NPM_VER:-installed} ($NPM_BIN)"
  else
    prereq_fail "npm" "not found on PATH (required for --with-pi-tuned)"
  fi
fi

if [ "$PREREQ_FAILED" -ne 0 ]; then
  echo "" >&2
  echo "error: prerequisites check failed. Fix the [FAIL] items above and re-run ./install.sh." >&2
  echo "See the Requirements section in README.md for copy-paste setup commands." >&2
  exit 1
fi

# --- Directories -------------------------------------------------------------
mkdir -p "$DATA_DIR" "$CONFIG_DIR" "$BIN_DIR" "$UNIT_DIR"
DATA_DIR="$(cd "$DATA_DIR" && pwd)"
CONFIG_DIR="$(cd "$CONFIG_DIR" && pwd)"
UNIT_DIR="$(cd "$UNIT_DIR" && pwd)"
chmod 700 "$DATA_DIR" "$CONFIG_DIR"
for dbfile in "$DATA_DIR/pulsar.db" "$DATA_DIR/pulsar.db-wal" "$DATA_DIR/pulsar.db-shm" "$DATA_DIR/guard.json"; do
  [ -f "$dbfile" ] && chmod 600 "$dbfile"
done
if [ -d "$DATA_DIR/pulses" ]; then
  find "$DATA_DIR/pulses" ! -type l -exec chmod go-rwx {} +
fi

# --- Python virtualenv + editable install ------------------------------------
echo "== setting up Python virtualenv at $VENV_DIR =="
if command -v uv >/dev/null 2>&1; then
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    uv venv --seed --python "$PYTHON_BIN" "$VENV_DIR"
  fi
  uv pip install --python "$VENV_DIR/bin/python" -r "$REPO_DIR/requirements.txt"
  uv pip install --python "$VENV_DIR/bin/python" --no-deps -e "$REPO_DIR"
else
  if [ ! -x "$VENV_DIR/bin/python" ]; then
    "$PYTHON_BIN" -m venv "$VENV_DIR"
  fi
  "$VENV_DIR/bin/python" -m pip install --quiet -r "$REPO_DIR/requirements.txt"
  "$VENV_DIR/bin/python" -m pip install --quiet --no-deps -e "$REPO_DIR"
fi

# --- Web dependencies + production build -------------------------------------
echo "== installing web dependencies and building web/live =="
(
  cd "$REPO_DIR/web"
  "$PNPM_BIN" install --frozen-lockfile
  ./scripts/build-live.sh
)

# --- CLI tools on PATH -------------------------------------------------------
echo "== linking ops/bin tools into $BIN_DIR =="
for tool in "$REPO_DIR"/ops/bin/*; do
  [ -f "$tool" ] && [ -x "$tool" ] || continue
  ln -sfn "$tool" "$BIN_DIR/$(basename "$tool")"
done

# --- Build service PATH (detected tool dirs, deduplicated) -------------------
append_path_dir() {
  local dir="$1"
  [ -n "$dir" ] && [ -d "$dir" ] || return 0
  if [[ ":$SERVICE_PATH:" != *":$dir:"* ]]; then
    SERVICE_PATH="${SERVICE_PATH:+$SERVICE_PATH:}$dir"
  fi
}

SERVICE_PATH=""
append_path_dir "$BIN_DIR"
append_path_dir "$REPO_DIR/ops/bin"
append_path_dir "$(dirname "$NODE_BIN")"
append_path_dir "$(dirname "$PNPM_BIN")"
for cli in claude codex pi agy grok; do
  cli_path="$(command -v "$cli" 2>/dev/null || true)"
  [ -n "$cli_path" ] && append_path_dir "$(dirname "$cli_path")"
done
for std in /usr/local/bin /usr/bin /bin; do
  append_path_dir "$std"
done

# --- Render systemd user units -----------------------------------------------
echo "== writing systemd user units to $UNIT_DIR =="
for unit in pulsar.slice pulsar-daemon.service pulsar-web.service; do
  "$VENV_DIR/bin/python" - \
    "$REPO_DIR/ops/systemd/${unit}.in" \
    "$UNIT_DIR/$unit" \
    "$REPO_DIR" \
    "$VENV_DIR" \
    "$DATA_DIR" \
    "$CONFIG_DIR" \
    "$DAEMON_PORT" \
    "$WEB_PORT" \
    "$NODE_BIN" \
    "$SERVICE_PATH" <<'PY'
import sys
from pathlib import Path

(
    src, dst, repo_dir, venv_dir, data_dir, config_dir,
    daemon_port, web_port, node_bin, service_path,
) = sys.argv[1:11]

text = Path(src).read_text(encoding="utf-8")
def esc_spec(val: str) -> str:
    return val.replace("%", "%%")
def esc_quoted(val: str) -> str:
    return val.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
replacements = {
    "@REPO_DIR_QUOTED@": esc_quoted(repo_dir),
    "@VENV_DIR_QUOTED@": esc_quoted(venv_dir),
    "@DATA_DIR_QUOTED@": esc_quoted(data_dir),
    "@CONFIG_DIR_QUOTED@": esc_quoted(config_dir),
    "@NODE_BIN_QUOTED@": esc_quoted(node_bin),
    "@SERVICE_PATH_QUOTED@": esc_quoted(service_path),
    "@REPO_DIR@": esc_spec(repo_dir),
    "@VENV_DIR@": esc_spec(venv_dir),
    "@DATA_DIR@": esc_spec(data_dir),
    "@CONFIG_DIR@": esc_spec(config_dir),
    "@DAEMON_PORT@": daemon_port,
    "@WEB_PORT@": web_port,
    "@NODE_BIN@": esc_spec(node_bin),
    "@SERVICE_PATH@": esc_spec(service_path),
}
for k, v in replacements.items():
    text = text.replace(k, v)
Path(dst).write_text(text, encoding="utf-8")
PY
  chmod 644 "$UNIT_DIR/$unit"
done

# --- Optional add-ons --------------------------------------------------------
ENV_FILE="$CONFIG_DIR/pulsar.env"

if [ "$WITH_PI_TUNED" -eq 1 ]; then
  echo "== installing optional add-on: pi-tuned =="
  PI_TARGET="$DATA_DIR/pi-tuned"
  "$REPO_DIR/addons/pi-tuned/install.sh" "$PI_TARGET"
  PI_BIN="$PI_TARGET/node_modules/.bin/pi"
  (umask 077 && touch "$ENV_FILE")
  if ! grep -q '^PULSAR_PI_BIN=' "$ENV_FILE"; then
    "$VENV_DIR/bin/python" - "$ENV_FILE" "$PI_BIN" <<'PY'
import sys
env_file, pi_bin = sys.argv[1], sys.argv[2]
escaped = pi_bin.replace("\\", "\\\\").replace('"', '\\"')
with open(env_file, "a", encoding="utf-8") as fh:
    fh.write(f'PULSAR_PI_BIN="{escaped}"\n')
PY
  fi
fi
[ -f "$ENV_FILE" ] && chmod 600 "$ENV_FILE"

# --- Enable and start user services when safe --------------------------------
REAL_HOME="$("$PYTHON_BIN" -c 'import os, pwd; print(pwd.getpwuid(os.getuid()).pw_dir)')"
CAN_SYSTEMCTL=1
SKIP_REASON=""

if [ "$NO_START" -eq 1 ]; then
  CAN_SYSTEMCTL=0
  SKIP_REASON="--no-start was passed"
elif [ "$(cd "$HOME" && pwd)" != "$(cd "$REAL_HOME" && pwd)" ]; then
  CAN_SYSTEMCTL=0
  SKIP_REASON="HOME ($HOME) differs from login home ($REAL_HOME); refusing to touch live user systemd"
elif ! systemctl --user show-environment >/dev/null 2>&1; then
  CAN_SYSTEMCTL=0
  SKIP_REASON="no active systemd --user session bus detected"
fi

wait_http_200() {
  local url="$1" timeout_s="${2:-15}"
  "$VENV_DIR/bin/python" - "$url" "$timeout_s" <<'PY'
import sys, time, urllib.request
url, timeout = sys.argv[1], float(sys.argv[2])
deadline = time.monotonic() + timeout
last_err = ""
while time.monotonic() < deadline:
    try:
        with urllib.request.urlopen(url, timeout=2) as r:
            if 200 <= r.status < 400:
                sys.exit(0)
            last_err = f"HTTP {r.status}"
    except Exception as exc:
        last_err = str(exc)
    time.sleep(0.3)
print(f"warning: {url} did not answer within {timeout:.0f}s ({last_err})", file=sys.stderr)
sys.exit(1)
PY
}

SERVICES_STARTED=0
if [ "$CAN_SYSTEMCTL" -eq 1 ]; then
  echo "== enabling and starting pulsar-daemon.service and pulsar-web.service =="
  systemctl --user daemon-reload
  systemctl --user enable pulsar-daemon.service pulsar-web.service
  systemctl --user restart pulsar-daemon.service pulsar-web.service
  if wait_http_200 "http://127.0.0.1:${DAEMON_PORT}/healthz" 15 && \
     wait_http_200 "http://127.0.0.1:${WEB_PORT}/" 15; then
    SERVICES_STARTED=1
  fi
else
  echo "== skipping systemctl --user ($SKIP_REASON) =="
fi

# --- Summary -----------------------------------------------------------------
echo ""
echo "Pulsar installed."
echo ""
echo "  Web UI     : http://127.0.0.1:${WEB_PORT}/"
echo "  Daemon API : http://127.0.0.1:${DAEMON_PORT}/ (localhost only)"
echo "  Data dir   : $DATA_DIR"
echo "  Env file   : $ENV_FILE (optional KEY=VALUE overrides for systemd units)"
echo "  Units      : $UNIT_DIR/pulsar.slice"
echo "               $UNIT_DIR/pulsar-daemon.service"
echo "               $UNIT_DIR/pulsar-web.service"
echo "  CLI tools  : $BIN_DIR/pulsar-*"

if [ "$SERVICES_STARTED" -eq 1 ]; then
  echo ""
  echo "Services are running and passed health checks."
  if command -v loginctl >/dev/null 2>&1; then
    echo "On a headless machine, keep user services alive after logout with:"
    echo "  loginctl enable-linger \"\$USER\""
  fi
else
  echo ""
  echo "User services were written to $UNIT_DIR but not started."
  echo "To enable and start them in a normal login session:"
  echo "  systemctl --user daemon-reload"
  echo "  systemctl --user enable --now pulsar-daemon.service pulsar-web.service"
fi

if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
  echo ""
  echo "Note: $BIN_DIR is not on your current PATH. Add it to your shell profile:"
  echo "  export PATH=\"$BIN_DIR:\$PATH\""
fi

echo ""
echo "Detected agent CLIs:"
for cli in claude codex pi agy grok; do
  if p="$(command -v "$cli" 2>/dev/null)"; then
    echo "  [found]   $cli -> $p"
  elif [ "$cli" = "agy" ] && [ -x "$HOME/.local/bin/agy" ]; then
    echo "  [found]   agy -> $HOME/.local/bin/agy"
  elif [ "$cli" = "grok" ] && [ -x "$HOME/.local/bin/grok" ]; then
    echo "  [found]   grok -> $HOME/.local/bin/grok"
  elif [ "$cli" = "pi" ] && [ -x "$DATA_DIR/pi-tuned/node_modules/.bin/pi" ]; then
    echo "  [found]   pi -> $DATA_DIR/pi-tuned/node_modules/.bin/pi (pi-tuned)"
  else
    echo "  [missing] $cli (install and sign in if you want to use it)"
  fi
done

echo ""
echo "Open Pulsar at: http://127.0.0.1:${WEB_PORT}/"
echo "For remote or phone access, see docs/setup/tailscale.md (never expose Pulsar to the public internet)."
