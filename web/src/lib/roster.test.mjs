import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRuneModule } from './runes-loader.mjs';

const { createRoster } = await loadRuneModule('roster.svelte.js');

const rootDir = mkdtempSync(join(tmpdir(), 'roster-test-'));
writeFileSync(
  join(rootDir, 'root.svelte.js'),
  `export function makeFakeLive() {
  let roster = $state(null);
  let grids = $state(null);
  let connected = $state(false);
  return {
    get roster() { return roster; },
    set roster(v) { roster = v; },
    get grids() { return grids; },
    set grids(v) { grids = v; },
    get connected() { return connected; },
    set connected(v) { connected = v; }
  };
}
export const effectRoot = (fn) => $effect.root(fn);
`
);
const { makeFakeLive, effectRoot: $effectRoot } = await loadRuneModule(
  join(rootDir, 'root.svelte.js')
);

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

test('applyRoster and /live effect update pulses, grids, connected, and loaded flags', async () => {
  const fakeLive = makeFakeLive();
  let r;
  const destroy = $effectRoot(() => {
    r = createRoster({ live: fakeLive });
  });
  await tick();

  assert.equal(r.pulsesLoaded, false);
  assert.equal(r.gridsLoaded, false);
  assert.equal(r.connected, false);

  fakeLive.roster = [{ id: 'p1', title: 'Alpha' }];
  fakeLive.grids = [{ id: 'g1', name: 'Work' }];
  await tick();

  assert.equal(r.pulsesLoaded, true);
  assert.equal(r.gridsLoaded, true);
  assert.equal(r.connected, true);
  assert.deepEqual(r.pulses.map((p) => p.id), ['p1']);
  assert.deepEqual(r.grids.map((g) => g.id), ['g1']);

  destroy();
});

test('movePane optimistically permutes visible slots, guards against mid-flight polls, and ignores extraPulse', async () => {
  const fakeLive = makeFakeLive();
  let resolveReorder;
  let serverPulses = [
    { id: 'p1', title: 'One', grid_id: 'g1' },
    { id: 'p2', title: 'Two', grid_id: 'g2' },
    { id: 'p3', title: 'Three', grid_id: 'g1' }
  ];

  let r;
  const destroy = $effectRoot(() => {
    r = createRoster({
      live: fakeLive,
      getVisiblePulses: () => [
        { id: 'p1', title: 'One', grid_id: 'g1' },
        { id: 'p3', title: 'Three', grid_id: 'g1' },
        { id: 'extra', title: 'Archived', grid_id: 'g1' }
      ],
      getExtraPulse: () => ({ id: 'extra' }),
      api: {
        listPulses: async () => ({ ok: true, value: serverPulses }),
        listGrids: async () => ({ ok: true, value: [] }),
        reorderPulses: (order) =>
          new Promise((res) => {
            resolveReorder = (result) => res({ ok: true, order, ...result });
          }),
        movePulseToGrid: async () => ({ ok: true }),
        reorderGrids: async () => ({ ok: true }),
        deleteGrid: async () => ({ ok: true })
      }
    });
  });
  await tick();
  await r.refresh();
  assert.deepEqual(r.pulses.map((p) => p.id), ['p1', 'p2', 'p3']);

  // Dragging extra is ignored:
  await r.movePane('extra', 'p1');
  assert.deepEqual(r.pulses.map((p) => p.id), ['p1', 'p2', 'p3']);

  // Dragging p3 onto p1 swaps only slots 0 and 2 (leaving p2 at slot 1):
  const movePromise = r.movePane('p3', 'p1');
  assert.deepEqual(r.pulses.map((p) => p.id), ['p3', 'p2', 'p1']);

  // While reordering is in flight, a stale poll or live frame does not overwrite `pulses`:
  r.applyRoster(serverPulses);
  assert.deepEqual(r.pulses.map((p) => p.id), ['p3', 'p2', 'p1']);

  resolveReorder({ ok: true });
  await movePromise;
  destroy();
});

