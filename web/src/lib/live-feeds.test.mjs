import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';

// These modules use only scalar $state initialization for the behavior under test.
function source(name) { return readFileSync(new URL(name, import.meta.url), 'utf8'); }

test('live dispatches feed signals, unsubscribes and retains listeners across reconnect', () => {
  const sockets = [];
  class Socket {
    static OPEN = 1;
    readyState = 1;
    constructor() { sockets.push(this); }
    send() {}
    close() { this.onclose?.(); }
  }
  let reconnect;
  const ctx = vm.createContext({
    $state: x => x, WebSocket: Socket,
    location: { protocol: 'https:', host: 'example.test' },
    setInterval: () => 1, clearInterval() {},
    setTimeout: cb => { reconnect = cb; return 1; }, clearTimeout() {}
  });
  vm.runInContext(source('./live.svelte.js').replace('export const live', 'globalThis.live'), ctx);
  let samples = 0;
  const off = ctx.live.onFeed('metrics_sample', () => samples++);
  ctx.live.connect();
  const emit = (type) => sockets.at(-1).onmessage({ data: JSON.stringify({ type }) });
  sockets[0].onopen();
  emit('metrics_sample'); emit('unknown');
  assert.equal(samples, 1);
  sockets[0].close(); reconnect(); sockets[1].onopen();
  emit('metrics_sample'); assert.equal(samples, 2);
  off(); emit('metrics_sample'); assert.equal(samples, 2);
  ctx.live.disconnect();
});

test('MetricsBar fetches on push, retains 15s fallback and cleans up subscription', async () => {
  const bar = source('./MetricsBar.svelte');
  const feedCode = bar.slice(bar.indexOf('  let data = $state(null);'), bar.indexOf('  // Host identity colors'));
  let cleanup, push, fallback, ms, calls = 0, removed = 0, visListeners = 0;
  const pending = [], urls = [];
  const doc = {
    visibilityState: 'visible',
    addEventListener(type) { assert.equal(type, 'visibilitychange'); visListeners++; },
    removeEventListener() { visListeners--; }
  };
  const ctx = vm.createContext({
    $state: x => x, $effect: fn => { cleanup = fn(); }, untrack: fn => fn(),
    isMetricsEnabled: () => true,
    document: doc, expanded: false, // `expanded` is declared further down the component
    live: { onFeed(type, cb) { assert.equal(type, 'metrics_sample'); push = cb; return () => removed++; } },
    fetch: (url) => { calls++; urls.push(url); return new Promise(resolve => pending.push(resolve)); },
    setInterval: (cb, interval) => { fallback = cb; ms = interval; return 7; },
    clearInterval: id => assert.equal(id, 7), console
  });
  vm.runInContext(feedCode, ctx);
  assert.equal(ms, 15000); assert.equal(calls, 1);
  push(); push(); assert.equal(calls, 1, 'one request in flight');
  const finish = async () => {
    pending.shift()({ ok: true, json: async () => ({ hosts: [] }) });
    await new Promise(resolve => setImmediate(resolve));
  };
  await finish(); assert.equal(calls, 2, 'coalesced push fetched after old request');
  await finish();
  fallback(); assert.equal(calls, 3);
  await finish();
  push(); assert.equal(calls, 4);
  await finish();
  assert.ok(urls.every(u => u === '/metrics-proxy/data.json?latest=1&speed=0'), 'collapsed bar polls /metrics-proxy without speed history');
  doc.visibilityState = 'hidden';
  push(); fallback(); assert.equal(calls, 4, 'hidden tab: pushes and fallback do not fetch');
  doc.visibilityState = 'visible';
  cleanup(); assert.equal(removed, 1); assert.equal(visListeners, 0);
});

test('MetricsBar makes zero fetches and registers no feed/interval when metrics.url is unset', () => {
  const bar = source('./MetricsBar.svelte');
  const feedCode = bar.slice(bar.indexOf('  let data = $state(null);'), bar.indexOf('  // Host identity colors'));
  let calls = 0, feedSub = 0, intervals = 0, visListeners = 0;
  const ctx = vm.createContext({
    $state: x => x, $effect: fn => { fn(); }, untrack: fn => fn(),
    isMetricsEnabled: () => false,
    document: { visibilityState: 'visible', addEventListener() { visListeners++; }, removeEventListener() {} },
    expanded: false,
    live: { onFeed() { feedSub++; return () => {}; } },
    fetch: () => { calls++; return Promise.resolve({ ok: true, json: async () => ({ hosts: [] }) }); },
    setInterval: () => { intervals++; return 1; },
    clearInterval() {}, console
  });
  vm.runInContext(feedCode, ctx);
  assert.equal(calls, 0);
  assert.equal(feedSub, 0);
  assert.equal(intervals, 0);
  assert.equal(visListeners, 0);
});
