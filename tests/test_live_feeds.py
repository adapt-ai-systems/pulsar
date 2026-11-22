"""Metrics invalidations use file metadata, never the large JSON body."""
from pulsar.daemon import live
from tests.test_live import sync_client, _recv_type  # noqa: F401
from tests.test_daemon import data_root, fake_spawn, fake_sock  # noqa: F401


def configure(monkeypatch, tmp_path):
    metrics = tmp_path / 'data.json'
    monkeypatch.setattr(live, 'METRICS_DATA_PATH', metrics)
    return metrics


def test_feed_changes_disabled_when_unset(monkeypatch):
    monkeypatch.setattr(live, 'METRICS_DATA_PATH', None)
    feeds = live._FeedChanges()
    assert feeds.sources == []
    assert feeds.poll() == []


def test_feed_changes_create_replace_delete_and_reconnect(monkeypatch, tmp_path):
    metrics = configure(monkeypatch, tmp_path)
    feeds = live._FeedChanges()
    assert feeds.poll() == []
    metrics.write_text('not parsed')
    assert [p['type'] for p in feeds.poll()] == ['metrics_sample']
    assert feeds.poll() == []
    replacement = tmp_path / 'sample.tmp'
    replacement.write_text('replacement')
    replacement.replace(metrics)
    assert [p['type'] for p in feeds.poll()] == ['metrics_sample']
    assert [p['type'] for p in live._FeedChanges().poll()] == ['metrics_sample']
    metrics.unlink()
    assert [p['type'] for p in feeds.poll()] == ['metrics_sample']
    assert feeds.poll() == []


def test_live_pushes_feed_updates_to_each_client(sync_client, monkeypatch, tmp_path):
    metrics = configure(monkeypatch, tmp_path)
    monkeypatch.setattr(live, 'TICK_S', 0.01)
    metrics.write_text('{}')
    with sync_client.websocket_connect('/live') as a, sync_client.websocket_connect('/live') as b:
        for ws in (a, b):
            _recv_type(ws, 'metrics_sample')
        metrics.write_text('{"new":true}')
        for ws in (a, b):
            assert _recv_type(ws, 'metrics_sample')['ts'] > 0
