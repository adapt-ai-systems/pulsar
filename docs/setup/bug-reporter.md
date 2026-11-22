# Automated 24-Hour Bug Reporter Pulse

Pulsar never phones home or sends telemetry on its own. If you are testing Pulsar and want bugs reported automatically without thinking about it, you can run a dedicated **Bug Reporter** pulse inside Pulsar on a 24-hour schedule.

Once set up, Pulsar wakes the pulse once every 24 hours, checks the last day of local daemon/web logs and pulse failures, scrubs any private data, deduplicates against existing GitHub issues, and files a formatted bug report only when it finds a real Pulsar bug. Between runs, the pulse stays stopped in **Watching** mode (off your main workspace grid, zero model cost while quiet).

## 1. Prerequisites

Install and sign in to the GitHub CLI (`gh`) as the account you want issues opened under:

```bash
gh auth login
gh auth status
```

Confirm Pulsar is running locally:

```bash
curl -s http://127.0.0.1:8726/healthz
```

## 2. Create the Bug Reporter pulse

Create a dedicated pulse in the web UI (titled `Bug Reporter`, using any model/vessel you have signed in), **or** create it via the daemon API. Replace `<MODEL>` with an enabled model ID from `curl -s http://127.0.0.1:8726/models` (and set `"vessel"` if you use a non-default vessel):

```bash
PULSE_ID=$(curl -s -X POST http://127.0.0.1:8726/pulses \
  -H 'Content-Type: application/json' \
  -d '{"title": "Bug Reporter", "model": "<MODEL>"}' \
  | python3 -c 'import sys, json; print(json.load(sys.stdin)["value"]["id"])')

echo "Created Bug Reporter pulse: $PULSE_ID"
```

## 3. Save the daily check prompt and attach a 24-hour schedule

Pulsar's scheduler supports two 24-hour modes:

- **Rolling 24-hour loop:** `"kind": "loop", "interval_minutes": 1440` (runs 24 hours after creation and every 24 hours after that).
- **Fixed daily UTC time:** `"kind": "daily", "cron_hhmm": "08:00"` (runs once a day at 08:00 UTC).

Attach the 24-hour schedule to your Bug Reporter pulse:

```bash
PULSAR_REPO="${PULSAR_REPO:-$HOME/pulsar}"

PROMPT=$(cat <<EOF
Run your daily 24-hour Pulsar bug check.

1. Collect the last 24 hours of local Pulsar diagnostics:
   - Health: curl -s http://127.0.0.1:8726/healthz
   - Daemon and web logs: journalctl --user -u pulsar-daemon.service -u pulsar-web.service --since "24 hours ago" --no-pager
   - Failed or crashed pulses: inspect GET http://127.0.0.1:8726/pulses for unexpected failure states in the last 24 hours.
   - Commit and OS: git -C "$PULSAR_REPO" rev-parse --short HEAD and /etc/os-release.

2. Filter for real Pulsar bugs only:
   - Report unhandled Python/Node tracebacks, HTTP 5xx daemon/web errors, crashed services, or broken Pulsar CLI/API behavior.
   - Ignore normal user cancellations, offline upstream model providers, rate limits (429), and auth errors.
   - Never file a public issue for a security vulnerability (see SECURITY.md instead).

3. Deduplicate before filing:
   - Keep a local state file reported-bugs.json in your pulse home listing error signatures you have already filed.
   - Search existing issues first: gh issue list -R https://github.com/adapt-ai-systems/pulsar --state all --label bug --search "<short error signature>"
   - If the same bug is already filed, do not open a duplicate issue.

4. Scrub all private data before submitting:
   - Replace home paths and usernames with <HOME> and <USER>.
   - Remove hostnames, tailnet names, IP addresses, API keys, tokens, cookies, and auth headers.
   - Never include prompts, code, or chat contents from other pulses.

5. File the bug report (only if a new, real Pulsar bug was found):
   - Use gh issue create -R https://github.com/adapt-ai-systems/pulsar --label bug --title "[auto] <short summary>"
   - Include these sections matching .github/ISSUE_TEMPLATE/bug_report.yml:
     What happened, Steps to reproduce (or trigger), Pulsar commit, Linux distribution and version, Agent tool and model (if pulse-related), and redacted Logs.
   - Record the signature and issue URL in reported-bugs.json.
   - Post a quiet Inbox notice via POST http://127.0.0.1:8726/inbox/items with channel "bug-reporter" and priority "quiet" linking the issue you filed.

6. If no new Pulsar bugs occurred in the last 24 hours, file nothing and end the turn.
EOF
)

python3 - "$PULSE_ID" "$PROMPT" <<'PY' | curl -s -X POST "http://127.0.0.1:8726/pulses/$PULSE_ID/schedules" \
  -H 'Content-Type: application/json' -d @-
import json, sys
print(json.dumps({
    "kind": "loop",
    "interval_minutes": 1440,
    "prompt": sys.argv[2],
}))
PY
```

To run at a fixed UTC time every day instead of a rolling 24-hour loop, replace `"kind": "loop", "interval_minutes": 1440` with `"kind": "daily", "cron_hhmm": "08:00"`.

## 4. Move the pulse to Watching mode

Move the pulse off your main workspace grid onto the **Watchers** shelf so it stays out of the way and sleeps between 24-hour runs:

```bash
curl -s -X PATCH "http://127.0.0.1:8726/pulses/$PULSE_ID/mode" \
  -H 'Content-Type: application/json' \
  -d '{"mode": "watching"}'
```

## 5. Run a dry test once (optional)

Send a one-time message telling the pulse to run a dry run and report what it would file without calling `gh issue create`:

```bash
curl -s -X POST "http://127.0.0.1:8726/pulses/$PULSE_ID/messages" \
  -H 'Content-Type: application/json' \
  -d '{"text": "Dry run: execute your daily bug check and summarize what you find, but do NOT run gh issue create."}'
```

## 6. Pause, inspect, or remove

```bash
# List the pulse's schedules (note the schedule id):
curl -s "http://127.0.0.1:8726/pulses/$PULSE_ID/schedules"

# Pause the 24-hour timer without deleting it:
curl -s -X PATCH "http://127.0.0.1:8726/schedules/<SCHEDULE_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"enabled": false}'

# Resume later:
curl -s -X PATCH "http://127.0.0.1:8726/schedules/<SCHEDULE_ID>" \
  -H 'Content-Type: application/json' \
  -d '{"enabled": true}'

# Stop the schedule and archive the pulse completely:
curl -s -X POST "http://127.0.0.1:8726/pulses/$PULSE_ID/archive" \
  -H 'Content-Type: application/json' \
  -d '{"monitoring": "stop"}'
```
