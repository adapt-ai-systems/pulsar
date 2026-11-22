# Adding Models (Cloud and Local)

Pulsar keeps its model picker and daemon configuration in one file: `pulsar/models.json`. Both `pulsar/models.py` (daemon, handoff thresholds, vessel routing) and `web/src/lib/models.js` (web UI pickers) read this same file.

After any change to `pulsar/models.json`, rebuild the web app and restart the daemon:

```bash
cd web && ./scripts/build-live.sh   # or: pnpm build
systemctl --user restart pulsar-daemon.service pulsar-web.service
```

(`pulsar-web.service` also runs `web/scripts/build-live.sh` as `ExecStartPre`, so restarting both services updates the live web copy and reloads the daemon.)

## Starter registry

Pulsar ships with **10 cloud models** in `pulsar/models.json` and an empty `pulsar/vessel/pi_providers.json` (`{"providers": {}}`):

- **Claude Code (`claude-code`)**: `claude-opus-5-5`, `claude-sonnet-5-5`, `claude-fable-5-2`
- **Codex (`codex`)**: `gpt-6.2-astra`, `gpt-6-sol`, `gpt-6-luna`
- **Antigravity (`antigravity`)**: `agy-gemini-4-neon`, `agy-gemini-4-carbon`, `agy-gemini-4-argon`
- **Grok (`grok`)**: `grok-4.7`

## Vessels and binary lookup

Every model entry declares a `vessel` — the agent CLI that runs turns for that model:

| `vessel` | CLI | Binary lookup | Auth |
|---|---|---|---|
| `claude-code` | `claude` | `PULSAR_CLAUDE_BIN` or `claude` on `PATH` | Interactive `claude` login |
| `codex` | `codex` | `PULSAR_CODEX_BIN` or `codex` on `PATH` | Interactive `codex` login |
| `pi` | `pi` | `PULSAR_PI_BIN` (daemon defaults pulse units to `~/.local/share/pulsar/pi-tuned/node_modules/.bin/pi`), else `pi` on `PATH` | Provider config in `~/.pi/agent/models.json` |
| `antigravity` | `agy` | `PULSAR_AGY_BIN` or `~/.local/bin/agy` | Interactive `agy` login (`~/.gemini/antigravity-cli`) |
| `grok` | `grok` | `PULSAR_GROK_BIN`, `grok` on `PATH`, or `~/.local/bin/grok` | `XAI_API_KEY` or cached token |

Pulsar does not ship credentials for any CLI. Install and sign in to whichever CLIs you plan to use.

## Adding a local model with `pulsar-models`

Pulsar ships **8 tuned local-model presets** in `pulsar/models.presets.json`:

- `pi-glm53-full` — GLM-5.3 Full on Pi (RPC, GLM thinking profile, text-only with `"prompt_notes": ["vision"]`, `max_concurrent: 2`)
- `pi-glm-flash` — GLM-5.3 Flash on Pi (RPC, GLM thinking profile, multimodal, 1M context)
- `pi-qwen38-flash` — Qwen 3.8 Flash on Pi (RPC, Qwen 3.8 thinking profile, 256k context)
- `pi-qwen27` — Qwen 3.8 27B on Pi (RPC, Qwen 3.8 thinking profile, multimodal, 192k context)
- `pi-ds41-flash` — DeepSeek V4.1 Flash on Pi (RPC, numeric `80/70/50/30/none` effort dial, 1M context)
- `codex-ds4-flash` — DeepSeek V4 Flash via Codex (`pulsar/vessel/ds4-models.json` catalog)
- `local-openai` — Generic OpenAI-compatible local model on Pi (RPC, passthrough thinking)
- `local-slot` — Dynamic local slot on Pi (probes `GET <base_url>/models` every 30 s)

If you plan to use `pi` models with Pulsar's local-model patches (bash descriptions, reasoning-effort forwarding, unclamped `maxTokens`, and automatic vLLM context-fit retries), install `addons/pi-tuned` first (requires Node.js `>= 20.6`, Node 22 LTS recommended):

```bash
./addons/pi-tuned/install.sh
```

(Pulsar defaults `PULSAR_PI_BIN` for spawned `pi` pulses to `~/.local/share/pulsar/pi-tuned/node_modules/.bin/pi`, so installing `pi-tuned` to its default directory works with no extra config.)

### List presets and install one

```bash
pulsar-models presets
pulsar-models add local-openai --url http://127.0.0.1:8000/v1 --served-id my-model --label "My Local Model"
```

Useful `pulsar-models add` flags:

- `--url URL` (required): OpenAI-compatible `/v1` base URL
- `--id ID`: override the Pulsar picker model ID
- `--served-id MODEL`: override the model ID sent to the server
- `--label TEXT`: override the display name in the picker
- `--provider NAME`: override the `pi` provider name
- `--force`: replace an existing entry with the same ID

`pulsar-models add` edits the tracked `pulsar/models.json` file (and merges the provider block into `~/.pi/agent/models.json` for `pi` presets). After running `add`, rebuild the web app and restart the daemon:

