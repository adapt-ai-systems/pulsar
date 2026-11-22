// Deleting a running or watched pulse from the UI: a running pulse is
// stopped first instead of surfacing the daemon's bare 409; one that owns
// watches comes back as has_watchers so the UI can ask. fetch is stubbed.
import test from 'node:test';
import assert from 'node:assert/strict';
import { deletePulseStopping } from './api.js';

test.after(() => { delete globalThis.fetch; });

const json = (body, status = 200) => new Response(JSON.stringify(body), {
  status, headers: { 'content-type': 'application/json' } });

test('a running pulse is stopped, then deleted', async () => {
  const calls = [];
  let running = true;
  globalThis.fetch = async (url, init) => {
    calls.push(`${init.method} ${url}`);
    if (init.method === 'POST') { running = false; return json({ ok: true, value: {} }); }
    if (running) return json({ ok: false, error: { code: 'bad_state',
      message: 'pulse must be stopped or archived before delete' } }, 409);
    return json({ ok: true, value: { deleted: true } });
  };
  const r = await deletePulseStopping('p1');
  assert.equal(r.ok, true);
  assert.deepEqual(calls.map((c) => c.replace(/^(\w+) .*\/pulses\//, '$1 ')),
    ['DELETE p1', 'POST p1/pause', 'DELETE p1']);
});

test('a pulse with watchers is not stopped and reports has_watchers', async () => {
  const calls = [];
  globalThis.fetch = async (url, init) => {
    calls.push(init.method);
    return json({ ok: false, error: { code: 'has_watchers',
      message: 'pulse has active watchers' } }, 409);
  };
  const r = await deletePulseStopping('p1');
  assert.equal(r.code, 'has_watchers');
  assert.deepEqual(calls, ['DELETE']);
});

test('closeWatchers is passed through', async () => {
  let seen = '';
  globalThis.fetch = async (url) => { seen = String(url); return json({ ok: true, value: {} }); };
  await deletePulseStopping('p1', { closeWatchers: true });
  assert.match(seen, /close_watchers=true/);
});
