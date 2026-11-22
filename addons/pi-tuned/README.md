# Pulsar Tuned `pi` Bundle

This bundle installs a pinned copy of `@mariozechner/pi-coding-agent` (`0.73.1`) with four idempotent patches applied via `npm` `postinstall`. It replicates the tuned `pi` CLI setup used with Pulsar's `pi` vessel (`pulsar/vessel/pi.py`, `pulsar/vessel/pi_ext/*.ts`, `pulsar/vessel/pi_providers.json`).

## What Each Patch Does

All four patches are marker-checked and idempotent. Each supports `apply` (default) and `check` (exits `0` if applied, `1` if missing, `3` if upstream anchors changed).

1. **`patches/bash-description.py`** -> `node_modules/@mariozechner/pi-coding-agent/dist/core/tools/bash.js`
   - Adds an optional `description` string argument to `bashSchema` ("One line on what this command is for and why you are running it").
   - Updates `formatBashCall` so the TUI renders `description` on a muted line above `$ <command>`.
   - Updates the bash tool prompt text to instruct the model to always supply a short description, so Pulsar shows tool intent rather than only raw command text.

2. **`patches/glm-thinking-effort.py`** -> `node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js`
   - Extends the `qwen-chat-template` reasoning branch so that, when `options?.reasoningEffort` is set, `chat_template_kwargs.reasoning_effort` is populated alongside `enable_thinking` and `preserve_thinking` (using `model.thinkingLevelMap?.[options.reasoningEffort] ?? options.reasoningEffort`).
   - Allows local vLLM / OpenAI-compatible templates (such as GLM models) to honor Pulsar's `--thinking` effort level instead of staying at the template default.

3. **`patches/max-tokens-clamp.py`** -> `node_modules/@mariozechner/pi-ai/dist/providers/simple-options.js`
   - Removes `pi-ai`'s built-in `Math.min(model.maxTokens, 32000)` clamp in `buildBaseOptions()` when `options?.maxTokens` is not explicitly provided.
   - Honors `model.maxTokens` as configured in `models.json`, preventing long reasoning turns from stopping on `length` at 32,000 tokens.

4. **`patches/context-fit-max-tokens.py`** -> `node_modules/@mariozechner/pi-ai/dist/providers/openai-completions.js`
   - Wraps `client.chat.completions.create(params, requestOptions).withResponse()` inside `streamOpenAICompletions` with `pulsarCreateWithContextFit`.
   - Sends the configured output token budget (`max_tokens` or `max_completion_tokens`) unchanged without a character-count pre-clamp heuristic.
   - Mints a fresh `x-client-request-id` header in `requestOptions.headers` per attempt and retries with a new id if the server rejects a replayed request id (`has already been executed`).
   - On a vLLM context-length 400 (`maximum context length is N tokens ... requested M output tokens ... at least P input tokens`), computes `fit = contextWindow - input - 64` (inclusive floor `256`). If the server reports a truncated input count such that `fit >= current budget`, halves the budget on each retry (up to `6` retries) until it fits or falls below `256`.
   - Unit tests live in `patches/test_context_fit.py`.

## Installation

Prerequisites: Node.js (`>=20.6.0`), `npm`, and Python 3.

Install into the default directory (`$HOME/.local/share/pulsar/pi-tuned`):

```bash
./addons/pi-tuned/install.sh
```

Or install into a custom directory:

```bash
./addons/pi-tuned/install.sh /path/to/target-dir
```

`install.sh` copies `package.json`, `package-lock.json`, and `patches/` into the target directory, runs `npm ci`, verifies every patch is applied (exiting non-zero if any patch failed), and prints the path to the installed `pi` binary.

To point Pulsar at the installed binary explicitly, set `PULSAR_PI_BIN`:

```bash
export PULSAR_PI_BIN="$HOME/.local/share/pulsar/pi-tuned/node_modules/.bin/pi"
```

## Verification and Tests

Check the installed binary and patches:

```bash
"$HOME/.local/share/pulsar/pi-tuned/node_modules/.bin/pi" --version
cd "$HOME/.local/share/pulsar/pi-tuned"
npm run check-patches
python3 -m unittest discover -s patches -p 'test_*.py' -v
```

## Uninstall

Remove the installed `pi-tuned` directory (use your custom target path or `$PULSAR_DATA/pi-tuned` if you changed it):

```bash
rm -rf "${PULSAR_DATA:-${XDG_DATA_HOME:-$HOME/.local/share}/pulsar}/pi-tuned"
```

If you installed `pi-tuned` through `./install.sh --with-pi-tuned`, remove `PULSAR_PI_BIN=` from `~/.config/pulsar/pulsar.env` and restart the daemon:

```bash
ENV_FILE="${XDG_CONFIG_HOME:-$HOME/.config}/pulsar/pulsar.env"
[ -f "$ENV_FILE" ] && sed -i '/^PULSAR_PI_BIN=/d' "$ENV_FILE"
systemctl --user restart pulsar-daemon.service
```

This leaves `~/.pi/agent/models.json` untouched because it can hold API keys for your providers. Inspect or remove it separately if you no longer use `pi`.

## Third-Party License Notice

`@mariozechner/pi-coding-agent` (and its companion packages `@mariozechner/pi-ai`, `@mariozechner/pi-agent-core`, and `@mariozechner/pi-tui`) are developed by **Mario Zechner** and distributed under the **MIT License**.

- Author: Mario Zechner
- Upstream repository: https://github.com/badlogic/pi-mono (`packages/coding-agent`, pinned to `0.73.1`)
- License: MIT
