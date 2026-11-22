import assert from 'node:assert/strict';
import test, { mock } from 'node:test';
import { loadRuneModule } from './runes-loader.mjs';

// W1 — single-instance live-socket watchdog. live.svelte.js uses runes, so
// compile it the way vite would (see runes-loader.mjs).

// Browser globals the module touches. Installed BEFORE import (createLive()
// runs at module load) and kept minimal.
const listeners = { document: new Map(), window: new Map() };
function countListeners() {
  let n = 0;
  for (const m of Object.values(listeners)) for (const s of m.values()) n += s.size;
  return n;
}
function fakeTarget(name) {
  return {
    addEventListener(type, fn) {
      if (!listeners[name].has(type)) listeners[name].set(type, new Set());
      listeners[name].get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners[name].get(type)?.delete(fn);
    },
    dispatch(type) {
      for (const fn of listeners[name].get(type) ?? []) fn();
    }
  };
}
globalThis.document = Object.assign(fakeTarget('document'), { visibilityState: 'visible' });
globalThis.window = fakeTarget('window');
globalThis.location = { protocol: 'http:', host: 'test.local' };

const sockets = [];
class FakeWebSocket {
  static OPEN = 1;
  constructor(url) {
    this.url = url;
    this.readyState = 0;
    this.sent = [];
    this.closed = 0;
    sockets.push(this);
  }
  send(d) { this.sent.push(d); }
  close() {
    this.closed += 1;
    if (this.readyState === 3) return;
    this.readyState = 3;
    this.onclose?.();
  }
  // test helpers
  _open() { this.readyState = FakeWebSocket.OPEN; this.onopen?.(); }
  _msg(obj) { this.onmessage?.({ data: JSON.stringify(obj) }); }
}
globalThis.WebSocket = FakeWebSocket;

const { live } = await loadRuneModule('live.svelte.js');

test('one watchdog interval + one listener pair across repeated reconnects; full cleanup on disconnect', (t) => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] });
  t.after(() => { mock.timers.reset(); live.disconnect(); });
  const setIntervalSpy = t.mock.method(globalThis, 'setInterval');
  const clearIntervalSpy = t.mock.method(globalThis, 'clearInterval');

  live.connect();
  assert.equal(sockets.length, 1);
  assert.equal(countListeners(), 1, 'online registered once (no visibility close)');

  // Cycle: open → silence past 45s → watchdog closes → reconnect after backoff.
  for (let i = 0; i < 4; i++) {
    const sock = sockets.at(-1);
    sock._open();
    assert.equal(live.connected, true);
    sock._msg({ type: 'roster', pulses: [], grids: [] });
    mock.timers.tick(40_000);
    assert.equal(sock.closed, 0, 'a fresh push keeps the socket alive past 15s');
    mock.timers.tick(10_001); // 50s since last message > SILENT_MS 45s
    assert.equal(sock.closed, 1, 'watchdog closed the silent socket');
    assert.equal(live.connected, false);
    mock.timers.tick(RECONNECT_TICK); // backoff ⇒ new socket
    assert.equal(sockets.length, i + 2, 'exactly one reconnect per cycle');
  }
  // Every interval that was started has been cleared (never two live at once).
  const started = setIntervalSpy.mock.callCount();
  assert.equal(started, 4, 'one interval per open');
  assert.ok(clearIntervalSpy.mock.callCount() >= started, 'each interval cleared');
  assert.equal(countListeners(), 1, 'listeners never duplicated across reconnects');
  // A second connect() is idempotent for listeners.
  live.connect();
  assert.equal(countListeners(), 1);

  live.disconnect();
  assert.equal(countListeners(), 0, 'listeners removed on disconnect');
  assert.equal(sockets.at(-1).closed >= 1, true);
  const before = sockets.length;
  mock.timers.tick(60_000);
  assert.equal(sockets.length, before, 'no reconnect or watchdog activity after disconnect');
});

test('visibilitychange does not close the socket; online still reconnects', (t) => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] });
  t.after(() => { mock.timers.reset(); live.disconnect(); });
  live.connect();
  const sock = sockets.at(-1);
  sock._open();
  document.visibilityState = 'hidden';
  document.dispatch('visibilitychange');
  assert.equal(sock.closed, 0, 'hidden tab: no close');
  document.visibilityState = 'visible';
  document.dispatch('visibilitychange');
  assert.equal(sock.closed, 0, 'becoming visible must not close a healthy socket');
  window.dispatch('online');
  assert.equal(sock.closed, 1, 'online while visible forces a reconnect');
});

const RECONNECT_TICK = 15_000;

