/** node src/lib/wstree.test.mjs — sub-workspace helpers in wscolor.js + attention.js */
import assert from 'node:assert/strict';
import { gridTree, gridFamily, topOf, topGrids, groupByWorkspace } from './wscolor.js';
import { attentionOf, rollUp, byAttention } from './attention.js';

const grids = [
  { id: 'a', name: 'A', parent_id: null },
  { id: 'a1', name: 'A1', parent_id: 'a' },
  { id: 'a2', name: 'A2', parent_id: 'a' },
  { id: 'b', name: 'B', parent_id: null },
  { id: 'z', name: 'orphan', parent_id: 'gone' }
];

assert.deepEqual(topGrids(grids).map((g) => g.id), ['a', 'b', 'z']);
assert.deepEqual(gridTree(grids).map((t) => [t.grid.id, t.children.map((c) => c.id)]), [
  ['a', ['a1', 'a2']], ['b', []], ['z', []]
]);
assert.deepEqual([...gridFamily('a', grids)], ['a', 'a1', 'a2']);
assert.deepEqual([...gridFamily('a1', grids)], ['a1']);
assert.equal(topOf('a2', grids), 'a');
assert.equal(topOf('b', grids), 'b');
assert.equal(topOf('nope', grids), 'nope');

// groupByWorkspace keeps children right after their parent (tab order).
const pulses = [{ id: 1, grid_id: 'b' }, { id: 2, grid_id: 'a2' }, { id: 3, grid_id: null }, { id: 4, grid_id: 'a' }];
assert.deepEqual(groupByWorkspace(pulses, grids).map((p) => p.id), [4, 2, 1, 3]);

// attention
const now = Date.now();
const live = (busy) => ({ socket_state: busy ? 'busy' : 'idle', reachable: true, heartbeat_fresh: true, heartbeat: { state: busy ? 'busy' : 'idle' } });
const run = (id, busy) => ({ id, state: 'running', live: live(busy) });
assert.equal(attentionOf({ id: 'x', state: 'archived' }), 'stopped');
assert.equal(attentionOf(run('w', true), { nowMs: now }), 'working');
assert.equal(attentionOf(run('i', false), { nowMs: now }), 'idle');
assert.equal(attentionOf(run('d', false), { nowMs: now, doneIds: new Set(['d']) }), 'done');
assert.equal(attentionOf(run('q', true), { nowMs: now, questionPulseIds: new Set(['q']) }), 'question');
assert.equal(rollUp(['idle', 'working', 'done']), 'done');
assert.equal(rollUp([]), 'stopped');
const order = ['idle', 'question', 'working', 'done'].sort(byAttention((x) => x));
assert.deepEqual(order, ['question', 'done', 'working', 'idle']);

console.log('wstree.test.mjs ok');
