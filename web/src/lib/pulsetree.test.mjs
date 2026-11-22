/** node --test src/lib/pulsetree.test.mjs — pure list helpers shared by the drawer and the full-screen window */
import assert from 'node:assert/strict';
import test from 'node:test';
import {
  LIT, PHASE_LABEL, phaseOf, schedByPulse, gridNameFn, matchesQuery, filterPulses, sortPulses, buildSections, reorderGridIds, moveId,
  withArchived
} from './pulsetree.js';
import { modelLabel } from './models.js';

const grids = [
  { id: 'a', name: 'Alpha', parent_id: null },
  { id: 'a1', name: 'AlphaKid', parent_id: 'a' },
  { id: 'b', name: 'Beta', parent_id: null },
  { id: 'c', name: 'Gamma', parent_id: null }
];
const P = (id, extra = {}) => ({ id, title: id.toUpperCase(), model: 'gpt-6-luna', state: 'running', grid_id: null, ...extra });
const ids = (l) => l.map((p) => p.id);

// One behaviour for both lists (D4): the drawer's rules.
const LIST = { gridName: gridNameFn(grids) };
const calm = () => 'idle';

test('constants: only working / question / done light up; labels name the loud phases', () => {
  assert.deepEqual([...LIT].sort(), ['done', 'question', 'working']);
  assert.equal(PHASE_LABEL.stalled, 'stalled');
  assert.equal(PHASE_LABEL.idle, '');
  assert.equal(PHASE_LABEL.stopped, '');
});

test('phaseOf: stopped / archived, question, then done', () => {
  const ctx = { questionIds: new Set(['q']), doneIds: new Set(['d']), nowMs: 1_000 };
  assert.equal(phaseOf(P('x', { state: 'stopped' }), ctx), 'stopped');
  assert.equal(phaseOf(P('x', { state: 'archived' }), ctx), 'stopped');
  assert.equal(phaseOf(P('q'), ctx), 'question');
  assert.equal(phaseOf(P('d'), ctx), 'done');
  assert.equal(phaseOf(P('plain'), {}), 'idle');
});

test('schedByPulse keeps one enabled schedule per pulse, loops win', () => {
  const m = schedByPulse([
    { pulse_id: 'p', kind: 'once', enabled: true, id: 1 },
    { pulse_id: 'p', kind: 'loop', enabled: true, id: 2 },
    { pulse_id: 'p', kind: 'once', enabled: true, id: 3 },
    { pulse_id: 'q', kind: 'loop', enabled: false, id: 4 },
    { pulse_id: 'r', kind: 'once', enabled: true, id: 5 },
    { pulse_id: 'r', kind: 'once', enabled: true, id: 6 }
  ]);
  assert.equal(m.get('p').id, 2);
  assert.equal(m.has('q'), false);
  assert.equal(m.get('r').id, 5);
});

test('matchesQuery: model label and workspace name are in the haystack (D4: both lists)', () => {
  const p = P('x', { title: 'plain', grid_id: 'a' });
  const label = modelLabel(p.model).toLowerCase();
  assert.notEqual(label, p.model.toLowerCase(), 'fixture needs a label that differs from the id');
  assert.equal(matchesQuery(p, label, LIST), true);
  assert.equal(matchesQuery(p, 'ALPHA', LIST), true); // workspace name, any case
  assert.equal(matchesQuery(p, '  ', LIST), true);
  assert.equal(matchesQuery(p, p.model, LIST), true);
  // a custom label function replaces modelLabel (the team-aware one)
  assert.equal(matchesQuery(p, 'shown-as', { ...LIST, modelLabelOf: () => 'Shown-As' }), true);
  // a pulse with no workspace is named 'no grid', an unknown id '—'
  assert.equal(matchesQuery(P('l'), 'no grid', LIST), true);
  assert.equal(matchesQuery(P('m', { grid_id: 'zzz' }), '—', LIST), true);
});

