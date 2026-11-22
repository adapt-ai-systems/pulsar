import { test } from 'node:test';
import assert from 'node:assert/strict';
import { revealPlan } from './reveal.js';

const baseOpts = {
  hideStopped: true,
  revealedShelvedIds: new Set(),
  selectedGrid: 'work',
  visibleFamily: new Set(['work', 'work-sub']),
};

test('drawer: reveals shelved, unticks hideStopped, adds openedIds, focuses composer', () => {
  const target = { id: 'p1', state: 'stopped', grid_id: 'ops', settings: { mode: 'watching' } };
  const plan = revealPlan(target, { ...baseOpts, from: 'drawer' });
  assert.deepEqual(plan, {
    revealShelved: true,
    untickHideStopped: true,
    clearGrid: true,
    addOpened: true,
    focusComposer: true,
  });
});

test('panel: reveals shelved and unticks hideStopped, does not addOpened or focusComposer', () => {
  const target = { id: 'p1', state: 'stopped', grid_id: 'work', settings: { mode: 'paused' } };
  const plan = revealPlan(target, { ...baseOpts, from: 'panel' });
  assert.deepEqual(plan, {
    revealShelved: true,
    untickHideStopped: true,
    clearGrid: false,
    addOpened: false,
    focusComposer: false,
  });
});

test('metrics: unticks hideStopped and reveals shelved pulse', () => {
  const target = { id: 'p1', state: 'stopped', grid_id: 'work-sub', settings: { mode: 'watching' } };
  const plan = revealPlan(target, { ...baseOpts, from: 'metrics' });
  assert.deepEqual(plan, {
    revealShelved: true,
    untickHideStopped: true,
    clearGrid: false,
    addOpened: false,
    focusComposer: false,
  });
});

test('activity: reveals shelved pulse, keeps hideStopped rule', () => {
  const target = { id: 'p1', state: 'stopped', grid_id: 'ops', settings: { mode: 'watching' } };
  const plan = revealPlan(target, { ...baseOpts, from: 'activity' });
  assert.deepEqual(plan, {
    revealShelved: true,
    untickHideStopped: false,
    clearGrid: true,
    addOpened: false,
    focusComposer: false,
  });
});

test('new-pulse: unticks hideStopped, reveals shelved pulse, honors explicit gridId override', () => {
  const target = { id: 'p1', state: 'stopped', grid_id: null, settings: { mode: 'watching' } };
  const plan = revealPlan(target, { ...baseOpts, from: 'new-pulse', gridId: 'work' });
  assert.deepEqual(plan, {
    revealShelved: true,
    untickHideStopped: true,
    clearGrid: false,
    addOpened: false,
    focusComposer: false,
  });
});

test('already-revealed shelved pulse or running pulse does not re-trigger flags', () => {
  const target = { id: 'p1', state: 'running', grid_id: 'work', settings: { mode: 'watching' } };
  const plan = revealPlan(target, {
    ...baseOpts,
    from: 'drawer',
    revealedShelvedIds: new Set(['p1']),
  });
  assert.equal(plan.revealShelved, false);
  assert.equal(plan.untickHideStopped, false);
  assert.equal(plan.clearGrid, false);
});

test('ALL tab (selectedGrid === null) never clears grid', () => {
  const target = { id: 'p1', state: 'running', grid_id: 'ops', settings: {} };
  const plan = revealPlan(target, {
    ...baseOpts,
    selectedGrid: null,
    visibleFamily: null,
    from: 'drawer',
  });
  assert.equal(plan.clearGrid, false);
});

test('target=null (archived / not in pulses): clears selectedGrid to ALL and does not untick hideStopped', () => {
  const plan = revealPlan(null, { ...baseOpts, from: 'panel' });
  assert.deepEqual(plan, {
    revealShelved: false,
    untickHideStopped: false,
    clearGrid: true,
    addOpened: false,
    focusComposer: false,
  });
});

test('+page.svelte revealPulse looks up target in pulses only (not notifiedExtraPulse) and passes created target on new-pulse', async () => {
  const { readFileSync } = await import('node:fs');
  const page = readFileSync(new URL('../routes/+page.svelte', import.meta.url), 'utf8');
  assert.match(
    page,
    /const target = targetOverride !== undefined \? targetOverride : \(pulses\.find\(\(x\) => x\.id === pulseId\) \?\? null\);/
  );
  assert.doesNotMatch(page, /notifiedExtraPulse\?\.id === pulseId \? notifiedExtraPulse/);
  assert.match(
    page,
    /revealPulse\(created\.id, \{ gridId: created\.grid_id \?\? null, from: 'new-pulse', target: created \}\)/
  );
});

