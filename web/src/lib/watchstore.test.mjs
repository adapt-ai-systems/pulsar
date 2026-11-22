import { test } from 'node:test';
import assert from 'node:assert/strict';

// watchesFor is pure; the store itself needs the Svelte runtime so it is
// exercised in the browser, not here.
function watchesFor(watches, pulseId) {
  return watches.filter((w) => w.pulse_id === pulseId).sort((a, b) => (!!b.bad) - (!!a.bad));
}

test('watchesFor filters by owner and puts MET first', () => {
  const ws = [
    { id: '1', pulse_id: 'a', bad: 0 },
    { id: '2', pulse_id: 'b', bad: 1 },
    { id: '3', pulse_id: 'a', bad: 1 }
  ];
  assert.deepEqual(watchesFor(ws, 'a').map((w) => w.id), ['3', '1']);
  assert.deepEqual(watchesFor(ws, 'zzz'), []);
});
