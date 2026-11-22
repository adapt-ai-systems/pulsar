"""forced_seed: the last ~N tokens of transcript + the file path."""
import json

from pulsar.daemon import handoff as handoff_mod
from pulsar.daemon import paths


def _write(pid, events):
    f = paths.events_path(pid)
    f.parent.mkdir(parents=True, exist_ok=True)
    with f.open("w") as fh:
        for e in events:
            fh.write(json.dumps(e) + "\n")


def test_forced_seed_keeps_newest_within_budget(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    pid = "p1"
    events = [{"type": "user_message", "text": f"msg {i} " + "x" * 200, "ts": "2026-09-11T10:00:00+0000"}
              for i in range(50)]
    events.append({"type": "tool_call", "name": "bash", "args": {"command": "ls"}})
    events.append({"type": "tool_result", "ok": True, "snippet": "a b c"})
    events.append({"type": "assistant_text", "text": "LAST REPLY"})
    _write(pid, events)
    seed = handoff_mod.forced_seed(pid, "cli died", budget_tokens=500)  # 2000 chars
    assert seed.startswith("[pulsar handoff — FORCED]")
    assert "(cli died)" in seed
    assert str(paths.events_path(pid)) in seed
    assert "LAST REPLY" in seed and "TOOL bash" in seed and "RESULT: a b c" in seed
    assert "msg 0 " not in seed  # oldest trimmed
    tail = seed.split("===== transcript tail =====\n", 1)[1]
    assert len(tail) <= 2000


def test_forced_seed_without_transcript(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    seed = handoff_mod.forced_seed("nope")
    assert "(no transcript found)" in seed
