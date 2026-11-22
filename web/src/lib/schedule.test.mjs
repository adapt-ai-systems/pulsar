/**
 * Self-contained tests for schedule.js (Milestone 3 time conversion + labels).
 *
 * No test framework is installed in web/, so this is a plain-node assertion
 * script (matches the "test everything" rule without adding a dependency):
 *
 *     node src/lib/schedule.test.mjs
 *
 * It PINS the timezone to America/Los_Angeles (TZ env, set below via a guard) so
 * the local<->UTC conversions are deterministic regardless of the host clock.
 */

import assert from 'node:assert/strict';

// Guard: these tests only make sense in Pacific time. Re-exec under TZ if needed.
if (process.env.TZ !== 'America/Los_Angeles') {
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, [new URL(import.meta.url).pathname], {
    stdio: 'inherit',
    env: { ...process.env, TZ: 'America/Los_Angeles' }
  });
  process.exit(r.status ?? 1);
}

const {
  localDatetimeToUtcIso,
  localHhmmToUtcHhmm,
  utcHhmmToLocalHhmm,
  scheduleLabel,
  DAY_KEYS
} = await import('./schedule.js');

let passed = 0;
function test(name, fn) {
  fn();
  passed++;
  console.log('  ok -', name);
}

// --- localDatetimeToUtcIso ------------------------------------------------ //
// Jan 15 2026 09:00 PST (UTC-8) -> 17:00Z
test('one-shot local (PST winter) -> UTC iso', () => {
  const iso = localDatetimeToUtcIso('2026-01-15T09:00');
  assert.equal(iso, '2026-01-15T17:00:00.000Z');
});
// Jul 15 2026 09:00 PDT (UTC-7) -> 16:00Z (DST handled via Date)
test('one-shot local (PDT summer) -> UTC iso', () => {
  const iso = localDatetimeToUtcIso('2026-07-15T09:00');
  assert.equal(iso, '2026-07-15T16:00:00.000Z');
});
test('one-shot empty/garbage -> null', () => {
  assert.equal(localDatetimeToUtcIso(''), null);
  assert.equal(localDatetimeToUtcIso('not-a-date'), null);
});

// --- localHhmmToUtcHhmm ---------------------------------------------------- //
// Uses today's offset. Assert it round-trips through utcHhmmToLocalHhmm rather
// than pinning to a season (the guard fixes the zone, not the date).
test('recurring local HH:MM round-trips through UTC', () => {
  for (const local of ['00:00', '09:30', '13:05', '23:59']) {
    const utc = localHhmmToUtcHhmm(local);
    assert.match(utc, /^\d{2}:\d{2}$/);
    assert.equal(utcHhmmToLocalHhmm(utc), local.padStart(5, '0'));
  }
});
test('recurring bad HH:MM -> null', () => {
  assert.equal(localHhmmToUtcHhmm('24:00'), null);
  assert.equal(localHhmmToUtcHhmm('9:99'), null);
  assert.equal(localHhmmToUtcHhmm('abc'), null);
  assert.equal(localHhmmToUtcHhmm(''), null);
});

// --- scheduleLabel -------------------------------------------------------- //
test('loop label', () => {
  assert.equal(scheduleLabel({ kind: 'loop', interval_minutes: 60 }), 'loop 60m');
});
// NOTE: daily labels convert cron_hhmm using TODAY's offset (a daily wake has no
// stored date), so we compute the expected local time the same way the code does.
test('daily label with days', () => {
  const local = utcHhmmToLocalHhmm('16:00'); // today's PT offset
  const [h] = local.split(':').map(Number);
  const expectedHr = (h % 12 || 12) + (h >= 12 ? 'pm' : 'am');
  const label = scheduleLabel({ kind: 'daily', cron_hhmm: '16:00', days: 'mon,wed,fri' });
  assert.equal(label, `wakes Mon Wed Fri ${expectedHr}`);
});
test('daily label no days = every day', () => {
  const local = utcHhmmToLocalHhmm('15:00');
  const [h] = local.split(':').map(Number);
  const expectedHr = (h % 12 || 12) + (h >= 12 ? 'pm' : 'am');
  const label = scheduleLabel({ kind: 'daily', cron_hhmm: '15:00', days: null });
  assert.equal(label, `wakes daily ${expectedHr}`);
});
test('once label produces a wakes string', () => {
  const label = scheduleLabel({ kind: 'once', at_iso: '2026-01-15T17:00:00.000Z' });
  assert.match(label, /^wakes /);
});
test('DAY_KEYS is sun..sat', () => {
  assert.deepEqual(DAY_KEYS, ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat']);
});

test('scheduleLabel: loops with event triggers show the trigger label', () => {
  assert.equal(scheduleLabel({ kind: 'loop', interval_minutes: 40 }), 'loop 40m');
  assert.equal(scheduleLabel({ kind: 'loop', interval_minutes: 40, triggers: ['timer'] }), 'loop 40m');
  assert.equal(
    scheduleLabel({ kind: 'loop', interval_minutes: 40, triggers: ['timer', 'tools:20'], trigger_mode: 'any' }),
    'loop every 40 min, every 20 tool calls'
  );
});

console.log(`\n${passed} schedule.js tests passed`);
