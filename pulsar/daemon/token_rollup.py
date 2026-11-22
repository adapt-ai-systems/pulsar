"""Token usage rollup for the ``/usage/tokens`` endpoint.

Scans every pulse's ``events.jsonl`` for ``usage`` events
(``{"in","out","ctx","ts"}``) and attributes each event to the model that
was active in that pulse at the time, read from ``runner.log`` launch
lines (pulses can switch models over their lifetime; the last launch at
or before the event byte-offset wins, falling back to the model in the
daemon DB). Results are cached 60s per (since, until) window.

The standalone CLI lives at ops/token_usage.py (same output shape).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sqlite3
import threading
import time
from typing import Any

from pulsar import models as models_mod
from pulsar.daemon import paths

MODELS_JSON = models_mod.REGISTRY_PATH
CACHE_TTL_SECONDS = 60.0

_MODEL_RE = re.compile(rb"model=([A-Za-z0-9._+-]+)")

_lock = threading.Lock()
_cache: dict[tuple[str, str], tuple[float, dict[str, Any]]] = {}


def _db_models() -> dict[str, str]:
    db_path = paths.data_root() / "pulsar.db"
    try:
        con = sqlite3.connect("file:%s?mode=ro" % db_path, uri=True)
        try:
            return dict(con.execute("SELECT id, model FROM pulses"))
        finally:
            con.close()
    except Exception:
        return {}


def _labels() -> dict[str, str]:
    try:
        with open(MODELS_JSON) as f:
            d = json.load(f)
        return {m["id"]: m.get("label", m["id"]) for m in d.get("models", [])}
    except Exception:
        return {}


def _model_timeline(pulse_dir: str) -> list[tuple[int, str]]:
    out: list[tuple[int, str]] = []
    try:
        with open(os.path.join(pulse_dir, "runner.log"), "rb") as f:
            pos = 0
            for line in f:
                m = _MODEL_RE.search(line[:400])
                if m:
                    out.append((pos, m.group(1).decode()))
                pos += len(line)
    except OSError:
        pass
    return out


def scan(since: dt.date, until: dt.date) -> dict[str, Any]:
    """Aggregate usage events for [since, until)."""
    stamps = _db_models()
    labels = _labels()
    totals = {"in": 0, "out": 0}
    by_model: dict[str, dict[str, int]] = {}
    series: dict[str, dict[str, list[int]]] = {}
    pulse_count = 0

    base = paths.data_root() / "pulses"
    try:
        pids = sorted(os.listdir(base))
    except OSError:
        pids = []
    for pid in pids:
        pulse_dir = os.path.join(base, pid)
        ev = os.path.join(pulse_dir, "events.jsonl")
        if not os.path.isfile(ev):
            continue
        timeline = _model_timeline(pulse_dir)
        fallback = stamps.get(pid) or ""
        ti = -1
        saw = False
        try:
            with open(ev, "rb") as f:
                pos = 0
                for line in f:
                    if b'"usage"' in line:
                        for idx, (p, _) in enumerate(timeline):
                            if p <= pos:
                                ti = idx
                            else:
                                break
                        model = timeline[ti][1] if ti >= 0 else (fallback or "unknown")
                        try:
                            e = json.loads(line)
                        except ValueError:
                            pos += len(line)
                            continue
                        ts = (e.get("ts") or "")[:10]
                        if len(ts) == 10:
                            try:
                                day = dt.date.fromisoformat(ts)
                            except ValueError:
                                day = None
                            if day is not None and since <= day < until:
                                din = int(e.get("in") or 0)
                                dout = int(e.get("out") or 0)
                                totals["in"] += din
                                totals["out"] += dout
                                slot = by_model.setdefault(model, {"in": 0, "out": 0})
                                slot["in"] += din
                                slot["out"] += dout
                                s = series.setdefault(model, {})
                                cell = s.setdefault(ts, [0, 0])
                                cell[0] += din
                                cell[1] += dout
                                saw = True
                    pos += len(line)
        except OSError:
            continue
        if saw:
            pulse_count += 1

    return {
        "since": since.isoformat(),
        "until": (until - dt.timedelta(days=1)).isoformat(),
        "totals": totals,
        "pulse_count": pulse_count,
        "labels": {k: labels.get(k, k) for k in by_model},
        "by_model": dict(sorted(
            by_model.items(), key=lambda kv: -(kv[1]["in"] + kv[1]["out"]))),
        "series": {m: sorted(s.items()) for m, s in series.items()},
    }


def get_window(since: dt.date, until: dt.date) -> dict[str, Any]:
    key = (since.isoformat(), until.isoformat())
    now = time.monotonic()
    with _lock:
        hit = _cache.get(key)
        if hit and now < hit[0]:
            payload = dict(hit[1])
            payload["cached"] = True
            return payload
    payload = scan(since, until)
    payload["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    with _lock:
        if len(_cache) > 32:
            _cache.clear()
        _cache[key] = (now + CACHE_TTL_SECONDS, payload)
    return payload
