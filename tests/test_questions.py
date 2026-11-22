"""Questions rail (2026-09-08) — store CRUD + routes + answer delivery.

    venv/bin/python -m pytest tests/test_questions.py -q
"""

from __future__ import annotations

import json

import httpx
import pytest

from pulsar.daemon import lifecycle, paths, questions
from pulsar.daemon.app import create_app
from pulsar.store import InvalidStateTransition, Store, UnknownQuestion

# Shared daemon fakes/fixtures (fake spawn/socket + ASGI client).
from tests.test_daemon import (  # noqa: F401
    data_root, fake_spawn, fake_sock, client,
)


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_DATA", str(tmp_path))
    s = Store(str(tmp_path / "test.db"))
    yield s
    s.close()


@pytest.fixture()
def asker(store):
    return store.create_pulse(title="asker", model="m", vessel="v").id


def test_create_list_resolve(store, asker):
    q = store.create_question(asker, "Which color?", choices=["red", "blue"],
                              default_choice="red", context="for the header")
    assert q.state == "open" and q.choices == ["red", "blue"]
    assert [x.id for x in store.list_questions(pulse_id=asker, state="open")] == [q.id]
    a = store.resolve_question(q.id, "answered", answer="blue")
    assert a.state == "answered" and a.answer == "blue" and a.answered_at
    assert store.list_questions(pulse_id=asker, state="open") == []
    with pytest.raises(InvalidStateTransition):
        store.resolve_question(q.id, "answered", answer="again")
    with pytest.raises(UnknownQuestion):
        store.get_question("nope")


def test_resolve_twice_loses_the_race_cleanly(store, asker, monkeypatch):
    """A resolver that read the question as open before another one
    answered it must not overwrite the first answer."""
    q = store.create_question(asker, "Which color?")
    stale = store.get_question(q.id)
    assert stale.state == "open"
    store.resolve_question(q.id, "answered", answer="blue")
    real_get = store.get_question
    calls = []

    def stale_get(question_id):
        calls.append(question_id)
        return stale if len(calls) == 1 else real_get(question_id)

    monkeypatch.setattr(store, "get_question", stale_get)
    with pytest.raises(InvalidStateTransition, match="already answered"):
        store.resolve_question(q.id, "answered", answer="red")
    monkeypatch.setattr(store, "get_question", real_get)
    assert store.get_question(q.id).answer == "blue"


def test_create_validation(store, asker):
    with pytest.raises(ValueError):
        store.create_question(asker, "   ")
    with pytest.raises(ValueError):
        store.create_question(asker, "x", choices=["a"], default_choice="b")
    for i in range(10):
        store.create_question(asker, f"q{i}")
    with pytest.raises(ValueError):
        store.create_question(asker, "one too many")


def test_delete_pulse_drops_questions(store, asker):
    store.create_question(asker, "q")
    store.set_state(asker, "stopped")
    store.delete_pulse(asker)
    assert store.list_questions(state="open") == []


@pytest.mark.anyio
async def test_routes_ask_answer_delivers(store, asker, monkeypatch):
    woken = []
    monkeypatch.setattr(lifecycle, "wake_locked",
                        lambda st, pid: woken.append(pid))
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
        async with app.router.lifespan_context(app):
            r = await c.post("/questions", json={
                "pulse_id": asker, "text": "Per feed or per day?",
                "choices": ["Per feed", "Per day"]})
            assert r.status_code == 201, r.text
            qid = r.json()["value"]["id"]
            r = await c.get("/questions")
            assert [q["id"] for q in r.json()["value"]] == [qid]
            r = await c.post(f"/questions/{qid}/answer", json={"text": "Per day"})
            assert r.status_code == 200, r.text
            assert r.json()["value"]["state"] == "answered"
            # delivered to the asker's inbox as a wrapped message
            files = sorted(paths.inbox_new(asker).glob("*.json"))
            assert len(files) == 1
            body = json.loads(files[0].read_text())
            assert "[The user answered your question" in body["text"]
            assert "A: Per day" in body["text"]
            assert woken == [asker]
            # second answer is a 409, empty list now
            r = await c.post(f"/questions/{qid}/answer", json={"text": "x"})
            assert r.status_code == 409
            r = await c.get("/questions")
            assert r.json()["value"] == []
            # unknown pulse / unknown question
            r = await c.post("/questions", json={"pulse_id": "zz", "text": "?"})
            assert r.status_code == 404
            r = await c.post("/questions/nope/dismiss")
            assert r.status_code == 404


@pytest.mark.anyio
async def test_archive_closes_questions_and_hides_them(client, store,
                                                        fake_spawn, fake_sock):
    """When a pulse is archived its questions go away —
    closed in the store AND excluded from /questions."""
    pid = (await client.post("/pulses", json={"title": "asker", "model": "m"})
           ).json()["value"]["id"]
    r = await client.post("/questions", json={"pulse_id": pid, "text": "q?"})
    assert r.status_code == 201, r.text
    qid = r.json()["value"]["id"]
    assert [q["id"] for q in (await client.get("/questions")).json()["value"]] \
        == [qid]

    r = await client.post(f"/pulses/{pid}/archive")
    assert r.status_code == 200, r.text
    # closed in the store, no longer listed (open or all)
    assert store.get_question(qid).state == "dismissed"
    assert store.list_questions(state="open") == []
    assert (await client.get("/questions")).json()["value"] == []
    assert (await client.get("/questions", params={"state": ""})).json()["value"] \
        == []


@pytest.mark.anyio
async def test_list_excludes_archived_even_if_stale(store, asker, monkeypatch):
    """Belt-and-braces: even a question left open on an archived pulse never
    surfaces via list_questions."""
    q = store.create_question(asker, "stale?")
    store.archive_pulse(asker)  # direct store transition, bypasses the hook
    store._conn.execute(
        "UPDATE questions SET state = 'open' WHERE id = ?;", (q.id,))
    assert store.list_questions() == []
    assert store.list_questions(include_archived=True) != []


@pytest.mark.anyio
async def test_answer_keeps_question_open_when_wake_fails(store, asker, monkeypatch):
    def boom(st, pid):
        raise RuntimeError("no runner")
    monkeypatch.setattr(lifecycle, "wake_locked", boom)
    q = store.create_question(asker, "q?")
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://d") as c:
        async with app.router.lifespan_context(app):
            r = await c.post(f"/questions/{q.id}/answer", json={"text": "a"})
            assert r.status_code == 503
    assert store.get_question(q.id).state == "open"
