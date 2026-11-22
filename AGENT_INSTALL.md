# Install Pulsar with an AI agent

**This file is for an AI agent** (Claude Code, Codex, or another assistant). A person gave you this file because they want Pulsar on their Linux machine. That person is "the user". Do the steps in order and take the user through the setup.

If you are a person: you do not need to read this file. Give your agent the message from the README section "Install with an AI agent".

## Rules for the agent

1. Do one step at a time. Before a step, tell the user in one or two short sentences what you will do and why. After the step, tell the user the result.
2. Each step has a **Check**. Do not start the next step until the check passes.
3. Ask the user before each command that uses `sudo`. Show the exact command first.
4. Never ask the user to put a password, an API key, or a token in the chat. The user signs in to tools in their own terminal.
5. Pulsar has no login. Never bind it to `0.0.0.0` and never open it to the public internet.
6. Do not guess. If a step fails and this file and `README.md` do not have the fix, show the user the error and stop.
7. Ask one question at a time. Give the options. Wait for the answer.
8. Work as the user's normal Linux account, not as `root`.
9. Each command you run can start in a new shell. When a step tells you to change `PATH`, put the same `export` line at the start of your later commands.

## Two ways to work

- **Shell mode:** you can run commands on the user's machine. Run them yourself.
- **Coach mode:** you cannot run commands on the user's machine (a chat without a shell, or your shell is a sandbox). Give the user ONE command block at a time. Ask for the output. Read it. Then continue.

If you are not sure which mode applies, ask the user: "Is the shell I have on the machine where you want Pulsar?"

## Start — tell the user the plan

Tell the user this, in your own words, before you run a command:

1. Check the machine.
2. Install the programs that Pulsar needs (this can need `sudo`; you will ask first).
3. Download and install Pulsar.
4. Set up one agent tool (Claude Code, Codex, pi, Antigravity, or Grok).
5. Start a first pulse and prove that it answers.
6. Optional: keep Pulsar running after logout, set a name, set up phone access.

## Step 1 — Check the machine

```bash
uname -s
. /etc/os-release && echo "$PRETTY_NAME"
echo "user=$(id -un) uid=$(id -u)"
systemctl --user show-environment >/dev/null 2>&1 && echo "systemd user session: OK" || echo "systemd user session: MISSING"
```

- `uname -s` is not `Linux`: stop. Pulsar runs on Linux only. On macOS or Windows the user must use a Linux virtual machine.
- `uid=0`: stop. Ask the user to sign in with their normal account.
- `systemd user session: MISSING`: Pulsar runs as `systemd --user` services, so it needs a real login session for this user (SSH or a desktop login, not `su` or `sudo -u`). If the file `/run/user/$(id -u)/bus` exists, run `export XDG_RUNTIME_DIR=/run/user/$(id -u)` and do the check again (see rule 9). If it is still missing, stop and tell the user.

**Check:** Linux, a normal user, and `systemd user session: OK`.

## Step 2 — Install the programs Pulsar needs

Pulsar needs `git`, `curl`, Python `>= 3.12` (with `venv`, or `uv`), Node.js `>= 20.6`, `npm`, and `pnpm >= 9`.

See what is there:

```bash
export PATH="$HOME/.local/bin:$PATH"
for c in git curl python3 uv node npm pnpm; do printf '%-8s %s\n' "$c" "$(command -v "$c" || echo MISSING)"; done
for c in python3 node pnpm; do if command -v "$c" >/dev/null; then "$c" --version; fi; done
```

On a new machine, most of these lines say `MISSING`. That is normal.

If all are there with the right versions, go to step 3. If not, use the block for the user's system. Show the block to the user and ask for approval, because it uses `sudo`.

In shell mode, run `sudo -n true` first. If it fails, you cannot type the user's password. Ask the user to run the block in their own terminal and to tell you when it is done.

In shell mode, a package can stop and ask a question (for example, `tzdata` asks for a time zone on a minimal system). If you cannot answer a prompt in your shell, write `sudo DEBIAN_FRONTEND=noninteractive apt-get` in place of `sudo apt-get` in the block. If a command already waits at a prompt, ask the user for the answer.

**Ubuntu 24.04**

