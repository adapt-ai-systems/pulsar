"""FUEL /usage endpoint tests.

    venv/bin/python -m pytest tests -k usage -q

Contract through the ASGI app with the collectors monkeypatched — the real
ones talk to the Anthropic OAuth endpoint / scan ~/.codex / hit the Grok
billing API, none of which belongs in a unit test. Normalization itself is
exercised on the ported module directly (Claude payload → windows; Codex
frame → merged snapshot; Grok billing → windows).
"""

from __future__ import annotations

import asyncio
import datetime
import json
from pathlib import Path

import httpx
import pytest

from pulsar.daemon import usage_limits

# Shared daemon fakes/fixtures (tmp PULSAR_DATA, fake spawn/socket).
from tests.test_daemon import (  # noqa: F401
    data_root, fake_spawn, fake_sock, store, client,
)


def _snap(vendor, pct):
    return {
        "vendor": vendor,
        "plan": "max",
        "windows": [{"id": "5h", "label": "5h", "used_percent": pct,
                     "resets_at": "2026-07-27T08:00:00Z", "severity": "normal"}],
        "fetched_at": "2026-07-27T02:00:00Z",
        "stale_since": None,
        "stale_reason": None,
    }


@pytest.mark.anyio
async def test_usage_endpoint_returns_all_vendors(client, monkeypatch):
    monkeypatch.setattr(usage_limits, "poll_claude_if_due", lambda: _snap("claude", 41))
    monkeypatch.setattr(usage_limits, "poll_codex_if_due", lambda: _snap("codex", 7))
    monkeypatch.setattr(usage_limits, "poll_grok_if_due", lambda: _snap("grok", 3))
    r = await client.get("/usage")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    vendors = body["value"]["vendors"]
    assert vendors["claude"]["windows"][0]["used_percent"] == 41
    assert vendors["codex"]["windows"][0]["used_percent"] == 7
    assert vendors["grok"]["windows"][0]["used_percent"] == 3


@pytest.mark.anyio
async def test_usage_endpoint_survives_stale_sources(client, monkeypatch):
    """A dead source yields a stale snapshot in the envelope — never a 500."""
    store_ = usage_limits.UsageLimitStore()
    stale = store_.mark_stale("claude", "credentials_unavailable")
    monkeypatch.setattr(usage_limits, "poll_claude_if_due", lambda: stale)
    monkeypatch.setattr(usage_limits, "poll_codex_if_due", lambda: _snap("codex", 12))
    monkeypatch.setattr(usage_limits, "poll_grok_if_due", lambda: _snap("grok", 4))
    r = await client.get("/usage")
    assert r.status_code == 200
    vendors = r.json()["value"]["vendors"]
    assert vendors["claude"]["stale_reason"] == "credentials_unavailable"
    assert vendors["claude"]["windows"] == []
    assert vendors["grok"]["windows"][0]["used_percent"] == 4



@pytest.mark.anyio
async def test_usage_serves_last_snapshot_and_refreshes_behind(client, monkeypatch):
    """Only the first call waits on the vendors (2026-11-02: nearly every
    60 s rail poll paid a 0.5-6.7 s refresh). A due call answers with the
    last snapshot at once; the refresh lands for the next call."""
    calls = []

    def claude():
        calls.append(1)
        return _snap("claude", len(calls))

    monkeypatch.setattr(usage_limits, "poll_claude_if_due", claude)
    monkeypatch.setattr(usage_limits, "poll_codex_if_due", lambda: _snap("codex", 1))
    monkeypatch.setattr(usage_limits, "poll_grok_if_due", lambda: _snap("grok", 1))
    monkeypatch.setattr(usage_limits, "poll_gemini_if_due", lambda: _snap("gemini", 1))

    async def pct():
        r = await client.get("/usage")
        return r.json()["value"]["vendors"]["claude"]["windows"][0]["used_percent"]

    assert await pct() == 1
    assert await pct() == 1 and len(calls) == 1  # fresh: no vendor call
    monkeypatch.setattr(usage_limits, "CACHE_TTL_SECONDS", 0.0)
    assert await pct() == 1  # due: the last snapshot, at once
    for _ in range(100):
        if len(calls) == 2:
            break
        await asyncio.sleep(0.01)
    assert len(calls) == 2
    monkeypatch.setattr(usage_limits, "CACHE_TTL_SECONDS", 60.0)
    assert await pct() == 2

