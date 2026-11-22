"""Run a pulse's runner as a fenced transient systemd user service.

Each runner runs as a transient systemd *user* service named
`pulsar-pulse-<id>.service` with MemoryMax + TasksMax set. The
cgroup covers the CLI and everything it spawns; a breach kills the whole
tree and systemd records why — `fence_report()` reads it back so the tile
can say "hit memory cap" instead of dying silently.

A transient service (not --scope) is deliberate: it detaches the runner from
the daemon's lifetime (daemon restarts never touch it) and gives
the orphan sweep one authoritative list: `systemctl --user list-units
'pulsar-pulse-*'`.

Daemon-side: runners never import this module.
"""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

from pulsar.launch.context import daemon_url
from pulsar.launch.diet import materialize_diet
from pulsar.vessel.profiles import PROFILES

log = logging.getLogger("pulsar.spawn")

DEFAULT_MEMORY_MAX = "6G"
DEFAULT_TASKS_MAX = 512
# Aggregate fence: every pulse unit lands in this slice, whose
# ~/.config/systemd/user/pulsar.slice caps total memory/CPU across ALL pulses
# (per-pulse fences do not compose cleanly on their own).
PULSE_SLICE = "pulsar.slice"
# Hard count brake on top of the slice: spawn refuses past this many live
# pulses. 0 disables. Override with PULSAR_MAX_RUNNING_PULSES. This is only
# a runaway-spawn backstop — the slice does the real resource limiting.
DEFAULT_MAX_RUNNING = 32

# pi (OMP) binary — the tuned install from addons/pi-tuned. systemd user
# units do NOT inherit the interactive shell PATH, so PiVessel's which("pi")
# would find nothing and spawn would raise. Every pi pulse's transient unit
# therefore gets PULSAR_PI_BIN pinned here (spawn_runner); env override lets
# an operator move it without a code change.
DEFAULT_PI_BIN = str(
    Path.home() / ".local" / "share" / "pulsar" / "pi-tuned"
    / "node_modules" / ".bin" / "pi"
)
PI_BIN = os.environ.get("PULSAR_PI_BIN") or DEFAULT_PI_BIN

# The tool path overrides a user can put in pulsar.env (README: "Agent tools
# and how Pulsar finds them"). The daemon reads that file; a transient unit
# does not, so spawn_runner passes the override for the pulse's vessel.
VESSEL_BIN_ENV = {
    "claude-code": "PULSAR_CLAUDE_BIN",
    "codex": "PULSAR_CODEX_BIN",
    "antigravity": "PULSAR_AGY_BIN",
    "grok": "PULSAR_GROK_BIN",
}


def _pi_bin_for_unit() -> str | None:
    """The pi path to pin in a pi pulse's unit. None = do not pin.

    PULSAR_PI_BIN or the tuned pi wins. When neither exists and a `pi` is on
    the daemon's PATH, the unit gets no pin: the runner searches the same
    PATH and finds the `pi` that GET /tools reports.
    """
    if PI_BIN != DEFAULT_PI_BIN or os.path.exists(PI_BIN):
        return PI_BIN
    if shutil.which("pi"):
        return None
    return PI_BIN


def _resolved_data_root() -> Path:
    """Resolve the active Pulsar data root without importing pulsar.daemon."""
    env = os.environ.get("PULSAR_DATA", "").strip()
    return Path(env) if env else Path(__file__).resolve().parents[2] / "data"


def unit_name(pulse_id: str) -> str:
    return f"pulsar-pulse-{pulse_id}.service"


def _run(cmd: list[str], what: str) -> subprocess.CompletedProcess:
    started = time.monotonic()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=30)
    dur = time.monotonic() - started
    if proc.returncode != 0:
        log.error("%s failed rc=%d in %.1fs: %s | stderr: %s",
                  what, proc.returncode, dur, " ".join(cmd), proc.stderr.strip())
    else:
        log.info("%s ok in %.1fs", what, dur)
    return proc


def _await_unit_gone(pulse_id: str, timeout_s: float = 10.0) -> None:
    """Wait for a previous incarnation's transient unit to leave systemd.

    A socket-stopped runner exits gracefully, but its unit lingers in
    ``deactivating`` (or ``failed``) for a moment — and ``systemd-run``
    refuses to reuse the name ("already loaded"). Fast stop→respawn paths
    (model switch, handoff) hit this race; waiting here fixes every caller.
    Logs the wait (standing rule); proceeds after timeout and lets
    systemd-run produce the real error if the unit is truly stuck.
    """
    unit = unit_name(pulse_id)
    state = "?"
    started = time.monotonic()
    while time.monotonic() - started < timeout_s:
        proc = subprocess.run(
            ["systemctl", "--user", "show", unit, "-p", "ActiveState",
             "--value"], capture_output=True, text=True, timeout=10)
        state = proc.stdout.strip()
        if state in ("inactive", "failed", ""):
            if state == "failed":
                _run(["systemctl", "--user", "reset-failed", unit],
                     f"unit-reset pulse={pulse_id} (pre-spawn)")
            if time.monotonic() - started > 0.05:
                log.info("spawn pulse=%s waited %.2fs for old unit to clear "
                         "(final state=%s)", pulse_id,
                         time.monotonic() - started, state or "gone")
            return
        time.sleep(0.2)
    log.warning("spawn pulse=%s old unit still %s after %.0fs — proceeding, "
                "systemd-run will report the truth", pulse_id, state, timeout_s)


