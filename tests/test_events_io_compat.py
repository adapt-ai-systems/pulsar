"""Characterization tests for events.jsonl readers, parser, cursor and appenders.

Uses one fixture file with:
- a torn first line (when the window starts mid-file),
- a blank line,
- bad JSON,
- and a U+2028 line separator inside a JSON string.

Asserts each current reader's exact output (split rule and bad-JSON rule),
and once ``pulsar.daemon.events_io`` exists, runs the same assertions against
the new functions.
"""

from __future__ import annotations

import importlib.util
import json
import re
from pathlib import Path

import pytest

from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import live as live_mod
from pulsar.daemon import events_io, paths

from tests.test_daemon import data_root  # noqa: F401


def _has_events_io() -> bool:
    return importlib.util.find_spec("pulsar.daemon.events_io") is not None


def _write_compat_fixture(pulse_id: str) -> tuple[Path, int, int]:
    """Write a fixture events.jsonl and return (path, window_size, after_offset).

    Layout (bytes):
      line 0 (head, >60 bytes): valid JSON that a window starting inside it tears
      line 1: valid JSON {"n": 1, "text": "alpha"}
      line 2: blank line ("\\n")
      line 3: bad JSON ("{bad json line")
      line 4: JSON string containing raw U+2028 ("\\u2028" unescaped in UTF-8)
      line 5: valid JSON {"n": 5, "text": "omega"}
    """
    p = paths.events_path(pulse_id)
    p.parent.mkdir(parents=True, exist_ok=True)
    head = json.dumps({"n": 0, "pad": "x" * 80}) + "\n"
    line1 = json.dumps({"n": 1, "text": "alpha"}) + "\n"
    blank = "\n"
    bad = "{bad json line\n"
    # Raw U+2028 inside a JSON string: splitlines() splits on U+2028 (tearing
    # the JSON line into two invalid fragments), whereas split("\n") keeps it
    # on one line so json.loads succeeds.
    u2028_line = '{"n": 4, "text": "before\u2028after"}\n'
    line5 = json.dumps({"n": 5, "text": "omega"}) + "\n"

    body = head + line1 + blank + bad + u2028_line + line5
    raw = body.encode("utf-8")
    p.write_bytes(raw)

    # Window starts 15 bytes into `head`, so the first line in the window is torn.
    window = len(raw) - 15
    after_head = len(head.encode("utf-8"))
    return p, window, after_head


def test_current_readers_on_compat_fixture(data_root, monkeypatch):  # noqa: F811
    pid = "compat-pulse"
    path, window, after_head = _write_compat_fixture(pid)

    # 1. events_io.read_tail_lines: split("\n"), drops torn first line, drops blank
    # lines, keeps U+2028 inside the single line.
    act_lines = events_io.read_tail_lines(path, window=window)
    assert act_lines == [
        '{"n": 1, "text": "alpha"}',
        "{bad json line",
        '{"n": 4, "text": "before\u2028after"}',
        '{"n": 5, "text": "omega"}',
    ]

    # 2. events_io.read_last_lines: same split("\n") + blank drop, last N lines.
    last_2 = events_io.read_last_lines(path, 2)
    assert last_2 == [
        '{"n": 4, "text": "before\u2028after"}',
        '{"n": 5, "text": "omega"}',
    ]

    # 3. events_io.read_lines_after: reads from `after_head`, split("\n"), drops blanks.
    after_lines = events_io.read_lines_after(path, after_head)
    assert after_lines == act_lines

    # 4. events_io.read_events_after: returns dict with lines and offset.
    cursor_dict = events_io.read_events_after(path, tail=10, after=None)
    assert cursor_dict["offset"] == path.stat().st_size
    assert cursor_dict["count"] == 5
    assert cursor_dict["events"][1:] == act_lines

    # 5. live._parse_event_lines: keeps bad JSON as {"_raw": line}, parses U+2028 line.
    live_parsed = live_mod._parse_event_lines(act_lines)
    assert live_parsed == [
        {"n": 1, "text": "alpha"},
        {"_raw": "{bad json line"},
        {"n": 4, "text": "before\u2028after"},
        {"n": 5, "text": "omega"},
    ]

    # 6. handoff._tail_events / _read_tail_events or events_io.tail_events:
    # uses splitlines(), so U+2028 splits the line into two bad-JSON fragments and is
    # skipped along with the torn first line, blank line, and "{bad json line".
    expected_splitlines_events = [
        {"n": 1, "text": "alpha"},
        {"n": 5, "text": "omega"},
    ]
    if hasattr(handoff_mod, "_read_tail_events"):
        assert handoff_mod._read_tail_events(pid, window) == expected_splitlines_events
    if hasattr(handoff_mod, "_tail_events"):
        monkeypatch.setattr(handoff_mod, "TAIL_BYTES", window)
        assert handoff_mod._tail_events(pid) == expected_splitlines_events


