"""Daemon contract tests — httpx ASGI client + fake spawn/socket.

    venv/bin/python -m pytest tests -k daemon -q

No real runners, no systemd, no tokens: ``spawn.*`` and the socket client are
monkeypatched, and we write ``heartbeat.json`` ourselves into a tmp data dir
(``PULSAR_DATA``). Covers: create spawns + rows; message lands atomically in
inbox/new; pause fallback path; restore passes the resume id; boot reconcile
marks a stale-heartbeat pulse stopped with an OOM reason; orphan sweep stops an
unknown unit.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path

import httpx
import pytest

import pulsar.launch.diet as diet_mod
import pulsar.vessel.profiles as profiles_mod
import pulsar.launch.units as units_mod
from pulsar.store import Store
from pulsar.daemon.app import create_app
from pulsar.daemon import paths
from pulsar.daemon.routes import events as events_routes
from pulsar.daemon.routes import messages as messages_routes
from pulsar.daemon.routes import lifecycle as lifecycle_routes
import pulsar.daemon.handoff.engine as handoff_engine_mod
from pulsar.daemon import sockclient as sockclient_mod
import pulsar.daemon.reconcile as reconcile_mod
from pulsar.vessel.claude import session_history_file


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #
class FakeSpawn:
    """Records spawn/stop/list/fence calls; writes a fresh heartbeat on spawn."""

    def __init__(self, data_root: Path):
        self.data_root = data_root
        self.spawned: list[dict] = []
        self.stopped: list[str] = []
        self.units: list[str] = []
        self.fence: dict[str, dict] = {}
        self.spawn_should_fail = False
        self.stop_should_fail = False

    def spawn_runner(self, pulse_id, home, model, vessel, resume=None,
                     memory_max="6G", tasks_max=512, diet=None, title=None,
                     effort=None, fast=False):
        self.spawned.append({
            "pulse_id": pulse_id, "home": str(home), "model": model,
            "vessel": vessel, "resume": resume, "memory_max": memory_max,
            "diet": diet, "effort": effort, "fast": fast,
        })
        if self.spawn_should_fail:
            raise RuntimeError("systemd-run failed (test)")
        # The real spawn_runner materializes the diet before systemd-run; do
        # the same so daemon tests cover diet.json + skill symlinks on disk.
        diet_mod.materialize_diet(pulse_id, Path(home), diet)
        # A live runner would begin heartbeating; simulate a fresh one.
        _write_heartbeat(pulse_id, state="idle",
                         cli_session_id=f"sess-{pulse_id}", age_s=0.0)

    def stop_unit(self, pulse_id):
        self.stopped.append(pulse_id)
        if self.stop_should_fail:
            return False
        # A real stop takes the transient unit out of systemd.
        self.units = [u for u in self.units if u != pulse_id]
        return True

    def list_pulse_units(self):
        return list(self.units)

    def unit_active(self, pulse_id):
        return pulse_id in self.units

    def fence_report(self, pulse_id):
        return self.fence.get(
            pulse_id, {"result": "success", "oom": False, "detail": {}})


class FakeSock:
    """Stands in for the socket client: status/stop/probe without a socket."""

    def __init__(self):
        self.stop_should_fail = False
        self.stopped: list[str] = []
        self.probes: list[str] = []
        self.SocketError = _SockErr
        self.interrupted: list[str] = []
        self.interrupt_should_fail = False
        self.interrupt_reply = {"v": 1, "ok": True}

    def stop(self, sock_path, pulse_id):
        if self.stop_should_fail:
            raise _SockErr("socket down (test)")
        self.stopped.append(pulse_id)
        return {"v": 1, "ok": True}

    def interrupt(self, sock_path, pulse_id):
        if self.interrupt_should_fail:
            raise _SockErr("socket down (test)")
        self.interrupted.append(pulse_id)
        return dict(self.interrupt_reply)

    def status(self, sock_path, pulse_id):
        return {"v": 1, "ok": True, "state": "idle",
                "cli_pid": 4242, "cli_session_id": f"sess-{pulse_id}"}

    def probe(self, sock_path, pulse_id):
        self.probes.append(pulse_id)
        return {"v": 1, "ok": True, "state": "idle",
                "cli_pid": 4242, "cli_session_id": f"sess-{pulse_id}"}


class _SockErr(Exception):
    pass


def _write_heartbeat(pulse_id: str, state: str, cli_session_id: str,
                     age_s: float) -> None:
    """Write a heartbeat.json aged ``age_s`` seconds into the past."""
    home = paths.pulse_home(pulse_id)
    home.mkdir(parents=True, exist_ok=True)
    ts_epoch = time.time() - age_s
    ts = datetime.fromtimestamp(ts_epoch, tz=timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%S%z")
    beat = {
        "ts": ts, "pid": 1234, "cli_pid": 4242, "state": state,
        "cli_session_id": cli_session_id, "protocol": 1,
    }
    (home / "heartbeat.json").write_text(json.dumps(beat))


# --------------------------------------------------------------------------- #
# Fixtures
# --------------------------------------------------------------------------- #
@pytest.fixture()
def data_root(tmp_path, monkeypatch):
    root = tmp_path / "data"
    (root / "pulses").mkdir(parents=True)
    monkeypatch.setenv("PULSAR_DATA", str(root))
    return root


@pytest.fixture()
def fake_spawn(data_root, monkeypatch):
    fs = FakeSpawn(data_root)
    # One patch point: the daemon modules call launch.units through the module.
    monkeypatch.setattr(units_mod, "spawn_runner", fs.spawn_runner)
    monkeypatch.setattr(units_mod, "stop_unit", fs.stop_unit)
    monkeypatch.setattr(units_mod, "list_pulse_units", fs.list_pulse_units)
    monkeypatch.setattr(units_mod, "fence_report", fs.fence_report)
    monkeypatch.setattr(units_mod, "unit_active", fs.unit_active)
    return fs


@pytest.fixture()
def fake_sock(monkeypatch):
    fk = FakeSock()
    monkeypatch.setattr(sockclient_mod, "stop", fk.stop)
    monkeypatch.setattr(sockclient_mod, "status", fk.status)
    monkeypatch.setattr(sockclient_mod, "probe", fk.probe)
    monkeypatch.setattr(sockclient_mod, "interrupt", fk.interrupt)
    monkeypatch.setattr(sockclient_mod, "SocketError", _SockErr)
    return fk


@pytest.fixture()
def store(data_root):
    s = Store(str(data_root / "pulsar.db"))
    yield s
    s.close()


@pytest.fixture()
async def client(store, fake_spawn, fake_sock):
    """ASGI client with an injected store (so reconcile runs against it)."""
    app = create_app(store=store)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://daemon"
    ) as c:
        # Trigger lifespan (startup reconcile) explicitly.
        async with app.router.lifespan_context(app):
            yield c


# --------------------------------------------------------------------------- #
# Tests
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_healthz(client):
    r = await client.get("/healthz")
    assert r.status_code == 200
    assert r.json() == {"ok": True, "value": {"status": "ok"}}


async def _post_grid(client, name="test-grid"):
    """Create a grid and return its id (pulses must live in existing grids)."""
    r = await client.post("/grids", json={"name": name})
    assert r.status_code in (200, 201), r.text
    return r.json()["value"]["id"]


async def _post_pulse(client, grid_id=None, title="hire",
                      model="claude-sonnet-4-5"):
    body = {"title": title, "model": model}
    if grid_id is not None:
        body["grid_id"] = grid_id
    r = await client.post("/pulses", json=body)
    assert r.status_code in (200, 201), r.text
    return r.json()["value"]["id"]


@pytest.mark.anyio
async def test_slow_spawn_does_not_block_event_loop(
    client, store, fake_spawn, monkeypatch
):
    """A blocking spawn_runner must not freeze the daemon.

    spawn_runner shells out to systemd-run and can sleep-loop up to 10s in
    _await_unit_gone; the create handler runs it via asyncio.to_thread, so a
    concurrent /healthz must sail through it.
    """
    def slow_spawn(*args, **kwargs):
        time.sleep(0.5)  # simulates systemd-run + _await_unit_gone polling
        fake_spawn.spawn_runner(*args, **kwargs)

    monkeypatch.setattr(units_mod, "spawn_runner", slow_spawn)

    t0 = time.monotonic()
    create = asyncio.ensure_future(client.post("/pulses", json={
        "title": "slow-spawn", "model": "claude-opus-5"}))
    await asyncio.sleep(0.05)  # let the POST reach the spawn call

    t_h = time.monotonic()
    r = await client.get("/healthz")
    health_ms = time.monotonic() - t_h

    assert r.status_code == 200
    assert health_ms < 0.25, (
        f"/healthz took {health_ms:.3f}s — the event loop was blocked by "
        "spawn_runner")

    cr = await create
    assert cr.status_code == 201, cr.text
    assert time.monotonic() - t0 >= 0.4  # the sleep really happened
    assert len(fake_spawn.spawned) == 1


@pytest.mark.anyio
async def test_create_spawns_and_persists(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "scraper", "model": "claude-opus-5", "memory_max": "2G"})
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["ok"] is True
    pulse = body["value"]
    pid = pulse["id"]

    # store row exists, state running, vessel defaulted
    row = store.get_pulse(pid)
    assert row.state == "running"
    assert row.vessel == "claude-code"
    assert row.settings.get("memory_max") == "2G"

    # spawn_runner was called with the fence + data home
    assert len(fake_spawn.spawned) == 1
    call = fake_spawn.spawned[0]
    assert call["pulse_id"] == pid
    assert call["memory_max"] == "2G"
    assert call["home"].endswith(f"pulses/{pid}")
    # live decoration present (fresh heartbeat the fake wrote)
    assert pulse["live"]["heartbeat_fresh"] is True
    assert pulse["live"]["reachable"] is True


@pytest.mark.anyio
@pytest.mark.parametrize(("model", "expected"), [
    ("grok-4.6", "grok"),
    ("gpt-6-sol", "codex"),
    ("codex-ds4-flash", "codex"),
    ("pi-v4-flash", "pi"),
    ("pi-qwen27-interface", "pi"),
    ("custom-model", "claude-code"),
])
async def test_create_infers_vessel_when_api_caller_omits_it(
    client, store, fake_spawn, model, expected,
):
    r = await client.post("/pulses", json={"title": "helper", "model": model})
    assert r.status_code == 201, r.text
    pulse = r.json()["value"]
    assert pulse["vessel"] == expected
    assert fake_spawn.spawned[-1]["vessel"] == expected


@pytest.mark.anyio
async def test_create_explicit_vessel_still_overrides_inference(
    client, store, fake_spawn,
):
    r = await client.post("/pulses", json={
        "title": "override", "model": "grok-4.6", "vessel": "codex",
    })
    assert r.status_code == 201, r.text
    assert r.json()["value"]["vessel"] == "codex"
    assert fake_spawn.spawned[-1]["vessel"] == "codex"


@pytest.mark.anyio
async def test_create_spawn_failure_marks_stopped(client, store, fake_spawn):
    fake_spawn.spawn_should_fail = True
    r = await client.post("/pulses", json={
        "title": "doomed", "model": "claude-opus-5"})
    assert r.status_code == 502, r.text
    assert r.json()["ok"] is False
    # the row was cleaned up to stopped with a reason, not left lying as running
    pulses = store.list_pulses(state="stopped")
    assert len(pulses) == 1
    assert "spawn failed" in pulses[0].settings.get("stop_reason", "")


@pytest.mark.anyio
async def test_message_lands_atomically(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]

    r2 = await client.post(f"/pulses/{pid}/messages", json={"text": "hello there"})
    assert r2.status_code == 202, r2.text
    name = r2.json()["value"]["queued"]

    new_dir = paths.inbox_new(pid)
    files = list(new_dir.glob("*.json"))
    assert len(files) == 1
    assert files[0].name == name
    # no leftover .tmp (atomic rename)
    assert list(new_dir.glob("*.tmp")) == []
    payload = json.loads(files[0].read_text())
    assert payload["text"] == "hello there"


@pytest.mark.anyio
async def test_message_reply_to_quotes_earlier_message(client, store, fake_spawn):
    """Reply button: the pulse receives the quoted message ahead of the user's
    text; the pane keeps their clean text and gets reply_to for the chip."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(f"/pulses/{pid}/messages", json={
        "text": "yes, do that one",
        "reply_to": {"text": "Option A: ship it\nOption B: wait", "ts": "2026-09-30T10:00:00Z"}})
    assert r2.status_code == 202, r2.text
    payload = json.loads(next(paths.inbox_new(pid).glob("*.json")).read_text())
    assert payload["display_text"] == "yes, do that one"
    assert payload["reply_to"] == {"text": "Option A: ship it\nOption B: wait",
                                   "ts": "2026-09-30T10:00:00Z"}
    assert payload["text"] == (
        "[The user is replying to this earlier message of yours (sent 2026-09-30T10:00:00Z):]\n"
        "> Option A: ship it\n> Option B: wait\n\nyes, do that one")


