"""Credential rotations must not terminate a healthy runner or replay tools."""
import asyncio
import json
import functools

import pytest

from pulsar.runner import vessel_bridge
from pulsar.runner.vessel_bridge import BridgedVessel
from pulsar.vessel.claude import ClaudeVessel, VesselError
from pulsar.vessel import VesselEvent


def run_async(fn):
    @functools.wraps(fn)
    def wrapped(*args, **kwargs):
        return asyncio.run(fn(*args, **kwargs))
    return wrapped


class FakeClaude(ClaudeVessel):
    def __init__(self):
        self.spawns = []
        self.sent = []
        self.stopped = []
        self.queues = {}
        self.on_send = None

    async def spawn(self, **kwargs):
        sid = str(len(self.spawns))
        self.spawns.append(kwargs)
        self.queues[sid] = asyncio.Queue()
        return sid

    def cli_session_id(self, sid):
        return 'saved-history'

    async def send(self, sid, text):
        self.sent.append((sid, text))
        if self.on_send:
            self.on_send(sid, text)

    def subscribe(self, sid):
        async def stream():
            while True:
                yield await self.queues[sid].get()
        return stream()

    async def stop(self, sid):
        self.stopped.append(sid)

    def emit(self, sid, kind, **payload):
        self.queues[sid].put_nowait(VesselEvent(kind=kind, payload=payload))


def credentials(path, token):
    (path / '.credentials.json').write_text(json.dumps(
        {'claudeAiOauth': {'accessToken': token}}))


async def collect(bridge, text='original request'):
    return [event async for event in bridge.send(text)]


@pytest.fixture(autouse=True)
def no_real_sync_wait(monkeypatch):
    monkeypatch.setattr(vessel_bridge, "CLAUDE_AUTH_SYNC_GRACE_SECONDS", 0.0)


def setup(tmp_path):
    credentials(tmp_path, 'old')
    return BridgedVessel('claude-test', tmp_path, None, vessel_cls=FakeClaude,
                         env={'CLAUDE_CONFIG_DIR': str(tmp_path)},
                         diet={'clean': True}, effort='low')


@run_async
async def test_idle_rotation_resumes_before_sending(tmp_path):
    b = setup(tmp_path)
    await b.start()
    lifetime = b._exited
    waiter = asyncio.create_task(b.wait())
    credentials(tmp_path, 'new')
    b._v.on_send = lambda sid, _: b._v.emit(sid, 'done', reason='result')
    try:
        await collect(b)
        assert len(b._v.spawns) == 2
        assert b._v.spawns[1]['resume_cli_session_id'] == 'saved-history'
        assert b._v.spawns[1]['effort'] == 'low'
        assert b._v.spawns[1]['diet'] == {'clean': True}
        assert b._v.sent == [('1', 'original request')]
        assert b._exited is lifetime and not lifetime.done()
        assert not waiter.done()
    finally:
        await b.stop()
        waiter.cancel()


@run_async
async def test_midturn_rotation_continues_without_replaying_tool_request(tmp_path):
    b = setup(tmp_path)
    await b.start()
    def respond(sid, text):
        if sid == '0':
            b._v.emit(sid, 'tool_call', name='send_email', id='one')
            b._v.emit(sid, 'tool_result', id='one', ok=True)
            credentials(tmp_path, 'new')
            b._v.emit(sid, 'assistant_text', text='Failed to authenticate: OAuth token revoked.')
            b._v.emit(sid, 'done', reason='error', subtype='success')
        else:
            b._v.emit(sid, 'assistant_text', text='Recovered')
            b._v.emit(sid, 'done', reason='result')
    b._v.on_send = respond
    try:
        events = await asyncio.wait_for(collect(b), 2)
        assert sum(e['type'] == 'tool_call' for e in events) == 1
        assert sum(e['type'] == 'auth_recovery' for e in events) == 1
        assert b._v.sent[0][1] == 'original request'
        assert 'Do not repeat completed tool' in b._v.sent[1][1]
        assert b._v.spawns[1]['resume_cli_session_id'] == 'saved-history'
        assert not b._exited.done()
        assert events[-1]['type'] == 'session'
    finally:
        await b.stop()


@run_async
@pytest.mark.parametrize('failure', ['same_token', 'second_failure', 'non_auth'])
async def test_failures_do_not_loop(tmp_path, failure):
    b = setup(tmp_path)
    await b.start()
    def respond(sid, text):
        if failure != 'same_token':
            credentials(tmp_path, 'new-' + sid)
        message = 'Failed to authenticate: OAuth token revoked.' if failure != 'non_auth' else 'Server failed'
        b._v.emit(sid, 'assistant_text', text=message)
        b._v.emit(sid, 'done', reason='error')
    b._v.on_send = respond
    try:
        with pytest.raises(VesselError):
            await asyncio.wait_for(collect(b), 2)
        assert len(b._v.spawns) == (2 if failure == 'second_failure' else 1)
    finally:
        await b.stop()


@run_async
async def test_missing_credentials_do_not_restart(tmp_path):
    b = setup(tmp_path)
    await b.start()
    (tmp_path / '.credentials.json').unlink()
    b._v.on_send = lambda sid, _: b._v.emit(sid, 'done', reason='result')
    try:
        await collect(b)
        assert len(b._v.spawns) == 1
    finally:
        await b.stop()


