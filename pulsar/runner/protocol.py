"""Daemon<->runner wire protocol, v1.

Newline-delimited JSON over the pulse's unix socket (`runner.sock`).
Every request carries {"v": 1, "op": <str>, ...}; every reply carries
{"v": 1, "ok": <bool>, ...}. A version other than PROTOCOL_V is refused
loudly (`{"ok": false, "error": "protocol_version_mismatch", ...}`) — a
mismatched daemon must never talk baby-talk to an old runner.

Ops:
  hello  -> {ok, pulse_id, pid, cli_pid, state, protocol: 1}
  status -> same shape as hello (alias kept separate for log clarity)
  stop   -> {ok} then the runner shuts the CLI down and exits.

Messages do NOT travel over the socket. Ingest-ack is filesystem-based so a
message survives any crash between layers: the daemon writes a JSON file into
`inbox/new/`, the runner feeds it to the CLI when idle and moves the file to
`inbox/acked/`. The file IS the ack ledger.
"""

PROTOCOL_V = 1

# Runner states as written to heartbeat.json. The store's `state` column is
# lifecycle (running|stopped|archived); these are live-process states.
STATE_STARTING = "starting"
STATE_IDLE = "idle"          # CLI alive, waiting for input
STATE_BUSY = "busy"          # message fed, reply streaming / model thinking
STATE_STOPPING = "stopping"
STATE_STOPPED = "stopped"    # final heartbeat before exit
STATE_DEAD_CLI = "cli_dead"  # CLI exited on its own; runner reports and exits