@pytest.mark.anyio
async def test_message_reply_to_multipart_and_clip(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    long = "x" * (messages_routes.REPLY_QUOTE_MAX_CHARS + 50)
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "see pic", "reply_to": json.dumps({"text": long})},
        files=[("file", ("shot.png", b"png", "image/png"))])
    assert r2.status_code == 202, r2.text
    payload = json.loads(next(paths.inbox_new(pid).glob("*.json")).read_text())
    assert payload["display_text"] == "see pic"
    assert payload["reply_to"]["text"].endswith("…[clipped]")
    assert len(payload["reply_to"]["text"]) < len(long)
    assert payload["text"].startswith("[The user is replying to this earlier message of yours:]")
    assert "\n\nsee pic\n\nAttached file:" in payload["text"]
    r3 = await client.post(f"/pulses/{pid}/messages",
                           data={"text": "x", "reply_to": "not json"},
                           files=[("file", ("a.txt", b"a", "text/plain"))])
    assert r3.status_code == 422


@pytest.mark.anyio
async def test_message_unknown_pulse_404(client):
    r = await client.post("/pulses/nope/messages", json={"text": "hi"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_message_multipart_attachments(client, store, fake_spawn):
    """Paste/attach path: files land under attachments/<msg-id>/, the vessel
    text carries the paths block, display_text stays clean, and the GET route
    serves the bytes back for thumbnails."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]

    png = b"\x89PNG-not-really-but-mime-is-what-counts"
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "look at this"},
        files=[("file", ("shot.png", png, "image/png")),
               ("file", ("notes.txt", b"hello", "text/plain"))])
    assert r2.status_code == 202, r2.text
    val = r2.json()["value"]
    assert len(val["attachments"]) == 2

    # Inbox message: delivered text has the Read-tool paths block, the clean
    # typed text rides display_text, attachment meta rides along.
    files = list(paths.inbox_new(pid).glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text())
    assert payload["display_text"] == "look at this"
    assert payload["text"].startswith("look at this")
    assert "Attached file:" in payload["text"]
    assert "shot.png" in payload["text"]
    assert len(payload["attachments"]) == 2

    # Bytes on disk exactly as uploaded…
    saved = val["attachments"][0]
    assert saved["filename"] == "shot.png"
    assert Path(saved["path"]).read_bytes() == png
    # …and served back over the attachment GET (thumbnail path).
    r3 = await client.get(saved["url"])
    assert r3.status_code == 200
    assert r3.content == png


@pytest.mark.anyio
async def test_message_multipart_files_only_no_text(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("shot.png", b"png", "image/png"))])
    assert r2.status_code == 202, r2.text
    payload = json.loads(next(paths.inbox_new(pid).glob("*.json")).read_text())
    assert payload["display_text"] == ""
    assert payload["text"].startswith("Attached file:")
    # Image uploads carry the look-first instruction after the path block.
    from pulsar.vessel.vision_sidecar import IMAGE_INSTRUCTION
    assert payload["text"].endswith(IMAGE_INSTRUCTION)


@pytest.mark.anyio
async def test_message_multipart_text_file_no_image_instruction(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("notes.txt", b"hello", "text/plain"))])
    assert r2.status_code == 202, r2.text
    payload = json.loads(next(paths.inbox_new(pid).glob("*.json")).read_text())
    from pulsar.vessel.vision_sidecar import IMAGE_INSTRUCTION
    assert IMAGE_INSTRUCTION not in payload["text"]


@pytest.mark.anyio
async def test_message_multipart_video(client, store, fake_spawn):
    """Common browser video types are saved and served like other files."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    video = b"fake-mp4-payload"
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("clip.mp4", video, "video/mp4"))])
    assert r2.status_code == 202, r2.text
    saved = r2.json()["value"]["attachments"][0]
    assert saved["mime_type"] == "video/mp4"
    assert saved["size"] == len(video)
    assert Path(saved["path"]).read_bytes() == video
    r3 = await client.get(saved["url"])
    assert r3.status_code == 200
    assert r3.content == video


@pytest.mark.anyio
async def test_message_multipart_video_infers_mime_from_extension(
        client, store, fake_spawn):
    """Browsers that report a blank MKV MIME still produce a video attachment."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("clip.mkv", b"fake-mkv", "application/octet-stream"))])
    assert r2.status_code == 202, r2.text
    saved = r2.json()["value"]["attachments"][0]
    assert saved["mime_type"] == "video/x-matroska"


@pytest.mark.anyio
async def test_message_multipart_audio(client, store, fake_spawn):
    """Attached audio (voice memo / dictation) saves and serves like any file —
    the chat-upload path a transcribe job arrives through."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    audio = b"fake-mp3-payload"
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("memo.mp3", audio, "audio/mpeg"))])
    assert r2.status_code == 202, r2.text
    saved = r2.json()["value"]["attachments"][0]
    assert saved["mime_type"] == "audio/mpeg"
    assert saved["size"] == len(audio)
    assert Path(saved["path"]).read_bytes() == audio
    r3 = await client.get(saved["url"])
    assert r3.status_code == 200
    assert r3.content == audio


@pytest.mark.anyio
async def test_message_multipart_audio_infers_mime_from_extension(
        client, store, fake_spawn):
    """Browsers reporting a blank MIME still produce an audio attachment for
    known audio extensions (.m4a -> audio/mp4), while unknown ones stay 415."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("memo.m4a", b"fake-m4a", "application/octet-stream"))])
    assert r2.status_code == 202, r2.text
    saved = r2.json()["value"]["attachments"][0]
    assert saved["mime_type"] == "audio/mp4"
    r3 = await client.post(
        f"/pulses/{pid}/messages",
        files=[("file", ("mystery.bin", b"x", "application/octet-stream"))])
    assert r3.status_code == 415
    assert r3.json()["error"]["code"] == "disallowed_mime_type"


@pytest.mark.anyio
async def test_message_multipart_markdown_by_extension(client, store, fake_spawn):
    """A .md file is markdown whatever MIME the OS sends (iOS sends none)."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    for mime in ("application/octet-stream", "text/x-markdown", "text/plain"):
        r2 = await client.post(
            f"/pulses/{pid}/messages",
            files=[("file", ("notes.md", b"# hi", mime))])
        assert r2.status_code == 202, r2.text
        assert r2.json()["value"]["attachments"][0]["mime_type"] == "text/markdown"


@pytest.mark.anyio
async def test_message_multipart_dup_filenames_both_survive(client, store,
                                                            fake_spawn):
    """Two pastes both named image.png must not clobber each other."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "two shots"},
        files=[("file", ("image.png", b"first", "image/png")),
               ("file", ("image.png", b"second", "image/png"))])
    assert r2.status_code == 202, r2.text
    atts = r2.json()["value"]["attachments"]
    assert len(atts) == 2
    assert atts[0]["filename"] != atts[1]["filename"]
    assert Path(atts[0]["path"]).read_bytes() == b"first"
    assert Path(atts[1]["path"]).read_bytes() == b"second"


@pytest.mark.anyio
async def test_message_multipart_zip_and_scan_exports(client, store, fake_spawn):
    """Zip archives and 3D scan exports (obj/ply/stl) are accepted, including
    the octet-stream / vendor-zip MIMEs browsers actually send for them."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        files=[
            ("file", ("scan.zip", b"PK\x03\x04", "application/x-zip-compressed")),
            ("file", ("scan.obj", b"v 0 0 0", "application/octet-stream")),
            ("file", ("scan.ply", b"ply", "application/octet-stream")),
        ])
    assert r2.status_code == 202, r2.text
    mimes = {a["filename"]: a["mime_type"] for a in r2.json()["value"]["attachments"]}
    assert mimes == {"scan.zip": "application/zip", "scan.obj": "model/obj",
                     "scan.ply": "application/x-ply"}


@pytest.mark.anyio
async def test_message_multipart_rejects_disallowed_mime(client, store,
                                                         fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "sneaky"},
        files=[("file", ("evil.bin", b"\x00\x01", "application/octet-stream"))])
    assert r2.status_code == 415
    assert r2.json()["error"]["code"] == "disallowed_mime_type"
    # Nothing queued, nothing saved.
    assert list(paths.inbox_new(pid).glob("*.json")) == []
    assert not paths.attachments_dir(pid).exists()