test('filterPulses: hides archived when query is empty, includes matching archived while searching', () => {
  const rows = [
    P('live', { title: 'Bright Star', state: 'running' }),
    P('arch', { title: 'Hidden Comet', state: 'archived' })
  ];
  assert.deepEqual(ids(filterPulses(rows, { ...LIST, hideArchived: true, query: '' })), ['live']);
  assert.deepEqual(ids(filterPulses(rows, { ...LIST, hideArchived: true, query: '   ' })), ['live']);
  assert.deepEqual(ids(filterPulses(rows, { ...LIST, hideArchived: true, query: 'Comet' })), ['arch']);
  assert.deepEqual(ids(filterPulses(rows, { ...LIST, hideArchived: false, query: '' })), ['live', 'arch']);
});

test('sortPulses: recent counts archived rows from archived_at (D4: both lists)', () => {
  const list = [
    P('live', { updated_at: '2026-10-05T00:00:00Z' }),
    P('arch', { state: 'archived', updated_at: '2026-10-01T00:00:00Z', archived_at: '2026-10-09T00:00:00Z' })
  ];
  assert.deepEqual(ids(sortPulses(list, 'recent')), ['arch', 'live']);
  // archived without archived_at falls back to updated_at
  const noStamp = [list[0], { ...list[1], archived_at: undefined }];
  assert.deepEqual(ids(sortPulses(noStamp, 'recent')), ['live', 'arch']);
  // the input is not mutated
  assert.deepEqual(ids(list), ['live', 'arch']);
});

test('sortPulses: name, created, byinput', () => {
  const list = [
    P('b', { title: 'bravo', created_at: '2026-10-01T00:00:00Z', last_input_at: '2026-10-03T00:00:00Z' }),
    P('a', { title: 'alpha', created_at: '2026-10-02T00:00:00Z' }),
    P('c', { title: 'charlie', created_at: '2026-10-03T00:00:00Z', last_input_at: '2026-10-04T00:00:00Z' })
  ];
  assert.deepEqual(ids(sortPulses(list, 'name')), ['a', 'b', 'c']);
  assert.deepEqual(ids(sortPulses(list, 'created')), ['c', 'a', 'b']);
  assert.deepEqual(ids(sortPulses(list, 'byinput')), ['c', 'b', 'a']);
});

test('sortPulses: attention puts urgent phases first, recency breaks ties', () => {
  const list = [
    P('old', { updated_at: '2026-10-01T00:00:00Z' }),
    P('ask', { updated_at: '2026-10-02T00:00:00Z' }),
    P('new', { updated_at: '2026-10-03T00:00:00Z' })
  ];
  const phase = (p) => (p.id === 'ask' ? 'question' : 'idle');
  assert.deepEqual(ids(sortPulses(list, 'attention', { phaseOf: phase })), ['ask', 'new', 'old']);
});

test('buildSections: workspace tree, sub-workspaces, no-workspace last', () => {
  const pulses = [P('p1', { grid_id: 'a' }), P('p2', { grid_id: 'a1' }), P('p3', { grid_id: 'b' }), P('p4')];
  {
    const s = buildSections(pulses, grids, { query: '', phaseOf: calm });
    assert.deepEqual(s.map((x) => x.grid?.id ?? null), ['a', 'b', 'c', null]);
    assert.deepEqual(ids(s[0].pulses), ['p1']);
    assert.deepEqual(s[0].subs.map((x) => [x.grid.id, ids(x.pulses)]), [['a1', ['p2']]]);
    assert.equal(s[0].count, 2); // own + sub
    assert.equal(s[2].count, 0); // an empty workspace still shows with no query
    assert.deepEqual(ids(s[3].pulses), ['p4']);
    assert.equal(s[0].phase, 'idle');
  }
});

