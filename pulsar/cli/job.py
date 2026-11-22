"""pulsar-job: run a command in its OWN systemd user unit so it outlives the pulse. Stdlib only.

Why: a pulse runs inside one systemd unit
(pulsar-pulse-<id>.service). A handoff, reset or failover stops that unit, and
systemd kills EVERY process in its cgroup — `&`, nohup and setsid do not escape
it. A job started here lives in pulsar-job-<pulse8>-<name>.service instead, so
the pulse can die and respawn around it.

Each job writes <jobs>/<name>.log (stdout+stderr) and, when it ends for any
reason, <jobs>/<name>.exit (the exit code or the killing signal). <jobs> is
$PULSAR_PULSE_HOME/jobs.

`run` registers a pulsar-wait file watch on the .exit file, so whichever
session of the pulse is alive is woken when the job ends. `run --follow`
streams the log and exits with the job's code (what the claude Bash hook uses
for run_in_background); it removes that watch itself when it sees the end, so
the watch only fires if the follower died first — i.e. across a handoff.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path

from .client import TransportError, send
from .common import cwd_pulse_id, daemon_address, err, logical_pwd, out

USAGE = """\
pulsar-job — run a command OUTSIDE your pulse so a handoff cannot kill it.

  pulsar-job run [opts] [--] <cmd...>   start it in its own systemd unit, return at once
      --name N       job name (default bg-<time>); log/exit files use it
      --mem SIZE     memory cap for the job (default 6G)
      --follow       stream the log and exit with the job's code (job still survives you)
      --no-wake      do not register the wake-on-exit watch
  pulsar-job list                       your jobs: running ones and finished exit codes
  pulsar-job stop <name>                stop a running job
  pulsar-job log <name> [-n N]          last N lines of the job's log (default 40)

