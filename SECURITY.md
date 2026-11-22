# Security

## Report a vulnerability

Do not open a public issue for a security problem.

Report it in private on GitHub: open the [Security tab](https://github.com/adapt-ai-systems/pulsar/security) of the repository and select **Report a vulnerability**. Give the version or commit, the steps to reproduce the problem, and what an attacker can do with it.

## What Pulsar protects

Pulsar is a single-user tool for one Linux account. See "Platform and security" in the [README](README.md).

These are security boundaries. A way around one of them is a vulnerability:

- **Loopback only.** The daemon and the web server listen on `127.0.0.1`. A default install must not be reachable from another machine.
- **Request validation.** The daemon and the web server check the `Host` header and block cross-site `Origin` and `Sec-Fetch-Site: cross-site` requests on state-changing routes and WebSockets. A web page in the user's browser must not be able to drive Pulsar.
- **File permissions.** The data directory, the configuration directory, and the database are readable by the owner only. Another local user must not be able to read them.

## What Pulsar does not protect

These are not security boundaries. A report about one of them is not a vulnerability:

- **No login.** Anyone who can reach the web UI or the daemon API can run agent sessions and shell commands as the Linux user. Do not expose Pulsar to the public internet or bind it to `0.0.0.0` on an untrusted network. For remote access, use Tailscale.
- **Pulses run with the user's rights.** A pulse is an agent session that can read and write what the user can. The per-pulse systemd service sets memory and task limits and gives process control. It is not a sandbox.
- **`pulsar-guard` is a safety net.** It stops accidental mass deletes. It is not a sandbox and it does not stop a command that is written to get around it.
- **The agent tools and the models.** Claude Code, Codex, pi, Antigravity, Grok, and the model servers are separate products. Report a problem in one of them to its maintainer.
