"""Tests for pulsar.daemon.token_rollup (respects PULSAR_DATA)."""
from __future__ import annotations

import datetime as dt
import json

from pulsar.daemon import paths, token_rollup
from pulsar.store import Store


def test_token_rollup_respects_pulsar_data(tmp_path, monkeypatch):
    """token_rollup reads only from paths.data_root() (PULSAR_DATA), never the
    hard-coded production directory."""
    data_dir = tmp_path / "custom_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PULSAR_DATA", str(data_dir))
    token_rollup._cache.clear()

    store = Store(paths.data_root() / "pulsar.db")
    try:
        pulse = store.create_pulse(
            title="Rollup test",
            model="claude-sonnet-4-6",
            vessel="claude-code",
        )
        pulse_db_fallback = store.create_pulse(
            title="DB fallback test",
            model="claude-sonnet-4-6",
            vessel="claude-code",
        )
    finally:
        store.close()

    pdir = paths.pulse_home(pulse.id)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "runner.log").write_text(
        "2026-10-10T10:00:00Z spawn model=claude-opus-4-6 vessel=claude-code\n",
        encoding="utf-8",
    )
    events = [
        {"kind": "usage", "in": 120, "out": 30, "ctx": 150, "ts": "2026-10-10T12:00:00+00:00"},
        {"kind": "usage", "in": 80, "out": 20, "ctx": 250, "ts": "2026-10-11T12:00:00+00:00"},
    ]
    (pdir / "events.jsonl").write_text(
        "".join(json.dumps(e) + "\n" for e in events),
        encoding="utf-8",
    )

    pdir2 = paths.pulse_home(pulse_db_fallback.id)
    pdir2.mkdir(parents=True, exist_ok=True)
    (pdir2 / "events.jsonl").write_text(
        json.dumps({"kind": "usage", "in": 50, "out": 10, "ctx": 60, "ts": "2026-10-10T15:00:00+00:00"}) + "\n",
        encoding="utf-8",
    )

    since = dt.date(2026, 10, 10)
    until = dt.date(2026, 10, 12)
    res = token_rollup.get_window(since, until)

    assert res["pulse_count"] == 2
    assert res["totals"] == {"in": 250, "out": 60}
    assert set(res["by_model"].keys()) == {"claude-opus-4-6", "claude-sonnet-4-6"}
    assert res["by_model"]["claude-opus-4-6"] == {"in": 200, "out": 50}
    assert res["by_model"]["claude-sonnet-4-6"] == {"in": 50, "out": 10}
    assert res["series"]["claude-opus-4-6"] == [
        ("2026-10-10", [120, 30]),
        ("2026-10-11", [80, 20]),
    ]
    assert res["series"]["claude-sonnet-4-6"] == [
        ("2026-10-10", [50, 10]),
    ]
    assert "claude-opus-4-6" in res["labels"]
    token_rollup._cache.clear()


def test_token_usage_cli_delegates_to_token_rollup_and_skips_bad_ts(tmp_path, monkeypatch, capsys):
    """ops/token_usage.py delegates to token_rollup.scan and does not crash on
    malformed 10-char timestamps (B33)."""
    import ops.token_usage as cli_mod

    data_dir = tmp_path / "cli_data"
    pdir = data_dir / "pulses" / "p1"
    pdir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("PULSAR_DATA", str(data_dir))
    (pdir / "events.jsonl").write_text(
        json.dumps({"kind": "usage", "in": 10, "out": 5, "ts": "not-a-date-string"}) + "\n"
        + json.dumps({"kind": "usage", "in": 40, "out": 15, "ts": "2026-10-10T12:00:00Z"}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr("sys.argv", ["token_usage.py", "--since", "2026-10-10", "--until", "2026-10-10", "--json"])
    cli_mod.main()
    out = json.loads(capsys.readouterr().out)
    assert out["pulse_count"] == 1
    assert out["totals"] == {"in": 40, "out": 15}