test('movePulseToWorkspace, reorderWorkspaces, and confirmBinDelete update state and clear deleted selectedGrid', async () => {
  const fakeLive = makeFakeLive();
  let selectedGrid = 'g1';
  let serverPulses = [{ id: 'p1', title: 'One', grid_id: 'g1' }];
  let serverGrids = [
    { id: 'g1', name: 'Work' },
    { id: 'g2', name: 'Ops' }
  ];

  let r;
  const destroy = $effectRoot(() => {
    r = createRoster({
      live: fakeLive,
      getSelectedGrid: () => selectedGrid,
      setSelectedGrid: (id) => {
        selectedGrid = id;
      },
      api: {
        listPulses: async () => ({ ok: true, value: serverPulses }),
        listGrids: async () => ({ ok: true, value: serverGrids }),
        reorderPulses: async () => ({ ok: true }),
        movePulseToGrid: async (id, to) => {
          serverPulses = serverPulses.map((p) => (p.id === id ? { ...p, grid_id: to } : p));
          return { ok: true };
        },
        reorderGrids: async (ids) => {
          const byId = new Map(serverGrids.map((g) => [g.id, g]));
          serverGrids = ids.map((id) => byId.get(id));
          return { ok: true };
        },
        deleteGrid: async (id) => {
          serverGrids = serverGrids.filter((g) => g.id !== id);
          return { ok: true, value: { detached: 1 } };
        }
      }
    });
  });
  await tick();
  await r.refresh();
  await r.refreshGrids();

  await r.movePulseToWorkspace('p1', 'g2');
  assert.equal(r.pulses[0].grid_id, 'g2');
  assert.equal(r.moveNote, 'One → Ops');

  await r.reorderWorkspaces(['g2', 'g1']);
  assert.deepEqual(r.grids.map((g) => g.id), ['g2', 'g1']);

  r.armWorkspaceDelete('g1');
  assert.deepEqual(r.binArmed, { id: 'g1', name: 'Work' });
  await r.confirmBinDelete();
  assert.equal(r.binArmed, null);
  assert.equal(selectedGrid, null);
  assert.equal(r.moveNote, 'Work deleted · 1 pulse moved out');

  destroy();
});

// Speed sweep 2026-11-02: the 3s timer's GET /pulses repeated the /live frame.
test('poll() skips the GET while /live frames are fresh; refresh() always fetches', async () => {
  let fresh = true;
  let calls = 0;
  const fakeLive = { roster: null, grids: null, connected: true, rosterFresh: () => fresh };
  let r;
  const destroy = $effectRoot(() => {
    r = createRoster({
      live: fakeLive,
      api: {
        listPulses: async () => {
          calls += 1;
          return { ok: true, value: [{ id: 'p1', title: 'One' }] };
        },
        listGrids: async () => ({ ok: true, value: [] })
      }
    });
  });
  await tick();
  await r.poll();
  assert.equal(calls, 0, 'fresh frame: no GET');
  await r.refresh();
  assert.equal(calls, 1, 'refresh() is the mutation hook: it always fetches');
  fresh = false;
  await r.poll();
  assert.equal(calls, 2, 'frames stopped: the poll fetches');
  assert.deepEqual(r.pulses.map((p) => p.id), ['p1']);
  destroy();
});

test('onGridsChanged forces listGrids even while /live is connected', async () => {
  let gridCalls = 0;
  const fakeLive = { roster: [], grids: [], connected: true, rosterFresh: () => true };
  let r;
  const destroy = $effectRoot(() => {
    r = createRoster({
      live: fakeLive,
      api: {
        listPulses: async () => ({ ok: true, value: [] }),
        listGrids: async () => {
          gridCalls += 1;
          return { ok: true, value: [{ id: 'g1', name: 'NewWS' }] };
        }
      }
    });
  });
  await tick();
  await r.refreshGrids();
  assert.equal(gridCalls, 0, 'periodic refreshGrids skips GET while /live carries grids');
  await r.onGridsChanged();
  assert.equal(gridCalls, 1, 'onGridsChanged forces GET /grids immediately');
  assert.deepEqual(r.grids.map((g) => g.name), ['NewWS']);
  destroy();
});
