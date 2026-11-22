import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  KV_LOOP_TEMPLATES, sanitizeTemplates, templateFromForm, upsertTemplate,
  removeTemplate, findTemplate, sortedTemplates
} from './looptemplates.js';

test('kv key', () => assert.equal(KV_LOOP_TEMPLATES, 'loop.templates'));

test('sanitizeTemplates drops garbage, fills defaults, collapses dupes', () => {
  const out = sanitizeTemplates([
    'x', null, { name: '', prompt: 'p', minutes: 5 }, { name: 'a', prompt: '', minutes: 5 },
    { name: 'a', prompt: 'p', minutes: 0 },
    { name: 'A', prompt: ' p1 ', minutes: 40 },
    { name: 'a', prompt: 'p2', minutes: 40, triggers: ['tools:20', 'bogus'], trigger_mode: 'all', extra: 1 }
  ]);
  assert.deepEqual(out, [{ name: 'a', minutes: 40, prompt: 'p2', triggers: ['tools:20'], trigger_mode: 'all' }]);
  assert.deepEqual(sanitizeTemplates([{ name: 'b', prompt: 'p', minutes: 5 }])[0].triggers, ['timer']);
  assert.deepEqual(sanitizeTemplates('nope'), []);
});

test('templateFromForm', () => {
  const form = { minutes: '40', prompt: ' rules ', triggerForm: { timer: true, tools: '20' }, mode: 'all' };
  assert.deepEqual(templateFromForm(' Model tuning ', form),
    { name: 'Model tuning', minutes: 40, prompt: 'rules', triggers: ['timer', 'tools:20'], trigger_mode: 'all' });
  assert.equal(templateFromForm('', form), null);
  assert.equal(templateFromForm('x', { ...form, minutes: 'abc' }), null);
  assert.equal(templateFromForm('x', { ...form, prompt: '' }), null);
  assert.equal(templateFromForm('x', { ...form, triggerForm: { timer: true, replies: 'z' } }), null);
});

test('upsert/remove/find/sorted are pure and case-insensitive', () => {
  const a = { name: 'b', minutes: 1, prompt: 'p', triggers: ['timer'], trigger_mode: 'any' };
  const list = [a];
  const up = upsertTemplate(list, { ...a, name: 'B', prompt: 'q' });
  assert.equal(list.length, 1);
  assert.equal(up[0].prompt, 'q');
  const two = upsertTemplate(up, { ...a, name: 'a' });
  assert.deepEqual(sortedTemplates(two).map((t) => t.name), ['a', 'B']);
  assert.equal(findTemplate(two, 'b').prompt, 'q');
  assert.deepEqual(removeTemplate(two, 'B').map((t) => t.name), ['a']);
});