@run_async
async def test_missing_resume_does_not_create_empty_conversation(tmp_path):
    b = setup(tmp_path)
    await b.start()
    b._v.cli_session_id = lambda sid: None
    def respond(sid, text):
        credentials(tmp_path, 'new')
        b._v.emit(sid, 'assistant_text', text='Failed to authenticate: OAuth token revoked.')
        b._v.emit(sid, 'done', reason='error')
    b._v.on_send = respond
    try:
        with pytest.raises(VesselError):
            await collect(b)
        assert len(b._v.spawns) == 1
    finally:
        await b.stop()


@run_async
async def test_auth_text_in_successful_reply_is_not_retried(tmp_path):
    b = setup(tmp_path)
    await b.start()
    def respond(sid, text):
        credentials(tmp_path, 'new')
        b._v.emit(sid, 'assistant_text', text='Failed to authenticate is the error you saw.')
        b._v.emit(sid, 'done', reason='result')
    b._v.on_send = respond
    try:
        await collect(b)
        assert len(b._v.spawns) == 1
    finally:
        await b.stop()


@run_async
async def test_late_injected_turn_is_not_proactively_killed(tmp_path):
    b = setup(tmp_path)
    await b.start()
    await b.inject('late steer still in flight')
    credentials(tmp_path, 'new')
    b._v.on_send = lambda sid, _: b._v.emit(sid, 'done', reason='result')
    try:
        await collect(b)
        assert len(b._v.spawns) == 1
        assert not b._v.stopped
    finally:
        await b.stop()


@run_async
async def test_error_array_without_assistant_text_recovers(tmp_path):
    b = setup(tmp_path)
    await b.start()
    def respond(sid, text):
        if sid == '0':
            credentials(tmp_path, 'new')
            b._v.emit(sid, 'done', reason='error', errors=['OAuth token revoked'])
        else:
            b._v.emit(sid, 'done', reason='result')
    b._v.on_send = respond
    try:
        events = await collect(b)
        assert any(e['type'] == 'auth_recovery' for e in events)
        assert len(b._v.spawns) == 2
    finally:
        await b.stop()


@run_async
async def test_actual_process_exit_still_resolves_wait(tmp_path):
    b = setup(tmp_path)
    await b.start()
    b._v.emit(b._sid, 'done', reason='exited')
    assert await asyncio.wait_for(b.wait(), 1) == 1
    await b.stop()


@run_async
async def test_non_claude_never_restarts_for_auth(tmp_path):
    b = setup(tmp_path)
    b._claude_auth = False
    await b.start()
    def respond(sid, text):
        credentials(tmp_path, 'new')
        b._v.emit(sid, 'assistant_text', text='Failed to authenticate: OAuth token revoked.')
        b._v.emit(sid, 'done', reason='error')
    b._v.on_send = respond
    try:
        with pytest.raises(VesselError):
            await collect(b)
        assert len(b._v.spawns) == 1
        assert b._exited.done()
    finally:
        await b.stop()


@run_async
async def test_delayed_sync_after_revocation_recovers(tmp_path, monkeypatch):
    monkeypatch.setattr(vessel_bridge, "CLAUDE_AUTH_SYNC_GRACE_SECONDS", 0.05)
    b = setup(tmp_path)
    await b.start()
    def respond(sid, text):
        if sid == '0':
            b._v.emit(sid, 'assistant_text', text='Failed to authenticate: OAuth token revoked.')
            b._v.emit(sid, 'done', reason='error')
            asyncio.get_running_loop().call_later(0.01, credentials, tmp_path, 'delivered-later')
        else:
            b._v.emit(sid, 'done', reason='result')
    b._v.on_send = respond
    try:
        events = await asyncio.wait_for(collect(b), 1)
        assert any(e['type'] == 'auth_recovery' for e in events)
        assert len(b._v.spawns) == 2
        assert not b._exited.done()
    finally:
        await b.stop()


@run_async
async def test_sync_grace_expires_without_retry(tmp_path, monkeypatch):
    monkeypatch.setattr(vessel_bridge, "CLAUDE_AUTH_SYNC_GRACE_SECONDS", 0.01)
    b = setup(tmp_path)
    await b.start()
    def respond(sid, text):
        b._v.emit(sid, 'assistant_text', text='Failed to authenticate: OAuth token revoked.')
        b._v.emit(sid, 'done', reason='error')
    b._v.on_send = respond
    try:
        with pytest.raises(VesselError):
            await asyncio.wait_for(collect(b), 1)
        assert len(b._v.spawns) == 1
    finally:
        await b.stop()


@run_async
async def test_sync_wait_is_cancellable(tmp_path, monkeypatch):
    monkeypatch.setattr(vessel_bridge, "CLAUDE_AUTH_SYNC_GRACE_SECONDS", 90)
    b = setup(tmp_path)
    await b.start()
    task = asyncio.create_task(b._wait_for_rotated_credentials())
    await asyncio.sleep(0)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    await b.stop()