def spawn_runner(pulse_id: str, home: Path, model: str, vessel: str,
                 resume: str | None = None,
                 memory_max: str = DEFAULT_MEMORY_MAX,
                 tasks_max: int = DEFAULT_TASKS_MAX,
                 diet: dict | None = None,
                 title: str | None = None,
                 effort: str | None = None,
                 fast: bool = False) -> None:
    """Start a fenced, detached runner. Raises RuntimeError on failure."""
    cap = int(os.environ.get("PULSAR_MAX_RUNNING_PULSES", DEFAULT_MAX_RUNNING))
    if cap > 0:
        running = [u for u in list_pulse_units() if u != pulse_id]
        if len(running) >= cap:
            raise RuntimeError(
                f"the server is full: {len(running)} of {cap} pulses are "
                f"running (idle ones count too). Stop or archive a pulse "
                f"you're done with, then try again")
    _await_unit_gone(pulse_id)
    if vessel == "pi":
        # Lazy: pulsar.daemon's package init pulls in the whole FastAPI app,
        # and vessel_bridge imports this module inside every runner.
        from pulsar.daemon import slots as slots_mod
        entry = slots_mod.models_mod.model_by_id(model)
        if entry is not None and entry.slot:
            _slot, status = slots_mod.resolve_slot(model)
            if not status.get("live") or not status.get("served_id"):
                raise RuntimeError(f"slot_unavailable:{model}")
            diet = dict(diet or {"clean": True})
            diet["slot"] = {"id": model,
                             "base_url": status.get("base_url") or entry.base_url,
                             "served_id": status["served_id"]}
    diet = materialize_diet(pulse_id, home, diet, vessel=vessel, title=title,
                            model=model)
    # A clean codex pulse gets an EMPTY ~/.agents inside its unit's mount
    # namespace — that dir's skills are the one ambient source codex resolves
    # from HOME rather than CODEX_HOME, so no config or env can hide them.
    # The bind covers the whole fenced tree, which is the intent: nothing the
    # pulse runs should see them either.
    bind = []
    prof = PROFILES.get(vessel)
    mounts = prof.bind_mounts(home) if (diet.get("clean") and prof is not None) else []
    if mounts:
        bind = ["-p", f"BindPaths={' '.join(mounts)}"]
    pi_bin = _pi_bin_for_unit() if vessel == "pi" else None
    bin_var = VESSEL_BIN_ENV.get(vessel)
    cmd = [
        # no --collect: a dead unit must stay inspectable until reconcile
        # reads its verdict and reset-fails it. --collect erases the evidence.
        "systemd-run", "--user",
        f"--unit={unit_name(pulse_id)}",
        f"--slice={PULSE_SLICE}",
        f"-p", f"MemoryMax={memory_max}",
        # Without MemorySwapMax=0 the cap silently spills into swap and never
        # fires cleanly under OOM.
        "-p", f"MemorySwapMax=0",
        # systemd's default OOMPolicy=stop tears down the WHOLE unit when the
        # kernel kills ANY process in it. Claude Code shims `grep`/`find` in
        # Bash commands to its bundled ugrep/bfs, which can balloon past the
        # fence on regex sweeps over huge files. continue lets the kernel kill
        # just the runaway child — the Bash command fails with 137, the pulse
        # lives. The runner still reports "hit memory cap" when the CLI
        # itself is the victim, via the cgroup oom_kill counter.
        "-p", "OOMPolicy=continue",
        "-p", f"TasksMax={tasks_max}",
        "-p", "Restart=no",
        # Long local-model thinking generations can exceed a 600s single-turn
        # stall timeout. Nothing reads this env var since the qwen vessel was
        # retired; kept so every vessel's spawn env golden stays byte-identical.
        "-p", "Environment=PULSAR_QWEN_STALL_TIMEOUT=2400",
        "-p", f"WorkingDirectory={home}",
        "-p", f"Environment=PULSAR_PULSE_HOME={home}",
        "-p", f"Environment=PULSAR_DATA={_resolved_data_root()}",
        "-p", f"Environment=PULSAR_DAEMON={daemon_url()}",
        # A transient unit does not inherit the daemon's PATH. install.sh puts
        # each detected agent tool directory on the daemon's PATH, so the
        # runner must search the same directories or a tool the daemon found
        # (nvm, npm global) is missing in the pulse. --setenv takes the value
        # as it is (a directory name can contain a space).
        *( [f"--setenv=PULSAR_DAEMON_PATH={os.environ['PATH']}"]
            if os.environ.get("PATH") else [] ),
        *bind,
        *( ["-p", f"Environment=PULSAR_PI_BIN={pi_bin}"]
            if vessel == "pi" and pi_bin else [] ),
        *( [f"--setenv={bin_var}={os.environ[bin_var]}"]
            if bin_var and os.environ.get(bin_var) else [] ),
        sys.executable, "-m", "pulsar.runner",
        "--pulse-id", pulse_id, "--home", str(home),
        "--model", model, "--vessel", vessel,
    ]
    if resume:
        cmd += ["--resume", resume]
    if effort:
        cmd += ["--effort", effort]
    if fast:
        cmd += ["--fast"]
    proc = _run(cmd, f"spawn pulse={pulse_id} fence={memory_max}/{tasks_max}t")
    if proc.returncode != 0 and bind:
        # Mount namespacing can be unavailable in a user manager. A pulse that
        # boots slightly dirty beats a pulse that won't boot — but say so
        # loudly, because "clean" is then a half-truth for this session.
        ambient = mounts[0].split(":", 1)[1]
        log.error("spawn pulse=%s systemd-run rejected BindPaths (%s) — "
                  "retrying WITHOUT it; this %s pulse WILL see the ambient %s",
                  pulse_id, proc.stderr.strip(), vessel, ambient)
        _await_unit_gone(pulse_id)
        i = cmd.index(bind[1])
        del cmd[i - 1:i + 1]  # drop the property AND its "-p"
        proc = _run(cmd, f"spawn pulse={pulse_id} (no bind)")
    if proc.returncode != 0:
        raise RuntimeError(f"systemd-run failed for {pulse_id}: {proc.stderr.strip()}")