Start as many jobs as you like; they run in parallel. Keep working while they
run. Each job writes <jobs>/<name>.log and <name>.exit, and `run` wakes your
pulse when the job ends — even after a handoff.
"""

_SKIP_ENV = frozenset({"INVOCATION_ID", "JOURNAL_STREAM", "SYSTEMD_EXEC_PID", "MANAGERPID",
                       "NOTIFY_SOCKET", "LISTEN_PID", "LISTEN_FDS", "LISTEN_FDNAMES",
                       "MEMORY_PRESSURE_WATCH", "MEMORY_PRESSURE_WRITE", "PWD", "OLDPWD", "_"})
_ENV_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_NAME_BAD = re.compile(r"[^A-Za-z0-9_.-]+")
DEFAULT_MEM = "6G"
SLICE = "pulsar-jobs.slice"


def _pulse_id() -> str:
    return os.environ.get("PULSAR_PULSE_ID") or os.environ.get("PULSE_ID") or cwd_pulse_id()


def jobs_dir() -> Path:
    home = os.environ.get("PULSAR_PULSE_HOME")
    base = Path(home) / "jobs" if home else Path.home() / ".cache" / "pulsar-jobs"
    base.mkdir(parents=True, exist_ok=True)
    return base


def unit_for(pulse_id: str, name: str) -> str:
    return f"pulsar-job-{(pulse_id or 'nopulse')[:8]}-{name}.service"


def clean_name(name: str) -> str:
    return _NAME_BAD.sub("-", name).strip("-.")[:48]


def default_name() -> str:
    return time.strftime("bg-%H%M%S-") + secrets.token_hex(2)


def build_command(pulse_id: str, name: str, script: Path, log: Path, exit_file: Path,
                  cwd: str, mem: str, env: dict) -> list:
    """The systemd-run argv. ExecStopPost writes the .exit file whatever ended the job.

    The writer is a script file, not inline sh -c: systemd's own $VAR and %
    expansion of an Exec line mangles inline shell. EXIT_STATUS (exit code or
    signal name) is set by systemd for ExecStopPost.
    """
    post = script.with_suffix(".post")
    post.write_text('printf "%s\\n" "${EXIT_STATUS:-unknown}" > "$1.tmp" && mv "$1.tmp" "$1"\n')
    stop_post = f"/bin/sh {post} {exit_file}"
    cmd = ["systemd-run", "--user", "--quiet", "--collect",
           f"--unit={unit_for(pulse_id, name)}", f"--slice={SLICE}",
           "-p", f"MemoryMax={mem}", "-p", "MemorySwapMax=0",
           "-p", f"WorkingDirectory={cwd}",
           "-p", f"StandardOutput=append:{log}", "-p", f"StandardError=append:{log}",
           "-p", f"ExecStopPost={stop_post}"]
    for key in sorted(env):
        if key in _SKIP_ENV or not _ENV_NAME.fullmatch(key) or "\n" in env[key]:
            continue
        cmd.append(f"--setenv={key}={env[key]}")
    cmd += ["/bin/bash", str(script)]
    return cmd


def _watch_add(pulse_id: str, exit_file: Path, note: str) -> str:
    body = {"kind": "file", "pulse_id": pulse_id, "spec": {"path": str(exit_file), "exists": True},
            "mode": "once", "note": note, "interval_seconds": 30}
    try:
        reply = send("POST", "/watches/conditions", body, base=daemon_address(), timeout=10)
        d = json.loads(reply.body)
    except (TransportError, ValueError, OSError):
        return ""
    v = d.get("value", d) if isinstance(d, dict) else {}
    v = (v.get("watch") or v) if isinstance(v, dict) else {}
    return str(v.get("id", "")) if isinstance(v, dict) else ""


def _watch_rm(watch_id: str) -> None:
    if not watch_id:
        return
    try:
        send("DELETE", f"/watches/conditions/{watch_id}", base=daemon_address(), timeout=10, headers={})
    except (TransportError, OSError):
        pass


def _exit_code(exit_file: Path) -> int:
    status = exit_file.read_text().strip()
    return int(status) if status.isdigit() else 1


def _follow(name: str, log: Path, exit_file: Path, watch_id: str) -> int:
    # A follower killed with the pulse (handoff) must not take the job with it:
    # it only stops reading. The job's own unit is not ours to stop here.
    signal.signal(signal.SIGTERM, lambda *_: os._exit(143))
    signal.signal(signal.SIGHUP, lambda *_: os._exit(129))
    pos = 0
    while True:
        done = exit_file.exists()
        try:
            with log.open("rb") as fh:
                fh.seek(pos)
                chunk = fh.read()
        except FileNotFoundError:
            chunk = b""
        if chunk:
            pos += len(chunk)
            sys.stdout.buffer.write(chunk)
            sys.stdout.flush()
        if done:
            break
        time.sleep(0.5)
    _watch_rm(watch_id)
    status = exit_file.read_text().strip()
    out(f"[pulsar-job] {name} ended: {status}")
    return _exit_code(exit_file)


def _run(args: list) -> int:
    name, mem, follow, wake = "", DEFAULT_MEM, False, True
    while args and args[0].startswith("--"):
        flag = args.pop(0)
        if flag == "--":
            break
        if flag in ("--name", "--mem"):
            if not args:
                err(f"{flag} needs a value")
                return 2
            value = args.pop(0)
            if flag == "--name":
                name = value
            else:
                mem = value
        elif flag == "--follow":
            follow = True
        elif flag == "--no-wake":
            wake = False
        else:
            err(f"unknown option {flag}")
            return 2
    if not args:
        err("usage: pulsar-job run [opts] [--] <cmd...>")
        return 2
    shell = args[0] if len(args) == 1 else shlex.join(args)
    name = clean_name(name) or default_name()
    pulse_id = _pulse_id()
    jobs = jobs_dir()
    script, log, exit_file = jobs / f"{name}.sh", jobs / f"{name}.log", jobs / f"{name}.exit"
    unit = unit_for(pulse_id, name)
    if subprocess.run(["systemctl", "--user", "is-active", "--quiet", unit]).returncode == 0:
        err(f"pulsar-job: job '{name}' is already running ({unit}); pick another --name")
        return 1
    for stale in (exit_file, Path(f"{exit_file}.tmp")):
        stale.unlink(missing_ok=True)
    script.write_text(shell + "\n")
    cmd = build_command(pulse_id, name, script, log, exit_file,
                        os.path.realpath(logical_pwd()), mem, dict(os.environ))
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        err(f"pulsar-job: systemd-run failed: {proc.stderr.strip()}")
        return 1
    watch_id = ""
    if wake and pulse_id:
        watch_id = _watch_add(pulse_id, exit_file,
                              f"pulsar-job '{name}' ended — exit status in {exit_file}, log {log}")
    out(f"[pulsar-job] {name} started in {unit} (survives handoff)")
    out(f"[pulsar-job] log: {log}   stop: pulsar-job stop {name}")
    if follow:
        return _follow(name, log, exit_file, watch_id)
    if watch_id:
        out(f"[pulsar-job] wake-on-exit watch {watch_id} registered. Keep working; you will be woken.")
    return 0


def _list() -> int:
    jobs = jobs_dir()
    pulse_id = _pulse_id()
    names = sorted({p.stem for p in jobs.glob("*.sh")}, key=lambda n: (jobs / f"{n}.sh").stat().st_mtime)
    if not names:
        out("no jobs")
        return 0
    for name in names:
        exit_file = jobs / f"{name}.exit"
        if exit_file.exists():
            state = f"ended {exit_file.read_text().strip()}"
        else:
            unit = unit_for(pulse_id, name)
            active = subprocess.run(["systemctl", "--user", "is-active", unit],
                                    capture_output=True, text=True).stdout.strip()
            state = "running" if active in ("active", "activating", "deactivating") else "gone"
        cmd = (jobs / f"{name}.sh").read_text().strip().replace("\n", " ")[:80]
        out(f"{name:<24} {state:<12} {cmd}")
    return 0


def _stop(args: list) -> int:
    if not args:
        err("usage: pulsar-job stop <name>")
        return 2
    unit = unit_for(_pulse_id(), clean_name(args[0]))
    proc = subprocess.run(["systemctl", "--user", "stop", unit], capture_output=True, text=True)
    if proc.returncode != 0:
        err(f"pulsar-job: stop failed: {proc.stderr.strip()}")
        return 1
    out(f"stopped {unit}")
    return 0


def _log(args: list) -> int:
    if not args:
        err("usage: pulsar-job log <name> [-n N]")
        return 2
    lines = int(args[2]) if len(args) >= 3 and args[1] == "-n" else 40
    log = jobs_dir() / f"{clean_name(args[0])}.log"
    if not log.exists():
        err(f"pulsar-job: no log {log}")
        return 1
    tail = log.read_bytes().splitlines()[-lines:]
    sys.stdout.buffer.write(b"\n".join(tail) + b"\n")
    return 0


def hook_rewrite(event: dict) -> dict | None:
    """Claude PreToolUse: a run_in_background Bash command becomes a pulsar-job.

    Returns the hook reply, or None to leave the call alone. --follow keeps
    Claude's own background-task tracking working (output + completion); the
    job itself lives in its own unit, so a handoff does not kill it.
    """
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command") or ""
    if not tool_input.get("run_in_background") or not command.strip():
        return None
    if command.lstrip().startswith(("pulsar-job", "systemd-run")):
        return None
    name = default_name()
    wrapped = f"pulsar-job run --follow --name {name} -- {shlex.quote(command)}"
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "allow",
        "permissionDecisionReason": "pulsar: background command runs in its own unit",
        "updatedInput": {**tool_input, "command": wrapped},
        "additionalContext": (f"Pulsar ran this background command as job '{name}' in its own "
                              f"systemd unit, so it survives a handoff. Stopping the background "
                              f"task does NOT stop the job: run `pulsar-job stop {name}`."),
    }}


def _hook() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    reply = hook_rewrite(event)
    if reply is not None:
        out(json.dumps(reply))
    return 0


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help", "help"):
        out(USAGE.rstrip("\n"))
        return 0 if args else 1
    verb, args = args[0], args[1:]
    if verb == "run":
        return _run(args)
    if verb == "list":
        return _list()
    if verb == "stop":
        return _stop(args)
    if verb == "log":
        return _log(args)
    if verb == "hook":
        return _hook()
    err(f"unknown verb '{verb}'")
    out(USAGE.rstrip("\n"))
    return 1
