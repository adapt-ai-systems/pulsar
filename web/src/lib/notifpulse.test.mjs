import test from 'node:test';
import assert from 'node:assert/strict';
import { notifiedPulseAction, reorderableOrder, rosterLoadedEmpty } from './notifpulse.js';

test('notifiedPulseAction: a normal pulse opens its pane', () => {
  assert.equal(notifiedPulseAction({ id: 'p1' }), 'pulse');
});

test('notifiedPulseAction: unknown id → none (caller resolves or falls back)', () => {
  assert.equal(notifiedPulseAction(undefined), 'none');
  assert.equal(notifiedPulseAction(null), 'none');
});

test('reorderableOrder: the notification-only pane never joins a reorder', () => {
  assert.deepEqual(reorderableOrder([{ id: 'a' }, { id: 'b' }, { id: 'arch' }], 'arch'), ['a', 'b']);
  // No injected row → the visible order passes through untouched.
  assert.deepEqual(reorderableOrder([{ id: 'a' }, { id: 'b' }], null), ['a', 'b']);
  assert.deepEqual(reorderableOrder([], 'arch'), []);
});

test('rosterLoadedEmpty: empty is only "no pulses" once a result landed', () => {
  assert.equal(rosterLoadedEmpty({ pulses: [], connected: true, errored: false }), true);
  // still loading
  assert.equal(rosterLoadedEmpty({ pulses: [], connected: false, errored: false }), false);
  // the last fetch failed — not a fact
  assert.equal(rosterLoadedEmpty({ pulses: [], connected: true, errored: true }), false);
  // non-empty is never "loaded empty"
  assert.equal(rosterLoadedEmpty({ pulses: [{}], connected: true, errored: false }), false);
  assert.equal(rosterLoadedEmpty({ pulses: undefined, connected: true, errored: false }), false);
});
