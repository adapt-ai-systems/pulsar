"""Per-pulse events_io tests — tail-read, cursor, wire trimming, task-event scan.

    venv/bin/python -m pytest tests/test_events_io.py -q

Reuses the daemon fixtures (fake spawn/socket, tmp PULSAR_DATA) from
``test_daemon`` so real runners/systemd never run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from pulsar.daemon.events_io import TOOL_RESULT_BODY_MAX
from pulsar.daemon import events_io, paths

# Pull in the shared daemon fakes/fixtures.
from tests.test_daemon import (  # noqa: F401
    data_root, fake_spawn, fake_sock, store, client,
)


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
def _write_events(pulse_id: str, events: list[dict]) -> Path:
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    path = home / "events.jsonl"
    with path.open("w") as fh:
        for ev in events:
            fh.write(json.dumps(ev) + "\n")
    return path


# --------------------------------------------------------------------------- #
# Unit: tail-read is bounded (the load-bearing proof)
# --------------------------------------------------------------------------- #
def test_read_tail_bounded_on_huge_file(tmp_path):
    """A multi-MB events file is read via a bounded seek window, not whole.

    We instrument the read by measuring the byte window we ask for vs. the file
    size, and by confirming only the tail lines come back.
    """
    path = tmp_path / "events.jsonl"
    # Genuinely large file: pad each event so the file clears several MB, and
    # give each a unique marker so we can prove we got the TAIL not the head.
    pad = "z" * 300
    n = 12000
    with path.open("w") as fh:
        for i in range(n):
            fh.write(json.dumps(
                {"type": "assistant_text", "text": f"msg-{i}", "pad": pad}) + "\n")
    file_size = path.stat().st_size
    assert file_size > 1_000_000  # genuinely large (~4 MB)

    window = 64 * 1024
    lines = events_io.read_tail_lines(path, window=window)

    # Bytes touched are bounded by the window, NOT the file size.
    joined = "\n".join(lines)
    assert len(joined.encode()) <= window
    assert len(joined.encode()) < file_size  # << the file

    # The lines we got are the LAST ones, not the first (proves tail, not head).
    got = [json.loads(ln)["text"] for ln in lines]
    assert got[-1] == f"msg-{n - 1}"
    assert f"msg-0" not in got
    # First (partial) line was dropped: every returned line parses cleanly.
    for ln in lines:
        json.loads(ln)


def test_read_tail_small_file_reads_all(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        for i in range(3):
            fh.write(json.dumps({"type": "assistant_text", "n": i}) + "\n")
    lines = events_io.read_tail_lines(path)
    assert [json.loads(l)["n"] for l in lines] == [0, 1, 2]


def test_read_tail_missing_file_empty(tmp_path):
    assert events_io.read_tail_lines(tmp_path / "nope.jsonl") == []


# --------------------------------------------------------------------------- #
# Unit: read_last_lines — line-counted tail, still bounded (events endpoint)
# --------------------------------------------------------------------------- #
def test_read_last_lines_widens_past_one_window(tmp_path):
    """Asking for more lines than fit in 64 KiB widens the window — but the
    read stays bounded, and the lines are the LAST ones."""
    path = tmp_path / "events.jsonl"
    pad = "z" * 300
    n = 12000
    with path.open("w") as fh:
        for i in range(n):
            fh.write(json.dumps(
                {"type": "assistant_text", "text": f"msg-{i}", "pad": pad}) + "\n")
    file_size = path.stat().st_size
    assert file_size > 1_000_000

    lines = events_io.read_last_lines(path, 500)
    assert len(lines) == 500
    got = [json.loads(ln)["text"] for ln in lines]
    assert got[0] == f"msg-{n - 500}"
    assert got[-1] == f"msg-{n - 1}"
    # Still far less than the file: 500 * ~330 bytes, not 4 MB.
    assert len("\n".join(lines).encode()) < file_size // 2


def test_read_last_lines_stops_at_max_bytes(tmp_path):
    """A caller asking for more history than max_bytes covers gets what the
    cap allows — never an unbounded read."""
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        for i in range(5000):
            fh.write(json.dumps({"n": i}) + "\n")
    lines = events_io.read_last_lines(path, 5000, max_bytes=4096)
    assert 0 < len(lines) < 5000
    assert json.loads(lines[-1])["n"] == 4999


def test_read_last_lines_short_file_and_edges(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        for i in range(3):
            fh.write(json.dumps({"n": i}) + "\n")
    assert [json.loads(l)["n"]
            for l in events_io.read_last_lines(path, 100)] == [0, 1, 2]
    assert [json.loads(l)["n"]
            for l in events_io.read_last_lines(path, 2)] == [1, 2]
    assert events_io.read_last_lines(path, 0) == []
    assert events_io.read_last_lines(tmp_path / "nope.jsonl", 10) == []
    (tmp_path / "empty.jsonl").write_text("")
    assert events_io.read_last_lines(tmp_path / "empty.jsonl", 10) == []


def test_read_last_lines_keeps_torn_final_line(tmp_path):
    """The runner may be mid-append: the torn LAST line is handed back raw
    (the events route surfaces it), while a torn window HEAD is dropped."""
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n{"n": 3')
    lines = events_io.read_last_lines(path, 10)
    assert lines[-1] == '{"n": 3'


# --------------------------------------------------------------------------- #
# Events byte cursor (read_events_after + /events?after=)
# --------------------------------------------------------------------------- #
def test_cursor_no_after_is_plain_tail_plus_offset(tmp_path):
    """No cursor = today's behaviour (last ``tail`` lines), plus the offset."""
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    out = events_io.read_events_after(path, tail=2, after=None)
    assert [json.loads(x)["n"] for x in out["events"]] == [2, 3]
    assert out["count"] == 2
    assert out["offset"] == path.stat().st_size
    assert "unchanged" not in out


