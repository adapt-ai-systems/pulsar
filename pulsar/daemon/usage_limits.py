"""Collect and normalize Claude, Codex, Grok, and Gemini usage-limit snapshots (FUEL rail).

Claude: the Anthropic OAuth usage endpoint via ~/.claude/.credentials.json
(read-only, token never leaves this host). Codex: newest rate-limit frame
across ambient and per-pulse rollouts. Grok: the cli-chat-proxy billing
endpoint via ~/.grok/auth.json (same read-only token rule). Gemini: the
Antigravity quota summary via
~/.gemini/antigravity-cli/antigravity-oauth-token (refreshed in memory,
never written back). Collectors cache for 60s and degrade to stale snapshots,
never exceptions.

The collectors keep credential material local, cache collection attempts for
one minute, and turn expected source failures into stale vendor snapshots.
"""

from __future__ import annotations

import copy
import datetime
import heapq
import http.client
import json
import os
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

from pulsar.daemon import paths


CLAUDE_USAGE_URL = "https://api.anthropic.com/api/oauth/usage"
GROK_BILLING_URL = "https://cli-chat-proxy.grok.com/v1/billing?format=credits"
GROK_USER_URL = "https://cli-chat-proxy.grok.com/v1/user?include=subscription"
CACHE_TTL_SECONDS = 60.0
CODEX_ROLLOUT_FILE_LIMIT = 20
CODEX_ROLLOUT_STALE_AFTER = datetime.timedelta(minutes=5)
DEFAULT_CLAUDE_CREDENTIALS_PATH = Path.home() / ".claude" / ".credentials.json"
DEFAULT_CODEX_SESSIONS_ROOT = Path.home() / ".codex" / "sessions"
DEFAULT_GROK_AUTH_PATH = Path.home() / ".grok" / "auth.json"
DEFAULT_AGY_TOKEN_PATH = (
    Path.home() / ".gemini" / "antigravity-cli" / "antigravity-oauth-token"
)
AGY_API = "https://daily-cloudcode-pa.googleapis.com/v1internal"
# OAuth token refresh requires PULSAR_AGY_CLIENT_ID and PULSAR_AGY_CLIENT_SECRET.
# Without this user agent loadCodeAssist and the quota RPCs answer 403.
AGY_USER_AGENT = "antigravity/1.3.4 linux/amd64"

UTC = datetime.timezone.utc


def _utc_now() -> datetime.datetime:
    return datetime.datetime.now(UTC)


def _iso(value: datetime.datetime) -> str:
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def _number(value: Any) -> int | float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    return value


def severity_for(used_percent: int | float) -> str:
    """Return the shared display severity for a percentage used."""
    if used_percent >= 90:
        return "critical"
    if used_percent >= 70:
        return "warn"
    return "normal"


