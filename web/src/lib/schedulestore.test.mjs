import { test } from 'node:test';
import assert from 'node:assert/strict';

function schedulesFor(schedules, pulseId) {
  return schedules.filter((s) => s.pulse_id === pulseId);
}

test('schedulesFor filters by owner pulse', () => {
  const ss = [
    { id: '1', pulse_id: 'a', kind: 'loop' },
    { id: '2', pulse_id: 'b', kind: 'once' },
    { id: '3', pulse_id: 'a', kind: 'daily' }
  ];
  assert.deepEqual(schedulesFor(ss, 'a').map((s) => s.id), ['1', '3']);
  assert.deepEqual(schedulesFor(ss, 'zzz'), []);
});
