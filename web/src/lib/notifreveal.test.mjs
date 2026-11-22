import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRuneModule } from './runes-loader.mjs';

const TEST_ORIGIN = 'http://localhost:5292';

const { createNotifReveal } = await loadRuneModule('notifreveal.svelte.js');

const rootDir = mkdtempSync(join(tmpdir(), 'notifreveal-test-'));
writeFileSync(join(rootDir, 'root.svelte.js'), 'export const effectRoot = (fn) => $effect.root(fn);\n');
const { effectRoot: $effectRoot } = await loadRuneModule(join(rootDir, 'root.svelte.js'));

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

test('cold-start pulse in roster reveals once roster arrives', async () => {
  delete globalThis.window;
  let rosterState = { pulses: [], connected: false, errored: false };
  const revealed = [];
  const fallbacks = [];

  let nr;
  const destroy = $effectRoot(() => {
    nr = createNotifReveal({
      initialPulseId: 'p1',
      initialFallbackItem: 'it-1',
      getRoster: () => rosterState,
      onReveal: (id) => revealed.push(id),
      onFallback: (it) => fallbacks.push(it)
    });
  });
  await tick();
  assert.deepEqual(revealed, []);

  // Roster arrives with p1:
  rosterState = { pulses: [{ id: 'p1', state: 'running' }], connected: true, errored: false };
  assert.equal(nr.revealNotifiedPulse('p1'), true);
  assert.deepEqual(revealed, ['p1']);
  assert.equal(nr.notifiedPulseId, null);
  assert.deepEqual(fallbacks, []);
  destroy();
});

test('archived pulse lookup resolves notifiedExtraPulse and calls onExtraFound', async () => {
  delete globalThis.window;
  let rosterState = { pulses: [{ id: 'p1', state: 'running' }], connected: true, errored: false };
  const revealed = [];
  const extras = [];
  const fallbacks = [];

  let nr;
  const destroy = $effectRoot(() => {
    nr = createNotifReveal({
      initialPulseId: 'p-arch',
      initialFallbackItem: 'it-99',
      getRoster: () => rosterState,
      onReveal: (id) => revealed.push(id),
      onFallback: (it) => fallbacks.push(it),
      onExtraFound: (p) => extras.push(p),
      fetchPulses: async () => ({
        ok: true,
        value: [{ id: 'p-arch', state: 'stopped', archived_at: '2026-01-01T00:00:00Z' }]
      })
    });
  });
  await tick(10);
  assert.equal(nr.notifiedExtraPulse?.id, 'p-arch');
  assert.equal(extras.length, 1);
  assert.equal(extras[0].id, 'p-arch');
  assert.deepEqual(revealed, ['p-arch']);
  assert.equal(nr.notifiedPulseId, null);
  assert.deepEqual(fallbacks, []);
  destroy();
});

test('missing pulse falls back to inbox item after archived lookup misses', async () => {
  delete globalThis.window;
  let rosterState = { pulses: [], connected: true, errored: false };
  const revealed = [];
  const fallbacks = [];

  const destroy = $effectRoot(() => {
    createNotifReveal({
      initialPulseId: 'p-gone',
      initialFallbackItem: 'it-fallback',
      getRoster: () => rosterState,
      onReveal: (id) => revealed.push(id),
      onFallback: (it) => fallbacks.push(it),
      fetchPulses: async () => ({ ok: true, value: [] })
    });
  });
  await tick(10);
  assert.deepEqual(revealed, []);
  assert.deepEqual(fallbacks, ['it-fallback']);
  destroy();
});

test('newer tap supersedes an in-flight archived lookup via _notifIntent', async () => {
  globalThis.window = { location: { origin: TEST_ORIGIN } };
  let rosterState = { pulses: [{ id: 'p2', state: 'running' }], connected: true, errored: false };
  const revealed = [];
  const extras = [];
  let resolveFetch;

  let nr;
  const destroy = $effectRoot(() => {
    nr = createNotifReveal({
      initialPulseId: 'p-slow-arch',
      getRoster: () => rosterState,
      onReveal: (id) => revealed.push(id),
      onFallback: () => {},
      onExtraFound: (p) => extras.push(p),
      fetchPulses: () =>
        new Promise((r) => {
          resolveFetch = r;
        })
    });
  });
  await tick();
  // While p-slow-arch lookup is in flight, user clicks a hot link to p2:
  let prevented = false;
  nr.onPulseLinkClick({
    defaultPrevented: false,
    button: 0,
    metaKey: false,
    ctrlKey: false,
    shiftKey: false,
    preventDefault: () => {
      prevented = true;
    },
    target: {
      closest: () => ({
        getAttribute: () => `${TEST_ORIGIN}/?pulse-focus=p2`
      })
    }
  });
  assert.equal(prevented, true);
  assert.deepEqual(revealed, ['p2']);

  // Now the stale fetch for p-slow-arch resolves — it must be ignored:
  resolveFetch({ ok: true, value: [{ id: 'p-slow-arch', state: 'stopped' }] });
  await tick(10);
  assert.deepEqual(revealed, ['p2']);
  assert.deepEqual(extras, []);
  assert.equal(nr.notifiedExtraPulse, null);
  destroy();
  delete globalThis.window;
});