class UsageLimitStore:
    """Thread-safe, copy-on-read storage for normalized vendor snapshots."""

    def __init__(self) -> None:
        self._snapshots: dict[str, dict[str, Any]] = {}
        self._lock = threading.RLock()

    def get(self, vendor: str) -> dict[str, Any] | None:
        with self._lock:
            snapshot = self._snapshots.get(vendor)
            return copy.deepcopy(snapshot) if snapshot is not None else None

    def put(self, snapshot: dict[str, Any]) -> dict[str, Any]:
        vendor = snapshot.get("vendor")
        if not isinstance(vendor, str) or not vendor:
            raise ValueError("usage snapshot requires a vendor")
        with self._lock:
            self._snapshots[vendor] = copy.deepcopy(snapshot)
            return copy.deepcopy(self._snapshots[vendor])

    def mark_stale(
        self,
        vendor: str,
        reason: str,
        *,
        now: datetime.datetime | None = None,
    ) -> dict[str, Any]:
        now = now or _utc_now()
        snapshot = self.get(vendor) or {
            "vendor": vendor,
            "plan": None,
            "windows": [],
            "fetched_at": _iso(now),
            "stale_since": None,
            "stale_reason": None,
        }
        snapshot["fetched_at"] = _iso(now)
        snapshot["stale_since"] = snapshot.get("stale_since") or _iso(now)
        snapshot["stale_reason"] = reason
        return self.put(snapshot)

    def record_codex_rate_limits(
        self,
        rate_limits: dict[str, Any],
        *,
        now: datetime.datetime | None = None,
        stale_reason: str | None = None,
    ) -> dict[str, Any] | None:
        """Merge one Codex rate-limit frame into the current snapshot."""
        if not isinstance(rate_limits, dict):
            return None
        now = now or _utc_now()
        previous = self.get("codex")
        windows_by_id = {
            window["id"]: window
            for window in (previous or {}).get("windows", [])
            if isinstance(window, dict) and isinstance(window.get("id"), str)
        }
        found_window = False
        for raw in (rate_limits.get("primary"), rate_limits.get("secondary")):
            window = _normalize_codex_window(raw)
            if window is not None:
                windows_by_id[window["id"]] = window
                found_window = True
        if not found_window:
            return previous

        plan = rate_limits.get("plan_type")
        if not isinstance(plan, str):
            plan = (previous or {}).get("plan")
        ordered = [windows_by_id[key] for key in ("5h", "weekly") if key in windows_by_id]
        ordered.extend(
            window
            for key, window in windows_by_id.items()
            if key not in {"5h", "weekly"}
        )
        return self.put(
            {
                "vendor": "codex",
                "plan": plan,
                "windows": ordered,
                "fetched_at": _iso(now),
                "stale_since": _iso(now) if stale_reason else None,
                "stale_reason": stale_reason,
            }
        )