def test_events_io_matches_compat_fixture(data_root):  # noqa: F811
    if not _has_events_io():
        return
    from pulsar.daemon import events_io

    pid = "compat-pulse-io"
    path, window, after_head = _write_compat_fixture(pid)

    # split="newline" matches read_tail_lines
    nl_lines = events_io.tail_lines(path, window, split="newline")
    assert nl_lines == events_io.read_tail_lines(path, window=window)

    # split="lines" matches handoff's splitlines() behaviour
    sl_lines = events_io.tail_lines(path, window, split="lines")
    assert events_io.parse(sl_lines, keep_raw=False) == [
        {"n": 1, "text": "alpha"},
        {"n": 5, "text": "omega"},
    ]
    assert events_io.tail_events(pid, window, split="lines") == [
        {"n": 1, "text": "alpha"},
        {"n": 5, "text": "omega"},
    ]
    assert events_io.tail_events(pid, window, split="newline") == [
        {"n": 1, "text": "alpha"},
        {"n": 4, "text": "before\u2028after"},
        {"n": 5, "text": "omega"},
    ]

    # parse with keep_raw=True vs keep_raw=False
    assert events_io.parse(nl_lines, keep_raw=True) == [
        {"n": 1, "text": "alpha"},
        {"_raw": "{bad json line"},
        {"n": 4, "text": "before\u2028after"},
        {"n": 5, "text": "omega"},
    ]
    assert events_io.parse(nl_lines, keep_raw=False) == [
        {"n": 1, "text": "alpha"},
        {"n": 4, "text": "before\u2028after"},
        {"n": 5, "text": "omega"},
    ]

    # last_lines and lines_after
    assert events_io.last_lines(path, 2) == events_io.read_last_lines(path, 2)
    assert events_io.lines_after(path, after_head) == events_io.read_lines_after(
        path, after_head
    )


def test_event_cursor_and_size_compat(data_root):  # noqa: F811
    pid = "cursor-compat"
    path, _, after_head = _write_compat_fixture(pid)

    cursor_cls = getattr(handoff_mod, "_EventCursor", None)
    size_fn = getattr(handoff_mod, "_events_size", None)
    if _has_events_io():
        from pulsar.daemon import events_io
        cursor_cls = events_io.EventCursor
        size_fn = events_io.events_size

    assert size_fn is not None and cursor_cls is not None
    assert size_fn(pid) == path.stat().st_size
    assert size_fn("nonexistent-pulse") == 0

    cur = cursor_cls(pid, after_head)
    evs = cur.poll()
    # EventCursor splits on b"\n", so U+2028 stays intact and is parsed!
    assert evs == [
        {"n": 1, "text": "alpha"},
        {"n": 4, "text": "before\u2028after"},
        {"n": 5, "text": "omega"},
    ]
    assert len(cur.ends) == 3
    assert cur.ends[-1] == path.stat().st_size


def test_writers_ts_format_compat(data_root):  # noqa: F811
    pid = "writer-compat"
    ev_path = paths.events_path(pid)
    ev_path.parent.mkdir(parents=True, exist_ok=True)

    events_io.emit(pid, {"type": "pulse_stopped", "by": "user"})

    lines = [json.loads(line) for line in ev_path.read_text().splitlines()]
    assert len(lines) == 1
    assert lines[0]["type"] == "pulse_stopped"
    assert re.search(r"[+-]\d{4}$", lines[0]["ts"])
