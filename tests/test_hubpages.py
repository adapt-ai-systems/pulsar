"""Hub pages listing — the catalog behind the built-in Hub surface."""
from __future__ import annotations

import os

import pytest

from pulsar.daemon import hubpages

from tests.test_daemon import data_root, fake_spawn  # noqa: F401


def _page(root, slug, body, mtime):
    d = root / slug
    d.mkdir()
    (d / "index.html").write_text(body)
    os.utime(d / "index.html", (mtime, mtime))
    return d


def test_lists_pages_newest_first_with_titles(tmp_path):
    _page(tmp_path, "old-report", "<html><title>Old &amp; Report</title>", 1000)
    _page(tmp_path, "new-plan", "<title>\n  New   Plan </title>", 2000)
    _page(tmp_path, "no-title", "<p>hi</p>", 1500)
    real = tmp_path.parent / "elsewhere"
    real.mkdir()
    (real / "index.html").write_text("<title>Linked</title>")
    (tmp_path / "linked").symlink_to(real, target_is_directory=True)
    (tmp_path / "dangling").symlink_to(tmp_path / "gone")
    (tmp_path / "index.html").write_text("catalog")
    (tmp_path / ".hidden").mkdir()

    pages = hubpages.list_pages(tmp_path)
    by = {p["slug"]: p for p in pages}

    assert set(by) == {"old-report", "new-plan", "no-title", "linked"}
    assert by["old-report"]["title"] == "Old & Report"
    assert by["new-plan"]["title"] == "New Plan"
    assert by["no-title"]["title"] == "No Title"
    assert by["linked"]["title"] == "Linked"
    rest = [p["slug"] for p in pages if p["slug"] != "linked"]
    assert rest == ["new-plan", "no-title", "old-report"]


def test_missing_root_is_empty(tmp_path):
    assert hubpages.list_pages(tmp_path / "nope") == []


def test_route(tmp_path, monkeypatch):
    from fastapi import FastAPI
    from starlette.testclient import TestClient

    _page(tmp_path, "a", "<title>A</title>", 1000)
    monkeypatch.setattr(hubpages, "ROOT", tmp_path)
    app = FastAPI()
    app.include_router(hubpages.build_router())
    r = TestClient(app).get("/hub/pages")
    assert r.status_code == 200
    body = r.json()
    assert body["ok"] is True
    assert [p["slug"] for p in body["value"]] == ["a"]



@pytest.fixture()
def app_client(data_root, fake_spawn, tmp_path, monkeypatch):  # noqa: F811
    from starlette.testclient import TestClient
    from pulsar.store import Store
    from pulsar.daemon.app import create_app

    pages = tmp_path / "pages"
    pages.mkdir()
    _page(pages, "alpha", "<title>Alpha Report</title>", 1000)
    _page(pages, "beta", "<title>Beta</title>", 2000)
    monkeypatch.setattr(hubpages, "ROOT", pages)
    store = Store(str(data_root / "pulsar.db"))
    with TestClient(create_app(store=store)) as c:
        yield c
    store.close()


def test_agent_pins_and_unpins(app_client):
    c = app_client
    assert c.get("/hub/pins").json()["value"] == []

    r = c.post("/hub/pins", json={"slug": "alpha"})
    assert r.status_code == 201
    assert r.json()["value"] == [{"slug": "alpha", "title": "Alpha Report"}]

    # Already pinned: no duplicate, 200.
    r = c.post("/hub/pins", json={"slug": "/alpha/"})
    assert r.status_code == 200
    assert len(r.json()["value"]) == 1

    c.post("/hub/pins", json={"slug": "beta", "title": "Custom"})
    assert [p["title"] for p in c.get("/hub/pins").json()["value"]] == ["Alpha Report", "Custom"]
    # The web reads the same settings key.
    assert [p["slug"] for p in c.get("/settings/hub.pins").json()["value"]] == ["alpha", "beta"]

    r = c.delete("/hub/pins/alpha")
    assert r.json()["value"] == [{"slug": "beta", "title": "Custom"}]
    assert c.delete("/hub/pins/alpha").json()["value"] == [{"slug": "beta", "title": "Custom"}]


def test_pin_rejects_unknown_and_bad_slugs(app_client):
    c = app_client
    assert c.post("/hub/pins", json={"slug": "nope"}).status_code == 404
    assert c.post("/hub/pins", json={"slug": "../etc"}).status_code == 404
    assert c.post("/hub/pins", json={}).status_code == 400
    assert c.post("/hub/pins", content=b"not json").status_code == 400
    assert c.get("/hub/pins").json()["value"] == []
