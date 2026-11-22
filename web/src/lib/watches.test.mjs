import { test } from 'node:test';
import assert from 'node:assert/strict';
import { watchLabel, watchDetail, plainWatchText } from './watches.js';

test('note wins over derived label', () => {
  assert.equal(watchLabel({ kind: 'cmd', note: 'DNS cutover', spec: { cmd: 'dig' } }), 'DNS cutover');
});

test('derived labels per kind', () => {
  assert.equal(
    watchLabel({ kind: 'pulse_state', spec: { target_id: 'abcdef1234', stopped: true, stale: true, max_ctx: 200000 } }),
    'pulse abcdef12 stops / goes silent / ctx ≥ 200,000'
  );
  assert.equal(watchLabel({ kind: 'file', spec: { path: '/tmp/x', exists: false } }), 'vanishes: x');
  assert.equal(watchLabel({ kind: 'log', spec: { path: '/tmp/a.log', pattern: 'ERROR' } }), '/ERROR/ in a.log');
  assert.equal(watchLabel({ kind: 'http', spec: { url: 'http://h/x', invert: true } }), 'http://h/x comes up');
  assert.equal(watchLabel({ kind: 'cmd', spec: { cmd: 'true' } }), 'true fails');
});

test('detail line reflects mode, interval, observation, met', () => {
  assert.equal(
    watchDetail({ kind: 'pulse_state', mode: 'once', interval_s: 60, checks_done: 3, detail: { state: 'running', ctx: 12345 }, bad: 0 }),
    'once · every 60s · 3 checks · running · 12,345 ctx'
  );
  assert.equal(
    watchDetail({ kind: 'http', mode: 'edge', interval_s: 30, checks_done: 1, spec: { invert: true }, detail: { status: 500 }, bad: 1 }),
    'edge · every 30s · 1 check · inverted · HTTP 500 · MET'
  );
  assert.equal(watchDetail({ kind: 'cmd', mode: 'edge', interval_s: 60, detail: null }), 'edge · every 60s · 0 checks');
});

test('plainWatchText drops the tool prefix and cuts paths to file names', () => {
  assert.equal(plainWatchText('pulsar-wait: file /home/user/notes.md'), 'file notes.md');
  assert.equal(plainWatchText('http https://x.com/a/b down'), 'http https://x.com/a/b down');
});

test('watchDetail says 1 check, 2 checks', () => {
  assert.match(watchDetail({ mode: 'edge', interval_s: 30, checks_done: 1 }), /1 check(?!s)/);
  assert.match(watchDetail({ mode: 'edge', interval_s: 30, checks_done: 2 }), /2 checks/);
});