@pytest.mark.anyio
async def test_message_multipart_bad_filename_400(client, store, fake_spawn):
    """Path separators in an upload name must not escape the message dir."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(
        f"/pulses/{pid}/messages",
        data={"text": "x"},
        files=[("file", ("..", b"data", "image/png"))])
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "invalid_filename"
    assert list(paths.inbox_new(pid).glob("*.json")) == []


@pytest.mark.anyio
async def test_attachment_get_unknown_404(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.get(f"/pulses/{pid}/attachments/nope/none.png")
    assert r2.status_code == 404
    assert r2.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_pause_fallback_path(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]

    # socket stop fails -> systemd fallback must fire
    fake_sock.stop_should_fail = True
    r2 = await client.post(f"/pulses/{pid}/pause")
    assert r2.status_code == 200, r2.text
    assert r2.json()["value"]["state"] == "stopped"
    assert pid in fake_spawn.stopped          # fell back to stop_unit
    assert store.get_pulse(pid).state == "stopped"


@pytest.mark.anyio
async def test_pause_socket_path(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(f"/pulses/{pid}/pause")
    assert r2.status_code == 200
    assert pid in fake_sock.stopped
    assert pid not in fake_spawn.stopped      # socket worked, no fallback


@pytest.mark.anyio
async def test_pause_both_fail_502(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    fake_sock.stop_should_fail = True
    fake_spawn.stop_should_fail = True
    r2 = await client.post(f"/pulses/{pid}/pause")
    assert r2.status_code == 502
    assert r2.json()["error"]["code"] == "stop_failed"


@pytest.mark.anyio
async def test_interrupt_running_pulse(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(f"/pulses/{pid}/interrupt")
    assert r2.status_code == 200, r2.text
    assert r2.json()["value"]["interrupted"] is True
    assert pid in fake_sock.interrupted
    # the pulse stays running — interrupt is NOT a stop
    assert store.get_pulse(pid).state == "running"


@pytest.mark.anyio
async def test_interrupt_not_running_409(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    r2 = await client.post(f"/pulses/{pid}/interrupt")
    assert r2.status_code == 409
    assert fake_sock.interrupted == []


@pytest.mark.anyio
async def test_interrupt_refused_by_runner(client, store, fake_spawn, fake_sock):
    # A vessel with no turn abort (claude/qwen runner) answers ok=false —
    # surfaced as a 409, never treated as success.
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    fake_sock.interrupt_reply = {"v": 1, "ok": False,
                                 "error": "not_interruptible"}
    r2 = await client.post(f"/pulses/{pid}/interrupt")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "not_interruptible"


@pytest.mark.anyio
async def test_interrupt_socket_down_502(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    fake_sock.interrupt_should_fail = True
    r2 = await client.post(f"/pulses/{pid}/interrupt")
    assert r2.status_code == 502
    assert r2.json()["error"]["code"] == "interrupt_failed"


@pytest.mark.anyio
async def test_restore_passes_resume_id(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    # pause it
    await client.post(f"/pulses/{pid}/pause")
    assert store.get_pulse(pid).state == "stopped"

    # heartbeat left behind by the last live runner carries the session id
    _write_heartbeat(pid, state="stopped", cli_session_id="resume-me-123",
                     age_s=30.0)
    fake_spawn.spawned.clear()

    r2 = await client.post(f"/pulses/{pid}/restore")
    assert r2.status_code == 200, r2.text
    assert r2.json()["value"]["state"] == "running"
    assert len(fake_spawn.spawned) == 1
    assert fake_spawn.spawned[0]["resume"] == "resume-me-123"


@pytest.mark.anyio
async def test_stop_then_restore_does_not_replay_the_stopped_turn(
        client, store, fake_spawn, fake_sock):
    """Restore's crash recovery must not replay the message the user had
    just stopped. A crash leftover (not recorded by pause) still replays."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    home = paths.pulse_home(pid)
    feeding = home / "inbox" / "feeding"
    feeding.mkdir(parents=True, exist_ok=True)
    (feeding / "stopped.json").write_text('{"text": "send X"}')
    await client.post(f"/pulses/{pid}/pause")
    (feeding / "crash.json").write_text('{"text": "later"}')
    events = (home / "events.jsonl").read_text() if (home / "events.jsonl").exists() else ""
    assert '"pulse_stopped"' in events
    r2 = await client.post(f"/pulses/{pid}/restore")
    assert r2.status_code == 200, r2.text
    assert not (feeding / "stopped.json").exists()
    assert (home / "inbox" / "acked" / "stopped.json").exists()
    assert (feeding / "crash.json").exists()
    assert not (home / "stopped_in_flight.json").exists()


@pytest.mark.anyio
async def test_restore_archived_refused_before_spawn(client, store, fake_spawn,
                                                     fake_sock):
    """archived -> running is not a legal transition. Refuse it up front —
    spawning first and 409ing afterwards left a live untracked runner."""
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    await client.post(f"/pulses/{pid}/archive")
    fake_spawn.spawned.clear()

    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "bad_state"
    assert fake_spawn.spawned == []                    # nothing was started
    assert store.get_pulse(pid).state == "archived"


@pytest.mark.anyio
async def test_create_rejects_nonpositive_handoff_tokens(client):
    """A negative threshold made every context instantly 'over' — churn."""
    for bad in (0, -1):
        r = await client.post("/pulses", json={
            "title": "t", "model": "m", "handoff_tokens": bad})
        assert r.status_code == 422, r.text


@pytest.mark.anyio
async def test_reset_respawns_without_resume(client, store, fake_spawn,
                                             fake_sock):
    """Reset = stop + fresh spawn (resume=None), even though a session id
    exists in the heartbeat — the context wipe is the point."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    _write_heartbeat(pid, state="idle", cli_session_id="would-resume-me",
                     age_s=5.0)
    fake_spawn.spawned.clear()

    r2 = await client.post(f"/pulses/{pid}/reset")
    assert r2.status_code == 200, r2.text
    assert r2.json()["value"]["state"] == "running"
    assert pid in fake_sock.stopped  # old runner was stopped first
    assert len(fake_spawn.spawned) == 1
    assert fake_spawn.spawned[0]["resume"] is None
    # The transcript marks the wipe.
    import json as _json
    lines = paths.events_path(pid).read_text().splitlines()
    assert _json.loads(lines[-1])["type"] == "context_reset"


@pytest.mark.anyio
async def test_reset_stopped_pulse_spawns_fresh(client, store, fake_spawn,
                                                fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    _write_heartbeat(pid, state="stopped", cli_session_id="stale-session",
                     age_s=60.0)
    fake_spawn.spawned.clear()
    fake_sock.stopped.clear()

    r2 = await client.post(f"/pulses/{pid}/reset")
    assert r2.status_code == 200, r2.text
    assert r2.json()["value"]["state"] == "running"
    assert fake_sock.stopped == []  # nothing was running — no stop needed
    assert len(fake_spawn.spawned) == 1
    assert fake_spawn.spawned[0]["resume"] is None


@pytest.mark.anyio
async def test_reset_archived_refused(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/archive")

    r2 = await client.post(f"/pulses/{pid}/reset")
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "bad_state"


@pytest.mark.anyio
async def test_reset_spawn_failure_leaves_stopped(client, store, fake_spawn,
                                                  fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    fake_spawn.spawn_should_fail = True

    r2 = await client.post(f"/pulses/{pid}/reset")
    assert r2.status_code == 502
    assert r2.json()["error"]["code"] == "spawn_failed"
    assert store.get_pulse(pid).state == "stopped"
    assert store.get_pulse(pid).settings.get("stop_reason") == \
        "reset respawn failed"


@pytest.mark.anyio
async def test_reset_retires_in_flight_messages(client, store, fake_spawn,
                                                fake_sock):
    """The redelivery race (2026-08-15): a message the runner claimed into
    inbox/feeding/ (in-turn, unacked) must NOT reach the fresh session after
    a reset — it is retired to acked/ before the respawn. Queued-unseen
    messages in new/ survive the reset on purpose."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    home = paths.pulse_home(pid)
    feeding = home / "inbox" / "feeding"
    new = home / "inbox" / "new"
    feeding.mkdir(parents=True, exist_ok=True)
    new.mkdir(parents=True, exist_ok=True)
    (feeding / "20260815T000000-brief.json").write_text(
        json.dumps({"text": "the brief the old session already finished"}))
    (new / "20260815T000001-queued.json").write_text(
        json.dumps({"text": "queued, never seen"}))

    r2 = await client.post(f"/pulses/{pid}/reset")
    assert r2.status_code == 200, r2.text
    assert list(feeding.glob("*.json")) == []
    assert (home / "inbox" / "acked" / "20260815T000000-brief.json").exists()
    assert (new / "20260815T000001-queued.json").exists()


