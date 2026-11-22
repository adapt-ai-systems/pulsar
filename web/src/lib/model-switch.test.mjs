import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { switchModel } from './api.js';

test('forced model switch passes the chosen target and force flag together', async (t) => {
  let sent;
  t.mock.method(globalThis, 'fetch', async (url, options) => {
    sent = { url, ...options, body: JSON.parse(options.body) };
    return new Response(JSON.stringify({ ok: true, value: {} }), {
      headers: { 'content-type': 'application/json' }
    });
  });
  assert.equal((await switchModel('p1', 'gpt-5.6-luna', 'codex', 'high', true)).ok, true);
  assert.deepEqual(sent.body, { model: 'gpt-5.6-luna', vessel: 'codex', effort: 'high', force: true });
  await switchModel('p1', 'gpt-5.6-luna', 'codex', 'high');
  assert.equal('force' in sent.body, false);
});

test('model settings expose forced switching even for the current model', () => {
  const source = readFileSync(new URL('./SettingsModal.svelte', import.meta.url), 'utf8');
  assert.match(source, /bind:checked=\{forceSwitch\}/);
  assert.match(source, /selectedEffort, forceSwitch\)/);
  assert.match(source, /if \(!forceSwitch && selectedModel === pulse.model/);
});
