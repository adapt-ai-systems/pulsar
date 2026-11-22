# Pulsar

Pulsar is a single-user Linux daemon and web interface for running multiple coding-agent CLI sessions ("pulses") in parallel.

Each pulse runs inside its own fenced transient `systemd --user` service (`pulsar-pulse-<id>.service` under `pulsar.slice`) with a per-pulse home directory, append-only event log, and SQLite state store. Restarting the daemon reconciles running pulses without killing them.

## Recommended setup: host on your Tailscale tailnet & install as an app

The recommended way to run Pulsar is on **one Linux machine** (workstation, home/office server, GPU box, or Linux VM) exposed privately on your **Tailscale tailnet** with `tailscale serve`.

- **Reachable from any machine on Tailscale:** open `https://<your-machine>.<your-tailnet>:5292/` from Mac, Windows, Linux, iPhone, or Android with automatic HTTPS and zero open public ports.
- **Install as a standalone app (PWA):**
  - **Mac & Windows (Chrome / Edge / Brave):** open your tailnet URL and click the **Install** icon in the address bar (or `⋮ → Cast, save, and share → Install page as app`) to pin it to your Dock or Taskbar.
  - **iPhone & iPad (Safari):** open your tailnet URL and tap **Share → Add to Home Screen**.
  - **Android (Chrome):** open your tailnet URL and tap `⋮ → Install app` (or **Add to Home screen**).

Full step-by-step instructions: [docs/setup/tailscale.md](docs/setup/tailscale.md).

## Install with an AI agent

The easiest way to install Pulsar is to let an AI agent do it with you. Open Claude Code, Codex, or another coding agent on the Linux machine and give it this message:

```text
Read https://raw.githubusercontent.com/adapt-ai-systems/pulsar/main/AGENT_INSTALL.md and follow it to install Pulsar on this machine and host it privately on my Tailscale tailnet so I can install it as an app on Mac, Windows, iPhone, and Android. Take me through it step by step.
```

The agent follows [AGENT_INSTALL.md](AGENT_INSTALL.md): it checks the machine, asks before it uses `sudo`, installs Pulsar, helps you set up one agent tool, proves that a first pulse answers, and walks you through Tailscale Serve hosting so every device on your tailnet can install it as an app. It never asks for a password or an API key in the chat.

No agent on that machine? Paste the contents of [AGENT_INSTALL.md](AGENT_INSTALL.md) into a chat assistant (for example the Claude app). It gives you one command at a time and reads the output that you paste back.