@pytest.mark.anyio
async def test_archive_pauses_running_first(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    r2 = await client.post(f"/pulses/{pid}/archive")
    assert r2.status_code == 200, r2.text
    assert r2.json()["value"]["state"] == "archived"
    assert store.get_pulse(pid).state == "archived"
    # it was running -> must have been stopped en route
    assert pid in fake_sock.stopped or pid in fake_spawn.stopped


@pytest.mark.anyio
async def test_events_tail(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    ev_path = paths.events_path(pid)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    with ev_path.open("w") as fh:
        for i in range(5):
            fh.write(json.dumps({"type": "assistant_text", "n": i}) + "\n")

    r2 = await client.get(f"/pulses/{pid}/events?tail=2")
    assert r2.status_code == 200
    events = r2.json()["value"]["events"]
    assert [e["n"] for e in events] == [3, 4]


@pytest.mark.anyio
async def test_task_events_scan_whole_log(client, store, fake_spawn):
    """The TODO card's timestamps need the task-tool calls from the whole
    log, not the pane's tail window (so items keep their real timestamps).
    Only task tools + TaskCreate results come back, oldest first, results
    clipped."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    ev_path = paths.events_path(pid)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    with ev_path.open("w") as fh:
        fh.write(json.dumps({"type": "session", "session_id": "s1", "ts": "S1",
                             "extra": "not forwarded"}) + "\n")
        fh.write(json.dumps({"type": "tool_call", "name": "todo_write", "ts": "T1",
                             "args": {"todos": [{"id": "a", "content": "one", "status": "pending"}]}}) + "\n")
        for i in range(300):
            fh.write(json.dumps({"type": "assistant_text", "text": "todo_write mentioned in prose %d" % i}) + "\n")
            fh.write(json.dumps({"type": "tool_call", "name": "bash", "args": {"command": "ls"}}) + "\n")
        fh.write(json.dumps({"type": "tool_call", "name": "TaskCreate", "id": "c1", "ts": "T2",
                             "args": {"subject": "Fix"}}) + "\n")
        fh.write(json.dumps({"type": "tool_result", "id": "c1", "ok": True,
                             "snippet": "Task #7 created successfully: Fix" + "x" * 500}) + "\n")
        fh.write(json.dumps({"type": "tool_result", "id": "other", "ok": True, "snippet": "todo_write"}) + "\n")
        fh.write(json.dumps({"type": "tool_call", "name": "TaskUpdate", "ts": "T3",
                             "args": {"taskId": "7", "status": "completed"}}) + "\n")
    r2 = await client.get(f"/pulses/{pid}/task-events")
    assert r2.status_code == 200
    events = r2.json()["value"]["events"]
    # Session markers ride along (the pane's record segments on them),
    # reduced to type/session_id/ts.
    assert [e.get("name", e["type"]) for e in events] == ["session", "todo_write", "TaskCreate", "tool_result", "TaskUpdate"]
    assert events[0] == {"type": "session", "session_id": "s1", "ts": "S1"}
    assert events[1]["ts"] == "T1"
    assert len(events[3]["snippet"]) <= 200 and "Task #7" in events[3]["snippet"]


@pytest.mark.anyio
async def test_events_tail_capped(client, store, fake_spawn):
    """tail is clamped to EVENTS_TAIL_MAX — a scrollback request can never ask
    the daemon for an unbounded read of an append-only log."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    ev_path = paths.events_path(pid)
    ev_path.parent.mkdir(parents=True, exist_ok=True)
    n = events_routes.EVENTS_TAIL_MAX + 5
    with ev_path.open("w") as fh:
        for i in range(n):
            fh.write(json.dumps({"type": "assistant_text", "n": i}) + "\n")

    r2 = await client.get(f"/pulses/{pid}/events?tail=999999")
    events = r2.json()["value"]["events"]
    assert len(events) == events_routes.EVENTS_TAIL_MAX
    assert events[-1]["n"] == n - 1
    assert events[0]["n"] == n - events_routes.EVENTS_TAIL_MAX


@pytest.mark.anyio
async def test_list_derives_live_without_probing(client, store, fake_spawn,
                                                 fake_sock):
    """The roster reads liveness off the heartbeat file. One dead socket must
    not cost /pulses a 5s timeout per pulse — the authoritative probe belongs
    to the single-pulse GET."""
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    fake_sock.probes.clear()

    rl = await client.get("/pulses")
    assert fake_sock.probes == []
    live = rl.json()["value"][0]["live"]
    assert live["reachable"] is True          # a fresh heartbeat IS the evidence
    assert live["socket_state"] == "idle"     # straight off the heartbeat
    assert live["cli_session_id"] == f"sess-{pid}"

    # Stale heartbeat, still no probe: honestly unreachable.
    _write_heartbeat(pid, state="idle", cli_session_id="s", age_s=99.0)
    rl2 = await client.get("/pulses")
    assert fake_sock.probes == []
    assert rl2.json()["value"][0]["live"]["reachable"] is False

    # The single-pulse GET does ask the runner directly.
    rg = await client.get(f"/pulses/{pid}")
    assert fake_sock.probes == [pid]
    assert rg.json()["value"]["live"]["reachable"] is True


@pytest.mark.anyio
async def test_list_and_get(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "one", "model": "m"})
    pid = r.json()["value"]["id"]
    rl = await client.get("/pulses")
    assert rl.status_code == 200
    ids = [p["id"] for p in rl.json()["value"]]
    assert pid in ids
    rg = await client.get(f"/pulses/{pid}")
    assert rg.status_code == 200
    assert rg.json()["value"]["title"] == "one"


# ---- boot reconcile --------------------------------------------------------- #
@pytest.mark.anyio
async def test_reconcile_marks_stale_heartbeat_stopped_oom(
        data_root, store, fake_spawn, fake_sock):
    # A pulse the store thinks is running, whose heartbeat is stale, killed by OOM.
    pulse = store.create_pulse(title="hungry", model="m", vessel="claude-code",
                               state="running")
    _write_heartbeat(pulse.id, state="busy", cli_session_id="s1", age_s=99.0)
    fake_spawn.fence[pulse.id] = {"result": "oom-kill", "oom": True, "detail": {}}

    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        pass

    row = store.get_pulse(pulse.id)
    assert row.state == "stopped"
    assert row.settings.get("stop_reason") == "hit memory cap"
    assert pulse.id in fake_spawn.stopped     # unit reaped


@pytest.mark.anyio
async def test_reconcile_keeps_fresh_heartbeat(
        data_root, store, fake_spawn, fake_sock):
    pulse = store.create_pulse(title="alive", model="m", vessel="claude-code",
                               state="running")
    _write_heartbeat(pulse.id, state="idle", cli_session_id="s1", age_s=1.0)

    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        pass

    assert store.get_pulse(pulse.id).state == "running"
    assert pulse.id not in fake_spawn.stopped


@pytest.mark.anyio
async def test_reconcile_keeps_stale_heartbeat_when_unit_active(
        data_root, store, fake_spawn, fake_sock):
    # Heavy CPU load: a busy runner missed beats, but its unit is alive.
    pulse = store.create_pulse(title="loaded", model="m", vessel="claude-code",
                               state="running")
    _write_heartbeat(pulse.id, state="busy", cli_session_id="s1", age_s=99.0)
    fake_spawn.units = [pulse.id]

    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        pass

    assert store.get_pulse(pulse.id).state == "running"
    assert pulse.id not in fake_spawn.stopped    # neither reaped nor orphaned


@pytest.mark.anyio
async def test_reconcile_keeps_stale_heartbeat_when_unit_state_unknown(
        data_root, store, fake_spawn, fake_sock, monkeypatch):
    # systemctl timed out under load: no proof of death, so keep the pulse.
    pulse = store.create_pulse(title="unsure", model="m", vessel="claude-code",
                               state="running")
    _write_heartbeat(pulse.id, state="busy", cli_session_id="s1", age_s=99.0)
    monkeypatch.setattr(units_mod, "unit_active", lambda pid: None)

    swept = reconcile_mod.sweep_dead_runners(store)

    assert swept["kept"] == 1 and swept["stopped"] == 0
    assert store.get_pulse(pulse.id).state == "running"
    assert pulse.id not in fake_spawn.stopped


def test_reconcile_grace_reads_updated_at_as_utc(
        data_root, store, fake_spawn, fake_sock, monkeypatch):
    # updated_at is UTC; time.mktime read it as local time, so on a PDT host
    # a pulse touched an hour ago looked 6h in the FUTURE and the grace
    # window kept its dead runner unreaped for ~7h (live 2026-10-13).
    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    try:
        pulse = store.create_pulse(title="dead", model="m",
                                   vessel="claude-code", state="running")
        hour_ago = datetime.fromtimestamp(time.time() - 3600, timezone.utc)
        store._conn.execute("UPDATE pulses SET updated_at = ? WHERE id = ?;",
                            (hour_ago.isoformat(timespec="seconds"), pulse.id))
        _write_heartbeat(pulse.id, state="busy", cli_session_id="s1",
                         age_s=99.0)

        swept = reconcile_mod.sweep_dead_runners(store, grace_s=20)
    finally:
        monkeypatch.delenv("TZ")
        time.tzset()

    assert swept["stopped"] == 1
    assert store.get_pulse(pulse.id).state == "stopped"


def test_reconcile_stale_final_beat_reaped_even_if_unit_active(
        data_root, store, fake_spawn, fake_sock):
    # The runner's own final beat says cli_dead: trust it over the unit.
    pulse = store.create_pulse(title="cli gone", model="m",
                               vessel="claude-code", state="running")
    _write_heartbeat(pulse.id, state="cli_dead", cli_session_id="s1",
                     age_s=99.0)
    fake_spawn.units = [pulse.id]

    reconcile_mod.sweep_dead_runners(store)

    assert store.get_pulse(pulse.id).state == "stopped"
    assert pulse.id in fake_spawn.stopped


def test_heartbeat_fresh_window_tolerates_missed_beats(data_root):
    # 30s without a beat (6 missed 5s beats) is still fresh; 61s is not.
    pid = "beat-window"
    _write_heartbeat(pid, state="busy", cli_session_id="s1", age_s=30.0)
    assert paths.heartbeat_fresh(paths.read_heartbeat(pid))
    _write_heartbeat(pid, state="busy", cli_session_id="s1", age_s=61.0)
    assert not paths.heartbeat_fresh(paths.read_heartbeat(pid))


@pytest.mark.anyio
async def test_reconcile_orphan_sweep(data_root, store, fake_spawn, fake_sock):
    # A live systemd unit for a pulse this store has, but not as running:
    # a true orphan, stopped. A unit for a pulse this store does not know
    # belongs to another daemon on the host (e.g. the live one while a test
    # or smoke daemon boots): left alone.
    orphan = store.create_pulse("orphan", "m", "claude-code", state="stopped")
    fake_spawn.units = [orphan.id, "foreign-live-pulse"]

    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        pass

    assert orphan.id in fake_spawn.stopped
    assert "foreign-live-pulse" not in fake_spawn.stopped


@pytest.mark.anyio
async def test_reconcile_missing_heartbeat_stopped(
        data_root, store, fake_spawn, fake_sock):
    # running in the store, but no heartbeat file at all -> dead
    pulse = store.create_pulse(title="ghost", model="m", vessel="claude-code",
                               state="running")
    app = create_app(store=store)
    async with app.router.lifespan_context(app):
        pass
    row = store.get_pulse(pulse.id)
    assert row.state == "stopped"
    # clean-exit scopes get the human wording, not "runner died (success)"
    assert "runner" in row.settings.get("stop_reason", "")
    assert "(success)" not in row.settings.get("stop_reason", "")


# --------------------------------------------------------------------------- #
# grids CRUD
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_grid_create_list(client, store):
    r = await client.post("/grids", json={"name": "work"})
    assert r.status_code == 201, r.text
    grid = r.json()["value"]
    assert grid["name"] == "work"
    assert grid["position"] == 0

    r2 = await client.post("/grids", json={"name": "research"})
    assert r2.json()["value"]["position"] == 1

    rl = await client.get("/grids")
    assert rl.status_code == 200
    names = [g["name"] for g in rl.json()["value"]]
    assert names == ["work", "research"]


@pytest.mark.anyio
async def test_grid_create_bad_body_422(client):
    r = await client.post("/grids", json={})  # name required
    assert r.status_code == 422


@pytest.mark.anyio
async def test_grid_patch_rename_and_reorder(client, store):
    gid = (await client.post("/grids", json={"name": "work"})).json()["value"]["id"]
    r = await client.patch(f"/grids/{gid}", json={"name": "job", "position": 5})
    assert r.status_code == 200, r.text
    g = r.json()["value"]
    assert g["name"] == "job"
    assert g["position"] == 5
    assert store.get_grid(gid).name == "job"


@pytest.mark.anyio
async def test_grid_patch_unknown_404(client):
    r = await client.patch("/grids/nope", json={"name": "x"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_grid_delete_empty(client, store):
    gid = (await client.post("/grids", json={"name": "scratch"})
           ).json()["value"]["id"]
    r = await client.delete(f"/grids/{gid}")
    assert r.status_code == 200, r.text
    assert r.json()["value"]["deleted"] == gid


@pytest.mark.anyio
async def test_grid_delete_nonempty_409(client, store):
    gid = (await client.post("/grids", json={"name": "busy"})
           ).json()["value"]["id"]
    await client.post("/pulses", json={"title": "t", "model": "m",
                                       "grid_id": gid})
    r = await client.delete(f"/grids/{gid}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "grid_not_empty"
    # grid survived
    assert store.get_grid(gid)


@pytest.mark.anyio
async def test_grid_reorder(client, store):
    ids = [(await client.post("/grids", json={"name": n})).json()["value"]["id"]
           for n in ("a", "b", "c")]
    r = await client.post("/grids/reorder", json={"ids": [ids[2], ids[0], ids[1]]})
    assert r.status_code == 200, r.text
    assert [g.id for g in store.list_grids()] == [ids[2], ids[0], ids[1]]

    bad = await client.post("/grids/reorder", json={"ids": [ids[0], "nope"]})
    assert bad.status_code == 404, bad.text


@pytest.mark.anyio
async def test_grid_delete_detach_moves_pulses_out(client, store):
    gid = (await client.post("/grids", json={"name": "busy"})
           ).json()["value"]["id"]
    pid = (await client.post("/pulses", json={"title": "t", "model": "m",
                                              "grid_id": gid})
           ).json()["value"]["id"]
    r = await client.delete(f"/grids/{gid}?detach=true")
    assert r.status_code == 200, r.text
    assert r.json()["value"] == {"deleted": gid, "detached": 1}
    # pulse kept, just out of any grid
    assert store.get_pulse(pid).grid_id is None
    assert [g.id for g in store.list_grids()] == []


@pytest.mark.anyio
async def test_grid_delete_unknown_404(client):
    r = await client.delete("/grids/nope")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# move pulse to grid
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_move_pulse_to_grid_and_ungrid(client, store):
    gid = (await client.post("/grids", json={"name": "work"})
           ).json()["value"]["id"]
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    assert store.get_pulse(pid).grid_id is None

    r = await client.patch(f"/pulses/{pid}/grid", json={"grid_id": gid})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["grid_id"] == gid
    assert store.get_pulse(pid).grid_id == gid

    # ungrid (grid_id null)
    r2 = await client.patch(f"/pulses/{pid}/grid", json={"grid_id": None})
    assert r2.status_code == 200
    assert r2.json()["value"]["grid_id"] is None
    assert store.get_pulse(pid).grid_id is None


@pytest.mark.anyio
async def test_move_pulse_unknown_grid_404(client, store):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/grid", json={"grid_id": "nope"})
    assert r.status_code == 404
    assert r.json()["error"]["code"] == "not_found"


@pytest.mark.anyio
async def test_move_unknown_pulse_404(client):
    r = await client.patch("/pulses/nope/grid", json={"grid_id": None})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# rename pulse
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_rename_pulse(client, store):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]

    r = await client.patch(f"/pulses/{pid}/title", json={"title": "  fresh  "})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["title"] == "fresh"
    assert store.get_pulse(pid).title == "fresh"

    # blank title is rejected, stored title untouched
    r2 = await client.patch(f"/pulses/{pid}/title", json={"title": "   "})
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "bad_request"
    assert store.get_pulse(pid).title == "fresh"


@pytest.mark.anyio
async def test_rename_unknown_pulse_404(client):
    r = await client.patch("/pulses/nope/title", json={"title": "x"})
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# unarchive
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_unarchive_restores_to_stopped(client, store, fake_spawn, fake_sock):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    await client.post(f"/pulses/{pid}/archive")
    assert store.get_pulse(pid).state == "archived"

    r = await client.post(f"/pulses/{pid}/unarchive")
    assert r.status_code == 200, r.text
    assert r.json()["value"]["state"] == "stopped"
    assert store.get_pulse(pid).state == "stopped"
    assert store.get_pulse(pid).archived_at is None


@pytest.mark.anyio
async def test_unarchive_non_archived_noop(client, store, fake_spawn, fake_sock):
    # a running pulse: unarchive is a no-op that returns it unchanged
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    r = await client.post(f"/pulses/{pid}/unarchive")
    assert r.status_code == 200
    assert r.json()["value"]["state"] == "running"
    assert store.get_pulse(pid).state == "running"


@pytest.mark.anyio
async def test_unarchive_unknown_404(client):
    r = await client.post("/pulses/nope/unarchive")
    assert r.status_code == 404


# --------------------------------------------------------------------------- #
# hard delete
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_delete_running_refused_409(client, store, fake_spawn, fake_sock):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    assert store.get_pulse(pid).state == "running"
    r = await client.delete(f"/pulses/{pid}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "bad_state"
    # still present
    assert store.get_pulse(pid).state == "running"


@pytest.mark.anyio
async def test_delete_stopped_removes_row_and_moves_home(
        client, store, fake_spawn, fake_sock):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    assert store.get_pulse(pid).state == "stopped"

    home = paths.pulse_home(pid)
    assert home.exists()  # spawn/heartbeat created it

    r = await client.delete(f"/pulses/{pid}")
    assert r.status_code == 200, r.text
    body = r.json()["value"]
    assert body["deleted"] == pid
    moved = body["home_moved_to"]
    assert moved is not None and moved.startswith(f"{pid}.deleted-")

    # store row gone
    with pytest.raises(Exception):
        store.get_pulse(pid)
    # original home gone, renamed dir present (never rm -rf)
    assert not home.exists()
    assert (home.parent / moved).exists()


@pytest.mark.anyio
async def test_delete_archived_removes_row(client, store, fake_spawn, fake_sock):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    await client.post(f"/pulses/{pid}/archive")
    assert store.get_pulse(pid).state == "archived"
    r = await client.delete(f"/pulses/{pid}")
    assert r.status_code == 200, r.text
    with pytest.raises(Exception):
        store.get_pulse(pid)


@pytest.mark.anyio
async def test_delete_keeps_pulse_record(
        client, store, fake_spawn, fake_sock):
    """A record of every deleted pulse survives in its renamed home dir."""
    pid = (await client.post("/pulses", json={"title": "rec", "model": "m"})
           ).json()["value"]["id"]
    home = paths.pulse_home(pid)
    await client.post(f"/pulses/{pid}/archive")
    moved = (await client.delete(f"/pulses/{pid}")).json()["value"]["home_moved_to"]
    aside = home.parent / moved
    record = json.loads((aside / "pulse-record.json").read_text())
    assert record["id"] == pid and record["title"] == "rec"
    assert record["deleted_at"]


@pytest.mark.anyio
async def test_delete_no_home_dir_ok(client, store, fake_spawn, fake_sock):
    # a store-only pulse with no home on disk still deletes cleanly
    pulse = store.create_pulse("orphan", "m", "claude-code", state="stopped")
    r = await client.delete(f"/pulses/{pulse.id}")
    assert r.status_code == 200, r.text
    assert r.json()["value"]["home_moved_to"] is None


@pytest.mark.anyio
async def test_delete_unknown_404(client):
    r = await client.delete("/pulses/nope")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_delete_stops_lingering_unit_before_moving_home(
        client, store, fake_spawn, fake_sock, monkeypatch):
    """Pause replies before the runner has finished shutting down. Delete must
    check systemd, not just the store row, before renaming the home dir."""
    monkeypatch.setattr(lifecycle_routes, "DELETE_UNIT_SETTLE_S", 0.01)
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    assert store.get_pulse(pid).state == "stopped"
    # ...but the transient unit is still there.
    fake_spawn.units = [pid]
    fake_spawn.stopped.clear()

    r = await client.delete(f"/pulses/{pid}")
    assert r.status_code == 200, r.text
    assert fake_spawn.stopped == [pid]      # stopped it before touching disk
    assert fake_spawn.units == []
    assert r.json()["value"]["home_moved_to"].startswith(f"{pid}.deleted-")


@pytest.mark.anyio
async def test_delete_refused_when_unit_will_not_stop(
        client, store, fake_spawn, fake_sock, monkeypatch):
    """A unit that won't die keeps its home dir: 409, row intact."""
    monkeypatch.setattr(lifecycle_routes, "DELETE_UNIT_SETTLE_S", 0.01)
    monkeypatch.setattr(lifecycle_routes, "DELETE_UNIT_SETTLE_TRIES", 2)
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    fake_spawn.units = [pid]
    fake_spawn.stop_should_fail = True

    r = await client.delete(f"/pulses/{pid}")
    assert r.status_code == 409, r.text
    assert r.json()["error"]["code"] == "bad_state"
    assert store.get_pulse(pid).state == "stopped"   # row survived
    assert paths.pulse_home(pid).exists()            # home not renamed


# --------------------------------------------------------------------------- #
# list query params: grid_id + include_archived
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_list_filters_grid_and_archived(client, store, fake_spawn, fake_sock):
    gid = (await client.post("/grids", json={"name": "work"})
           ).json()["value"]["id"]
    in_grid = (await client.post("/pulses", json={"title": "in", "model": "m",
                                                  "grid_id": gid})
               ).json()["value"]["id"]
    out_grid = (await client.post("/pulses", json={"title": "out", "model": "m"})
                ).json()["value"]["id"]
    arch = (await client.post("/pulses", json={"title": "arch", "model": "m"})
            ).json()["value"]["id"]
    await client.post(f"/pulses/{arch}/archive")

    # default: archived hidden
    r = await client.get("/pulses")
    ids = {p["id"] for p in r.json()["value"]}
    assert in_grid in ids and out_grid in ids
    assert arch not in ids

    # include_archived=true shows it
    r2 = await client.get("/pulses?include_archived=true")
    ids2 = {p["id"] for p in r2.json()["value"]}
    assert arch in ids2

    # grid filter
    r3 = await client.get(f"/pulses?grid_id={gid}")
    ids3 = {p["id"] for p in r3.json()["value"]}
    assert ids3 == {in_grid}


@pytest.mark.anyio
async def test_list_slim_trims_archived_settings_only(client, store, fake_spawn, fake_sock):
    heavy = {"diet": {"skills": ["x" * 50]}, "backup": {"model": "b"},
             "spawned_by": "p0", "mode": "watching"}
    live_id = (await client.post("/pulses", json={"title": "live", "model": "m"})
               ).json()["value"]["id"]
    arch = (await client.post("/pulses", json={"title": "arch", "model": "m"})
            ).json()["value"]["id"]
    store.update_settings(live_id, heavy)
    store.update_settings(arch, heavy)
    await client.post(f"/pulses/{arch}/archive")

    rows = {p["id"]: p for p in (await client.get(
        "/pulses?include_archived=true&slim=true")).json()["value"]}
    assert rows[arch]["settings"] == {"mode": "watching"}
    assert rows[arch]["title"] == "arch" and rows[arch]["state"] == "archived"
    # live rows keep every setting; so do archived rows without slim
    assert rows[live_id]["settings"]["diet"] == heavy["diet"]
    full = {p["id"]: p for p in (await client.get(
        "/pulses?include_archived=true")).json()["value"]}
    assert full[arch]["settings"]["backup"] == {"model": "b"}
    only = (await client.get("/pulses?state=archived&slim=true")).json()["value"]
    assert [p["id"] for p in only] == [arch]


# --------------------------------------------------------------------------- #
# schedules — routes
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_create_loop_schedule(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    r = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "loop", "prompt": "go", "interval_minutes": 15})
    assert r.status_code == 201, r.text
    sched = r.json()["value"]
    assert sched["kind"] == "loop"
    assert sched["interval_minutes"] == 15
    assert sched["enabled"] is True
    assert store.list_schedules(pulse_id=pid)[0].id == sched["id"]


@pytest.mark.anyio
async def test_create_once_and_daily_schedule(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    r1 = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "once", "prompt": "x", "at_iso": "2026-08-01T09:00:00+00:00"})
    assert r1.status_code == 201, r1.text
    r2 = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "daily", "prompt": "x", "cron_hhmm": "09:00",
        "days": "mon,fri"})
    assert r2.status_code == 201, r2.text


