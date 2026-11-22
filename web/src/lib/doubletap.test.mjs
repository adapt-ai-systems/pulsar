import { test } from 'node:test';
import assert from 'node:assert/strict';
import { doubleTap, DOUBLE_TAP_MS, DOUBLE_TAP_SLOP } from './doubletap.js';

// A pointerup on plain (non-interactive) content.
const up = (t, x = 100, y = 20) => ({ timeStamp: t, clientX: x, clientY: y, target: null });
// A pointerup on a button — `closest` finds one.
const upOnButton = (t, x = 100, y = 20) => ({
  timeStamp: t, clientX: x, clientY: y, target: { closest: () => ({}) }
});

function harness(opts = {}) {
  const fired = [];
  const swallowed = [];
  const handler = doubleTap(
    (e) => fired.push(e.timeStamp),
    { swallow: () => swallowed.push(true), vibrate: false, ...opts }
  );
  return { handler, fired, swallowed };
}

test('two quick taps in the same spot fire once', () => {
  const { handler, fired } = harness();
  handler(up(0));
  assert.deepEqual(fired, [], 'one tap is not a double tap');
  handler(up(200));
  assert.deepEqual(fired, [200]);
});

test('the second tap swallows the click it is about to produce', () => {
  // This is the bug that made the overview invisible: it opened during pointerup
  // and the second tap's compatibility click closed it again.
  const { handler, swallowed } = harness();
  handler(up(0));
  handler(up(200));
  assert.deepEqual(swallowed, [true]);
});

test('a slow second tap does not fire', () => {
  const { handler, fired } = harness();
  handler(up(0));
  handler(up(DOUBLE_TAP_MS + 1));
  assert.deepEqual(fired, []);
});

test('a second tap too far away does not fire', () => {
  const { handler, fired } = harness();
  handler(up(0, 100, 20));
  handler(up(100, 100 + DOUBLE_TAP_SLOP, 20));
  assert.deepEqual(fired, []);
});

test('a tap on a button is never half of a double tap', () => {
  // Tapping ⋯ twice must open the menu twice, not the overview.
  const { handler, fired } = harness();
  handler(upOnButton(0));
  handler(upOnButton(100));
  assert.deepEqual(fired, []);
  // ...and a button tap in the middle breaks a pending pair.
  handler(up(200));
  handler(upOnButton(240));
  handler(up(280));
  assert.deepEqual(fired, []);
});

test('a third tap does not fire a second time on its own', () => {
  // Triple tap = one open, not two toggles.
  const { handler, fired } = harness();
  handler(up(0));
  handler(up(100));
  handler(up(200));
  assert.deepEqual(fired, [100]);
  // A fresh pair after that still works.
  handler(up(300));
  assert.deepEqual(fired, [100, 300]);
});
