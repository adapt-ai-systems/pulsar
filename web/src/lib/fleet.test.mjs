import { test } from 'node:test';
import assert from 'node:assert/strict';
import { nextFireAt, fmtUntil, orderSchedules, groupWatches, groupMonitoring, watchDelivery } from './fleet.js';

const NOW = new Date('2026-08-30T12:00:00Z');

test('loop fires interval after last fire (or creation)', () => {
  const s = { kind: 'loop', enabled: true, interval_minutes: 30, last_fired_at: '2026-08-30T11:50:00Z' };
  assert.equal(nextFireAt(s, NOW).toISOString(), '2026-08-30T12:20:00.000Z');
  const fresh = { kind: 'loop', enabled: true, interval_minutes: 5, last_fired_at: null, created_at: '2026-08-30T11:58:00Z' };
  assert.equal(nextFireAt(fresh, NOW).toISOString(), '2026-08-30T12:03:00.000Z');
  assert.equal(nextFireAt({ ...s, enabled: false }, NOW), null);
});

test('once only while future; daily honors days + UTC hhmm', () => {
  assert.equal(nextFireAt({ kind: 'once', enabled: true, at_iso: '2026-08-30T13:00:00Z' }, NOW).toISOString(), '2026-08-30T13:00:00.000Z');
  assert.equal(nextFireAt({ kind: 'once', enabled: true, at_iso: '2026-08-30T11:00:00Z' }, NOW), null);
  // 2026-08-30 is a Sunday. 06:00 UTC already passed today → tomorrow (Mon).
  assert.equal(nextFireAt({ kind: 'daily', enabled: true, cron_hhmm: '06:00', days: '' }, NOW).toISOString(), '2026-08-31T06:00:00.000Z');
  // Weekdays only, 14:00 → today is Sunday, so Monday 14:00.
  assert.equal(nextFireAt({ kind: 'daily', enabled: true, cron_hhmm: '14:00', days: 'mon,tue,wed,thu,fri' }, NOW).toISOString(), '2026-08-31T14:00:00.000Z');
});

test('fmtUntil', () => {
  assert.equal(fmtUntil(new Date('2026-08-30T12:04:00Z'), NOW), 'in 4m');
  assert.equal(fmtUntil(new Date('2026-08-30T14:05:00Z'), NOW), 'in 2h 05m');
  assert.equal(fmtUntil(new Date('2026-09-02T12:00:00Z'), NOW), 'in 3d');
  assert.equal(fmtUntil(new Date('2026-08-30T11:48:00Z'), NOW), 'overdue 12m');
  assert.equal(fmtUntil(null), '—');
});

test('orderSchedules: soonest first, disabled last', () => {
  const rows = [
    { id: 'a', pulse_id: 'p1', kind: 'loop', enabled: false, interval_minutes: 5 },
    { id: 'b', pulse_id: 'p2', kind: 'once', enabled: true, at_iso: '2026-08-30T15:00:00Z' },
    { id: 'c', pulse_id: 'p3', kind: 'loop', enabled: true, interval_minutes: 10, last_fired_at: '2026-08-30T11:55:00Z' }
  ];
  assert.deepEqual(orderSchedules(rows, {}, NOW).map((r) => r.s.id), ['c', 'b', 'a']);
  // An archived owner's enabled loop never fires: it sorts with the dead.
  const dead = orderSchedules(rows, {}, NOW, (s) => s.pulse_id === 'p3');
  assert.deepEqual(dead.map((r) => [r.s.id, r.at === null]), [['b', false], ['a', true], ['c', true]]);
});

test('groupWatches: MET owners first', () => {
  const ws = [
    { id: '1', pulse_id: 'p1', bad: 0 },
    { id: '2', pulse_id: 'p2', bad: 1 },
    { id: '3', pulse_id: 'p2', bad: 0 }
  ];
  const g = groupWatches(ws, { p1: 'Alpha', p2: 'Zulu' });
  assert.deepEqual(g.map((x) => [x.title, x.met, x.watches.length]), [['Zulu', 1, 2], ['Alpha', 0, 1]]);
});

// --------------------------------------------------------------------------- //
// groupMonitoring — the Watchers shelf (2026-09-07)
// --------------------------------------------------------------------------- //
const INFO = {
  a: { title: 'Campaign monitor', state: 'stopped', mode: 'watching' },
  b: { title: 'Build', state: 'running', mode: null },
  c: { title: 'Old thing', state: 'archived', mode: null },
  d: { title: 'Parked', state: 'stopped', mode: 'paused' }
};