```bash
sudo apt-get update
sudo apt-get install -y git curl ca-certificates python3 python3-venv python3-pip
curl -fsSL https://deb.nodesource.com/setup_22.x | sudo -E bash -
sudo apt-get install -y nodejs
sudo npm install -g pnpm
```

**Ubuntu 22.04**

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

On Ubuntu 22.04, if `apt-get` reports a `libnode-dev` overwrite error, remove the old Node.js packages, then run `sudo apt-get install -y nodejs` again:

```bash
sudo apt-get remove -y nodejs npm libnode-dev libnode72 && sudo apt-get autoremove -y
```

**Debian 12**

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

**Another Linux system:** these four blocks are the tested ones. Tell the user that their system is not in the list. Offer the equivalent packages for their package manager, say that the commands are not tested, and ask for approval before you run them.

**Check:** run the "see what is there" block again. No `MISSING` for `git`, `curl`, `node`, `npm`, `pnpm`. Python is `3.12` or later (from `python3`, or installed by `uv`). Node.js is `20.6` or later. pnpm is `9` or later.

## Step 3 — Download and install Pulsar

```bash
export PATH="$HOME/.local/bin:$PATH"
git clone https://github.com/adapt-ai-systems/pulsar.git "$HOME/pulsar"
cd "$HOME/pulsar" && ./install.sh
```

- If `$HOME/pulsar` is already there, do not clone again. Run `cd "$HOME/pulsar" && ./install.sh`. The installer is safe to run more than once.
- The installer uses no `sudo`. It prints one `[OK]` or `[FAIL]` line for each requirement. If there is a `[FAIL]` line, fix that item with step 2 and run the installer again.
- If another program uses port `8726` or `5292`, run `./install.sh --daemon-port <port> --web-port <port>` and use those ports in all later commands.

**Check:** the installer prints `Services are running and passed health checks.` and these two commands give `{"ok":true,"value":{"status":"ok"}}` and `200`:

```bash
curl -s http://127.0.0.1:8726/healthz
curl -s -o /dev/null -w '%{http_code}\n' http://127.0.0.1:5292/
```

If `command -v pulsar-roster` prints nothing, add `~/.local/bin` to the user's `PATH` (tell the user first):

```bash
echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$HOME/.bashrc"
```

## Step 4 — Set up one agent tool

Pulsar does not contain a model. It drives agent tools that the user installs and signs in to. Ask Pulsar which tools it finds:

```bash
curl -s http://127.0.0.1:8726/tools
```

The answer has one entry for each tool (`claude-code`, `codex`, `pi`, `antigravity`, `grok`) at `value.vessels.<tool>`, with `"available": true` or `false`. `value.any_available` is `true` when one tool or more is available.

"Their own terminal" in this step is a terminal on the machine that has Pulsar, with the same Linux account. For a server, the user connects with SSH. The user types there; you do not see what they type.

**If one or more tools are available:** tell the user which ones. Ask which one they are signed in to. Use that tool in step 5.

**If no tool is available:** ask the user which of these they have. Then do that part.

- **Claude Code** (an Anthropic account). Install: `curl -fsSL https://claude.ai/install.sh | bash`. Then the user runs `claude` in their own terminal and signs in.
- **Codex** (an OpenAI account). Install: `npm install -g @openai/codex` (with `sudo` if `npm` needs it). Then the user runs `codex` in their own terminal and signs in.
- **A model server of their own** with an OpenAI-compatible `/v1` API (for example vLLM). Pulsar uses the `pi` tool for this. Ask the user for the server URL and the model name; these are not secrets. Then run:

  ```bash
  export PATH="$HOME/.local/bin:$PATH"
  cd "$HOME/pulsar" && ./install.sh --with-pi-tuned
  pulsar-models add local-openai --url <server-url>/v1 --served-id <model-name> --label "<name to show>"
  cd "$HOME/pulsar/web" && ./scripts/build-live.sh
  systemctl --user restart pulsar-daemon.service pulsar-web.service
  ```

  - The URL in `--url` must end in `/v1`. If the user gives `http://host:8000`, use `http://host:8000/v1`.
  - `--label` is the name in the model list. If the user gives no name, use the model name.
  - `pulsar-models add` prints the provider name and writes `"apiKey": "none"` for that provider in `~/.pi/agent/models.json`. If the server needs an API key, the user opens that file in an editor in their own terminal and puts the key in place of `none`. Do not ask for the key in the chat, and do not print that file after the user changes it.