def test_cursor_unchanged_reads_no_file_body(tmp_path, monkeypatch):
    """after == size answers from the stat alone — the file body never opens.

    That is the entire point of the cursor: a pane polling a quiet pulse every
    two seconds must not re-decode and re-parse the tail window.
    """
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n')
    size = path.stat().st_size

    def _no_read(*a, **k):
        raise AssertionError("file body must not be read when nothing changed")

    monkeypatch.setattr(Path, "open", _no_read)
    out = events_io.read_events_after(path, tail=100, after=size)
    assert out == {"events": [], "count": 0, "offset": size, "unchanged": True}


def test_cursor_changed_returns_full_tail(tmp_path):
    """Something appended: the pane swaps its whole list for the response, so
    the changed case is the normal full tail, never just the delta."""
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n')
    after = path.stat().st_size
    with path.open("a") as fh:
        fh.write('{"n": 3}\n{"n": 4}\n')

    out = events_io.read_events_after(path, tail=100, after=after)
    assert [json.loads(x)["n"] for x in out["events"]] == [1, 2, 3, 4]
    assert out["count"] == 4
    assert out["offset"] == path.stat().st_size
    assert "unchanged" not in out


def test_cursor_changed_capped_by_tail_keeps_true_offset(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 0}\n')
    after = path.stat().st_size
    with path.open("a") as fh:
        for i in range(1, 11):
            fh.write(json.dumps({"n": i}) + "\n")

    out = events_io.read_events_after(path, tail=3, after=after)
    assert [json.loads(x)["n"] for x in out["events"]] == [8, 9, 10]
    assert out["offset"] == path.stat().st_size


def test_cursor_shrunken_file_falls_back_to_tail(tmp_path):
    """after > size (a reset / fork re-copy rewrote the log) — the cursor is
    worthless, so serve a normal tail instead of an empty or torn delta."""
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n{"n": 3}\n')
    out = events_io.read_events_after(path, tail=2, after=9999)
    assert [json.loads(x)["n"] for x in out["events"]] == [2, 3]
    assert out["offset"] == path.stat().st_size
    assert "unchanged" not in out


def test_cursor_missing_file(tmp_path):
    out = events_io.read_events_after(tmp_path / "nope.jsonl", 100, None)
    assert out == {"events": [], "count": 0, "offset": 0}


def test_read_lines_after_returns_only_the_gap(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n{"n": 2}\n')
    after = path.stat().st_size
    with path.open("a") as fh:
        fh.write('{"n": 3}\n{"n": 4}\n')
    lines = events_io.read_lines_after(path, after)
    assert [json.loads(x)["n"] for x in lines] == [3, 4]


def test_read_lines_after_quiet_is_empty(tmp_path):
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 1}\n')
    assert events_io.read_lines_after(path, path.stat().st_size) == []


def test_read_lines_after_oversize_gap_is_empty(tmp_path, monkeypatch):
    """A huge append must not be read as a delta — caller falls back to tail."""
    path = tmp_path / "events.jsonl"
    path.write_text('{"n": 0}\n')
    after = path.stat().st_size
    with path.open("a") as fh:
        fh.write('{"n": 1}\n')
    monkeypatch.setattr(events_io, "TAIL_MAX_BYTES", 1)
    assert events_io.read_lines_after(path, after, max_bytes=1) == []


