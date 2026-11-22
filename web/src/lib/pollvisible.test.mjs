import assert from 'node:assert/strict';
import test, { mock } from 'node:test';
import { readFileSync } from 'node:fs';
import { pollWhileVisible, sharedPoll } from './pollvisible.js';

const listeners = new Map();
function installDocument(state) {
  listeners.clear();
  globalThis.document = {
    visibilityState: state,
    addEventListener(type, fn) {
      if (!listeners.has(type)) listeners.set(type, new Set());
      listeners.get(type).add(fn);
    },
    removeEventListener(type, fn) {
      listeners.get(type)?.delete(fn);
    },
    dispatch(type) {
      for (const fn of listeners.get(type) ?? []) fn();
    }
  };
}

test('polls immediately and on the interval while visible; hidden stops', (t) => {
  mock.timers.enable({ apis: ['setInterval'] });
  t.after(() => mock.timers.reset());
  installDocument('visible');
  const calls = [];
  const off = pollWhileVisible(() => calls.push('tick'), 3000);
  t.after(off);
  assert.equal(calls.length, 1, 'immediate fetch on mount');
  mock.timers.tick(3000);
  assert.equal(calls.length, 2);
  document.visibilityState = 'hidden';
  document.dispatch('visibilitychange');
  mock.timers.tick(9000);
  assert.equal(calls.length, 2, 'hidden tab does not poll');
  document.visibilityState = 'visible';
  document.dispatch('visibilitychange');
  assert.equal(calls.length, 3, 'visible again fetches immediately');
  mock.timers.tick(3000);
  assert.equal(calls.length, 4);
});

test('hidden at mount does not start; unsubscribe clears listeners', (t) => {
  mock.timers.enable({ apis: ['setInterval'] });
  t.after(() => mock.timers.reset());
  installDocument('hidden');
  const calls = [];
  const off = pollWhileVisible(() => calls.push('tick'), 10000);
  assert.equal(calls.length, 0);
  mock.timers.tick(20000);
  assert.equal(calls.length, 0);
  off();
  assert.equal([...listeners.values()].reduce((n, s) => n + s.size, 0), 0);
});

test('sharedPoll: one loop for many subscribers, paused while hidden, refresh on return', (t) => {
  mock.timers.enable({ apis: ['setInterval'] });
  t.after(() => mock.timers.reset());
  installDocument('visible');
  const calls = [];
  const subscribe = sharedPoll(() => calls.push('tick'), 10000);
  const offA = subscribe();
  const offB = subscribe();
  assert.equal(calls.length, 1, 'second subscriber does not refetch');
  mock.timers.tick(10000);
  assert.equal(calls.length, 2, 'one tick per interval, not one per subscriber');
  document.visibilityState = 'hidden';
  document.dispatch('visibilitychange');
  mock.timers.tick(60000);
  assert.equal(calls.length, 2, 'hidden tab makes no requests');
  document.visibilityState = 'visible';
  document.dispatch('visibilitychange');
  assert.equal(calls.length, 3, 'visible again refreshes at once');
  offA();
  mock.timers.tick(10000);
  assert.equal(calls.length, 4, 'still polling with one subscriber left');
  offB();
  mock.timers.tick(30000);
  assert.equal(calls.length, 4, 'last unsubscribe stops the loop');
  assert.equal([...listeners.values()].reduce((n, s) => n + s.size, 0), 0);
  const offC = subscribe();
  assert.equal(calls.length, 5, 'resubscribe starts again');
  offC();
});

test('background pollers use the visibility helper, not a bare setInterval', () => {
  for (const f of ['watchstore.svelte.js', 'schedulestore.svelte.js', 'questionstore.svelte.js']) {
    const src = readFileSync(new URL(`./${f}`, import.meta.url), 'utf8');
    assert.match(src, /sharedPoll\(/, f);
    assert.doesNotMatch(src, /setInterval/, f);
  }
  const fuel = readFileSync(new URL('./FuelRail.svelte', import.meta.url), 'utf8');
  assert.match(fuel, /pollWhileVisible\(refresh, pollIntervalMs\)/);
  assert.doesNotMatch(fuel, /setInterval\(refresh/);
});