- **Antigravity** (`agy`) or **Grok** (`grok`). This file has no install command for these two. Use the vendor's official install instructions. Pulsar looks for `~/.local/bin/agy`, and for `grok` on `PATH` or at `~/.local/bin/grok`.

Then ask Pulsar again: `curl -s http://127.0.0.1:8726/tools`. If the new tool is still `"available": false`, run `cd "$HOME/pulsar" && ./install.sh` again; it adds the tool's directory to the service `PATH`. If that does not help, see the README section "Agent tools and how Pulsar finds them".

**Check:** one tool or more has `"available": true`, and the user says that they are signed in to it (or, for their own model server, that the server is up).

**If the user cannot sign in now:** the check does not pass, so do not do step 5. Ask the user if they want the optional items of step 6. Then give the End report. In the report, say that the sign-in and step 5 are not done, and give the user the commands to finish them later (the sign-in command of the tool, then the commands of step 5).

## Step 5 — Start a first pulse and prove that it answers

List the models and pick one whose `vessel` is the tool from step 4:

```bash
curl -s http://127.0.0.1:8726/models | python3 -c 'import json,sys
v=json.load(sys.stdin)["value"]
for m in v["models"]:
    if v["tools"].get(m["vessel"],{}).get("available"): print(m["id"], "|", m["label"], "|", m["vessel"])'
```

Start a pulse with that model id. The answer contains the pulse id in `value.id`:

```bash
curl -s -X POST http://127.0.0.1:8726/pulses -H 'Content-Type: application/json' \
  -d '{"title": "First pulse", "model": "<model-id>", "first_message": "Reply with the one word: ready"}'
```

Wait 20 seconds, then read what the pulse did (do this again if the list is empty):

```bash
curl -s "http://127.0.0.1:8726/pulses/<pulse-id>/events?tail=50" | python3 -c 'import json,sys
for e in json.load(sys.stdin)["value"]["events"]:
    if e.get("type") in ("assistant_text", "runner_error", "done"): print(e.get("type"), "|", e.get("text") or e.get("reason") or e)'
```

- An `assistant_text` line with the answer: the pulse works.
- A `runner_error` line, or no answer after 2 minutes: run `pulsar-roster` and `curl -s http://127.0.0.1:8726/pulses` and read the `stop_reason` of the pulse. The usual cause is that the user is not signed in to the tool. Show the reason to the user, fix it with step 4, and start a new pulse.

**Check:** an `assistant_text` line shows the answer of the pulse.

Now tell the user to open Pulsar in a browser on that machine: `http://127.0.0.1:5292/`. The pulse "First pulse" is on the screen. The green `+` button makes a new pulse.

If the machine has no screen (a server that the user reaches with SSH), the user runs this on their own computer and then opens `http://127.0.0.1:5292/` there:

```bash
ssh -L 5292:127.0.0.1:5292 <user>@<machine>
```

## Step 6 — Optional items

Ask the user about these one at a time. Each one is optional.

1. **Keep Pulsar running after logout** (for a server or a virtual machine): `loginctl enable-linger "$USER"`. If it prints `Access denied`, do the check first; if the check does not pass, ask the user and then run `sudo loginctl enable-linger "$USER"`. Check: `loginctl show-user "$USER" --property=Linger` prints `Linger=yes`.
2. **The name that pulses use for the user** (the default is "the user"):

   ```bash
   curl -s -X PUT http://127.0.0.1:8726/settings/owner.name -H 'Content-Type: application/json' -d '"<name>"'
   ```
3. **Phone or remote access:** use Tailscale only. Follow `docs/setup/tailscale.md` in the Pulsar folder step by step. See rule 5.

## End — give the user a short report

Tell the user:

- The address of Pulsar: `http://127.0.0.1:5292/`.
- Which agent tool works, and the model of the first pulse.
- Restart command: `systemctl --user restart pulsar-daemon.service pulsar-web.service`.
- Where the folder is (`~/pulsar`) and that the README section "Uninstall" has the removal steps.
- Each step that is not done, and what the user must do to finish it.
- The safety rule: Pulsar has no login, so it must stay on `127.0.0.1` or inside Tailscale.

Do not say that the setup is complete if a check did not pass.
