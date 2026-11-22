import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  defaultTriggerForm, triggerFormFromList, triggerListFromForm,
  normalizeTriggerMode, triggerLabel, hasEventTriggers
} from './looptriggers.js';

test('null/garbage list -> timer-only form', () => {
  assert.deepEqual(triggerFormFromList(null), defaultTriggerForm());
  assert.deepEqual(triggerFormFromList(['bogus', 'replies:0']), defaultTriggerForm());
});

test('wire list round-trips through the form', () => {
  const list = ['timer', 'handoff', 'message', 'replies:5', 'tools:20'];
  const form = triggerFormFromList(['tools:20', 'timer', 'replies:5', 'message', 'handoff']);
  assert.deepEqual(form, { timer: true, handoff: true, message: true, replies: '5', tools: '20' });
  assert.deepEqual(triggerListFromForm(form), { list });
});

test('counters: blank = off, bad N = error, no triggers = error', () => {
  assert.deepEqual(triggerListFromForm({ timer: true, replies: '', tools: ' ' }), { list: ['timer'] });
  assert.match(triggerListFromForm({ timer: true, replies: 'x' }).error, /replies/);
  assert.match(triggerListFromForm({ timer: true, tools: '-3' }).error, /tool calls/);
  assert.match(triggerListFromForm({ timer: false }).error, /at least one/);
});

test('mode clamps to any|all', () => {
  assert.equal(normalizeTriggerMode('all'), 'all');
  assert.equal(normalizeTriggerMode('ALL'), 'any');
  assert.equal(normalizeTriggerMode(undefined), 'any');
});

test('label matches the daemon wording', () => {
  assert.equal(triggerLabel(null, 40), 'every 40 min');
  assert.equal(triggerLabel(['timer', 'message', 'tools:20'], 40), 'every 40 min, every message, every 20 tool calls');
  assert.equal(triggerLabel(['handoff', 'replies:5'], 40, 'all'), 'every handoff, every 5 replies (all)');
  assert.equal(triggerLabel(['timer'], 40, 'all'), 'every 40 min');
});

test('hasEventTriggers', () => {
  assert.equal(hasEventTriggers(null), false);
  assert.equal(hasEventTriggers(['timer']), false);
  assert.equal(hasEventTriggers(['timer', 'message']), true);
});