test('partial pushes go to the optional partial callback, not events', (t) => {
  t.after(() => live.disconnect());
  live.connect();
  const sock = sockets.at(-1);
  sock._open();
  const events = [];
  const partials = [];
  const off = live.watch(
    'p1',
    100,
    (m) => events.push(m),
    (m) => partials.push(m)
  );
  sock._msg({ type: 'events', pulse_id: 'p1', events: [{ type: 'x' }] });
  sock._msg({ type: 'partial', pulse_id: 'p1', text: 'hello' });
  sock._msg({ type: 'partial', pulse_id: 'other', text: 'nope' });
  sock._msg({ type: 'roster', pulses: [], grids: [] });
  assert.equal(events.length, 1);
  assert.equal(events[0].events[0].type, 'x');
  assert.equal(partials.length, 1);
  assert.equal(partials[0].text, 'hello');
  off();
  sock._msg({ type: 'partial', pulse_id: 'p1', text: 'after unsub' });
  assert.equal(partials.length, 1, 'unsubscribe drops partials');
});

test('events pushes pass delta through to the watch callback', (t) => {
  t.after(() => live.disconnect());
  live.connect();
  const sock = sockets.at(-1);
  sock._open();
  const got = [];
  const off = live.watch('p1', 100, (m) => got.push(m));
  sock._msg({ type: 'events', pulse_id: 'p1', events: [{ type: 'a' }], delta: false, offset: 10 });
  sock._msg({ type: 'events', pulse_id: 'p1', events: [{ type: 'b' }], delta: true, offset: 20 });
  assert.equal(got[0].delta, false);
  assert.equal(got[1].delta, true);
  assert.equal(got[1].events[0].type, 'b');
  off();
});

test('reasoning pushes go to the optional reasoning callback, not events', (t) => {
  t.after(() => live.disconnect());
  live.connect();
  const sock = sockets.at(-1);
  sock._open();
  const events = [];
  const partials = [];
  const reasoning = [];
  const off = live.watch(
    'p1',
    100,
    (m) => events.push(m),
    (m) => partials.push(m),
    (m) => reasoning.push(m)
  );
  sock._msg({ type: 'reasoning', pulse_id: 'p1', text: 'weighing it' });
  sock._msg({ type: 'reasoning', pulse_id: 'other', text: 'nope' });
  sock._msg({ type: 'partial', pulse_id: 'p1', text: 'visible' });
  assert.equal(reasoning.length, 1);
  assert.equal(reasoning[0].text, 'weighing it');
  assert.equal(partials.length, 1, 'reasoning must not hit the partial callback');
  assert.equal(events.length, 0);
  off();
  sock._msg({ type: 'reasoning', pulse_id: 'p1', text: 'after unsub' });
  assert.equal(reasoning.length, 1, 'unsubscribe drops reasoning pushes');
});

test('a watch with only a reasoning callback is still subscribed', (t) => {
  t.after(() => live.disconnect());
  live.connect();
  const sock = sockets.at(-1);
  sock._open();
  const reasoning = [];
  const off = live.watch('p1', 100, () => {}, undefined, (m) => reasoning.push(m));
  const sub = JSON.parse(sock.sent.at(-1));
  assert.deepEqual(sub.pulses, ['p1']);
  sock._msg({ type: 'reasoning', pulse_id: 'p1', text: 'trace' });
  assert.equal(reasoning.length, 1);
  off();
  const after = JSON.parse(sock.sent.at(-1));
  assert.deepEqual(after.pulses, []);
});

// Speed sweep 2026-11-02: the page's 3s GET /pulses repeated the roster frame
// /live had just pushed. rosterFresh() tells the poll when it can skip.
test('rosterFresh: true only while roster frames keep arriving', (t) => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval', 'Date'] });
  t.after(() => { mock.timers.reset(); live.disconnect(); });
  live.connect();
  const sock = sockets.at(-1);
  sock._open();
  assert.equal(live.rosterFresh(), false, 'open, no frame yet');
  sock._msg({ type: 'roster', pulses: [], grids: [] });
  assert.equal(live.rosterFresh(), true);
  mock.timers.tick(6_000);
  assert.equal(live.rosterFresh(), true, 'one late frame is tolerated');
  mock.timers.tick(1_001);
  assert.equal(live.rosterFresh(), false, 'frames stopped: poll again');
  sock._msg({ type: 'roster', pulses: [], grids: [] });
  assert.equal(live.rosterFresh(), true);
  sock.close();
  assert.equal(live.rosterFresh(), false, 'socket down: poll at once');
});
