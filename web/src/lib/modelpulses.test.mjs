import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { groupModelPulses } from './modelpulses.js';
import { revealPlan } from './reveal.js';

test('groups busy pulses by exact model ID across workspaces', () => {
  const pulses = [
    { id: '1', title: 'Paused', model: 'glm', state: 'stopped', grid_id: 'a' },
    { id: '2', title: 'Working', model: 'glm', state: 'running', live: { socket_state: 'busy' }, grid_id: 'b' },
    { id: '3', title: 'Different model', model: 'glm-u', state: 'running', live: { socket_state: 'busy' } },
    { id: '4', model: 'glm', state: 'archived' },
  ];
  const groups = groupModelPulses(pulses);
  assert.deepEqual(groups.map(g => [g.model, g.pulses.map(p => p.id)]),
    [['glm', ['2']], ['glm-u', ['3']]]);
  assert.equal(pulses[0].id, '1');
  assert.equal(groupModelPulses(pulses.slice(1))[0].pulses.length, 1);
});

test('empty roster and unnamed pulses are safe', () => {
  assert.deepEqual(groupModelPulses(), []);
  assert.equal(groupModelPulses([null, {}, { id: 'x', state: 'running', live: { socket_state: 'busy' } }])[0].model, 'Unknown model');
});

test('dashboard selection closes overlay and forwards exact pulse ID', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  const handler = source.match(/function openModelPulse\(id\) \{[\s\S]*?\n  \}/)[0];
  let received;
  const ctx = { open: true, onopenpulse(id) { received = id; } };
  vm.createContext(ctx);
  vm.runInContext(handler, ctx);
  ctx.openModelPulse('selected');
  assert.equal(ctx.open, false);
  assert.equal(received, 'selected');
  assert.match(source, /onclick=\{\(\) => openModelPulse\(pulse.id\)\}/);
});

test('metrics navigation removes covering panels and uses existing focus routing', () => {
  const source = readFileSync(new URL('../routes/+page.svelte', import.meta.url), 'utf8');
  const plan = revealPlan(
    { id: 'selected', state: 'stopped', grid_id: 'other' },
    {
      from: 'metrics',
      hideStopped: true,
      selectedGrid: 'work',
      visibleFamily: new Set(['work']),
    },
  );
  assert.equal(plan.untickHideStopped, true);
  assert.equal(plan.clearGrid, true);
  assert.equal(plan.addOpened, false);
  assert.match(source, /<MetricsBar \{pulses\} onopenpulse=\{openFromMetrics\}/);
});


test('idle, paused, archived and unknown activity are excluded; live idle overrides old busy heartbeat', () => {
  const pulses = [
    { id: 'idle', state: 'running', live: { socket_state: 'idle' } },
    { id: 'old', state: 'running', live: { socket_state: 'idle', heartbeat: { state: 'busy' } } },
    { id: 'paused', state: 'stopped', live: { socket_state: 'busy' } },
    { id: 'archived', state: 'archived', live: { socket_state: 'busy' } },
    { id: 'unknown', state: 'running' },
    { id: 'starting', state: 'running', live: { socket_state: 'starting' } },
  ];
  assert.deepEqual(groupModelPulses(pulses), []);
});

test('heartbeat fallback works and a pulse disappears when its turn finishes', () => {
  const pulse = { id: 'busy', model: 'glm', state: 'running',
    live: { socket_state: null, heartbeat: { state: 'busy' } } };
  assert.equal(groupModelPulses([pulse])[0].pulses[0].id, 'busy');
  pulse.live.socket_state = 'idle';
  assert.deepEqual(groupModelPulses([pulse]), []);
});