def test_claude_normalization_maps_limits_to_windows():
    """Mirror of the LIVE 2026-07-27 payload shape: two `weekly` groups
    (all-models + model-scoped) must yield UNIQUE ids and readable labels."""
    payload = {
        "plan_type": "max",
        "limits": [
            {"group": "session", "kind": "session", "scope": None, "percent": 30,
             "resets_at": "2026-07-27T05:00:00Z"},
            {"group": "weekly", "kind": "weekly_all", "scope": None, "percent": 6,
             "resets_at": "2026-07-31T13:00:00Z"},
            {"group": "weekly", "kind": "weekly_scoped", "percent": 91,
             "scope": {"model": {"id": None, "display_name": "Fable"}},
             "resets_at": "2026-07-31T13:00:00Z"},
            {"group": "bad", "percent": None, "resets_at": ""},  # dropped
        ],
    }
    snap = usage_limits._normalize_claude_snapshot(payload)
    assert snap["plan"] == "max"
    ids = [w["id"] for w in snap["windows"]]
    assert ids == ["session", "weekly_all", "weekly_scoped"]
    assert len(ids) == len(set(ids))  # Svelte keyed-each safety
    assert [w["label"] for w in snap["windows"]] == [
        "session", "weekly", "weekly · Fable"]
    assert snap["windows"][2]["severity"] == "critical"


def test_codex_frame_merges_primary_and_secondary():
    store_ = usage_limits.UsageLimitStore()
    snap = store_.record_codex_rate_limits({
        "plan_type": "pro",
        "primary": {"window_minutes": 300, "used_percent": 22,
                     "resets_at": 1_790_000_000},
        "secondary": {"window_minutes": 10_080, "used_percent": 48,
                       "resets_at": 1_790_500_000},
    })
    assert snap["plan"] == "pro"
    assert [w["id"] for w in snap["windows"]] == ["5h", "weekly"]


def test_grok_normalization_maps_billing_to_windows():
    """Live 2026-08-13 payload: weekly pool + product breakdown, unique ids."""
    payload = {
        "config": {
            "currentPeriod": {
                "type": "USAGE_PERIOD_TYPE_WEEKLY",
                "start": "2026-08-13T03:40:32.545974+00:00",
                "end": "2026-08-20T03:40:32.545974+00:00",
            },
            "creditUsagePercent": 1.0,
            "productUsage": [
                {"product": "GrokBuild", "usagePercent": 1.0},
                {"product": "weekly", "usagePercent": 9.0},  # id collision
            ],
            "billingPeriodEnd": "2026-08-20T03:40:32.545974+00:00",
        }
    }
    snap = usage_limits._normalize_grok_snapshot(payload, plan="GrokPro")
    assert snap["vendor"] == "grok"
    assert snap["plan"] == "GrokPro"
    ids = [w["id"] for w in snap["windows"]]
    assert ids == ["weekly", "GrokBuild", "weekly+"]
    assert len(ids) == len(set(ids))
    assert snap["windows"][0]["used_percent"] == 1.0
    assert snap["windows"][0]["resets_at"] == "2026-08-20T03:40:32.545974Z"
    assert snap["windows"][1]["label"] == "GrokBuild"


def test_grok_collector_marks_stale_without_credentials(tmp_path):
    store_ = usage_limits.UsageLimitStore()
    collector = usage_limits.GrokUsageCollector(
        auth_path=tmp_path / "missing-auth.json",
        store=store_,
        ttl_seconds=60,
    )
    snap = collector.poll()
    assert snap["vendor"] == "grok"
    assert snap["stale_reason"] == "credentials_unavailable"
    assert snap["windows"] == []


def _write_rollout(path: Path, used_percent: int) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    record = {
        "timestamp": "2026-09-04T00:00:00Z",
        "payload": {
            "rate_limits": {
                "plan_type": "pro",
                "primary": {
                    "window_minutes": 300,
                    "used_percent": used_percent,
                    "resets_at": 1_790_000_000,
                },
            }
        },
    }
    path.write_text(json.dumps(record) + "\n")


def test_codex_jsonl_fallback_scans_today_and_yesterday_only(tmp_path):
    now = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=usage_limits.UTC)
    today = tmp_path / "2026" / "09" / "04" / "rollout-today.jsonl"
    yest = tmp_path / "2026" / "09" / "03" / "rollout-yest.jsonl"
    old = tmp_path / "2026" / "08" / "01" / "rollout-old.jsonl"
    _write_rollout(today, 11)
    _write_rollout(yest, 22)
    _write_rollout(old, 99)
    dirs = usage_limits._codex_recent_session_dirs(tmp_path, now=now)
    assert [d.name for d in dirs] == ["04", "03"]
    frame = usage_limits.read_codex_jsonl_fallback(tmp_path, now=now)
    assert frame is not None
    assert frame.rate_limits["primary"]["used_percent"] != 99
    # Newest mtime wins among the two recent files; old dir is never opened.
    assert frame.rate_limits["primary"]["used_percent"] in (11, 22)