```bash
cd web && ./scripts/build-live.sh
systemctl --user restart pulsar-daemon.service pulsar-web.service
```

If your local server needs an API key, set `"apiKey"` for that provider in `~/.pi/agent/models.json` (`pulsar-models add` writes `"none"`). Keep that file private (`chmod 600`).

Check live endpoint status at any time with:

```bash
pulsar-models
pulsar-models --check   # exits 1 if any visible local model is DOWN or SWAPPED
```

## Adding a cloud model or custom entry manually

### Model entry fields (`pulsar/models.json`)

| Field | Required | Meaning |
|---|---|---|
| `id` | yes | Unique picker ID used in the UI and `POST /pulses` |
| `label` | yes | Display name in the picker |
| `vessel` | yes | One of `claude-code`, `codex`, `pi`, `antigravity`, `grok` |
| `local` | yes | `true` for self-hosted/local models, `false` for cloud providers |
| `default_handoff` | yes | Token threshold that triggers automatic context handoff |
| `efforts` | no | Per-model override of the vessel's reasoning-effort dial |
| `default_effort` | no | Default effort level selected when creating a pulse |
| `effort_aliases` | no | Map of retired effort names to current effort values |
| `wire_model` | `pi` / local codex | `<provider>/<model-id>` for `pi`, or served model slug for local `codex` |
| `pi_rpc` | no | Set `true` to run a `pi` model over persistent RPC mode |
| `thinking` | no | `pi` thinking profile (`glm`, `glm52`, `qwen38`, `ds41`, `passthrough`, `required-low`) |
| `slot` | no | `true` for a dynamic local slot probed via `GET <base_url>/models` |
| `base_url` / `base_urls` | `slot` / local codex | OpenAI-compatible `/v1` base URL(s) polled every 30 s by `SlotProbe` |
| `max_concurrent` | no | Max simultaneously running pulses on this model (`POST /pulses` returns 409 past it) |
| `prompt_notes` | no | Standing context notes (`ds4_clarity`, `vision`, `qwen`) |
| `hidden` | no | `true` hides the entry from pickers while keeping old pulses resolving |

### Cloud CLI example

Add an entry to `models` in `pulsar/models.json`:

```json
{
  "id": "claude-sonnet-example",
  "label": "Sonnet Example",
  "vessel": "claude-code",
  "local": false,
  "default_handoff": 200000
}
```

### Cloud OpenAI-compatible provider via `pi` (e.g. OpenRouter)

1. Define the provider and model in `~/.pi/agent/models.json` with your `baseUrl` and API key.
2. Add an entry in `pulsar/models.json` pointing `wire_model` at `<provider>/<model-id>`:

```json
{
  "id": "pi-openrouter-example",
  "label": "Example Model (OpenRouter)",
  "vessel": "pi",
  "local": false,
  "default_handoff": 200000,
  "wire_model": "openrouter/vendor/model-name",
  "pi_rpc": true,
  "thinking": "passthrough"
}
```

## Optional local-helper and override environment variables

Add any of these to `~/.config/pulsar/pulsar.env` and restart `pulsar-daemon.service`:

- **Vision sidecar** (`pulsar/vessel/vision_sidecar.py`): off when `PULSAR_VISION_BASE_URL` is unset. Text-only local models with `"prompt_notes": ["vision"]` (such as the `pi-glm53-full` preset) need a multimodal endpoint configured to read image attachments:
  - `PULSAR_VISION_BASE_URL` — OpenAI-compatible VL `/v1` base URL (unset = off)
  - `PULSAR_VISION_MODEL` — VL model name on that endpoint
  - `PULSAR_VISION_TIMEOUT` — seconds per request (default `300`)
  - `PULSAR_VISION_EVIDENCE_MAX` — max evidence chars per image (default `8000`)
- **Codex command summarizer** (`pulsar/vessel/codex.py`): off when `PULSAR_CODEX_DESC_URL` is unset. Optionally asks a local OpenAI-compatible model for a one-line display description of Codex shell commands:
  - `PULSAR_CODEX_DESC_URL` — OpenAI-compatible `/v1` base URL (unset = off)
  - `PULSAR_CODEX_DESC_MODEL` — model name (default `qwen3.8-27b`)
  - `PULSAR_CODEX_DESC_TIMEOUT` — request timeout in seconds (default `3.0`)
  - `PULSAR_CODEX_DESC_DISABLED` — set `1`/`true` to force off
- **Local Codex DS4 route override**:
  - `PULSAR_DS4_BASE_URL` — overrides the base URL for `codex-ds4-flash` and other local Codex routes
- **Registry and provider file path overrides**:
  - `PULSAR_MODELS_PATH` — alternate path to `pulsar/models.json`
  - `PULSAR_PI_PROVIDERS_PATH` — alternate path to `pulsar/vessel/pi_providers.json`
  - `PULSAR_PRESETS_PATH` — alternate path to `pulsar/models.presets.json` (`pulsar-models`)
  - `PULSAR_PI_MODELS_PATH` — alternate path to `~/.pi/agent/models.json` (`pulsar-models`; falls back to `$PI_CODING_AGENT_DIR/models.json`)