@pytest.mark.anyio
async def test_create_schedule_bad_kind_400(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    r = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "loop", "prompt": "go"})  # no interval
    assert r.status_code == 400, r.text
    assert r.json()["error"]["code"] == "bad_request"

    r2 = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "once", "prompt": "go", "at_iso": "not-a-date"})
    assert r2.status_code == 400

    r3 = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "daily", "prompt": "go", "cron_hhmm": "99:99"})
    assert r3.status_code == 400

    r4 = await client.post(f"/pulses/{pid}/schedules", json={
        "kind": "weekly", "prompt": "go"})
    assert r4.status_code == 400


@pytest.mark.anyio
async def test_create_schedule_unknown_pulse_404(client):
    r = await client.post("/pulses/nope/schedules", json={
        "kind": "loop", "prompt": "go", "interval_minutes": 5})
    assert r.status_code == 404


@pytest.mark.anyio
async def test_list_schedules_by_pulse_and_all(client, store, fake_spawn):
    p1 = (await client.post("/pulses", json={"title": "a", "model": "m"})
          ).json()["value"]["id"]
    p2 = (await client.post("/pulses", json={"title": "b", "model": "m"})
          ).json()["value"]["id"]
    await client.post(f"/pulses/{p1}/schedules",
                      json={"kind": "loop", "prompt": "x", "interval_minutes": 5})
    await client.post(f"/pulses/{p2}/schedules",
                      json={"kind": "loop", "prompt": "y", "interval_minutes": 5})

    r = await client.get(f"/pulses/{p1}/schedules")
    assert r.status_code == 200
    assert len(r.json()["value"]) == 1

    r_all = await client.get("/schedules")
    assert len(r_all.json()["value"]) == 2