def stop_unit(pulse_id: str) -> bool:
    """Hard-stop the whole fenced tree (used when socket stop fails).

    Also reset-fails the unit so a dead unit doesn't linger after its verdict
    has been read (we dropped --collect to keep the evidence until now).
    """
    proc = _run(["systemctl", "--user", "stop", unit_name(pulse_id)],
                f"unit-stop pulse={pulse_id}")
    _run(["systemctl", "--user", "reset-failed", unit_name(pulse_id)],
         f"unit-reset pulse={pulse_id}")
    if proc.returncode != 0 and "not loaded" in (proc.stderr or ""):
        # The unit is already gone — a stop's desired end state. Treating this
        # as failure would 502 archive/pause on pulses whose runner had died.
        log.info("unit-stop pulse=%s unit already gone — treating as stopped",
                 pulse_id)
        return True
    return proc.returncode == 0


def unit_active(pulse_id: str) -> bool | None:
    """Is this pulse's runner unit still alive? True/False, None if unknown.

    The reconcile sweep asks this before reaping a stale-heartbeat pulse:
    under heavy CPU load a busy runner can miss beats while its unit is fine.
    """
    try:
        proc = subprocess.run(
            ["systemctl", "--user", "is-active", unit_name(pulse_id)],
            capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.SubprocessError) as exc:
        log.error("unit-active pulse=%s check failed: %s", pulse_id, exc)
        return None
    state = proc.stdout.strip()
    if state in ("active", "activating", "reloading"):
        return True
    if state in ("inactive", "failed", "deactivating"):
        return False
    log.error("unit-active pulse=%s unexpected state %r rc=%d", pulse_id,
              state, proc.returncode)
    return None


def list_pulse_units() -> list[str]:
    """Pulse ids of every live pulsar-pulse-* unit — the orphan-sweep source."""
    proc = _run(["systemctl", "--user", "list-units", "pulsar-pulse-*",
                 "--output=json", "--no-legend"], "list pulse units")
    if proc.returncode != 0 or not proc.stdout.strip():
        return []
    try:
        units = json.loads(proc.stdout)
    except json.JSONDecodeError:
        log.exception("list-units returned non-JSON: %r", proc.stdout[:200])
        return []
    ids = []
    for u in units:
        name = u.get("unit", "")
        if name.startswith("pulsar-pulse-") and name.endswith(".service"):
            ids.append(name[len("pulsar-pulse-"):-len(".service")])
    return ids


def fence_report(pulse_id: str) -> dict:
    """Why did this unit die? Reads systemd's verdict for the tile/log.

    Returns {result, oom: bool, detail} — result e.g. 'oom-kill' when the
    memory fence fired ('hit memory cap' in UI copy).
    """
    proc = _run(["systemctl", "--user", "show", unit_name(pulse_id),
                 "-p", "Result", "-p", "ExecMainStatus", "-p", "OOMPolicy"],
                f"fence-report pulse={pulse_id}")
    props = dict(line.split("=", 1) for line in proc.stdout.strip().splitlines()
                 if "=" in line)
    result = props.get("Result", "unknown")
    report = {"result": result, "oom": result == "oom-kill",
              "detail": props}
    log.info("fence report pulse=%s result=%s oom=%s", pulse_id, result,
             report["oom"])
    return report
