# AGENTS.md — rules for coding agents that work on Pulsar

You are an AI coding agent and someone asked you to change this repository. Follow this file. People read [CONTRIBUTING.md](CONTRIBUTING.md); the rules are the same.

To install and run Pulsar (not to change it), use [AGENT_INSTALL.md](AGENT_INSTALL.md) instead.

## Setup

```bash
./dev-setup.sh
```

It makes `./venv`, installs the pinned Python packages plus pytest, and installs the web packages from the lock file. It does not use `sudo` and it does not install or start a service. It needs Python `>= 3.12`, Node.js, and pnpm `>= 9` (see "Install prerequisites" in [README.md](README.md)).

Do not run `./install.sh` to test a change. It installs the Pulsar services for the current user.

## The one check

```bash
./ci.sh
```

Exit 0 = pass. It runs the Python tests, the web unit tests, and `svelte-check`. CI on a pull request runs the same two commands (`./dev-setup.sh`, then `./ci.sh`). Run it before you say that the work is done, and report the result as it is. If it fails, say so and show the output.

To run a part of it:

```bash
venv/bin/python -m pytest tests/test_cli_guard.py -q      # one Python test file
( cd web && node --test src/lib/theme.test.mjs )          # one web test file
( cd web && npx --no-install svelte-check --threshold error )
```

## Map

| Area | Where |
| --- | --- |
| Web UI (SvelteKit) | `web/src/lib`, `web/src/routes` |
| Daemon and HTTP API | `pulsar/daemon` (routes in `pulsar/daemon/routes`) |
| Per-pulse process | `pulsar/runner` |
| Persistence (SQLite) | `pulsar/store` |
| Agent-tool adapters ("vessels") | `pulsar/vessel/{claude,codex,antigravity,grok,pi}.py` |
| Model list | `pulsar/models.json`, `pulsar/models.presets.json` |
| Command-line tools | `pulsar/cli`, wrappers in `ops/bin` |
| Delete guard | `pulsar/cli/guard.py`, `ops/guard-bin` |
| Installer | `install.sh` |
| Optional add-ons | `addons/` |
| Setup guides | `docs/setup` |

## Tests

- Python tests are in `tests/` (pytest). Web unit tests are in `web/src/lib/*.test.mjs` (`node:test`).
- No test reaches an external host. Use the fakes in `tests/` (`fake_claude.py`, `fake_codex.py`, `fake_pi.py`). Do not add a test that needs the network, a signed-in agent tool, or a model server.
- No test may read or write the real Pulsar data directory or the user's home configuration. Use `tmp_path`.
- A test states the behavior that must hold. Do not change a test to make a failing change pass unless the old behavior was the bug, and say so in the pull request.

## Rules for a change

1. One fix or one feature per pull request. No unrelated cleanup, renames, or formatting in the same diff.
2. A bug claim needs a reproduction on the current `main`: the steps or a failing test, and the output.
3. A bug fix carries a test that fails before the fix and passes after it.
4. Keep the diff small. Extend the code that is there before you add a new module or a new dependency.
5. When you add a setting, an environment variable, a CLI flag, or an API field, update the README or the guide in `docs/setup` in the same pull request.
6. Do not put secrets, tokens, personal paths, or machine names in code, tests, or fixtures.

## Files that need a maintainer review

A change to one of these is reviewed by a maintainer (see [.github/CODEOWNERS](.github/CODEOWNERS)). Do not change them as a side effect of other work. If the task needs such a change, make it a separate pull request and give the reason.

- `.github/`, `ci.sh`, `dev-setup.sh`, `install.sh`
- `requirements.txt`, `requirements-dev.txt`, `pyproject.toml`, `web/package.json`, `web/pnpm-lock.yaml`
- `SECURITY.md`, `LICENSE`, `NOTICE`
- `pulsar/cli/guard.py`, `ops/guard-bin/`, `pulsar/daemon/security.py`

## What goes in this repository

Wanted:

- Bug fixes with a reproduction.
- New agent-tool adapters (a new file in `pulsar/vessel` plus tests with a fake of the tool).

Not in the core (keep them in your own repository):

- Skills.
- New add-ons.

Open an issue first and wait for an answer:

- Support for other platforms. Pulsar runs on Linux only today.
- Anything that opens Pulsar to the network. Pulsar has no login. It listens on localhost, and remote access is through Tailscale.

## Pull request

Fill in [.github/PULL_REQUEST_TEMPLATE.md](.github/PULL_REQUEST_TEMPLATE.md). Say which agent and model made the change. Contributions are licensed under the GNU Affero General Public License v3.0 (see [CONTRIBUTING.md](CONTRIBUTING.md)).
