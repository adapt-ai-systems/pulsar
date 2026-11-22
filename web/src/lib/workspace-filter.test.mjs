import test from 'node:test';
import assert from 'node:assert/strict';
import { gridFamily } from './wscolor.js';
import { visiblePulsesFor } from './pulsefilter.js';

const grids = [
  { id: 'personal' }, { id: 'personal-child', parent_id: 'personal' },
  { id: 'research' }
];
const pulses = [
  { id: 'bot', grid_id: 'research' },
  { id: 'personal-pulse', grid_id: 'personal' },
  { id: 'child-pulse', grid_id: 'personal-child' },
  { id: 'unassigned', grid_id: null }
];
function visible(selected, { opened = [], extra = null, hidden = [], roster = pulses } = {}) {
  return visiblePulsesFor({
    selectedGrid: selected,
    visibleFamily: selected === null ? null : gridFamily(selected, grids),
    renderedGridPulses: roster,
    grids,
    wsHidden: { ids: hidden },
    openedIds: new Set(opened),
    notifiedExtraPulse: extra
  }).map(p => p.id);
}

test('opened pulses stay available in ALL but do not follow workspace switches', () => {
  const session = { opened: ['bot', 'unassigned'] };
  assert.deepEqual(visible(null, session), ['personal-pulse', 'child-pulse', 'bot', 'unassigned']);
  assert.deepEqual(visible('personal', session), ['personal-pulse', 'child-pulse']);
  assert.deepEqual(visible('personal-child', session), ['child-pulse']);
  assert.deepEqual(visible('research', session), ['bot']);
  assert.deepEqual(visible('personal', session), ['personal-pulse', 'child-pulse']);
});

test('notification-only panes obey workspace membership too', () => {
  const extra = { id: 'archived-bot', grid_id: 'research' };
  const session = { extra, roster: [...pulses, extra] };
  assert.ok(visible(null, session).includes(extra.id));
  assert.deepEqual(visible('research', session), ['bot', extra.id]);
  assert.deepEqual(visible('personal', session), ['personal-pulse', 'child-pulse']);
  assert.deepEqual(visible('personal-child', session), ['child-pulse']);
});

test('opening a pulse still reveals a hidden workspace pulse in ALL', () => {
  assert.deepEqual(visible(null, { hidden: ['research'] }), ['personal-pulse', 'child-pulse', 'unassigned']);
  assert.deepEqual(visible(null, { hidden: ['research'], opened: ['bot'] }),
    ['personal-pulse', 'child-pulse', 'bot', 'unassigned']);
});

test('a manually opened pulse moved out of a workspace disappears on roster update', () => {
  const session = { opened: ['personal-pulse'] };
  assert.ok(visible('personal', session).includes('personal-pulse'));
  session.roster = pulses.map(p => p.id === 'personal-pulse' ? { ...p, grid_id: 'research' } : p);
  assert.deepEqual(visible('personal', session), ['child-pulse']);
  assert.deepEqual(visible('research', session), ['bot', 'personal-pulse']);
});