def test_codex_jsonl_fallback_ignores_old_tree_when_recent_empty(tmp_path):
    now = datetime.datetime(2026, 9, 4, 12, 0, tzinfo=usage_limits.UTC)
    old = tmp_path / "2025" / "01" / "01" / "rollout-old.jsonl"
    _write_rollout(old, 77)
    assert usage_limits.read_codex_jsonl_fallback(tmp_path, now=now) is None


def test_codex_collector_reads_live_per_pulse_rollout(tmp_path, monkeypatch):
    """Clean pulses write under CODEX_HOME, not the daemon's ~/.codex."""
    now = datetime.datetime.now(usage_limits.UTC)
    day = now.strftime("%Y/%m/%d")
    pulse_homes = tmp_path / "pulses"
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    rollout = (pulse_homes / "pulse-a" / "codex-home" / "sessions" / day
               / "rollout-live.jsonl")
    rollout.parent.mkdir(parents=True)
    rollout.write_text(json.dumps({
        "timestamp": now.isoformat(),
        "type": "event_msg",
        "payload": {"type": "token_count", "rate_limits": {
            "plan_type": "pro",
            "primary": {"window_minutes": 10_080, "used_percent": 32,
                        "resets_at": int(now.timestamp()) + 86_400},
        }},
    }) + "\n")
    snap = usage_limits.CodexUsageCollector(
        sessions_root=tmp_path / "empty-ambient",
        store=usage_limits.UsageLimitStore(),
        now_fn=lambda: now,
    ).poll()
    assert snap["plan"] == "pro"
    assert snap["stale_reason"] is None
    assert [(w["id"], w["used_percent"]) for w in snap["windows"]] == [
        ("weekly", 32),
    ]


def test_claude_collector_marks_stale_without_credentials(tmp_path):
    from pulsar.daemon.usage_limits import ClaudeUsageCollector, UsageLimitStore

    snap = ClaudeUsageCollector(
        credentials_path=tmp_path / "missing.json",
        store=UsageLimitStore(),
    ).poll()
    assert snap["stale_reason"] == "credentials_unavailable"


def test_normalize_gemini_snapshot_orders_and_converts():
    from pulsar.daemon.usage_limits import _normalize_gemini_snapshot

    payload = {"groups": [
        {"buckets": [
            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 1},
            {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.5},
        ]},
        {"buckets": [
            {"bucketId": "gemini-weekly", "window": "weekly", "remainingFraction": 0.75,
             "resetTime": "2026-10-20T17:43:12Z"},
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.1},
        ]},
    ]}
    snap = _normalize_gemini_snapshot(payload, plan="Google AI Ultra")
    assert [w["id"] for w in snap["windows"]] == ["gemini-5h", "gemini-weekly", "3p-5h", "3p-weekly"]
    assert snap["windows"][0]["used_percent"] == 90.0
    assert snap["windows"][1]["used_percent"] == 25.0
    assert snap["windows"][2]["label"] == "claude/gpt 5h"
    assert snap["plan"] == "Google AI Ultra"


def test_gemini_collector_does_not_invent_missing_argon_windows(monkeypatch):
    """The live quota summary may contain only third-party buckets."""
    collector = usage_limits.GeminiUsageCollector(store=usage_limits.UsageLimitStore())
    monkeypatch.setattr(collector, "_token", lambda _now: "test-token")
    def post(method, _token, _body):
        if method == "loadCodeAssist":
            return {"cloudaicompanionProject": "test", "paidTier": {"name": "Ultra"}}
        if method == "retrieveUserQuota":
            return {"buckets": [{"modelId": "claude-opus-5-5-high", "remainingFraction": 1,
                                 "resetTime": "2026-11-10T22:59:55Z"}]}
        assert method == "retrieveUserQuotaSummary"
        return {"groups": [{"buckets": [
            {"bucketId": "3p-weekly", "window": "weekly", "remainingFraction": 0.05},
            {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.2},
        ]}]}
    monkeypatch.setattr(collector, "_post", post)
    snap = collector.poll()
    assert snap["stale_reason"] is None
    assert [w["id"] for w in snap["windows"]] == ["3p-5h", "3p-weekly"]


def test_gemini_collector_adds_pool_window_from_per_model_quota(monkeypatch):
    """Summary lacks Gemini; retrieveUserQuota's gemini-* buckets give the 5h pool."""
    collector = usage_limits.GeminiUsageCollector(store=usage_limits.UsageLimitStore())
    monkeypatch.setattr(collector, "_token", lambda _now: "test-token")
    def post(method, _token, _body):
        if method == "loadCodeAssist":
            return {"cloudaicompanionProject": "test", "paidTier": {"name": "Ultra"}}
        if method == "retrieveUserQuota":
            return {"buckets": [
                {"modelId": "chat_20706", "remainingFraction": 0.1},
                {"modelId": "claude-opus-5-5-high", "remainingFraction": 0.2,
                 "resetTime": "2026-11-10T22:59:55Z"},
                {"modelId": "gemini-3.8-flash-tiered", "remainingFraction": 0.75,
                 "resetTime": "2026-11-10T22:59:34Z"},
                {"modelId": "gemini-2.5-pro", "remainingFraction": 0.75,
                 "resetTime": "2026-11-10T22:59:34Z"},
            ]}
        return {"groups": [{"buckets": [
            {"bucketId": "3p-5h", "window": "5h", "remainingFraction": 0.5},
        ]}]}
    monkeypatch.setattr(collector, "_post", post)
    snap = collector.poll()
    assert [w["id"] for w in snap["windows"]] == ["gemini-5h", "3p-5h"]
    assert snap["windows"][0]["used_percent"] == 25.0
    assert snap["windows"][0]["label"] == "gemini 5h"


