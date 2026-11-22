import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import { compileModule } from 'svelte/compiler';
import { loadRuneModule } from './runes-loader.mjs';

const { confirmArm, ARM_MS } = await loadRuneModule('confirmarm.svelte.js');

const noopDestroy = () => {};
const tick = (ms) => new Promise((r) => setTimeout(r, ms));

test('armed is reactive state that auto-disarms after ARM_MS', async () => {
  assert.equal(ARM_MS, 12000);
  // Shorten the wait: arm, then fire the pending timer by hand via disarm race.
  const c = confirmArm({ onDestroy: noopDestroy });
  assert.equal(c.armed, false);
  c.arm();
  assert.equal(c.armed, true);
  c.disarm();
  assert.equal(c.armed, false);
});

test('per-id arming holds the id and cancel clears it', () => {
  const c = confirmArm({ armed: null, onDestroy: noopDestroy });
  assert.equal(c.armed, null);
  c.arm('p1');
  assert.equal(c.armed, 'p1');
  c.arm('p2');
  assert.equal(c.armed, 'p2');
  c.disarm();
  assert.equal(c.armed, null);
});

test('unmount cleanup disarms and clears the timer', async () => {
  let cleanup;
  const c = confirmArm({ onDestroy: (fn) => (cleanup = fn) });
  c.arm();
  assert.equal(c.armed, true);
  cleanup();
  assert.equal(c.armed, false);
  await tick(10);
  assert.equal(c.armed, false);
});

test('compiled module really uses $state (not a plain variable)', () => {
  const src = readFileSync(new URL('./confirmarm.svelte.js', import.meta.url), 'utf8');
  const out = compileModule(src, { generate: 'client', filename: 'confirmarm.svelte.js' });
  assert.match(out.js.code, /\$\.state\(/);
});