@pytest.mark.anyio
async def test_patch_schedule_enabled(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    sid = (await client.post(f"/pulses/{pid}/schedules",
           json={"kind": "loop", "prompt": "x", "interval_minutes": 5})
           ).json()["value"]["id"]
    r = await client.patch(f"/schedules/{sid}", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["enabled"] is False
    assert store.get_schedule(sid).enabled is False


@pytest.mark.anyio
async def test_patch_schedule_unknown_404(client):
    r = await client.patch("/schedules/nope", json={"enabled": False})
    assert r.status_code == 404


@pytest.mark.anyio
async def test_patch_schedule_edit_loop(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    sid = (await client.post(f"/pulses/{pid}/schedules",
           json={"kind": "loop", "prompt": "x", "interval_minutes": 5})
           ).json()["value"]["id"]
    r = await client.patch(f"/schedules/{sid}",
                           json={"prompt": "y", "interval_minutes": 20})
    assert r.status_code == 200, r.text
    body = r.json()["value"]
    assert body["prompt"] == "y"
    assert body["interval_minutes"] == 20
    assert store.get_schedule(sid).prompt == "y"


@pytest.mark.anyio
async def test_patch_schedule_edit_rejections(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    loop_sid = (await client.post(f"/pulses/{pid}/schedules",
                json={"kind": "loop", "prompt": "x", "interval_minutes": 5})
                ).json()["value"]["id"]
    once_sid = (await client.post(f"/pulses/{pid}/schedules",
                json={"kind": "once", "prompt": "z",
                      "at_iso": "2026-01-01T00:00:00Z"})
                ).json()["value"]["id"]
    # empty prompt
    r = await client.patch(f"/schedules/{loop_sid}", json={"prompt": "  "})
    assert r.status_code == 400
    # bad interval
    r = await client.patch(f"/schedules/{loop_sid}",
                           json={"interval_minutes": 0})
    assert r.status_code == 400
    # once/daily schedules are not editable
    r = await client.patch(f"/schedules/{once_sid}", json={"prompt": "y"})
    assert r.status_code == 400


@pytest.mark.anyio
async def test_delete_schedule(client, store, fake_spawn):
    pid = (await client.post("/pulses", json={"title": "t", "model": "m"})
           ).json()["value"]["id"]
    sid = (await client.post(f"/pulses/{pid}/schedules",
           json={"kind": "loop", "prompt": "x", "interval_minutes": 5})
           ).json()["value"]["id"]
    r = await client.delete(f"/schedules/{sid}")
    assert r.status_code == 200, r.text
    assert r.json()["value"]["deleted"] == sid
    assert store.list_schedules(pulse_id=pid) == []


@pytest.mark.anyio
async def test_delete_schedule_unknown_404(client):
    r = await client.delete("/schedules/nope")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_create_pulse_with_loop_atomic(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "looped", "model": "m",
        "loop_prompt": "do the loop", "loop_minutes": 20})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    scheds = store.list_schedules(pulse_id=pid)
    assert len(scheds) == 1
    assert scheds[0].kind == "loop"
    assert scheds[0].interval_minutes == 20
    assert scheds[0].prompt == "do the loop"


@pytest.mark.anyio
async def test_create_pulse_loop_half_spec_400(client, store, fake_spawn):
    # only loop_minutes, no prompt -> 400, and no pulse created
    r = await client.post("/pulses", json={
        "title": "bad", "model": "m", "loop_minutes": 20})
    assert r.status_code == 400, r.text
    assert store.list_pulses() == []


@pytest.mark.anyio
async def test_create_pulse_loop_bad_minutes_400(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "bad", "model": "m",
        "loop_prompt": "x", "loop_minutes": 0})
    assert r.status_code == 400
    assert store.list_pulses() == []


# --------------------------------------------------------------------------- #
# Model switch
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_switch_model_stopped_pulse_store_only(client, store, fake_spawn,
                                                     fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    spawns_before = len(fake_spawn.spawned)

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-fable-5"})
    assert r.status_code == 200, r.text
    row = store.get_pulse(pid)
    assert (row.model, row.vessel, row.state) == \
        ("claude-fable-5", "claude-code", "stopped")
    # nothing respawned — restore will use the new store values
    assert len(fake_spawn.spawned) == spawns_before


@pytest.mark.anyio
async def test_switch_model_follows_registered_vessel(client, store, fake_spawn,
                                                      fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": "pi-glm-53",
                                           "vessel": "pi"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "agy-gemini-4-argon"})
    assert r.status_code == 200, r.text
    row = store.get_pulse(pid)
    assert (row.model, row.vessel) == ("agy-gemini-4-argon", "antigravity")


@pytest.mark.anyio
async def test_switch_model_running_same_vessel_respawns_with_resume(
        client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-fable-5"})
    assert r.status_code == 200, r.text
    # old runner stopped over the socket, new one spawned resuming the session
    assert fake_sock.stopped == [pid]
    call = fake_spawn.spawned[-1]
    assert call["model"] == "claude-fable-5"
    assert call["resume"] == f"sess-{pid}"
    row = store.get_pulse(pid)
    assert (row.model, row.state) == ("claude-fable-5", "running")


@pytest.mark.anyio
async def test_switch_model_noop_when_unchanged(client, store, fake_spawn,
                                                fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    spawns_before = len(fake_spawn.spawned)
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5"})
    assert r.status_code == 200
    assert fake_sock.stopped == []
    assert len(fake_spawn.spawned) == spawns_before


@pytest.mark.anyio
async def test_switch_model_cross_vessel_rides_handoff(client, store,
                                                       fake_spawn, fake_sock,
                                                       monkeypatch):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]

    calls = []

    async def fake_handoff(st, pulse, *, new_model=None, new_vessel=None,
                           reason="", reply_timeout_s=None,
                           fallback_to_transcript=False):
        calls.append((pulse.id, new_model, new_vessel, reason))
        # A switch waits a bounded time, then seeds from the transcript.
        assert reply_timeout_s and fallback_to_transcript
        st.set_model(pulse.id, new_model, new_vessel)
        return True

    monkeypatch.setattr(handoff_engine_mod, "run_handoff", fake_handoff)
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "gpt-5.5-codex", "vessel": "codex"})
    assert r.status_code == 200, r.text
    assert calls == [(pid, "gpt-5.5-codex", "codex", "model_switch")]
    row = store.get_pulse(pid)
    assert (row.model, row.vessel) == ("gpt-5.5-codex", "codex")


@pytest.mark.anyio
async def test_create_with_effort_stores_and_spawns(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "e", "model": "claude-opus-5", "effort": "high"})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings.get("effort") == "high"
    assert fake_spawn.spawned[-1]["effort"] == "high"


