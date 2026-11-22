import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRuneModule } from './runes-loader.mjs';

const { createWindowStack, clampSpawn, panesInDomOrder, PANE_Z_BASE } = await loadRuneModule(
  'windowstack.svelte.js'
);

const rootDir = mkdtempSync(join(tmpdir(), 'windowstack-test-'));
writeFileSync(
  join(rootDir, 'root.svelte.js'),
  `export function makeContextBox(initial) {
  let v = $state(initial);
  return { get value() { return v; }, set value(n) { v = n; } };
}
export const effectRoot = (fn) => $effect.root(fn);
`
);
const { makeContextBox, effectRoot: $effectRoot } = await loadRuneModule(
  join(rootDir, 'root.svelte.js')
);

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

test('clampSpawn keeps head bar on screen and wraps off-edge cascades to {110,84}', () => {
  const vp = { width: 1000, height: 700 };
  assert.deepEqual(clampSpawn({ x: 150, y: 110 }, vp), { x: 150, y: 110 });
  assert.deepEqual(clampSpawn({ x: -10, y: 2 }, vp), { x: 8, y: 8 });
  assert.deepEqual(clampSpawn({ x: 750, y: 100 }, vp), { x: 110, y: 84 });
  assert.deepEqual(clampSpawn({ x: 100, y: 550 }, vp), { x: 110, y: 84 });
});

test('panes dedupe by pulse id, spawn at lastPanePos {110,84}, and cascade +36,+30', () => {
  const ws = createWindowStack({ getViewport: () => ({ width: 1280, height: 800 }) });
  ws.openPane({ id: 'p1' });
  assert.equal(ws.panes.length, 1);
  assert.deepEqual(ws.panes[0], { win: 1, id: 'p1', full: false, x: 110, y: 84 });

  ws.openPane({ id: 'p2' }, true);
  assert.equal(ws.panes.length, 2);
  assert.deepEqual(ws.panes[1], { win: 2, id: 'p2', full: true, x: 146, y: 114 });

  // Re-opening p1 raises it to the end without duplicating or changing win id:
  ws.openPane({ id: 'p1' });
  assert.deepEqual(ws.panes.map((p) => p.id), ['p2', 'p1']);
  assert.deepEqual(ws.panes.map((p) => p.win), [2, 1]);

  // switchPane swaps the pulse id hosted in that win slot:
  ws.switchPane(2, 'p3');
  assert.deepEqual(ws.panes.map((p) => p.id), ['p3', 'p1']);

  // movePane updates lastPanePos; closing all and opening a fresh pane uses lastPanePos:
  ws.movePane('p1', { x: 300, y: 220 });
  assert.equal(ws.panes[1].x, 300);
  assert.equal(ws.panes[1].y, 220);
  ws.closePane('p3');
  ws.closePane('p1');
  assert.equal(ws.panes.length, 0);

  ws.openFullPane({ id: 'p4' });
  assert.deepEqual(ws.panes[0], { win: 3, id: 'p4', full: true, x: 300, y: 220 });
});

test('workspace/surface change clears panes; Escape closes last pane; z-order base is 60', async () => {
  const ctx = makeContextBox('all:pulses');
  let ws;
  const destroy = $effectRoot(() => {
    ws = createWindowStack({
      getContextKey: () => ctx.value,
      getViewport: () => ({ width: 1280, height: 800 })
    });
  });
  await tick();

  ws.openPane({ id: 'p1' });
  ws.openPane({ id: 'p2' });

  assert.equal(PANE_Z_BASE, 60);
  assert.equal(ws.paneZ(0), 60);
  assert.equal(ws.paneZ(1), 61);

  // Escape closes the last pane (p2), leaving p1:
  assert.equal(ws.onEscape({ key: 'Escape', defaultPrevented: false }), true);
  assert.deepEqual(ws.panes.map((p) => p.id), ['p1']);

  // Escape again closes p1; a third Escape does nothing:
  assert.equal(ws.onEscape({ key: 'Escape', defaultPrevented: false }), true);
  assert.equal(ws.panes.length, 0);
  assert.equal(ws.onEscape({ key: 'Escape', defaultPrevented: false }), false);

  // Re-open a pane, then change context (workspace/surface): panes clear:
  ws.openPane({ id: 'p1' });
  assert.equal(ws.panes.length, 1);
  ctx.value = 'ops:pulses';
  await tick();
  assert.equal(ws.panes.length, 0);

  destroy();
});

test('panesInDomOrder keeps creation order stable while raising z-index', () => {
  const ws = createWindowStack({ getViewport: () => ({ width: 1280, height: 800 }) });
  ws.openPane({ id: 'p1' });
  ws.openPane({ id: 'p2' });
  assert.deepEqual(
    panesInDomOrder(ws.panes).map(({ pane, z }) => [pane.id, pane.win, z]),
    [['p1', 1, 60], ['p2', 2, 61]]
  );
  ws.frontPane('p1');
  assert.deepEqual(
    panesInDomOrder(ws.panes).map(({ pane, z }) => [pane.id, pane.win, z]),
    [['p1', 1, 61], ['p2', 2, 60]]
  );
});
