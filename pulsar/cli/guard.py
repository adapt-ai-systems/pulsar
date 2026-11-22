"""pulsar-guard: block accidental mass deletes before a pulse runs them.

This check runs on every shell command BEFORE execution so a malformed or
overly broad delete cannot wipe protected directories, regardless of how the
pulse was spawned.

Three entry points:

* ``pulsar-guard hook``   Claude Code PreToolUse (stdin JSON). Prints a
                          ``permissionDecision: deny`` reply when the command
                          is refused. A hook deny holds even under
                          ``--dangerously-skip-permissions``.
* ``pulsar-guard check [--cwd DIR] -- <command>``
                          exit 0 = fine, exit 2 = refused (reason on stdout).
                          Used by the pi extension (pi_ext/guard.ts).
* ``pulsar-guard argv rm -rf /x``
                          same check for one argv; used by the PATH shims in
                          ops/guard-bin (rm, rmdir, find), which every pulse
                          runner puts first on PATH (pulsar/runner/runner.py)
                          so every vessel is covered without a tool hook. The
                          shims only see the local binary; a delete sent over
                          ssh is caught by the Claude hook / pi extension.

What is refused (see ``check_command``):

1. ``rm`` / ``rmdir`` / ``find … -delete`` / ``shutil.rmtree`` whose target
   is a protected root, or a direct child of one. Built-in protected roots
   cover the Pulsar data directory (and ``pulses/``), plus any user-added
   ``guard.extra_paths``, alongside never-delete system/home roots (/, /home,
   $HOME, /etc, /usr, /var, /mnt, ...).
2. Any such delete whose target contains ``..``. Write the real path.
3. Any such delete whose target starts with an unexpanded ``$VAR`` the guard
   cannot see (``rm -rf $DIR/`` with DIR unset).
4. ``/x/*`` counts as ``/x``: emptying a protected root is deleting it.

Settings: the daemon writes ``guard.enabled`` (default true) and
``guard.extra_paths`` (list of paths, default []) to ``<data>/guard.json``.
The guard reads that file directly with no daemon call; a missing file means
enabled with no extra paths.

The check looks inside quoted strings too, so ``ssh host 'rm -rf /etc'`` and
``bash -c "…"`` are caught the same way. Every refusal is appended to
``<data>/guard-denials.jsonl``.

Stdlib only: ops/bin/pulsar-guard runs without the venv.
"""
from __future__ import annotations

import json
import os
import posixpath
import re
import shlex
import sys
import tempfile
import time
from pathlib import Path

HOME = os.path.expanduser("~")
REPO_ROOT = Path(__file__).resolve().parents[2]

# Deleting the path itself is refused (children are not special).
NEVER_DELETE = [
    "/", "/home", HOME, "/mnt", "/etc", "/usr", "/var",
    "/boot", "/opt", "/root", "/srv",
    str(REPO_ROOT), f"{HOME}/.claude", f"{HOME}/.ssh", f"{HOME}/.pi",
]

# Optional override for tests; None resolves dynamically under data_dir().
DENIALS_LOG: Path | None = None

_PREFIX_CMDS = {"sudo", "nohup", "exec", "env", "nice", "ionice", "command",
                "builtin", "time", "stdbuf", "doas"}
_SEPARATORS = {";", "&&", "||", "|", "&", "(", ")", "\n"}
_RMTREE_RE = re.compile(r"""rmtree\(\s*[rf]?(['"])([^'"]+)\1""")


def data_dir() -> Path:
    """Resolve Pulsar's data directory, honoring ``PULSAR_DATA``."""
    env = os.environ.get("PULSAR_DATA")
    return Path(env) if env else REPO_ROOT / "data"


def default_data_dir() -> Path:
    """Resolve the default install data directory (``~/.local/share/pulsar``)."""
    xdg = os.environ.get("XDG_DATA_HOME", "").strip()
    base = Path(xdg) if xdg else Path(HOME) / ".local" / "share"
    return base / "pulsar"