@pytest.mark.anyio
async def test_create_with_bad_effort_400(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "e", "model": "claude-opus-5", "effort": "turbo"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_effort"
    # pi V4 has no dial
    r = await client.post("/pulses", json={
        "title": "v", "model": "pi-v4-flash", "effort": "high"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_effort"


@pytest.mark.anyio
async def test_create_glm52_effort_off_high_max(client, store, fake_spawn):
    for level in ("off", "high", "max"):
        r = await client.post("/pulses", json={
            "title": f"g-{level}", "model": "pi-glm-52", "effort": level})
        assert r.status_code == 201, r.text
        pid = r.json()["value"]["id"]
        assert store.get_pulse(pid).settings.get("effort") == level
        assert fake_spawn.spawned[-1]["effort"] == level
    r = await client.post("/pulses", json={
        "title": "g-low", "model": "pi-glm-52", "effort": "low"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_effort"


@pytest.mark.anyio
async def test_switch_effort_only_respawns_with_resume(client, store,
                                                       fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]

    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5", "effort": "max"})
    assert r.status_code == 200, r.text
    assert fake_sock.stopped == [pid]  # same-vessel respawn path
    call = fake_spawn.spawned[-1]
    assert call["effort"] == "max"
    assert call["resume"] == f"sess-{pid}"  # the conversation continues
    assert store.get_pulse(pid).settings.get("effort") == "max"

    # "" clears back to the CLI default (and respawns again)
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5", "effort": ""})
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["effort"] is None
    assert store.get_pulse(pid).settings.get("effort") is None


@pytest.mark.anyio
async def test_switch_effort_unchanged_noop(client, store, fake_spawn,
                                            fake_sock):
    r = await client.post("/pulses", json={
        "title": "s", "model": "claude-opus-5", "effort": "high"})
    pid = r.json()["value"]["id"]
    spawns_before = len(fake_spawn.spawned)
    # same model, effort omitted (= keep) -> no-op
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5"})
    assert r.status_code == 200
    assert len(fake_spawn.spawned) == spawns_before
    # same model, same effort explicit -> still a no-op
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5", "effort": "high"})
    assert r.status_code == 200
    assert len(fake_spawn.spawned) == spawns_before


@pytest.mark.anyio
async def test_switch_bad_effort_400(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    spawns_before = len(fake_spawn.spawned)
    # "none" is codex-only — refused for a claude vessel, nothing respawned
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5", "effort": "none"})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_effort"
    assert len(fake_spawn.spawned) == spawns_before


@pytest.mark.anyio
async def test_restore_and_reset_carry_effort(client, store, fake_spawn,
                                              fake_sock):
    r = await client.post("/pulses", json={
        "title": "s", "model": "claude-opus-5", "effort": "xhigh"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["effort"] == "xhigh"
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["effort"] == "xhigh"


@pytest.mark.anyio
async def test_create_with_fast_stores_and_spawns(client, store, fake_spawn):
    r = await client.post("/pulses", json={
        "title": "f", "model": "gpt-6-sol", "vessel": "codex", "fast": True})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]
    assert store.get_pulse(pid).settings.get("fast") is True
    assert fake_spawn.spawned[-1]["fast"] is True


@pytest.mark.anyio
async def test_create_with_fast_non_codex_400(client, store, fake_spawn):
    # only the codex CLI takes a service-tier switch — refused honestly
    r = await client.post("/pulses", json={
        "title": "f", "model": "claude-opus-5", "fast": True})
    assert r.status_code == 400
    assert r.json()["error"]["code"] == "bad_fast"


@pytest.mark.anyio
async def test_restore_and_reset_carry_fast(client, store, fake_spawn,
                                            fake_sock):
    r = await client.post("/pulses", json={
        "title": "f", "model": "gpt-6-sol", "vessel": "codex", "fast": True})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    r = await client.post(f"/pulses/{pid}/restore")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["fast"] is True
    r = await client.post(f"/pulses/{pid}/reset")
    assert r.status_code == 200, r.text
    assert fake_spawn.spawned[-1]["fast"] is True


@pytest.mark.anyio
async def test_switch_off_codex_clears_fast(client, store, fake_spawn,
                                            fake_sock):
    # A stored fast flag can't follow the pulse onto a non-codex vessel.
    r = await client.post("/pulses", json={
        "title": "f", "model": "gpt-6-sol", "vessel": "codex", "fast": True})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    r = await client.patch(f"/pulses/{pid}/model",
                           json={"model": "claude-opus-5",
                                 "vessel": "claude-code"})
    assert r.status_code == 200, r.text
    assert not store.get_pulse(pid).settings.get("fast")


@pytest.mark.anyio
async def test_set_handoff_threshold(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "h", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    r = await client.patch(f"/pulses/{pid}/handoff",
                           json={"handoff_tokens": 250000})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).handoff_tokens == 250000
    r = await client.patch(f"/pulses/{pid}/handoff", json={"handoff_tokens": None})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).handoff_tokens is None


@pytest.mark.anyio
async def test_stream_toggle_and_partial_endpoint(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    # Live streaming is ON by default for every agent tool.
    assert store.get_pulse(pid).settings.get("stream_partials") is True
    assert r.json()["value"]["settings"].get("stream_partials") is True

    r_pi = await client.post("/pulses", json={
        "title": "pi-s", "model": "pi-qwen38-flash", "vessel": "pi"})
    assert r_pi.status_code in (200, 201), r_pi.text
    # A local pi model: also ON in the store and the roster view.
    assert r_pi.json()["value"]["settings"].get("stream_partials") is True
    assert store.get_pulse(r_pi.json()["value"]["id"]).settings.get(
        "stream_partials") is True

    r = await client.patch(f"/pulses/{pid}/stream", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("stream_partials") is False
    assert r.json()["value"]["settings"]["stream_partials"] is False

    r = await client.patch(f"/pulses/{pid}/stream", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("stream_partials") is True
    assert r.json()["value"]["settings"]["stream_partials"] is True

    # No partial on disk -> null, not an error.
    r = await client.get(f"/pulses/{pid}/partial")
    assert r.status_code == 200 and r.json()["value"]["partial"] is None

    home = paths.pulse_home(pid)
    home.mkdir(parents=True, exist_ok=True)
    (home / "partial.json").write_text(
        json.dumps({"ts": "2026-08-08T00:00:00+0000", "text": "half a rep"}))
    r = await client.get(f"/pulses/{pid}/partial")
    assert r.json()["value"]["partial"]["text"] == "half a rep"

    # A torn write reads as "not streaming", never a 500.
    (home / "partial.json").write_text('{"ts": "t", "te')
    r = await client.get(f"/pulses/{pid}/partial")
    assert r.json()["value"]["partial"] is None

    r = await client.patch(f"/pulses/{pid}/stream", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("stream_partials") is False


@pytest.mark.anyio
async def test_reasoning_endpoint_reads_trace(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "r", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    home = paths.pulse_home(pid)
    home.mkdir(parents=True, exist_ok=True)

    # No trace on disk -> null, not an error.
    r = await client.get(f"/pulses/{pid}/reasoning")
    assert r.status_code == 200 and r.json()["value"]["reasoning"] is None

    (home / "reasoning.json").write_text(
        json.dumps({"ts": "2026-09-12T00:00:00+0000", "text": "weighing it"}))
    r = await client.get(f"/pulses/{pid}/reasoning")
    assert r.json()["value"]["reasoning"]["text"] == "weighing it"
    # Old runner (no "block"): null, the client falls back to "text".
    assert r.json()["value"]["reasoning"]["block"] is None

    # "block" = the not-yet-carded tail for the pane's live thinking ghost.
    (home / "reasoning.json").write_text(json.dumps(
        {"ts": "2026-09-14T00:00:00+0000", "text": "a\n\nb", "block": "b"}))
    r = await client.get(f"/pulses/{pid}/reasoning")
    assert r.json()["value"]["reasoning"]["block"] == "b"

    # A torn write reads as "nothing", never a 500.
    (home / "reasoning.json").write_text('{"ts": "t", "te')
    r = await client.get(f"/pulses/{pid}/reasoning")
    assert r.json()["value"]["reasoning"] is None


@pytest.mark.anyio
async def test_reasoning_toggle_is_a_per_pulse_ui_gate(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "rt", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    # ON by default on every vessel (stored at create and in the view).
    assert store.get_pulse(pid).settings.get("stream_reasoning") is True
    assert r.json()["value"]["settings"].get("stream_reasoning") is True

    r_pi = await client.post("/pulses", json={
        "title": "pi-rt", "model": "pi-qwen38-flash", "vessel": "pi"})
    assert r_pi.status_code in (200, 201), r_pi.text
    # A local pi model: also ON in the store and the roster view.
    assert r_pi.json()["value"]["settings"].get("stream_reasoning") is True
    assert store.get_pulse(r_pi.json()["value"]["id"]).settings.get(
        "stream_reasoning") is True

    r = await client.patch(f"/pulses/{pid}/reasoning", json={"enabled": True})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("stream_reasoning") is True
    assert r.json()["value"]["settings"]["stream_reasoning"] is True

    r = await client.patch(f"/pulses/{pid}/reasoning", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("stream_reasoning") is False


@pytest.mark.anyio
async def test_code_endpoint_and_toggle(client, store, fake_spawn):
    r = await client.post("/pulses", json={"title": "c", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    # ON by default in the view.
    assert r.json()["value"]["settings"].get("stream_code") is True
    home = paths.pulse_home(pid)
    home.mkdir(parents=True, exist_ok=True)

    r = await client.get(f"/pulses/{pid}/code")
    assert r.status_code == 200 and r.json()["value"]["code"] is None
    (home / "code.json").write_text(json.dumps(
        {"ts": "t", "id": "t1", "name": "Write", "text": '{"a', "truncated": False}))
    code = (await client.get(f"/pulses/{pid}/code")).json()["value"]["code"]
    assert code == {"ts": "t", "id": "t1", "name": "Write", "text": '{"a',
                    "truncated": False}
    (home / "code.json").write_text('{"ts": "t", "te')
    r = await client.get(f"/pulses/{pid}/code")
    assert r.json()["value"]["code"] is None

    r = await client.patch(f"/pulses/{pid}/code", json={"enabled": False})
    assert r.status_code == 200, r.text
    assert store.get_pulse(pid).settings.get("stream_code") is False
    assert r.json()["value"]["settings"]["stream_code"] is False


@pytest.mark.anyio
async def test_settings_kv_roundtrip(client):
    r = await client.get("/settings/theme_profiles")
    assert r.status_code == 200 and r.json()["value"] is None
    r = await client.put("/settings/theme_profiles",
                         content=json.dumps([{"name": "Pulsar Cyan"}]))
    assert r.status_code == 200
    r = await client.get("/settings/theme_profiles")
    assert r.json()["value"] == [{"name": "Pulsar Cyan"}]
    r = await client.delete("/settings/theme_profiles")
    assert r.status_code == 200
    r = await client.get("/settings/theme_profiles")
    assert r.json()["value"] is None


# --------------------------------------------------------------------------- #
# Fork (branch the conversation into a new pulse)
# --------------------------------------------------------------------------- #
@pytest.mark.anyio
async def test_fork_copies_claude_history_and_resumes(client, store, fake_spawn,
                                                      fake_sock, tmp_path,
                                                      monkeypatch):
    monkeypatch.setenv("PULSAR_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    # FakeSpawn heartbeats cli_session_id=f"sess-{pid}" on spawn.
    sid = f"sess-{pid}"
    src_home = paths.pulse_home(pid)
    hist = session_history_file(src_home / "cli", sid)
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text('{"a":1}\n{"torn')  # torn tail: source may be mid-append
    (src_home / "events.jsonl").write_text('{"e":1}\n')
    fake_spawn.spawned.clear()

    r2 = await client.post(f"/pulses/{pid}/fork", json={})
    assert r2.status_code == 201, r2.text
    new = r2.json()["value"]
    assert new["id"] != pid
    assert new["title"] == "t (fork)"
    assert new["state"] == "running"
    assert len(fake_spawn.spawned) == 1
    assert fake_spawn.spawned[0]["resume"] == sid
    # History copied into the NEW cwd's munged project dir, torn line trimmed.
    dst = session_history_file(paths.pulse_home(new["id"]) / "cli", sid)
    assert dst.read_text() == '{"a":1}\n'
    # Pane transcript rides along.
    assert (paths.pulse_home(new["id"]) / "events.jsonl").read_text() == '{"e":1}\n'
    # Source untouched.
    assert hist.read_text() == '{"a":1}\n{"torn'
    assert store.get_pulse(pid).state == "running"


@pytest.mark.anyio
async def test_fork_custom_title(client, store, fake_spawn, fake_sock,
                                 tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    hist = session_history_file(paths.pulse_home(pid) / "cli", f"sess-{pid}")
    hist.parent.mkdir(parents=True, exist_ok=True)
    hist.write_text('{"a":1}\n')

    r2 = await client.post(f"/pulses/{pid}/fork", json={"title": "branch A"})
    assert r2.status_code == 201, r2.text
    assert r2.json()["value"]["title"] == "branch A"


@pytest.mark.anyio
async def test_fork_without_session_409s(client, store, fake_spawn, fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    _write_heartbeat(pid, state="idle", cli_session_id=None, age_s=0.0)

    r2 = await client.post(f"/pulses/{pid}/fork", json={})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "no_session"


@pytest.mark.anyio
async def test_fork_without_history_file_409s(client, store, fake_spawn,
                                              fake_sock, tmp_path, monkeypatch):
    monkeypatch.setenv("PULSAR_CLAUDE_PROJECTS", str(tmp_path / "projects"))
    r = await client.post("/pulses", json={"title": "t", "model": "m"})
    pid = r.json()["value"]["id"]
    # Heartbeat has a session id but no session file exists on disk.
    before = {p.id for p in store.list_pulses()}

    r2 = await client.post(f"/pulses/{pid}/fork", json={})
    assert r2.status_code == 409
    assert r2.json()["error"]["code"] == "no_history"
    # Pre-flight failed — no half-made pulse row.
    assert {p.id for p in store.list_pulses()} == before


@pytest.mark.anyio
@pytest.mark.parametrize("vessel", [
    "grok", "antigravity",
])
async def test_fork_unsupported_vessels_return_400(client, store, fake_spawn,
                                                   fake_sock, vessel):
    r = await client.post("/pulses", json={"title": "t", "model": "m",
                                           "vessel": vessel})
    assert r.status_code == 201, r.text
    pid = r.json()["value"]["id"]

    r2 = await client.post(f"/pulses/{pid}/fork", json={})
    assert r2.status_code == 400
    assert r2.json()["error"]["code"] == "fork_unsupported"
    assert vessel in r2.json()["error"]["message"]


@pytest.mark.anyio
async def test_fork_pi_copies_pi_sessions_dir(client, store, fake_spawn,
                                              fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m",
                                           "vessel": "pi"})
    pid = r.json()["value"]["id"]
    sessions = paths.pulse_home(pid) / "cli" / "pi-sessions"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "sess.jsonl").write_text('{"a":1}\n')
    fake_spawn.spawned.clear()

    r2 = await client.post(f"/pulses/{pid}/fork", json={})
    assert r2.status_code == 201, r2.text
    new = r2.json()["value"]
    assert fake_spawn.spawned[0]["resume"] == f"sess-{pid}"
    dst = paths.pulse_home(new["id"]) / "cli" / "pi-sessions" / "sess.jsonl"
    assert dst.read_text() == '{"a":1}\n'


@pytest.mark.anyio
async def test_fork_codex_copies_sessions_dir(client, store, fake_spawn,
                                              fake_sock):
    r = await client.post("/pulses", json={"title": "t", "model": "m",
                                           "vessel": "codex"})
    pid = r.json()["value"]["id"]
    sessions = profiles_mod.codex_home(paths.pulse_home(pid)) / "sessions" / "2026"
    sessions.mkdir(parents=True, exist_ok=True)
    (sessions / "rollout.jsonl").write_text('{"a":1}\n')
    fake_spawn.spawned.clear()

    r2 = await client.post(f"/pulses/{pid}/fork", json={})
    assert r2.status_code == 201, r2.text
    new = r2.json()["value"]
    assert fake_spawn.spawned[0]["resume"] == f"sess-{pid}"
    dst = (profiles_mod.codex_home(paths.pulse_home(new["id"]))
           / "sessions" / "2026" / "rollout.jsonl")
    assert dst.read_text() == '{"a":1}\n'


# --------------------------------------------------------------------------- #
# Same-workspace default — helpers inherit the spawner's grid
# --------------------------------------------------------------------------- #

@pytest.mark.anyio
async def test_spawned_helper_inherits_spawner_grid(client, store, fake_spawn):
    grid = await _post_grid(client, "lead-grid")
    lead = await _post_pulse(client, grid_id=grid, title="lead")
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-sonnet-4-5",
        "from_pulse_id": lead})
    assert r.status_code == 201, r.text
    assert r.json()["value"]["grid_id"] == grid


@pytest.mark.anyio
async def test_spawned_helper_explicit_grid_wins(client, store, fake_spawn):
    lead_grid = await _post_grid(client, "lead-grid")
    other = await _post_grid(client, "other-grid")
    lead = await _post_pulse(client, grid_id=lead_grid, title="lead")
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-sonnet-4-5",
        "from_pulse_id": lead, "grid_id": other})
    assert r.status_code == 201, r.text
    assert r.json()["value"]["grid_id"] == other


@pytest.mark.anyio
async def test_spawned_helper_no_spawner_grid_stays_null(client, store, fake_spawn):
    lead = await _post_pulse(client, title="lead")
    r = await client.post("/pulses", json={
        "title": "helper", "model": "claude-sonnet-4-5",
        "from_pulse_id": lead})
    assert r.status_code == 201, r.text
    assert r.json()["value"]["grid_id"] is None


# --------------------------------------------------------------------------- #
# Archive idle — bulk archive by last-used age
# --------------------------------------------------------------------------- #

def _backdate(store, pulse_id, hours):
    import sqlite3
    from datetime import timedelta
    ts = (datetime.now(timezone.utc) - timedelta(hours=hours)).isoformat(timespec="seconds")
    with sqlite3.connect(store._path) as c:
        c.execute("UPDATE pulses SET updated_at = ? WHERE id = ?;", (ts, pulse_id))


@pytest.mark.anyio
async def test_archive_idle_archives_only_old_pulses(client, store, fake_spawn):
    old = await _post_pulse(client, title="old")
    fresh = await _post_pulse(client, title="fresh")
    _backdate(store, old, hours=30)
    r = await client.post("/pulses/archive-idle", json={"idle_hours": 12})
    assert r.status_code == 200, r.text
    v = r.json()["value"]
    assert [a["id"] for a in v["archived"]] == [old]
    assert v["skipped"] == []
    assert store.get_pulse(old).state == "archived"
    assert store.get_pulse(fresh).state == "running"


@pytest.mark.anyio
async def test_archive_idle_recent_events_file_keeps_pulse(client, store, fake_spawn):
    """updated_at is stale but events.jsonl was touched recently → still used."""
    p = await _post_pulse(client, title="chatty")
    _backdate(store, p, hours=30)
    ev = paths.events_path(p)
    ev.parent.mkdir(parents=True, exist_ok=True)
    ev.write_text('{"kind":"x"}\n')  # mtime = now
    r = await client.post("/pulses/archive-idle", json={"idle_hours": 12})
    assert r.status_code == 200, r.text
    assert r.json()["value"]["archived"] == []
    assert store.get_pulse(p).state == "running"


@pytest.mark.anyio
async def test_archive_idle_rejects_bad_hours(client, store, fake_spawn):
    r = await client.post("/pulses/archive-idle", json={"idle_hours": 0})
    assert r.status_code == 422
    r = await client.post("/pulses/archive-idle", json={"hours": 5})
    assert r.status_code == 422


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["running", "stopped", "archived"])
async def test_delete_watch_owner_refused(client, store, state):
    pulse = store.create_pulse("watched", "m", "v")
    if state != "running":
        store.set_state(pulse.id, state)
    watch = store.create_condition_watch(
        pulse.id, "file", {"path": "/tmp/not-used"},
        next_check_at="2099-01-01T00:00:00+00:00")
    r = await client.delete(f"/pulses/{pulse.id}")
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "has_watchers"
    assert store.get_pulse(pulse.id).state == state
    assert store.get_condition_watch(watch.id).state == "watching"


@pytest.mark.anyio
async def test_delete_pulse_preserves_mail(client, store):
    pulse = store.create_pulse("Mail sender", "m", "v", state="stopped")
    first = store.create_inbox_item(
        "projects", "pulse", "First report", "Keep this message",
        source_label=pulse.title, source_pulse_id=pulse.id, thread_key="keep-mail")
    second = store.create_inbox_item(
        "projects", "pulse", "Follow-up", "Keep this too",
        source_label=pulse.title, source_pulse_id=pulse.id, thread_key="keep-mail")
    store.set_inbox_read(first.id, True)
    before = [store.get_inbox_item(i.id) for i in (first, second)]
    r = await client.delete(f"/pulses/{pulse.id}")
    assert r.status_code == 200, r.text
    assert [store.get_inbox_item(i.id) for i in (first, second)] == before
    r = await client.get("/inbox", params={"thread_key": "keep-mail", "unread_only": "false"})
    assert r.status_code == 200, r.text
    messages = r.json()["value"]
    assert {m["id"] for m in messages} == {first.id, second.id}
    assert {m["body"] for m in messages} == {"Keep this message", "Keep this too"}
    assert all(m["source_label"] == "Mail sender" for m in messages)


@pytest.mark.anyio
async def test_delete_delegation_watch(client, store):
    """DELETE /watches/{id} closes a delegation watch early (outcome
    cancelled); unknown id 404; already-closed is a no-op."""
    from datetime import datetime, timedelta, timezone
    a = store.create_pulse("lead", "m", "claude-code")
    b = store.create_pulse("helper", "m", "claude-code")
    soon = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    w = store.create_watch(a.id, b.id, "message", next_check_at=soon)
    r = await client.delete(f"/watches/{w.id}")
    assert r.status_code == 200, r.text
    assert r.json()["value"]["state"] == "closed"
    assert r.json()["value"]["outcome"] == "cancelled"
    r = await client.delete(f"/watches/{w.id}")
    assert r.status_code == 200 and r.json()["value"]["state"] == "closed"
    r = await client.delete("/watches/nope")
    assert r.status_code == 404


@pytest.mark.anyio
async def test_grid_create_sub_workspace_and_patch_parent(client, store):
    a = (await client.post("/grids", json={"name": "A"})).json()["value"]
    sub = (await client.post("/grids", json={"name": "sub", "parent_id": a["id"]})).json()["value"]
    assert sub["parent_id"] == a["id"]
    listing = (await client.get("/grids")).json()["value"]
    assert [g["id"] for g in listing][:2] == [a["id"], sub["id"]]
    # deeper nesting is a 400, unknown parent a 404
    r = await client.post("/grids", json={"name": "deep", "parent_id": sub["id"]})
    assert r.status_code == 400
    r = await client.post("/grids", json={"name": "x", "parent_id": "nope"})
    assert r.status_code == 404
    # lift it back to top level with an explicit null
    r = await client.patch(f"/grids/{sub['id']}", json={"parent_id": None})
    assert r.status_code == 200 and r.json()["value"]["parent_id"] is None
    # a rename without parent_id leaves nesting alone
    await client.patch(f"/grids/{sub['id']}", json={"parent_id": a["id"]})
    r = await client.patch(f"/grids/{sub['id']}", json={"name": "sub2"})
    assert r.json()["value"]["parent_id"] == a["id"]


@pytest.mark.anyio
@pytest.mark.parametrize("succeeds", [True, False])
async def test_stopped_cross_vessel_switch_forces_fresh_handoff(
        client, store, fake_spawn, fake_sock, monkeypatch, succeeds):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    await client.post(f"/pulses/{pid}/pause")
    calls = []

    async def handoff(st, pulse, **kwargs):
        calls.append((pulse.state, kwargs))
        if succeeds:
            st.set_model(pid, kwargs["new_model"], kwargs["new_vessel"])
        return succeeds

    monkeypatch.setattr(handoff_engine_mod, "run_handoff", handoff)
    r = await client.patch(f"/pulses/{pid}/model", json={
        "model": "gpt-6-luna", "vessel": "codex", "effort": "high"})
    assert r.status_code == (200 if succeeds else 502), r.text
    assert calls == [("stopped", {
        "new_model": "gpt-6-luna", "new_vessel": "codex",
        "reason": "model_switch", "force": True})]
    assert store.get_pulse(pid).vessel == ("codex" if succeeds else "claude-code")


@pytest.mark.anyio
@pytest.mark.parametrize("state", ["running", "stopped"])
async def test_force_switch_even_same_model_never_resumes_or_asks_old_model(
        client, store, fake_spawn, fake_sock, monkeypatch, state):
    r = await client.post("/pulses", json={"title": "s", "model": "claude-opus-5"})
    pid = r.json()["value"]["id"]
    if state == "stopped":
        await client.post(f"/pulses/{pid}/pause")
    calls = []

    async def handoff(st, pulse, **kwargs):
        calls.append(kwargs)
        return True

    monkeypatch.setattr(handoff_engine_mod, "run_handoff", handoff)
    r = await client.patch(f"/pulses/{pid}/model", json={
        "model": "claude-opus-5", "force": True})
    assert r.status_code == 200, r.text
    assert len(calls) == 1 and calls[0]["force"] is True