test('buildSections: a query keeps only branches with a match and prunes empty subs', () => {
  const pulses = [P('p1', { title: 'one', grid_id: 'a' }), P('p2', { title: 'two', grid_id: 'a1' }), P('p3', { title: 'three', grid_id: 'b' })];
  const s = buildSections(pulses, grids, { query: 'two', phaseOf: calm });
  assert.deepEqual(s.map((x) => x.grid.id), ['a']);
  assert.deepEqual(s[0].subs.map((x) => x.grid.id), ['a1']);
  assert.equal(s[0].count, 1);
  const none = buildSections(pulses, grids, { query: 'nothing', phaseOf: calm });
  assert.deepEqual(none, []);
  // the workspace name matches too: Beta holds p3 only
  const byGrid = buildSections(pulses, grids, { query: 'beta', phaseOf: calm });
  assert.deepEqual(byGrid.map((x) => x.grid.id), ['b']);
  // a pulse is found by its model label
  const label = modelLabel('gpt-6-luna').toLowerCase();
  assert.equal(buildSections(pulses, grids, { query: label, phaseOf: calm }).length, 2);
});

test('buildSections: byInput is one flat section in the given order, roll-up phase kept', () => {
  const pulses = [P('b', { grid_id: 'b' }), P('a', { grid_id: 'a' })];
  const phase = (p) => (p.id === 'a' ? 'working' : 'idle');
  const [s, ...rest] = buildSections(pulses, grids, { byInput: true, phaseOf: phase });
  assert.equal(rest.length, 0);
  assert.equal(s.flat, true);
  assert.equal(s.grid, null);
  assert.deepEqual(ids(s.pulses), ['b', 'a']);
  assert.equal(s.count, 2);
  assert.equal(s.phase, 'working');
});

test('reorderGridIds: before / after a sibling, subs travel with their parent', () => {
  assert.deepEqual(reorderGridIds(grids, 'c', null, { id: 'a', after: false }), ['c', 'a', 'a1', 'b']);
  assert.deepEqual(reorderGridIds(grids, 'a', null, { id: 'b', after: true }), ['b', 'a', 'a1', 'c']);
  assert.deepEqual(reorderGridIds(grids, 'a', null, { id: 'c', after: true }), ['b', 'c', 'a', 'a1']);
  // a sub only reorders among its own siblings
  const two = [...grids, { id: 'a2', name: 'Two', parent_id: 'a' }];
  assert.deepEqual(reorderGridIds(two, 'a2', 'a', { id: 'a1', after: false }), ['a', 'a2', 'a1', 'b', 'c']);
  // a target that is not a sibling gives nothing
  assert.equal(reorderGridIds(grids, 'a1', 'a', { id: 'b', after: false }), null);
});

test('moveId: the id takes the target slot; missing ids and self-drops do nothing', () => {
  assert.deepEqual(moveId(['a', 'b', 'c', 'd'], 'd', 'b'), ['a', 'd', 'b', 'c']);
  assert.deepEqual(moveId(['a', 'b', 'c', 'd'], 'a', 'c'), ['b', 'c', 'a', 'd']);
  assert.equal(moveId(['a', 'b'], 'a', 'a'), null);
  assert.equal(moveId(['a', 'b'], 'x', 'a'), null);
  assert.equal(moveId(['a', 'b'], 'a', 'x'), null);
  const order = ['a', 'b', 'c'];
  moveId(order, 'c', 'a');
  assert.deepEqual(order, ['a', 'b', 'c'], 'input untouched');
});

test('withArchived: live rows first, an unarchived pulse shows once as its live row', () => {
  const live = [{ id: 'a', state: 'running' }, { id: 'b', state: 'stopped' }];
  const archived = [{ id: 'b', state: 'archived' }, { id: 'c', state: 'archived' }];
  assert.deepEqual(withArchived(live, archived), [
    { id: 'a', state: 'running' }, { id: 'b', state: 'stopped' }, { id: 'c', state: 'archived' }
  ]);
  assert.deepEqual(withArchived(live, []), live);
  assert.deepEqual(withArchived([], archived), archived);
});
