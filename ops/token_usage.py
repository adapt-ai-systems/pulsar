#!/usr/bin/env python3
"""CLI wrapper over ``pulsar.daemon.token_rollup.scan``."""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from pulsar.daemon.token_rollup import scan


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--since")
    ap.add_argument("--until")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    today = dt.date.today()
    since = (dt.date.fromisoformat(a.since) if a.since
             else today - dt.timedelta(days=a.days - 1))
    until = ((dt.date.fromisoformat(a.until) + dt.timedelta(days=1)) if a.until
             else today + dt.timedelta(days=1))
    out = scan(since, until)
    out["updated_at"] = dt.datetime.now(dt.timezone.utc).isoformat(
        timespec="seconds")
    print(json.dumps(out))


if __name__ == "__main__":
    main()
