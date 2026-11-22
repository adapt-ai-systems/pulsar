"""Daemon request validation tests (Host, Origin, Sec-Fetch-Site)."""

from __future__ import annotations

import httpx
import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from pulsar.daemon.app import create_app
from pulsar.daemon.security import is_host_allowed, is_origin_allowed, parse_allowed_hosts
from pulsar.store import Store

from tests.test_daemon import data_root, fake_spawn, fake_sock  # noqa: F401


@pytest.fixture()
def strict_env(monkeypatch):
    """Clear the synthetic test host list so default production rules apply."""
    monkeypatch.delenv("PULSAR_WEB_ALLOWED_HOSTS", raising=False)


@pytest.fixture()
def sync_client(strict_env, data_root, fake_spawn, fake_sock):  # noqa: F811
    store = Store(str(data_root / "pulsar.db"))
    app = create_app(store=store)
    with TestClient(app, base_url="http://127.0.0.1:8726") as c:
        c.store = store
        yield c
    store.close()


def test_host_and_origin_helpers():
    assert parse_allowed_hosts("") == ()
    assert parse_allowed_hosts(" pulsar.example.test , .tail.example ") == (
        "pulsar.example.test",
        ".tail.example",
    )

    assert is_host_allowed("localhost:8726") is True
    assert is_host_allowed("127.0.0.1:8726") is True
    assert is_host_allowed("[::1]:8726") is True
    assert is_host_allowed("192.0.2.10:8726") is True
    assert is_host_allowed("example.invalid") is False
    assert is_host_allowed("") is False
    assert is_host_allowed("evil@localhost") is False

    # Origin must only allow loopback or explicit allowlist — never arbitrary bare IPs.
    assert is_origin_allowed("http://localhost:5292") is True
    assert is_origin_allowed("http://127.0.0.1:15296") is True
    assert is_origin_allowed("http://[::1]:5292") is True
    assert is_origin_allowed("http://192.0.2.10:5292") is False
    assert is_origin_allowed("https://example.invalid") is False
    assert is_origin_allowed("null") is False
    assert is_origin_allowed("file:///etc/passwd") is False
    assert is_origin_allowed("http://localhost:5292/extra") is False

    extra = ("pulsar.example.test", ".tail.example")
    assert is_host_allowed("pulsar.example.test:5292", extra) is True
    assert is_origin_allowed("https://pulsar.example.test", extra) is True
    assert is_origin_allowed("https://box.tail.example:5292", extra) is True
    assert is_origin_allowed("https://other.example.test", extra) is False


def test_http_host_validation(sync_client, monkeypatch):
    for host in ("127.0.0.1:8726", "localhost:8726", "[::1]:8726"):
        r = sync_client.get("/healthz", headers={"Host": host})
        assert r.status_code == 200, host

    r = sync_client.get("/healthz", headers={"Host": "example.invalid"})
    assert r.status_code == 403
    assert r.json()["ok"] is False

    monkeypatch.setenv("PULSAR_WEB_ALLOWED_HOSTS", "pulsar.example.test")
    r = sync_client.get("/healthz", headers={"Host": "pulsar.example.test"})
    assert r.status_code == 200


