"""Helpers shared by the CLIs ported from bash (pulsar-wait, -ask, -roster).

Stdlib only: the ops/bin shims run without the venv. These keep the bash scripts' rules:
the daemon address comes from PULSAR_DAEMON (empty counts as unset), the caller's pulse id
comes from the cwd (`*/pulses/*/cli`), and text read from the daemon is echoed as bytes.
"""
from __future__ import annotations

import os
import re
import sys

from .client import DEFAULT_DAEMON

_CLI_DIR = re.compile(r".*/pulses/.*/cli", re.DOTALL)  # the bash glob */pulses/*/cli


def daemon_address() -> str:
    """$PULSAR_DAEMON as written (bash `${PULSAR_DAEMON:-default}`), shown in messages."""
    return os.environ.get("PULSAR_DAEMON") or DEFAULT_DAEMON


def logical_pwd() -> str:
    """bash's $PWD: the inherited PWD when it still names the cwd, else the physical cwd."""
    value = os.environ.get("PWD")
    try:
        if value and os.path.isabs(value) and os.path.samefile(value, "."):
            return value
    except OSError:
        pass
    return os.getcwd()


def cwd_pulse_id() -> str:
    """The id from `<data>/pulses/<id>/cli` as pulses run (readlink -f $PWD), else ''."""
    here = os.path.realpath(logical_pwd())
    if _CLI_DIR.fullmatch(here):
        return os.path.basename(os.path.dirname(here))
    return ""


def text_of(data: bytes) -> str:
    """Body bytes as bash `$(...)` holds them: trailing newlines gone, bytes kept."""
    return data.decode("utf-8", "surrogateescape").rstrip("\n")


def write(stream, text: str) -> None:
    """Write `text` as UTF-8 bytes whatever the locale (bash `echo` never fails on encoding)."""
    data = text.encode("utf-8", "surrogateescape")
    buffer = getattr(stream, "buffer", None)
    if buffer is None:
        stream.write(text)
        return
    stream.flush()
    buffer.write(data)
    buffer.flush()


def out(text: str = "") -> None:
    write(sys.stdout, text + "\n")


def err(text: str = "") -> None:
    write(sys.stderr, text + "\n")

