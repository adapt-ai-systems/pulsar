import assert from 'node:assert/strict';
import test, { mock } from 'node:test';
import { readFileSync } from 'node:fs';
import { loadRuneModule } from './runes-loader.mjs';

const listeners = { document: new Map(), window: new Map() };
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

const { clock, subscribeClock, clockIntervalMs, CLOCK_FAST_MS, CLOCK_SLOW_MS } = await loadRuneModule('clock.svelte.js');

test('N subscribers share one interval; focused+visible ticks at 1s', (t) => {
  mock.timers.enable({ apis: ['setInterval', 'Date'] });
  t.after(() => mock.timers.reset());
  const offs = [subscribeClock({ focused: true }), subscribeClock({ focused: true }), subscribeClock({ focused: true })];
  t.after(() => offs.forEach((off) => off()));

  assert.equal(clockIntervalMs(), CLOCK_FAST_MS);
  const start = clock.now;
  mock.timers.tick(CLOCK_FAST_MS);
  assert.equal(clock.now, start + CLOCK_FAST_MS);
  mock.timers.tick(CLOCK_FAST_MS);
  assert.equal(clock.now, start + 2 * CLOCK_FAST_MS);
});

test('no focused subscriber (or hidden tab) ticks at 5s; cleanup stops the timer', (t) => {
  mock.timers.enable({ apis: ['setInterval', 'Date'] });
  t.after(() => mock.timers.reset());

  const offA = subscribeClock({ focused: false });
  assert.equal(clockIntervalMs(), CLOCK_SLOW_MS);
  const start = clock.now;
  mock.timers.tick(CLOCK_FAST_MS);
  assert.equal(clock.now, start, '1s is not enough when unfocused');
  mock.timers.tick(CLOCK_SLOW_MS - CLOCK_FAST_MS);
  assert.equal(clock.now, start + CLOCK_SLOW_MS);

  const offB = subscribeClock({ focused: true });
  assert.equal(clockIntervalMs(), CLOCK_FAST_MS, 'one focused pane raises the cadence');

  document.visibilityState = 'hidden';
  document.dispatch('visibilitychange');
  assert.equal(clockIntervalMs(), CLOCK_SLOW_MS, 'hidden tab drops to 5s');

  document.visibilityState = 'visible';
  document.dispatch('visibilitychange');
  assert.equal(clockIntervalMs(), CLOCK_FAST_MS);

  offA();
  offB();
  assert.equal(clockIntervalMs(), 0);
  const frozen = clock.now;
  mock.timers.tick(60_000);
  assert.equal(clock.now, frozen, 'unsubscribed clock does not tick');
});

test('header clock is hidden at the app mobile breakpoint only', () => {
  const source = readFileSync(new URL('./Clock.svelte', import.meta.url), 'utf8');
  assert.match(source, /@media \(max-width: 699px\)\s*\{\s*\.clock\s*\{\s*display: none;/);
  assert.match(source, /\.clock\s*\{[^}]*display: flex;/);
});