def test_cursor_changed_read_is_bounded(tmp_path):
    """A changed poll on a multi-MB file is still a bounded tail read."""
    path = tmp_path / "events.jsonl"
    pad = "z" * 300
    with path.open("w") as fh:
        for i in range(8000):
            fh.write(json.dumps({"type": "assistant_text", "text": f"m{i}",
                                 "pad": pad}) + "\n")
    after = path.stat().st_size
    with path.open("a") as fh:
        fh.write(json.dumps({"type": "assistant_text", "text": "fresh"}) + "\n")

    out = events_io.read_events_after(path, tail=5, after=after)
    texts = [json.loads(x)["text"] for x in out["events"]]
    assert len(texts) == 5 and texts[-1] == "fresh"
    assert after < out["offset"] == path.stat().st_size


@pytest.mark.anyio
async def test_events_endpoint_returns_offset(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    path = _write_events(pid, [{"n": 1}, {"n": 2}])

    v = (await client.get(f"/pulses/{pid}/events")).json()["value"]
    assert [e["n"] for e in v["events"]] == [1, 2]
    assert v["offset"] == path.stat().st_size
    assert "unchanged" not in v


@pytest.mark.anyio
async def test_events_endpoint_unchanged_poll(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    path = _write_events(pid, [{"n": 1}])
    off = path.stat().st_size

    v = (await client.get(f"/pulses/{pid}/events?after={off}")).json()["value"]
    assert v == {"events": [], "count": 0, "offset": off, "unchanged": True}


@pytest.mark.anyio
async def test_events_endpoint_delta_then_unchanged(client, store, fake_spawn):
    """The poll loop a pane actually runs: full tail, changed, unchanged."""
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    path = _write_events(pid, [{"n": 1}])
    v1 = (await client.get(f"/pulses/{pid}/events")).json()["value"]
    assert v1["count"] == 1

    with path.open("a") as fh:
        fh.write(json.dumps({"n": 2}) + "\n")
    v2 = (await client.get(
        f"/pulses/{pid}/events?after={v1['offset']}")).json()["value"]
    assert [e["n"] for e in v2["events"]] == [1, 2]

    v3 = (await client.get(
        f"/pulses/{pid}/events?after={v2['offset']}")).json()["value"]
    assert v3["unchanged"] is True and v3["count"] == 0


@pytest.mark.anyio
async def test_events_endpoint_no_file_offset_zero(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    v = (await client.get(f"/pulses/{pid}/events?after=0")).json()["value"]
    assert v == {"events": [], "count": 0, "offset": 0}


@pytest.mark.anyio
async def test_events_endpoint_trims_tool_result_bodies(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    fat = "x" * 5000
    path = _write_events(pid, [
        {"type": "tool_call", "id": "c1", "name": "bash"},
        {"type": "tool_result", "id": "c1", "ok": True, "snippet": fat},
        {"type": "assistant_text", "text": "done"},
    ])
    on_disk = path.read_text()
    assert fat in on_disk

    v = (await client.get(f"/pulses/{pid}/events")).json()["value"]
    tr = next(e for e in v["events"] if e["type"] == "tool_result")
    assert tr["truncated"] is True
    assert tr["snippet"] == fat[:TOOL_RESULT_BODY_MAX]
    assert len(tr["snippet"]) == TOOL_RESULT_BODY_MAX
    # Durable log untouched.
    assert path.read_text() == on_disk

    # Tap-to-expand: existing endpoint, untrimmed match.
    one = (await client.get(f"/pulses/{pid}/events?event_id=c1")).json()["value"]
    ids = [e["type"] for e in one["events"]]
    assert "tool_result" in ids and "tool_call" in ids
    full = next(e for e in one["events"] if e["type"] == "tool_result")
    assert full["snippet"] == fat
    assert "truncated" not in full

    raw = (await client.get(f"/pulses/{pid}/events?full=1")).json()["value"]
    full2 = next(e for e in raw["events"] if e["type"] == "tool_result")
    assert full2["snippet"] == fat


@pytest.mark.anyio
async def test_events_trim_does_not_touch_short_or_non_results(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    _write_events(pid, [
        {"type": "tool_result", "id": "s", "ok": True, "snippet": "short"},
        {"type": "assistant_text", "text": "hi"},
    ])
    v = (await client.get(f"/pulses/{pid}/events")).json()["value"]
    tr = next(e for e in v["events"] if e["type"] == "tool_result")
    assert tr["snippet"] == "short"
    assert "truncated" not in tr
    assert v["events"][1]["text"] == "hi"


def test_trim_event_for_wire_unit():
    short = {"type": "tool_result", "id": "a", "snippet": "ok"}
    assert events_io.trim_event_for_wire(short) is short
    fat = {"type": "tool_result", "id": "b", "snippet": "z" * 500,
           "result": "z" * 500}
    out = events_io.trim_event_for_wire(fat)
    assert out["truncated"] is True
    assert out["snippet"] == "z" * TOOL_RESULT_BODY_MAX
    assert "result" not in out
    assert fat["snippet"] == "z" * 500  # input not mutated


@pytest.mark.anyio
async def test_events_endpoint_cursor_after_truncate_falls_back(
        client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    path = _write_events(pid, [{"n": i} for i in range(6)])
    good = path.stat().st_size
    path.write_text(json.dumps({"n": "fresh"}) + "\n")

    v = (await client.get(
        f"/pulses/{pid}/events?after={good}")).json()["value"]
    assert [e["n"] for e in v["events"]] == ["fresh"]
    assert "unchanged" not in v


# --------------------------------------------------------------------------- #
# task events: resumable scan (2026-11-02 — a 59 MB log froze the daemon 5 s
# per pane open)
# --------------------------------------------------------------------------- #

def _jl(fh, ev):
    fh.write(json.dumps(ev) + "\n")


def test_task_events_resume_reads_only_appended_lines(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        _jl(fh, {"type": "session", "session_id": "s1", "ts": "S1"})
        _jl(fh, {"type": "tool_call", "name": "TaskCreate", "id": "c1", "ts": "T1",
                 "args": {"subject": "one"}})
    first = events_io.read_task_events(path)
    assert [e["type"] for e in first] == ["session", "tool_call"]
    scan = events_io._task_scans[str(path)]
    assert scan.offset == path.stat().st_size

    # The result arrives later, then a line still being written (no newline).
    with path.open("a") as fh:
        _jl(fh, {"type": "tool_result", "ok": True, "snippet": "Task #1 created", "id": "c1"})
        fh.write('{"type": "tool_call", "name": "TaskUpdate", "ts": "T2", "args"')
    second = events_io.read_task_events(path)
    assert [e["type"] for e in second] == ["session", "tool_call", "tool_result"]
    assert first == second[:2]

    # The torn line was not consumed: once whole, it is read.
    with path.open("a") as fh:
        fh.write(': {"taskId": "1", "status": "completed"}}\n')
    third = events_io.read_task_events(path)
    assert [e.get("name") for e in third] == [None, "TaskCreate", None, "TaskUpdate"]
    assert scan.offset == path.stat().st_size


def test_task_events_rewritten_log_rescans(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        _jl(fh, {"type": "tool_call", "name": "todo_write", "ts": "OLD", "args": {}})
    assert [e["ts"] for e in events_io.read_task_events(path)] == ["OLD"]
    # Same inode, rewritten longer: the bytes before the resume point changed.
    with path.open("w") as fh:
        _jl(fh, {"type": "tool_call", "name": "update_plan", "ts": "NEW1", "args": {}})
        _jl(fh, {"type": "tool_call", "name": "update_plan", "ts": "NEW2", "args": {}})
    assert [e["ts"] for e in events_io.read_task_events(path)] == ["NEW1", "NEW2"]
    # Shrunk below the resume point.
    with path.open("w") as fh:
        _jl(fh, {"type": "tool_call", "name": "TodoWrite", "ts": "S", "args": {}})
    assert [e["ts"] for e in events_io.read_task_events(path)] == ["S"]


def test_task_events_prefilter_matches_keys_not_text(tmp_path):
    """A result is kept by its own "id", wherever the key sits on the line —
    never because its snippet mentions a TaskCreate id or a task tool."""
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        _jl(fh, {"type": "tool_call", "name": "TaskCreate", "id": "c1", "args": {}})
        _jl(fh, {"type": "tool_result", "id": "zz", "snippet": 'echo "id": "c1" TaskCreate'})
        _jl(fh, {"type": "tool_call", "name": "Bash", "args": {"command": "TaskUpdate"}})
        _jl(fh, {"type": "tool_result", "id": "c1", "ok": True, "snippet": "Task #1 created"})
        _jl(fh, {"type": "tool_call", "name": "taskupdate", "args": {"taskId": "1"}})
        _jl(fh, {"type": "tool_call", "name": "TaskCreate", "id": 7, "args": {}})
        _jl(fh, {"type": "tool_result", "snippet": "Task #2 created", "id": 7})
    evs = events_io.read_task_events(path)
    assert [(e["type"], e.get("id")) for e in evs] == [
        ("tool_call", "c1"), ("tool_result", "c1"), ("tool_call", None),
        ("tool_call", 7), ("tool_result", 7)]


def test_task_events_cap_keeps_newest(tmp_path):
    path = tmp_path / "events.jsonl"
    with path.open("w") as fh:
        for i in range(25):
            _jl(fh, {"type": "tool_call", "name": "todo_write", "n": i, "args": {}})
    assert [e["n"] for e in events_io.read_task_events(path, max_keep=10)] == list(range(15, 25))
    with path.open("a") as fh:
        _jl(fh, {"type": "tool_call", "name": "todo_write", "n": 25, "args": {}})
    assert [e["n"] for e in events_io.read_task_events(path, max_keep=3)] == [23, 24, 25]
    assert len(events_io.read_task_events(path)) == 26