test('groupMonitoring merges watches + schedules by owner and includes shelved pulses', () => {
  const watches = [
    { id: 'w1', pulse_id: 'a', bad: 0, next_check_at: '2026-08-30T12:01:00Z', last_fired_at: '2026-08-30T11:00:00Z', last_delivery: 'delivered' },
    { id: 'w2', pulse_id: 'b', bad: 1, next_check_at: '2026-08-30T12:05:00Z' }
  ];
  const schedules = [
    { id: 's1', pulse_id: 'a', kind: 'loop', enabled: true, interval_minutes: 60, last_fired_at: '2026-08-30T11:30:00Z' },
    { id: 's2', pulse_id: 'c', kind: 'loop', enabled: true, interval_minutes: 5, created_at: '2026-08-30T11:00:00Z' }
  ];
  const groups = groupMonitoring(watches, schedules, INFO, NOW);
  assert.deepEqual(groups.map((g) => g.pulseId), ['b', 'a', 'c', 'd']); // met, watching, plain, paused
  const a = groups.find((g) => g.pulseId === 'a');
  assert.equal(a.watches.length, 1);
  assert.equal(a.schedules.length, 1);
  assert.equal(a.mode, 'watching');
  assert.equal(a.nextAt.toISOString(), '2026-08-30T12:01:00.000Z'); // watch check beats loop fire
  assert.equal(a.lastFiredAt.toISOString(), '2026-08-30T11:30:00.000Z'); // schedule fired later
  const c = groups.find((g) => g.pulseId === 'c');
  assert.equal(c.nextAt, null); // archived owner: the scheduler never wakes it
  const d = groups.find((g) => g.pulseId === 'd');
  assert.equal(d.watches.length + d.schedules.length, 0);
  assert.equal(d.mode, 'paused');
});

test('groupMonitoring ranks failed deliveries above quiet watchers, unknown owner keeps short id', () => {
  const watches = [
    { id: 'w1', pulse_id: 'a', bad: 0 },
    { id: 'w2', pulse_id: 'zzzzzzzz-unknown', bad: 0, last_delivery: 'failed: spawn' }
  ];
  const groups = groupMonitoring(watches, [], INFO, NOW);
  assert.deepEqual(groups.map((g) => g.pulseId), ['zzzzzzzz-unknown', 'a', 'd']); // d = paused shelf entry
  assert.equal(groups[0].title, 'zzzzzzzz');
  assert.equal(groups[0].failed, 1);
});

test('watchDelivery', () => {
  assert.equal(watchDelivery({ bad: 1 }), 'met');
  assert.equal(watchDelivery({ bad: 0, last_delivery: 'failed: x' }), 'failed');
  assert.equal(watchDelivery({ bad: 0, last_delivery: 'delivered' }), 'delivered');
  assert.equal(watchDelivery({ bad: 0 }), null);
});

test('a zone-less once at_iso is UTC, like the daemon', async () => {
  const { parseAtIso } = await import('./schedule.js');
  assert.equal(parseAtIso('2026-09-24T04:36:28.432242').toISOString(), '2026-09-24T04:36:28.432Z');
  assert.equal(parseAtIso('2026-09-24T04:36:00+00:00').toISOString(), '2026-09-24T04:36:00.000Z');
  assert.equal(parseAtIso('2026-09-24T04:36:00Z').toISOString(), '2026-09-24T04:36:00.000Z');
  assert.equal(parseAtIso('2026-09-24 09:00').toISOString(), '2026-09-24T09:00:00.000Z');
  const now = new Date('2026-09-24T04:31:28Z');
  const at = nextFireAt({ kind: 'once', enabled: true, at_iso: '2026-09-24T04:36:28' }, now);
  assert.equal(at.getTime() - now.getTime(), 5 * 60000);
});

test('an archived pulse with an old paused mode is not shelved', () => {
  const groups = groupMonitoring([], [], {
    a: { title: 'gone', state: 'archived', mode: 'paused' },
    b: { title: 'resting', state: 'stopped', mode: 'paused' }
  });
  assert.deepEqual(groups.map((g) => g.title), ['resting']);
});