def _normalize_codex_window(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    minutes = raw.get("window_minutes")
    used_percent = _number(raw.get("used_percent"))
    resets_at = _number(raw.get("resets_at"))
    if isinstance(minutes, bool) or not isinstance(minutes, int):
        return None
    if used_percent is None or resets_at is None:
        return None

    if minutes == 300:
        identifier, label = "5h", "5h"
    elif minutes == 10_080:
        identifier, label = "weekly", "weekly"
    else:
        identifier, label = f"{minutes}m", f"{minutes}m"
    try:
        reset_time = datetime.datetime.fromtimestamp(resets_at, UTC)
    except (OverflowError, OSError, ValueError):
        return None
    return {
        "id": identifier,
        "label": label,
        "used_percent": used_percent,
        "resets_at": _iso(reset_time),
        "severity": severity_for(used_percent),
    }


def _normalize_claude_snapshot(
    payload: Any,
    *,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    now = now or _utc_now()
    limits = payload.get("limits") if isinstance(payload, dict) else None
    if not isinstance(limits, list):
        raise ValueError("Claude usage response has no limits list")

    windows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    for limit in limits:
        if not isinstance(limit, dict):
            continue
        # `kind` is the unique key (session / weekly_all / weekly_scoped);
        # `group` alone collides — the live API returns TWO `weekly` limits
        # (all-models + model-scoped) and duplicate ids crash Svelte's keyed
        # each.
        identifier = limit.get("kind") or limit.get("group")
        used_percent = _number(limit.get("percent"))
        resets_at = limit.get("resets_at")
        if (
            not isinstance(identifier, str)
            or not identifier
            or used_percent is None
            or not isinstance(resets_at, str)
            or not resets_at
        ):
            continue
        while identifier in seen_ids:  # defensive: never emit duplicate ids
            identifier += "+"
        seen_ids.add(identifier)
        label = limit.get("group") or identifier
        scope = limit.get("scope")
        if isinstance(scope, str) and scope:
            label = scope
        elif isinstance(scope, dict):
            model = scope.get("model")
            display = model.get("display_name") if isinstance(model, dict) else None
            if isinstance(display, str) and display:
                label = f"{label} · {display}"
        windows.append(
            {
                "id": identifier,
                "label": label,
                "used_percent": used_percent,
                "resets_at": resets_at,
                "severity": severity_for(used_percent),
            }
        )

    plan = payload.get("plan_type") if isinstance(payload, dict) else None
    if not isinstance(plan, str):
        plan = None
    return {
        "vendor": "claude",
        "plan": plan,
        "windows": windows,
        "fetched_at": _iso(now),
        "stale_since": None,
        "stale_reason": None,
    }


class _CredentialsUnavailable(ValueError):
    pass


class _CredentialsExpired(ValueError):
    pass


def _read_claude_access_token(
    credentials_path: Path,
    *,
    now: datetime.datetime | None = None,
) -> str:
    try:
        data = json.loads(credentials_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _CredentialsUnavailable from exc
    oauth = data.get("claudeAiOauth") if isinstance(data, dict) else None
    token = oauth.get("accessToken") if isinstance(oauth, dict) else None
    if not isinstance(token, str) or not token:
        raise _CredentialsUnavailable

    expires_at = oauth.get("expiresAt")
    if _number(expires_at) is not None:
        expiry_seconds = expires_at / 1000 if expires_at >= 1_000_000_000_000 else expires_at
        if expiry_seconds <= (now or _utc_now()).timestamp():
            raise _CredentialsExpired
    return token


class ClaudeUsageCollector:
    """Poll Anthropic OAuth usage with a cache covering successes and failures."""

    def __init__(
        self,
        *,
        credentials_path: Path | None = None,
        store: UsageLimitStore | None = None,
        opener: Callable[..., Any] | None = None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now_fn: Callable[[], datetime.datetime] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.credentials_path = credentials_path or DEFAULT_CLAUDE_CREDENTIALS_PATH
        self.store = store or default_store
        self.opener = opener or urllib.request.urlopen
        self.ttl_seconds = ttl_seconds
        self.monotonic = monotonic or time.monotonic
        self.now_fn = now_fn or _utc_now
        self.timeout_seconds = timeout_seconds
        self._cached: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._lock = threading.Lock()

    def poll(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and self.monotonic() < self._cache_until:
                return copy.deepcopy(self._cached)
            now = self.now_fn()
            try:
                token = _read_claude_access_token(self.credentials_path, now=now)
                request = urllib.request.Request(
                    CLAUDE_USAGE_URL,
                    headers={
                        "Authorization": f"Bearer {token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "Accept": "application/json",
                    },
                )
                with self.opener(request, timeout=self.timeout_seconds) as response:
                    payload = json.loads(response.read())
                snapshot = self.store.put(_normalize_claude_snapshot(payload, now=now))
            except _CredentialsExpired:
                snapshot = self.store.mark_stale("claude", "auth_expired", now=now)
            except _CredentialsUnavailable:
                snapshot = self.store.mark_stale(
                    "claude", "credentials_unavailable", now=now
                )
            except urllib.error.HTTPError as exc:
                reason = "auth_expired" if exc.code in (401, 403) else "unreachable"
                snapshot = self.store.mark_stale("claude", reason, now=now)
            except (
                OSError,
                http.client.HTTPException,
                UnicodeDecodeError,
                ValueError,
            ):
                snapshot = self.store.mark_stale("claude", "unreachable", now=now)

            self._cached = copy.deepcopy(snapshot)
            self._cache_until = self.monotonic() + self.ttl_seconds
            return copy.deepcopy(snapshot)


def _rate_limits_from_session_record(record: Any) -> dict[str, Any] | None:
    if not isinstance(record, dict):
        return None
    payload = record.get("payload")
    if not isinstance(payload, dict):
        return None
    rate_limits = payload.get("rate_limits") or payload.get("rateLimits")
    return rate_limits if isinstance(rate_limits, dict) else None


def _event_time_from_session_record(record: Any) -> datetime.datetime | None:
    if not isinstance(record, dict):
        return None
    value = record.get("timestamp")
    if isinstance(value, str):
        try:
            parsed = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=UTC)
        return parsed.astimezone(UTC)
    if _number(value) is not None:
        try:
            return datetime.datetime.fromtimestamp(value, UTC)
        except (OverflowError, OSError, ValueError):
            return None
    return None


class CodexRateLimitFrame:
    """One rollout rate-limit payload paired with its source event time."""

    __slots__ = ("rate_limits", "fetched_at")

    def __init__(
        self,
        rate_limits: dict[str, Any],
        fetched_at: datetime.datetime,
    ) -> None:
        self.rate_limits = rate_limits
        self.fetched_at = fetched_at


def _codex_recent_session_dirs(
    sessions_root: Path,
    now: datetime.datetime | None = None,
) -> list[Path]:
    """Codex writes rollouts under sessions/YYYY/MM/DD.

    Rate-limit frames go stale after ``CODEX_ROLLOUT_STALE_AFTER`` (5 min),
    so only today and yesterday can hold a still-fresh frame. Scanning the
    whole ~/.codex/sessions tree (multi-GB, 10k+ files) is wasted work.
    """
    now = now or datetime.datetime.now(UTC)
    if now.tzinfo is None:
        now = now.replace(tzinfo=UTC)
    today = now.date()
    yesterday = today - datetime.timedelta(days=1)
    dirs: list[Path] = []
    for day in (today, yesterday):
        path = sessions_root / f"{day.year:04d}" / f"{day.month:02d}" / f"{day.day:02d}"
        if path.is_dir():
            dirs.append(path)
    return dirs


def read_codex_jsonl_fallback(
    sessions_root: Path,
    *,
    pulse_homes_root: Path | None = None,
    now: datetime.datetime | None = None,
) -> CodexRateLimitFrame | None:
    """Return the newest valid rate-limit frame across Codex rollout files.

    Only the newest ``CODEX_ROLLOUT_FILE_LIMIT`` rollouts can win, so they are
    picked with ``heapq.nlargest`` rather than by sorting the whole (unbounded,
    ever-growing) sessions tree, and each one is iterated line by line instead
    of materialized — this runs on every ``/usage`` poll past the 60s cache.
    The walk itself is bounded to today+yesterday day directories in each
    session root. Pulse-private CODEX_HOME rollouts are included because clean
    pulses do not write to the daemon's ambient ~/.codex/sessions.
    """
    candidates: list[tuple[Path, datetime.datetime]] = []
    session_roots = [sessions_root]
    if pulse_homes_root is not None:
        try:
            session_roots.extend(
                home / "codex-home" / "sessions"
                for home in pulse_homes_root.iterdir()
                if home.is_dir()
            )
        except OSError:
            pass
    try:
        for root in session_roots:
            for day_dir in _codex_recent_session_dirs(root, now=now):
                for path in day_dir.glob("rollout-*.jsonl"):
                    try:
                        mtime = datetime.datetime.fromtimestamp(path.stat().st_mtime, UTC)
                    except (OSError, OverflowError, ValueError):
                        continue
                    candidates.append((path, mtime))
    except OSError:
        return None
    newest_files = heapq.nlargest(
        CODEX_ROLLOUT_FILE_LIMIT, candidates,
        key=lambda candidate: candidate[1],
    )

    newest_frame: CodexRateLimitFrame | None = None
    newest_key: tuple[datetime.datetime, datetime.datetime, int] | None = None
    for path, mtime in newest_files:
        try:
            handle = path.open("r", errors="replace")
        except OSError:
            continue
        with handle:
            for line_number, line in enumerate(handle):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                rate_limits = _rate_limits_from_session_record(record)
                if rate_limits is None:
                    continue
                event_time = _event_time_from_session_record(record) or mtime
                selection_key = (event_time, mtime, line_number)
                if newest_key is None or selection_key > newest_key:
                    newest_key = selection_key
                    newest_frame = CodexRateLimitFrame(
                        copy.deepcopy(rate_limits), event_time
                    )
    return newest_frame


class CodexUsageCollector:
    """Read Codex CLI rollout frames with a one-minute scan cache."""

    def __init__(
        self,
        *,
        sessions_root: Path | None = None,
        pulse_homes_root: Path | None = None,
        store: UsageLimitStore | None = None,
        reader: Callable[
            [Path], CodexRateLimitFrame | dict[str, Any] | None
        ]
        | None = None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now_fn: Callable[[], datetime.datetime] | None = None,
    ) -> None:
        self.sessions_root = sessions_root or DEFAULT_CODEX_SESSIONS_ROOT
        self.pulse_homes_root = pulse_homes_root
        self.store = store or default_store
        self.reader = reader or (
            lambda root: read_codex_jsonl_fallback(
                root,
                pulse_homes_root=self.pulse_homes_root or paths.data_root() / "pulses",
                now=self.now_fn(),
            )
        )
        self.ttl_seconds = ttl_seconds
        self.monotonic = monotonic or time.monotonic
        self.now_fn = now_fn or _utc_now
        self._cached: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._lock = threading.Lock()

    def poll(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and self.monotonic() < self._cache_until:
                return copy.deepcopy(self._cached)
            now = self.now_fn()
            try:
                frame = self.reader(self.sessions_root)
                if isinstance(frame, CodexRateLimitFrame):
                    rate_limits = frame.rate_limits
                    fetched_at = frame.fetched_at
                else:
                    rate_limits = frame
                    fetched_at = now
                stale_reason = (
                    "rollout_stale"
                    if now - fetched_at > CODEX_ROLLOUT_STALE_AFTER
                    else None
                )
                snapshot = (
                    self.store.record_codex_rate_limits(
                        rate_limits,
                        now=fetched_at,
                        stale_reason=stale_reason,
                    )
                    if rate_limits is not None
                    else None
                )
                if snapshot is None:
                    snapshot = self.store.mark_stale(
                        "codex", "rollout_unavailable", now=now
                    )
            except (OSError, ValueError, TypeError):
                snapshot = self.store.mark_stale(
                    "codex", "rollout_unavailable", now=now
                )

            self._cached = copy.deepcopy(snapshot)
            self._cache_until = self.monotonic() + self.ttl_seconds
            return copy.deepcopy(snapshot)


def _read_grok_access_token(
    auth_path: Path,
    *,
    now: datetime.datetime | None = None,
) -> str:
    """Return a live Grok session token from ~/.grok/auth.json.

    The file is a map of issuer -> credential dict. The CLI stores the bearer
    under ``key``. Expired entries are skipped; if every entry is expired the
    collector reports auth_expired rather than sending a dead token.
    """
    try:
        data = json.loads(auth_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise _CredentialsUnavailable from exc
    if not isinstance(data, dict):
        raise _CredentialsUnavailable

    now = now or _utc_now()
    saw_expired = False
    for entry in data.values():
        if not isinstance(entry, dict):
            continue
        token = entry.get("key")
        if not isinstance(token, str) or not token:
            continue
        expires_at = entry.get("expires_at")
        if isinstance(expires_at, str) and expires_at:
            try:
                expiry = datetime.datetime.fromisoformat(
                    expires_at.replace("Z", "+00:00")
                )
            except ValueError:
                return token
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=UTC)
            if expiry <= now:
                saw_expired = True
                continue
        return token
    if saw_expired:
        raise _CredentialsExpired
    raise _CredentialsUnavailable


def _grok_iso(value: Any) -> str | None:
    if not isinstance(value, str) or not value:
        return None
    return value.replace("+00:00", "Z")


def _normalize_grok_snapshot(
    payload: Any,
    *,
    plan: str | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Map the Grok billing payload onto the shared vendor snapshot shape.

    Live 2026-08-13 shape: ``config.creditUsagePercent`` is the weekly pool,
    ``config.currentPeriod.end`` is the reset, and ``config.productUsage``
    breaks the same pool down by product (GrokBuild, Chat, …).
    """
    now = now or _utc_now()
    config = payload.get("config") if isinstance(payload, dict) else None
    if not isinstance(config, dict):
        raise ValueError("Grok billing response has no config object")

    period = config.get("currentPeriod")
    if not isinstance(period, dict):
        period = {}
    resets_at = _grok_iso(period.get("end")) or _grok_iso(config.get("billingPeriodEnd"))
    windows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()

    weekly_percent = _number(config.get("creditUsagePercent"))
    if weekly_percent is not None and resets_at:
        windows.append(
            {
                "id": "weekly",
                "label": "weekly",
                "used_percent": weekly_percent,
                "resets_at": resets_at,
                "severity": severity_for(weekly_percent),
            }
        )
        seen_ids.add("weekly")

    products = config.get("productUsage")
    if isinstance(products, list):
        for product in products:
            if not isinstance(product, dict):
                continue
            name = product.get("product")
            used_percent = _number(product.get("usagePercent"))
            if (
                not isinstance(name, str)
                or not name
                or used_percent is None
                or not resets_at
            ):
                continue
            identifier = name
            while identifier in seen_ids:
                identifier += "+"
            seen_ids.add(identifier)
            windows.append(
                {
                    "id": identifier,
                    "label": name,
                    "used_percent": used_percent,
                    "resets_at": resets_at,
                    "severity": severity_for(used_percent),
                }
            )

    if not isinstance(plan, str) or not plan:
        plan = None
    return {
        "vendor": "grok",
        "plan": plan,
        "windows": windows,
        "fetched_at": _iso(now),
        "stale_since": None,
        "stale_reason": None,
    }


class GrokUsageCollector:
    """Poll Grok's consumer billing API with a cache covering successes and failures."""

    def __init__(
        self,
        *,
        auth_path: Path | None = None,
        store: UsageLimitStore | None = None,
        opener: Callable[..., Any] | None = None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now_fn: Callable[[], datetime.datetime] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.auth_path = auth_path or DEFAULT_GROK_AUTH_PATH
        self.store = store or default_store
        self.opener = opener or urllib.request.urlopen
        self.ttl_seconds = ttl_seconds
        self.monotonic = monotonic or time.monotonic
        self.now_fn = now_fn or _utc_now
        self.timeout_seconds = timeout_seconds
        self._cached: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._lock = threading.Lock()

    def _get_json(self, url: str, token: str) -> Any:
        request = urllib.request.Request(
            url,
            headers={
                "Authorization": f"Bearer {token}",
                "X-XAI-Token-Auth": "xai-grok-cli",
                "Accept": "application/json",
            },
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read())

    def _plan_for(self, token: str) -> str | None:
        try:
            payload = self._get_json(GROK_USER_URL, token)
        except (
            OSError,
            http.client.HTTPException,
            UnicodeDecodeError,
            ValueError,
            urllib.error.HTTPError,
        ):
            return None
        if not isinstance(payload, dict):
            return None
        tier = payload.get("subscriptionTier")
        return tier if isinstance(tier, str) and tier else None

    def poll(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and self.monotonic() < self._cache_until:
                return copy.deepcopy(self._cached)
            now = self.now_fn()
            try:
                token = _read_grok_access_token(self.auth_path, now=now)
                payload = self._get_json(GROK_BILLING_URL, token)
                snapshot = self.store.put(
                    _normalize_grok_snapshot(
                        payload, plan=self._plan_for(token), now=now
                    )
                )
            except _CredentialsExpired:
                snapshot = self.store.mark_stale("grok", "auth_expired", now=now)
            except _CredentialsUnavailable:
                snapshot = self.store.mark_stale(
                    "grok", "credentials_unavailable", now=now
                )
            except urllib.error.HTTPError as exc:
                reason = "auth_expired" if exc.code in (401, 403) else "unreachable"
                snapshot = self.store.mark_stale("grok", reason, now=now)
            except (
                OSError,
                http.client.HTTPException,
                UnicodeDecodeError,
                ValueError,
            ):
                snapshot = self.store.mark_stale("grok", "unreachable", now=now)

            self._cached = copy.deepcopy(snapshot)
            self._cache_until = self.monotonic() + self.ttl_seconds
            return copy.deepcopy(snapshot)


def _normalize_gemini_snapshot(
    payload: Any,
    *,
    plan: str | None = None,
    now: datetime.datetime | None = None,
) -> dict[str, Any]:
    """Map retrieveUserQuotaSummary onto the shared vendor snapshot shape.

    ``groups[].buckets[]`` carries ``bucketId``, ``window``,
    ``remainingFraction`` and ``resetTime``. The source may omit Gemini
    buckets entirely (observed 2026-10-28); never invent an Argon window.
    When present, Gemini buckets come first.
    """
    now = now or _utc_now()
    groups = payload.get("groups") if isinstance(payload, dict) else None
    if not isinstance(groups, list):
        raise ValueError("Gemini quota response has no groups")
    windows: list[dict[str, Any]] = []
    for group in groups:
        buckets = group.get("buckets") if isinstance(group, dict) else None
        for bucket in buckets if isinstance(buckets, list) else []:
            if not isinstance(bucket, dict):
                continue
            ident = bucket.get("bucketId")
            remaining = _number(bucket.get("remainingFraction"))
            window = bucket.get("window")
            if not isinstance(ident, str) or remaining is None:
                continue
            used = round((1 - remaining) * 100, 1)
            label = window if isinstance(window, str) and window else ident
            if not ident.startswith("gemini"):
                label = f"claude/gpt {label}"
            windows.append(
                {
                    "id": ident,
                    "label": label,
                    "used_percent": used,
                    "resets_at": _grok_iso(bucket.get("resetTime")),
                    "severity": severity_for(used),
                }
            )
    windows.sort(key=lambda w: (not w["id"].startswith("gemini"), not w["id"].endswith("5h")))
    return {
        "vendor": "gemini",
        "plan": plan if isinstance(plan, str) and plan else None,
        "windows": windows,
        "fetched_at": _iso(now),
        "stale_since": None,
        "stale_reason": None,
    }


def _gemini_pool_window(payload: Any) -> dict[str, Any] | None:
    """The shared Gemini 5h pool from ``retrieveUserQuota`` per-model buckets.

    The quota summary can omit Gemini groups entirely (observed 2026-10-28 and
    2026-11-10), but ``retrieveUserQuota`` lists every ``gemini-*`` model with
    the same pooled ``remainingFraction`` and a 5h ``resetTime`` (verified
    2026-11-10: one Carbon call moved every gemini-* bucket 1 -> 0.992).
    Only buckets with a resetTime count; no Gemini buckets means no window.
    """
    buckets = payload.get("buckets") if isinstance(payload, dict) else None
    fractions: list[float] = []
    resets: list[str] = []
    for bucket in buckets if isinstance(buckets, list) else []:
        if not isinstance(bucket, dict):
            continue
        model = bucket.get("modelId")
        remaining = _number(bucket.get("remainingFraction"))
        reset = bucket.get("resetTime")
        if (not isinstance(model, str) or not model.startswith("gemini")
                or remaining is None or not isinstance(reset, str) or not reset):
            continue
        fractions.append(float(remaining))
        resets.append(reset)
    if not fractions:
        return None
    used = round((1 - min(fractions)) * 100, 1)
    return {
        "id": "gemini-5h",
        "label": "gemini 5h",
        "used_percent": used,
        "resets_at": _grok_iso(min(resets)),
        "severity": severity_for(used),
    }


class GeminiUsageCollector:
    """Poll the Antigravity (agy) quota summary with a 60s cache."""

    def __init__(
        self,
        *,
        token_path: Path | None = None,
        store: UsageLimitStore | None = None,
        opener: Callable[..., Any] | None = None,
        ttl_seconds: float = CACHE_TTL_SECONDS,
        monotonic: Callable[[], float] | None = None,
        now_fn: Callable[[], datetime.datetime] | None = None,
        timeout_seconds: float = 10.0,
    ) -> None:
        self.token_path = token_path or DEFAULT_AGY_TOKEN_PATH
        self.store = store or default_store
        self.opener = opener or urllib.request.urlopen
        self.ttl_seconds = ttl_seconds
        self.monotonic = monotonic or time.monotonic
        self.now_fn = now_fn or _utc_now
        self.timeout_seconds = timeout_seconds
        self._cached: dict[str, Any] | None = None
        self._cache_until = 0.0
        self._access: tuple[str, datetime.datetime] | None = None
        self._lock = threading.Lock()

    def _token(self, now: datetime.datetime) -> str:
        if self._access and self._access[1] - now > datetime.timedelta(minutes=2):
            return self._access[0]
        try:
            tok = json.loads(self.token_path.read_text())["token"]
        except (OSError, json.JSONDecodeError, KeyError, TypeError) as exc:
            raise _CredentialsUnavailable from exc
        try:
            expiry = datetime.datetime.fromisoformat(tok["expiry"][:26] + tok["expiry"][-6:])
        except (KeyError, TypeError, ValueError):
            expiry = now
        if tok.get("access_token") and expiry - now > datetime.timedelta(minutes=2):
            self._access = (tok["access_token"], expiry)
            return tok["access_token"]
        if not tok.get("refresh_token"):
            raise _CredentialsExpired
        client_id = os.environ.get("PULSAR_AGY_CLIENT_ID", "").strip()
        client_secret = os.environ.get("PULSAR_AGY_CLIENT_SECRET", "").strip()
        if not client_id or not client_secret:
            raise _CredentialsExpired
        body = urllib.parse.urlencode(
            {
                "client_id": client_id,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": tok["refresh_token"],
            }
        ).encode()
        request = urllib.request.Request("https://oauth2.googleapis.com/token", data=body)
        try:
            with self.opener(request, timeout=self.timeout_seconds) as response:
                fresh = json.loads(response.read())
        except urllib.error.HTTPError as exc:
            raise _CredentialsExpired from exc
        access = fresh.get("access_token")
        if not isinstance(access, str) or not access:
            raise _CredentialsExpired
        life = _number(fresh.get("expires_in")) or 3600
        self._access = (access, now + datetime.timedelta(seconds=life))
        return access

    def _post(self, method: str, token: str, body: dict[str, Any]) -> Any:
        request = urllib.request.Request(
            f"{AGY_API}:{method}",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": AGY_USER_AGENT,
            },
        )
        with self.opener(request, timeout=self.timeout_seconds) as response:
            return json.loads(response.read())

    def poll(self) -> dict[str, Any]:
        with self._lock:
            if self._cached is not None and self.monotonic() < self._cache_until:
                return copy.deepcopy(self._cached)
            now = self.now_fn()
            try:
                token = self._token(now)
                info = self._post(
                    "loadCodeAssist",
                    token,
                    {"metadata": {"ideType": "ANTIGRAVITY", "platform": "LINUX_AMD64",
                                  "pluginType": "GEMINI"}},
                )
                project = info.get("cloudaicompanionProject") if isinstance(info, dict) else None
                paid = info.get("paidTier") if isinstance(info, dict) else None
                plan = paid.get("name") if isinstance(paid, dict) else None
                payload = self._post(
                    "retrieveUserQuotaSummary", token, {"project": project or ""}
                )
                normalized = _normalize_gemini_snapshot(payload, plan=plan, now=now)
                if not any(w["id"].startswith("gemini") for w in normalized["windows"]):
                    try:
                        pool = _gemini_pool_window(
                            self._post("retrieveUserQuota", token, {"project": project or ""})
                        )
                    except (OSError, http.client.HTTPException, UnicodeDecodeError, ValueError):
                        pool = None  # the 3p summary is still good; skip the pool
                    if pool is not None:
                        normalized["windows"].insert(0, pool)
                snapshot = self.store.put(normalized)
            except _CredentialsExpired:
                snapshot = self.store.mark_stale("gemini", "auth_expired", now=now)
            except _CredentialsUnavailable:
                snapshot = self.store.mark_stale(
                    "gemini", "credentials_unavailable", now=now
                )
            except urllib.error.HTTPError as exc:
                if exc.code in (401, 403):
                    self._access = None
                reason = "auth_expired" if exc.code in (401, 403) else "unreachable"
                snapshot = self.store.mark_stale("gemini", reason, now=now)
            except (
                OSError,
                http.client.HTTPException,
                UnicodeDecodeError,
                ValueError,
            ):
                snapshot = self.store.mark_stale("gemini", "unreachable", now=now)

            self._cached = copy.deepcopy(snapshot)
            self._cache_until = self.monotonic() + self.ttl_seconds
            return copy.deepcopy(snapshot)


default_store = UsageLimitStore()
default_claude_collector = ClaudeUsageCollector()
default_codex_collector = CodexUsageCollector()
default_grok_collector = GrokUsageCollector()
default_gemini_collector = GeminiUsageCollector()


def poll_claude_if_due() -> dict[str, Any]:
    return default_claude_collector.poll()


def poll_codex_if_due() -> dict[str, Any]:
    return default_codex_collector.poll()


def poll_grok_if_due() -> dict[str, Any]:
    return default_grok_collector.poll()


def poll_gemini_if_due() -> dict[str, Any]:
    return default_gemini_collector.poll()