@pytest.mark.parametrize(
    ("cid", "csec"),
    [(None, None), ("client-id-only", None), (None, "client-secret-only")],
)
def test_gemini_collector_skips_refresh_when_oauth_env_unset(
    tmp_path, monkeypatch, cid, csec
):
    """When either OAuth env var is unset, expired tokens do not call the
    refresh endpoint and poll() reports the same stale result as a failed refresh."""
    monkeypatch.delenv("PULSAR_AGY_CLIENT_ID", raising=False)
    monkeypatch.delenv("PULSAR_AGY_CLIENT_SECRET", raising=False)
    if cid is not None:
        monkeypatch.setenv("PULSAR_AGY_CLIENT_ID", cid)
    if csec is not None:
        monkeypatch.setenv("PULSAR_AGY_CLIENT_SECRET", csec)

    now = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=usage_limits.UTC)
    tok_file = tmp_path / "antigravity-oauth-token"
    tok_file.write_text(json.dumps({
        "token": {
            "access_token": "expired-token",
            "refresh_token": "refresh-tok",
            "expiry": "2026-11-10T11:00:00.000000+00:00",
        }
    }))

    def fail_opener(*_a, **_k):
        raise AssertionError("opener must not be called when OAuth env is incomplete")

    collector = usage_limits.GeminiUsageCollector(
        token_path=tok_file,
        store=usage_limits.UsageLimitStore(),
        opener=fail_opener,
        now_fn=lambda: now,
    )
    snap = collector.poll()
    assert snap["vendor"] == "gemini"
    assert snap["stale_reason"] == "auth_expired"
    assert snap["windows"] == []


def test_gemini_collector_refreshes_with_env_credentials(tmp_path, monkeypatch):
    """When both PULSAR_AGY_CLIENT_ID and PULSAR_AGY_CLIENT_SECRET are set,
    an expired token refreshes using those values."""
    import io
    import urllib.parse

    monkeypatch.setenv("PULSAR_AGY_CLIENT_ID", "test-agy-id")
    monkeypatch.setenv("PULSAR_AGY_CLIENT_SECRET", "test-agy-secret")

    now = datetime.datetime(2026, 11, 10, 12, 0, tzinfo=usage_limits.UTC)
    tok_file = tmp_path / "antigravity-oauth-token"
    tok_file.write_text(json.dumps({
        "token": {
            "access_token": "expired-token",
            "refresh_token": "refresh-tok",
            "expiry": "2026-11-10T11:00:00.000000+00:00",
        }
    }))

    seen_refresh = {}

    class _Resp(io.BytesIO):
        def __enter__(self):
            return self
        def __exit__(self, *_a):
            return False

    def fake_opener(req, timeout=None):
        if req.full_url == "https://oauth2.googleapis.com/token":
            parsed = urllib.parse.parse_qs(req.data.decode())
            seen_refresh.update({k: v[0] for k, v in parsed.items()})
            return _Resp(json.dumps({"access_token": "fresh-tok", "expires_in": 3600}).encode())
        assert req.headers.get("Authorization") == "Bearer fresh-tok"
        if req.full_url.endswith(":loadCodeAssist"):
            return _Resp(json.dumps({"cloudaicompanionProject": "proj", "paidTier": {"name": "Ultra"}}).encode())
        return _Resp(json.dumps({"groups": [{"buckets": [
            {"bucketId": "gemini-5h", "window": "5h", "remainingFraction": 0.6},
        ]}]}).encode())

    collector = usage_limits.GeminiUsageCollector(
        token_path=tok_file,
        store=usage_limits.UsageLimitStore(),
        opener=fake_opener,
        now_fn=lambda: now,
    )
    snap = collector.poll()
    assert seen_refresh == {
        "client_id": "test-agy-id",
        "client_secret": "test-agy-secret",
        "grant_type": "refresh_token",
        "refresh_token": "refresh-tok",
    }
    assert snap["stale_reason"] is None
    assert [(w["id"], w["used_percent"]) for w in snap["windows"]] == [("gemini-5h", 40.0)]