def config_dir() -> Path:
    """Resolve Pulsar's config directory (``~/.config/pulsar``)."""
    xdg = os.environ.get("XDG_CONFIG_HOME", "").strip()
    base = Path(xdg) if xdg else Path(HOME) / ".config"
    return base / "pulsar"


def config_path() -> Path:
    """Path to the daemon-synced guard settings file."""
    return data_dir() / "guard.json"


def _denials_log() -> Path:
    return DENIALS_LOG if DENIALS_LOG is not None else data_dir() / "guard-denials.jsonl"


def _normalize_root(raw: str) -> str:
    t = raw.strip()
    if not t:
        return ""
    if t == "~" or t.startswith("~/"):
        t = HOME + t[1:]
    t = t.replace("${HOME}", HOME).replace("$HOME", HOME)
    return posixpath.normpath(t)


def load_config(path: Path | None = None) -> tuple[bool, list[str]]:
    """Read (enabled, extra_paths) from guard.json. Missing/corrupt = enabled, []."""
    cfg_file = path if path is not None else config_path()
    try:
        data = json.loads(cfg_file.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return True, []
    if not isinstance(data, dict):
        return True, []
    raw_enabled = data.get("enabled")
    enabled = True if raw_enabled is None else bool(raw_enabled)
    raw_extra = data.get("extra_paths")
    extra: list[str] = []
    if isinstance(raw_extra, list):
        for item in raw_extra:
            if isinstance(item, str):
                norm = _normalize_root(item)
                if norm and norm not in extra:
                    extra.append(norm)
    return enabled, extra


def write_config(
    enabled: bool = True,
    extra_paths: list[str] | None = None,
    path: Path | None = None,
) -> Path:
    """Atomically write guard.json for the shell wrapper to read."""
    cfg_file = path if path is not None else config_path()
    cfg_file.parent.mkdir(parents=True, exist_ok=True)
    cleaned: list[str] = []
    for item in (extra_paths or []):
        if isinstance(item, str) and item.strip():
            cleaned.append(item.strip())
    payload = json.dumps({"enabled": bool(enabled), "extra_paths": cleaned},
                         indent=2) + "\n"
    fd, tmp_name = tempfile.mkstemp(prefix=".guard-", suffix=".tmp",
                                    dir=str(cfg_file.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_name, cfg_file)
    except Exception:
        try:
            os.unlink(tmp_name)
        except OSError:
            pass
        raise
    return cfg_file


def sync_from_store(store) -> Path:
    """Read guard.enabled + guard.extra_paths from app_settings and write guard.json."""
    raw_enabled = store.get_setting("guard.enabled")
    enabled = True if raw_enabled is None else bool(raw_enabled)
    raw_extra = store.get_setting("guard.extra_paths")
    extra = ([p.strip() for p in raw_extra if isinstance(p, str) and p.strip()]
             if isinstance(raw_extra, list) else [])
    return write_config(enabled=enabled, extra_paths=extra)


def protected_roots(extra_paths: list[str] | None = None) -> list[str]:
    """Deleting a protected root itself, or any direct child, is refused."""
    roots: list[str] = []

    def _add(p: str) -> None:
        norm = _normalize_root(p)
        if norm and norm not in roots:
            roots.append(norm)

    for d_path in (data_dir(), default_data_dir()):
        d = posixpath.normpath(str(d_path))
        _add(d)
        _add(posixpath.join(d, "pulses"))
    _add(str(config_dir()))
    for item in (extra_paths or []):
        _add(item)
    return roots


def _tokens(command: str) -> list[str]:
    """Shell-ish tokens with ; && || | as their own tokens. Falls back to a
    whitespace split on unbalanced quotes so a broken command is still read."""
    try:
        lex = shlex.shlex(command, posix=True, punctuation_chars=True)
        lex.whitespace_split = True
        return list(lex)
    except ValueError:
        return command.replace(";", " ; ").replace("&&", " && ").split()


def _simple_commands(tokens: list[str]) -> list[list[str]]:
    cmds: list[list[str]] = []
    cur: list[str] = []
    for t in tokens:
        if t in _SEPARATORS:
            if cur:
                cmds.append(cur)
            cur = []
        else:
            cur.append(t)
    if cur:
        cmds.append(cur)
    return cmds


def _strip_prefixes(argv: list[str]) -> list[str]:
    i = 0
    while i < len(argv):
        a = _base(argv[i])
        if a in _PREFIX_CMDS:
            i += 1
            if a == "timeout" and i < len(argv):
                i += 1
            while i < len(argv) and (argv[i].startswith("-") or ("=" in argv[i] and a == "env")):
                i += 1
            continue
        if a == "timeout":
            i += 2
            continue
        if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*=.*", a):  # VAR=x cmd
            i += 1
            continue
        break
    return argv[i:]


def _base(name: str) -> str:
    return posixpath.basename(name)


def _normalize(target: str, cwd: str) -> str:
    """Expand ~ and $HOME, make absolute against cwd, fold a trailing glob."""
    t = target
    if t == "~" or t.startswith("~/"):
        t = HOME + t[1:]
    t = t.replace("${HOME}", HOME).replace("$HOME", HOME)
    if not t.startswith("/"):
        t = posixpath.join(cwd, t)
    # /x/* , /x/*.* , /x/** -> /x : emptying is deleting
    t = re.sub(r"(/\*[^/]*)+/?$", "", t) or "/"
    return posixpath.normpath(t)


def _judge_target(raw: str, cwd: str, verb: str, roots: list[str]) -> str | None:
    if raw in ("", "-", "--") or raw.startswith("-"):
        return None
    if raw == ".." or raw.startswith("../") or "/../" in raw or raw.endswith("/.."):
        return (f"{verb} target '{raw}' contains '..'. "
                f"Write the real absolute path instead.")
    stripped = raw.replace("${HOME}", "").replace("$HOME", "")
    if stripped.startswith("$"):
        return (f"{verb} target '{raw}' starts with a shell variable the guard cannot "
                f"see. Write the literal path instead.")
    if raw in ("*", "./*", ".", "./"):
        return (f"{verb} target '{raw}' empties the current directory ({cwd}). "
                f"Name the paths you mean.")
    path = _normalize(raw, cwd)
    if path in NEVER_DELETE:
        return f"{verb} of '{raw}' -> '{path}' is never allowed from a pulse."
    for root in roots:
        if path == root:
            return (f"{verb} of '{raw}' -> '{path}' would delete a protected tree. "
                    f"Refused.")
        if posixpath.dirname(path) == root:
            return (f"{verb} of '{raw}' -> '{path}' would delete a whole top-level "
                    f"entry of protected '{root}'. Refused; delete inside it, "
                    f"or ask the user.")
    return None


def _check_simple(argv: list[str], cwd: str, roots: list[str]) -> str | None:
    argv = _strip_prefixes(argv)
    if not argv:
        return None
    name = _base(argv[0])
    if name in ("rm", "rmdir"):
        for a in argv[1:]:
            if a.startswith("-") and a != "--":
                continue
            r = _judge_target(a, cwd, name, roots)
            if r:
                return r
        return None
    if name == "find":
        toks = argv[1:]
        destructive = any(t in ("-delete", "-exec", "-execdir", "-ok") for t in toks) \
            and any(t in ("-delete",) or _base(t) == "rm" for t in toks)
        if not destructive:
            return None
        for a in toks:
            if a.startswith("-") or a in ("(", "!"):
                break
            r = _judge_target(a, cwd, "find -delete", roots)
            if r:
                return r
        return None
    if name == "xargs" and any(_base(t) == "rm" for t in argv[1:]):
        return None  # targets come from stdin; the producer command was checked
    return None


def _check_command(command: str, cwd: str, roots: list[str]) -> str | None:
    if not command or not command.strip():
        return None
    m = _RMTREE_RE.search(command)
    if m:
        r = _judge_target(m.group(2), cwd, "shutil.rmtree", roots)
        if r:
            return r
    tokens = _tokens(command)
    for simple in _simple_commands(tokens):
        r = _check_simple(simple, cwd, roots)
        if r:
            return r
    for t in tokens:
        if any(ch in t for ch in " \t\n") and t != command:
            r = _check_command(t, cwd, roots)
            if r:
                return r
    return None


def check_command(command: str, cwd: str | None = None) -> str | None:
    """Reason the command is refused, or None. Looks inside quoted strings
    (ssh '…', bash -c "…", heredocs) by re-checking every token that holds
    whitespace. Returns None when disabled via guard.json."""
    enabled, extra_paths = load_config()
    if not enabled:
        return None
    cwd = cwd or os.getcwd()
    roots = protected_roots(extra_paths)
    return _check_command(command, cwd, roots)


def _log_denial(command: str, reason: str, cwd: str, source: str) -> None:
    log_path = _denials_log()
    try:
        log_path.parent.mkdir(parents=True, exist_ok=True)
        with log_path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps({"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                 "source": source, "cwd": cwd, "command": command,
                                 "reason": reason}) + "\n")
    except OSError:
        pass


def hook_reply(event: dict) -> dict | None:
    """Claude PreToolUse: deny reply for a refused Bash command, else None."""
    if event.get("tool_name") != "Bash":
        return None
    tool_input = event.get("tool_input") or {}
    command = tool_input.get("command") or ""
    cwd = event.get("cwd") or os.getcwd()
    reason = check_command(command, cwd)
    if reason is None:
        return None
    _log_denial(command, reason, cwd, "claude-hook")
    return {"hookSpecificOutput": {
        "hookEventName": "PreToolUse",
        "permissionDecision": "deny",
        "permissionDecisionReason": f"pulsar-guard: {reason}",
    }}


def _hook() -> int:
    try:
        event = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        return 0
    reply = hook_reply(event)
    if reply is not None:
        sys.stdout.write(json.dumps(reply) + "\n")
    return 0


def _check(args: list[str]) -> int:
    cwd = None
    if args[:1] == ["--cwd"] and len(args) >= 2:
        cwd, args = args[1], args[2:]
    if args[:1] == ["--"]:
        args = args[1:]
    command = " ".join(args) if len(args) != 1 else args[0]
    reason = check_command(command, cwd)
    if reason is None:
        return 0
    _log_denial(command, reason, cwd or os.getcwd(), "check")
    sys.stdout.write(f"pulsar-guard: {reason}\n")
    return 2


def _argv(args: list[str]) -> int:
    """PATH-shim entry: ``pulsar-guard argv rm -rf /x`` checks the one command
    whose argv follows (the shim then execs the real binary). exit 2 = refused."""
    if not args:
        return 0
    command = shlex.join(args)
    cwd = os.getcwd()
    reason = check_command(command, cwd)
    if reason is None:
        return 0
    _log_denial(command, reason, cwd, "path-shim")
    sys.stderr.write(f"pulsar-guard: {reason}\n")
    return 2


USAGE = """usage: pulsar-guard hook                      # Claude PreToolUse (stdin JSON)
       pulsar-guard check [--cwd DIR] -- <cmd>  # exit 2 + reason if refused
       pulsar-guard argv <cmd> [args...]        # PATH shim: check one argv, exit 2 if refused
"""


def main(argv=None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in ("-h", "--help"):
        sys.stdout.write(USAGE)
        return 0 if args else 1
    if args[0] == "hook":
        return _hook()
    if args[0] == "check":
        return _check(args[1:])
    if args[0] == "argv":
        return _argv(args[1:])
    sys.stderr.write(f"unknown verb '{args[0]}'\n{USAGE}")
    return 1