To install by hand, see [Requirements](#requirements) and [Install](#install).

## What Pulsar does

- **Multi-pane web UI** (`pulsar-web.service`, SvelteKit on `127.0.0.1:5292`) with named workspace grids, pane reordering, a global session drawer, and a mobile swipe deck.
- **Five agent CLI adapters ("vessels")**: Claude Code, Codex, pi, Antigravity, and Grok. Use whichever tools you install and sign in to.
- **Pulse lifecycle**: create, pause, restore, reset, fork, switch model/effort, archive, and delete.
- **Context handoff and backup failover**: automatically summarizes and respawns a pulse when context usage reaches its per-model token threshold (or on demand via `POST /pulses/{id}/handoff`), and can move a failing pulse to a configured backup model.
- **Pulse-to-pulse messaging and helper delegation**: pulses can inspect the roster (`pulsar-roster`), message each other (`pulsar-msg`), and spawn helper pulses (`POST /pulses` with `role: "helper"` and `from_pulse_id`) tracked by automatic delegation watches.
- **Daemon-checked watches and schedules**: condition watches (`pulsar-wait`: file, log, HTTP, command, pulse idle/stop, and one-shot timers) and interval/one-time/daily schedules wake stopped pulses when due.
- **Detached background jobs**: `pulsar-job` runs long commands in their own `pulsar-jobs.slice` systemd unit so they survive a pulse handoff or reset and wake the pulse on exit.
- **Clean-memory isolation and skills**: pulses start clean by default and opt into skills from `~/.claude/skills`.
- **Delete guard**: `pulsar-guard` blocks accidental mass deletes (`rm`, `rmdir`, `find -delete`, `shutil.rmtree`) targeting protected roots, `..` paths, or unexpanded shell variables.
- **Async Inbox and questions**: pulses can post finished reports to the Inbox (`POST /inbox/items`) or ask structured questions (`pulsar-ask`).
- **Optional surfaces (off until configured)**: a static Pages Hub, a live hardware/LLM Metrics Bar, and optional add-ons in `addons/`.

## Platform and security

- **Linux only.** Pulsar relies on `systemd --user` and `systemd-run` for service management and per-pulse cgroup fencing. **On macOS or Windows, run Pulsar inside a Linux virtual machine.**
- **No login — localhost only.** Pulsar has no built-in authentication. Both `pulsar-daemon.service` (`127.0.0.1:8726`) and `pulsar-web.service` (`127.0.0.1:5292`) listen on loopback (`127.0.0.1`) only. On a single Linux machine this works out of the box.
- **Request validation.** Both the daemon and the web server check the `Host` header, block cross-site `Origin` and `Sec-Fetch-Site: cross-site` headers on state-changing requests and WebSockets, and allow extra names listed in `PULSAR_WEB_ALLOWED_HOSTS`. CLI tools that send no `Origin` header keep working on loopback.
- **Remote and phone access:** use [Tailscale](docs/setup/tailscale.md) (`tailscale serve` + `PULSAR_WEB_ALLOWED_HOSTS`).
- **Warning:** **never expose Pulsar to the public internet** or bind it to `0.0.0.0` on an untrusted network. Anyone who can reach the web UI or daemon API can run agent sessions and shell commands as your Linux user.

## Requirements

Versions match `pyproject.toml`, `web/pnpm-lock.yaml`, and `addons/pi-tuned/README.md`:

- **OS:** Linux with `systemd --user` (`systemctl`, `systemd-run`)
- **Tools:** `git`, `curl`, and `npm`
- **Python:** `>= 3.12` (with `venv` and `pip`, or `uv`)
- **Node.js:** `>= 20.6` (Node 22 LTS recommended; `>= 20.6` is required when `addons/pi-tuned` is used, while `>= 18.13` works without it)
- **pnpm:** `>= 9` (`web/pnpm-lock.yaml` uses `lockfileVersion: 9.0`)
- **Agent CLI:** at least one of Claude Code, Codex, pi, Antigravity, or Grok installed and signed in

### Install prerequisites

Run the block for your system on a fresh machine. Each block installs `git`, `curl`, Python `>= 3.12`, Node.js 22 LTS (`>= 20.6`), `npm`, and `pnpm`.

**Ubuntu 24.04**

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 python3-venv python3-pip
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pnpm
```

**Ubuntu 22.04** (ships Python 3.10 and Node.js 12, so this installs Python 3.12 with `uv` and Node.js 22 LTS from NodeSource)

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install --default 3.12
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pnpm
```

If Ubuntu's older `nodejs` package is already installed and `apt-get` reports a `libnode-dev` overwrite error, remove the old packages first, then run `sudo apt-get install -y nodejs` again:

```bash
sudo apt-get remove -y nodejs npm libnode-dev libnode72 && sudo apt-get autoremove -y
```

**Debian 12** (ships Python 3.11 and Node.js 18, so this installs Python 3.12 with `uv` and Node.js 22 LTS from NodeSource)

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates
curl -LsSf https://astral.sh/uv/install.sh | sh
export PATH="$HOME/.local/bin:$PATH"
uv python install --default 3.12
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pnpm
```

**Fedora**

```bash
sudo dnf install -y git curl python3 python3-pip nodejs npm
sudo npm install -g pnpm
```

## Install

Clone the repository and run `./install.sh`:

```bash
git clone https://github.com/adapt-ai-systems/pulsar.git pulsar
cd pulsar
./install.sh
```

`install.sh` is non-interactive and safe to run more than once. It:

1. Checks Linux, `systemd` and the user session, Python `>= 3.12`, Node.js `>= 18.13` (`>= 20.6.0` with `--with-pi-tuned`), `pnpm >= 9`, and required tools in one pass.
2. Creates a Python virtualenv at `./venv` and installs the pinned Python dependencies from `requirements.txt` plus the `pulsar` package (`pip` or `uv`).
3. Installs web dependencies (`pnpm install --frozen-lockfile`) and builds `web/live` (`web/scripts/build-live.sh`).
4. Symlinks the 8 CLI helpers from `ops/bin/*` (`pulsar-ask`, `pulsar-guard`, `pulsar-job`, `pulsar-models`, `pulsar-msg`, `pulsar-roster`, `pulsar-search`, `pulsar-wait`) into `~/.local/bin`.
5. Renders `ops/systemd/*.in` templates into `~/.config/systemd/user/` (`pulsar.slice`, `pulsar-daemon.service`, `pulsar-web.service`) with no hard-coded paths.
6. Enables and starts `pulsar-daemon.service` and `pulsar-web.service`, verifies health checks, and prints the UI URL.

### Installer options

```bash
./install.sh --with-pi-tuned          # also install addons/pi-tuned and set PULSAR_PI_BIN
./install.sh --daemon-port 8726       # override daemon port (env: PULSAR_DAEMON_PORT)
./install.sh --web-port 5292          # override web UI port (env: PULSAR_WEB_PORT)
./install.sh --data-dir DIR           # override data dir (default: ~/.local/share/pulsar, env: PULSAR_DATA)
./install.sh --no-start               # write unit files without calling systemctl --user
```

On a headless Linux machine or VM, enable systemd user lingering so Pulsar stays up after you disconnect:

```bash
loginctl enable-linger "$USER"
```

### Recommended: host on your Tailscale tailnet

To make Pulsar reachable over HTTPS from every machine on your tailnet and installable as a Chrome / home-screen app on Mac, Windows, iPhone, and Android:

```bash
# Replace <your-machine>.<your-tailnet> with your MagicDNS name from `tailscale status`
echo "PULSAR_WEB_ALLOWED_HOSTS=<your-machine>.<your-tailnet>" >> "$HOME/.config/pulsar/pulsar.env"
systemctl --user restart pulsar-daemon.service pulsar-web.service
tailscale serve --bg --https=5292 http://127.0.0.1:5292
loginctl enable-linger "$USER"
```

See [docs/setup/tailscale.md](docs/setup/tailscale.md) for MagicDNS/HTTPS setup and Mac, Windows, iPhone, and Android app install steps.

If `~/.local/bin` is not yet on your `PATH`, add `export PATH="$HOME/.local/bin:$PATH"` to your shell profile.

## Agent tools and how Pulsar finds them

Pulsar ships with 10 starter cloud models in `pulsar/models.json` and does not ship credentials or API keys. Install and sign in to whichever of the five supported agent CLIs you want to use before creating your first pulse:

| Tool | Vessel ID | How Pulsar finds the binary | Authentication |
|---|---|---|---|
| **Claude Code** | `claude-code` | `PULSAR_CLAUDE_BIN` or `claude` on `PATH` | Run `claude` interactively to sign in |
| **Codex** | `codex` | `PULSAR_CODEX_BIN` or `codex` on `PATH` | Run `codex` interactively to sign in |
| **pi** | `pi` | `PULSAR_PI_BIN` (daemon defaults pulse units to `~/.local/share/pulsar/pi-tuned/node_modules/.bin/pi`), else `pi` on `PATH` | Configure providers in `~/.pi/agent/models.json` (or install `addons/pi-tuned`) |
| **Antigravity** | `antigravity` | `PULSAR_AGY_BIN` or `~/.local/bin/agy` | Run `agy` once interactively (`~/.gemini/antigravity-cli`) |
| **Grok** | `grok` | `PULSAR_GROK_BIN`, `grok` on `PATH`, or `~/.local/bin/grok` | Set `XAI_API_KEY` or sign in to cache a token |

`install.sh` puts detected tool directories on the `PATH` inside `pulsar-daemon.service`. If you install a CLI later in a non-standard directory (or want to pin a specific binary), add `KEY=VALUE` lines to `~/.config/pulsar/pulsar.env` and restart the daemon:

```bash
cat <<'EOF' >> ~/.config/pulsar/pulsar.env
PULSAR_PI_BIN=/path/to/pi
EOF
systemctl --user restart pulsar-daemon.service
```

To add local OpenAI-compatible models from the 8 built-in presets (`pulsar-models presets` and `pulsar-models add <preset> --url ...`) or add custom cloud models, see [docs/setup/models.md](docs/setup/models.md).

## First run

Follow these steps to verify a fresh install end to end:

### 1. Confirm the daemon and UI are up

```bash
curl -s http://127.0.0.1:8726/healthz
# -> {"ok":true,"value":{"status":"ok"}}
```

Open **http://127.0.0.1:5292/** in your browser. The Pulsar workspace grid should load.

### 2. Create a pulse and get an answer

1. Click the green **`+`** (**new pulse**) button in the left sidebar (or press `Ctrl+Enter`). The top-left **`+`** button creates a workspace.
2. Give it a title (for example, `Smoke test`) and select a model belonging to an agent tool you installed and signed in to.
3. Type a short message such as `Say hello and print your current working directory.` and send it.
4. Confirm the pulse streams a response and returns to idle.

### 3. Start a helper pulse

You can ask your pulse in chat to delegate a small task to a helper, or spawn one directly via the daemon API (replace `<lead-id>` with the first pulse's ID from `pulsar-roster` and `<model-id>` with an installed model):

```bash
pulsar-roster
curl -s -X POST http://127.0.0.1:8726/pulses \
  -H 'Content-Type: application/json' \
  -d '{
    "title": "Helper test",
    "model": "<model-id>",
    "role": "helper",
    "from_pulse_id": "<lead-id>",
    "first_message": "Reply to pulse <lead-id> using pulsar-msg with the word pong, then stop."
  }'
```

The helper appears in `pulsar-roster` and the UI, runs its brief, and messages the lead pulse back.

### 4. Fire a condition watch

Ask your pulse to run:

```bash
pulsar-wait file /tmp/pulsar-watch-test --note "watch test file appeared"
```

(Or register it from your shell with `PULSE_ID=<pulse-id> pulsar-wait file /tmp/pulsar-watch-test`.) Wait for the pulse's turn to end, then create the file from your shell:

```bash
touch /tmp/pulsar-watch-test
```

Within one scheduler tick, the daemon detects the file and wakes the pulse with a watch notification message. Clean up `/tmp/pulsar-watch-test` afterward.

### 5. Trigger a handoff

Trigger a manual handoff from the pulse's menu in the UI, or from your shell:

```bash
curl -s -X POST http://127.0.0.1:8726/pulses/<pulse-id>/handoff
```

Pulsar asks the pulse to summarize its current state, stops the old runner unit, and spawns a fresh session seeded with the handoff summary. Send a follow-up message to confirm the successor responds with continuity.

## Settings

All 6 per-user settings can be edited in the **web UI settings window** (click the sliders icon at the bottom of the left sidebar, then **settings…**) or via the daemon's `app_settings` HTTP API (`GET`, `PUT`, and `DELETE` at `http://127.0.0.1:8726/settings/<key>` with JSON bodies):

| Key | Type | Default (when unset) | Purpose |
|---|---|---|---|
| `owner.name` | string | `"the user"` | Name used in generated pulse context and UI labels |
| `owner.timezone` | string (IANA) | Machine time zone | Time zone for schedules and reported times (e.g. `"America/New_York"`, `"UTC"`) |
| `guard.enabled` | boolean | `true` | Enables or disables `pulsar-guard` delete protection |
| `guard.extra_paths` | string[] | `[]` | Extra protected root directories that pulses cannot delete or empty |
| `hub.url` | string | unset (off) | Base URL of your optional Pages Hub server ([docs/setup/pages-hub.md](docs/setup/pages-hub.md)) |
| `metrics.url` | string | unset (off) | Dashboard **base URL** for the optional Metrics Bar; the web app proxies `<metrics.url>/data.json` with shape `{hosts:[{id,name,latest:{online,ts,llm:{...}},history}]}` ([docs/setup/metrics-bar.md](docs/setup/metrics-bar.md)) |

API examples:

```bash
# Set your display name and time zone:
curl -s -X PUT http://127.0.0.1:8726/settings/owner.name \
  -H 'Content-Type: application/json' -d '"Alex"'

curl -s -X PUT http://127.0.0.1:8726/settings/owner.timezone \
  -H 'Content-Type: application/json' -d '"America/New_York"'

# Protect extra directories with pulsar-guard:
curl -s -X PUT http://127.0.0.1:8726/settings/guard.extra_paths \
  -H 'Content-Type: application/json' -d '["/srv/production", "/home/alex/important"]'

# Preview whether pulsar-guard would refuse a command:
pulsar-guard check -- 'rm -rf /etc'
```

## Environment variables

Optional `KEY=VALUE` overrides can be placed in `~/.config/pulsar/pulsar.env` (read by both `pulsar-daemon.service` and `pulsar-web.service`):

### Core, daemon, and web server

| Variable | Default | Purpose |
|---|---|---|
| `PULSAR_DATA` | `~/.local/share/pulsar` (via `install.sh`; `<repo>/data` when unset) | Runtime database (`pulsar.db`), `guard.json`, and per-pulse directories |
| `PULSAR_PORT` | `8726` | Daemon listen port on `127.0.0.1` |
| `PULSAR_DAEMON` | `http://127.0.0.1:8726` | Full daemon URL for CLI helpers, pulse units, and `web/server.js` (takes precedence over `PULSAR_DAEMON_PORT`) |
| `PULSAR_DAEMON_PORT` | `8726` | Daemon localhost port for `web/server.js` when `PULSAR_DAEMON` is unset |
| `PORT` | `5292` | Web UI listen port on `127.0.0.1` (`web/server.js`) |
| `PULSAR_WEB_ALLOWED_HOSTS` | unset (localhost only for Origin; localhost + bare IPs for Host) | Comma-separated extra hostnames allowed by `pulsar-daemon` and `web/server.js` (exact host, `.suffix`, or `*` to turn off Host and Origin checks) |
| `PULSAR_UI_URL` | `http://127.0.0.1:5292/` | Target URL for `web/probe/ui-probe.py` and `web/probe/inbox-probe.py` |
| `PULSAR_MAX_RUNNING_PULSES` | `32` | Maximum simultaneously running pulse units (`0` disables) |

### Optional integrations and local-model helpers

| Variable | Default | Purpose |
|---|---|---|
| `PULSAR_HUB_PAGES_DIR` | unset (disabled) | Directory of `<slug>/index.html` pages scanned by `GET /hub/pages` and shown in pulse context |
| `PULSAR_METRICS_DATA_PATH` | unset (disabled) | Local path to metrics `data.json` for `/live` WebSocket invalidation pushes |
| `PULSAR_AGY_CLIENT_ID` | unset (Gemini usage meter cannot refresh its token) | OAuth client ID for refreshing the Gemini (`agy`) usage meter token |
| `PULSAR_AGY_CLIENT_SECRET` | unset (Gemini usage meter cannot refresh its token) | OAuth client secret for refreshing the Gemini (`agy`) usage meter token |
| `PULSAR_VISION_BASE_URL` | unset (off) | OpenAI-compatible VL `/v1` endpoint for text-only models with `"prompt_notes": ["vision"]` (e.g. `pi-glm53-full`) |
| `PULSAR_VISION_MODEL` | unset | VL model ID served at `PULSAR_VISION_BASE_URL` |
| `PULSAR_CODEX_DESC_URL` | unset (off) | OpenAI-compatible `/v1` endpoint for optional Codex shell-command one-line display summaries |
| `PULSAR_DS4_BASE_URL` | unset | Base URL override for local Codex DS4 routes (e.g. `codex-ds4-flash`) |
| `PULSAR_MODELS_PATH` | `pulsar/models.json` | Override path to the model registry |
| `PULSAR_PI_PROVIDERS_PATH` | `pulsar/vessel/pi_providers.json` | Override path to repo-seeded `pi` providers |
| `PULSAR_PRESETS_PATH` | `pulsar/models.presets.json` | Override path to local-model presets (`pulsar-models`) |
| `PULSAR_PI_MODELS_PATH` | `~/.pi/agent/models.json` | Override path to user `pi` provider config (`pulsar-models`; falls back to `$PI_CODING_AGENT_DIR/models.json`) |

## Remote and phone access (Tailscale)

Pulsar intentionally has no login screen and binds only to `127.0.0.1`. To open Pulsar from another laptop or your phone, allow your Tailscale hostname in `PULSAR_WEB_ALLOWED_HOSTS` and expose `http://127.0.0.1:5292` strictly inside your private tailnet with `tailscale serve`:

```bash
echo "PULSAR_WEB_ALLOWED_HOSTS=<your-machine>.<your-tailnet>" >> "$HOME/.config/pulsar/pulsar.env"
systemctl --user restart pulsar-daemon.service pulsar-web.service
tailscale serve --bg --https=5292 http://127.0.0.1:5292
```

**Never expose Pulsar to the public internet.** See [docs/setup/tailscale.md](docs/setup/tailscale.md) for full instructions.

## Setup guides

- [docs/setup/models.md](docs/setup/models.md) — Starter cloud models, `pulsar-models presets` / `add`, local OpenAI-compatible servers, and vision/summarizer helpers
- [docs/setup/pages-hub.md](docs/setup/pages-hub.md) — Setting up the optional Pages Hub (off until `hub.url` is set)
- [docs/setup/metrics-bar.md](docs/setup/metrics-bar.md) — Setting up the optional Metrics Bar (off until `metrics.url` is set)
- [docs/setup/tailscale.md](docs/setup/tailscale.md) — Remote and phone access with Tailscale
- [docs/setup/bug-reporter.md](docs/setup/bug-reporter.md) — Optional automated 24-hour bug reporter pulse for testers

## Optional add-ons

Add-ons live in `addons/` and have their own installers. An add-on is not installed unless you ask for it:

- **`addons/pi-tuned`** (`./install.sh --with-pi-tuned` or `./addons/pi-tuned/install.sh`) — Installs a pinned copy of `@mariozechner/pi-coding-agent` (`0.73.1`) with four idempotent patches for Pulsar (bash tool descriptions, reasoning-effort forwarding for local templates, unclamped `maxTokens`, and automatic vLLM context-fit retries). See [addons/pi-tuned/README.md](addons/pi-tuned/README.md).

## Uninstall

Stop running pulses and jobs, stop and disable Pulsar's systemd user units, and remove the unit files, CLI symlinks, config directory, and build lock file:

```bash
systemctl --user stop 'pulsar-pulse-*.service' 'pulsar-job-*.service'   # running pulses and jobs
systemctl --user disable --now pulsar-daemon.service pulsar-web.service
systemctl --user stop pulsar.slice
rm -f "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/pulsar-daemon.service" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/pulsar-web.service" \
      "${XDG_CONFIG_HOME:-$HOME/.config}/systemd/user/pulsar.slice"
systemctl --user daemon-reload

rm -f "$HOME/.local/bin"/pulsar-{ask,guard,job,models,msg,roster,search,wait}
rm -rf "${XDG_CONFIG_HOME:-$HOME/.config}/pulsar"
rm -f "${XDG_RUNTIME_DIR:-${XDG_CACHE_HOME:-$HOME/.cache}/pulsar}/pulsar-web-build.lock" \
      "${XDG_CACHE_HOME:-$HOME/.cache}/pulsar/pulsar-web-build.lock"

# Optional: remove the cloned repository and runtime database/pulse histories:
# rm -rf "$HOME/pulsar"   # adjust if you cloned Pulsar elsewhere
# rm -rf "${PULSAR_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/pulsar}"
```

What stays behind after the commands above, and how to remove it if you want a full cleanup:

- **Cloned repo and runtime data:** `$HOME/pulsar` and `${PULSAR_DATA:-~/.local/share/pulsar}` (which also holds `pi-tuned` when installed with `--with-pi-tuned`). Uncomment the two `rm -rf` lines above to delete them.
- **Pi provider config (`~/.pi/agent/models.json`):** created by `pulsar-models add` and can hold API keys for local or custom providers. Inspect it first, then remove it if you no longer use `pi`: `rm -f "$HOME/.pi/agent/models.json"`.
- **User linger setting:** if you ran `loginctl enable-linger "$USER"` for Pulsar and no other user services need it, turn it off with `loginctl disable-linger "$USER"`.
- **Build lock file:** removed by the `rm -f` line above (`$XDG_RUNTIME_DIR/pulsar-web-build.lock`, or `${XDG_CACHE_HOME:-$HOME/.cache}/pulsar/pulsar-web-build.lock` when `XDG_RUNTIME_DIR` was unset). If the cache directory is now empty, remove it with `rmdir "${XDG_CACHE_HOME:-$HOME/.cache}/pulsar" 2>/dev/null || true`.
- **Optional add-ons:** see [addons/pi-tuned/README.md](addons/pi-tuned/README.md#uninstall) (and other READMEs under `addons/`).
- **Tailscale Serve:** if you exposed Pulsar on your tailnet, run `tailscale serve --https=5292 off`.
- **Package caches and tools:** `pnpm`, `pip`, and `uv` caches remain in your home directory until you remove them.

## Contributing

Bug fixes and new agent-tool adapters are welcome. Read [CONTRIBUTING.md](CONTRIBUTING.md) first. If an AI coding agent does the work, point it at [AGENTS.md](AGENTS.md). To prepare a clone and run the checks:

```bash
./dev-setup.sh   # makes ./venv and installs the test tools; no sudo, no services
./ci.sh          # the one check; exit 0 = pass
```

Questions and ideas go to [GitHub Discussions](https://github.com/adapt-ai-systems/pulsar/discussions). For a security problem, see [SECURITY.md](SECURITY.md).

## Credits

Credit: [Adapt AI Systems](https://adaptaisystems.com) and Adapt Digital Solutions.

## License

Pulsar is licensed under the GNU Affero General Public License v3.0 (AGPL-3.0). See [LICENSE](LICENSE) and [NOTICE](NOTICE).
