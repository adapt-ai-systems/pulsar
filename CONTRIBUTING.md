# Contributing to Pulsar

Thank you for helping. This page is for people. If an AI coding agent does the work for you, point it at [AGENTS.md](AGENTS.md); the rules are the same.

## Before you start

1. Search the open and closed [issues](https://github.com/adapt-ai-systems/pulsar/issues) and [pull requests](https://github.com/adapt-ai-systems/pulsar/pulls) for the same problem or idea.
2. For a question or an idea, use [GitHub Discussions](https://github.com/adapt-ai-systems/pulsar/discussions).
3. For a security problem, do not open an issue. See [SECURITY.md](SECURITY.md).
4. Want to help test automatically? See [docs/setup/bug-reporter.md](docs/setup/bug-reporter.md) to run an opt-in Pulsar pulse that checks local logs every 24 hours and files deduplicated, redacted bug reports.

## What we take

Wanted:

- Bug fixes with a reproduction.
- New agent-tool adapters (a new file in `pulsar/vessel` plus tests with a fake of the tool).

Not in the core (keep them in your own repository):

- Skills.
- New add-ons.

Open an issue first and wait for an answer:

- Support for other platforms. Pulsar runs on Linux only today.
- Anything that opens Pulsar to the network. Pulsar has no login. It listens on localhost, and remote access is through Tailscale.

## Set up and run the checks

You need Python `>= 3.12`, Node.js, and pnpm `>= 9` (see "Install prerequisites" in the [README](README.md)).

```bash
git clone https://github.com/<your-account>/pulsar.git
cd pulsar
./dev-setup.sh   # makes ./venv and installs the test tools; no sudo, no services
./ci.sh          # the one check; exit 0 = pass
```

CI runs the same two commands on every pull request.

## Pull request rules

1. One fix or one feature per pull request. No unrelated cleanup in the same diff.
2. A bug report or a bug fix shows how to reproduce the bug on the current `main`.
3. A bug fix carries a test that fails before the fix and passes after it.
4. `./ci.sh` passes.
5. When you add a setting, an environment variable, a CLI flag, or an API field, update the README or the guide in `docs/setup`.
6. Fill in the pull request template. If an AI agent wrote the change, say which agent and model.

Some files need a review by a maintainer: CI, the installer, the dependency files, and the security code. The list is in [.github/CODEOWNERS](.github/CODEOWNERS). Put a change to one of them in its own pull request.

## License of contributions

Pulsar is licensed under the [GNU Affero General Public License v3.0](LICENSE) (AGPL-3.0). When you open a pull request, you agree that your contribution is licensed under the same license. There is no separate contributor agreement to sign.