@pytest.mark.anyio
async def test_http_origin_and_sec_fetch_site(strict_env, data_root, fake_spawn, fake_sock, monkeypatch):  # noqa: F811
    store = Store(str(data_root / "pulsar.db"))
    p = store.create_pulse(title="t", model="claude-opus-5", vessel="claude-code", state="stopped")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    try:
        async with (
            httpx.AsyncClient(transport=transport, base_url="http://127.0.0.1:8726") as c,
            app.router.lifespan_context(app),
        ):
            # No Origin and no Sec-Fetch-Site (CLI tools) -> allowed
            r = await c.post(f"/pulses/{p.id}/bookmark")
            assert r.status_code == 200

            # Allowed loopback Origins -> allowed
            for origin in ("http://127.0.0.1:5292", "http://localhost:5292", "http://[::1]:5292"):
                r = await c.post(f"/pulses/{p.id}/bookmark", headers={"Origin": origin})
                assert r.status_code == 200, origin

            # Foreign Origin, null Origin, and bare non-loopback IP Origin -> 403
            for bad_origin in ("https://example.invalid", "null", "http://192.0.2.10:5292", ""):
                r = await c.post(
                    f"/pulses/{p.id}/messages",
                    headers={"Origin": bad_origin, "Content-Type": "text/plain"},
                    content='{"text":"blocked"}',
                )
                assert r.status_code == 403, bad_origin
                r2 = await c.post(f"/pulses/{p.id}/bookmark", headers={"Origin": bad_origin})
                assert r2.status_code == 403, bad_origin

            # PUT / PATCH / DELETE with foreign Origin -> 403
            assert (await c.put("/settings/owner.name", json="Test",
                                headers={"Origin": "https://example.invalid"})).status_code == 403
            assert (await c.patch(f"/pulses/{p.id}/title", json={"title": "x"},
                                  headers={"Origin": "https://example.invalid"})).status_code == 403
            assert (await c.delete(f"/pulses/{p.id}/bookmark",
                                   headers={"Origin": "https://example.invalid"})).status_code == 403

            # Sec-Fetch-Site: cross-site -> 403 even without Origin
            r = await c.post(f"/pulses/{p.id}/bookmark",
                             headers={"Sec-Fetch-Site": "cross-site"})
            assert r.status_code == 403

            # GET with foreign Origin or Sec-Fetch-Site: cross-site -> still allowed
            assert (await c.get("/healthz", headers={"Origin": "https://example.invalid"})).status_code == 200
            assert (await c.get("/healthz", headers={"Sec-Fetch-Site": "cross-site"})).status_code == 200

            # PULSAR_WEB_ALLOWED_HOSTS permits matching Host and Origin
            monkeypatch.setenv("PULSAR_WEB_ALLOWED_HOSTS", "pulsar.example.test")
            r = await c.post(
                f"/pulses/{p.id}/bookmark",
                headers={"Host": "pulsar.example.test", "Origin": "https://pulsar.example.test"},
            )
            assert r.status_code == 200
    finally:
        store.close()


def test_websocket_host_and_origin_validation(sync_client, monkeypatch):
    # No Origin on loopback Host -> connects
    with sync_client.websocket_connect("/live", headers={"Host": "127.0.0.1:8726"}) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "roster"

    # Allowed loopback Origin -> connects
    with sync_client.websocket_connect(
        "/live",
        headers={"Host": "127.0.0.1:8726", "Origin": "http://localhost:5292"},
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "roster"

    # Disallowed Host -> refused
    with pytest.raises(WebSocketDisconnect):
        with sync_client.websocket_connect("/live", headers={"Host": "example.invalid"}):
            pass

    # Foreign Origin, null Origin, bare non-loopback IP Origin, cross-site -> refused
    for bad_headers in (
        {"Host": "127.0.0.1:8726", "Origin": "https://example.invalid"},
        {"Host": "127.0.0.1:8726", "Origin": "null"},
        {"Host": "127.0.0.1:8726", "Origin": "http://192.0.2.10:5292"},
        {"Host": "127.0.0.1:8726", "Sec-Fetch-Site": "cross-site"},
    ):
        with pytest.raises(WebSocketDisconnect):
            with sync_client.websocket_connect("/live", headers=bad_headers):
                pass

    # Name from PULSAR_WEB_ALLOWED_HOSTS -> connects
    monkeypatch.setenv("PULSAR_WEB_ALLOWED_HOSTS", "pulsar.example.test")
    with sync_client.websocket_connect(
        "/live",
        headers={"Host": "pulsar.example.test", "Origin": "https://pulsar.example.test"},
    ) as ws:
        frame = ws.receive_json()
        assert frame["type"] == "roster"
