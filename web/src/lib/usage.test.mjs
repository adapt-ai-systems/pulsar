/**
 * Self-contained tests for usage.js helpers (FUEL rail).
 *
 * No test framework is installed in web/, so this is a plain-node assertion
 * script (same pattern as schedule.test.mjs):
 *
 *     node src/lib/usage.test.mjs
 */

import assert from 'node:assert/strict';
import { clampPercent, severityForPercent, formatResetIn, usageColor } from './usage.js';

// --- clampPercent ------------------------------------------------------------
assert.equal(clampPercent(-5), 0);
assert.equal(clampPercent(55), 55);
assert.equal(clampPercent(140), 100);

// --- severityForPercent -------------------------------------------------------
assert.equal(severityForPercent(10), 'normal');
assert.equal(severityForPercent(60), 'warn');
assert.equal(severityForPercent(85), 'hot');

// --- usageColor ---------------------------------------------------------------
// The ramp must be anchored at the ends and monotonic through the middle: an
// almost-full meter has to read red, an empty one green, with no colour that
// travels backwards on the way.
assert.match(usageColor(0), /--sprite\) 100%/);     // empty = pure green
assert.match(usageColor(100), /var\(--bad\)\)$/);   // full  = pure red
assert.match(usageColor(100), /var\(--matron\) 0%/);

// Each stop lands exactly ON its own token. The mix is expressed from the
// segment's lower end, so "pure lantern" reads as `sprite 0%, lantern` — the
// share, not the variable name, is what carries the meaning here.
assert.equal(usageColor(40), 'color-mix(in srgb, var(--sprite) 0%, var(--lantern))');
assert.equal(usageColor(70), 'color-mix(in srgb, var(--lantern) 0%, var(--matron))');

// Midpoints sit between the right pair of stops.
assert.match(usageColor(20), /--sprite\) 50%, var\(--lantern\)/);
assert.match(usageColor(85), /--matron\) 50%, var\(--bad\)/);

// Out-of-range and junk clamp instead of producing a nonsense mix. Number(null)
// is 0 in JS, so `null` must be caught as absent BEFORE it reads as "empty" —
// the same trap that bit the calendar's past-days value.
assert.equal(usageColor(-20), usageColor(0));
assert.equal(usageColor(140), usageColor(100));
assert.equal(usageColor(NaN), usageColor(0));
assert.equal(usageColor(undefined), usageColor(0));

// No share can fall outside 0..100, at any input, or CSS drops the declaration
// and the pip renders colourless.
for (let p = -50; p <= 150; p += 0.5) {
  const m = /(\d+)%/.exec(usageColor(p));
  assert.ok(m, `no share in usageColor(${p})`);
  const share = Number(m[1]);
  assert.ok(share >= 0 && share <= 100, `share ${share} out of range at ${p}`);
}

// --- formatResetIn ------------------------------------------------------------
const now = Date.parse('2026-07-27T03:34:00Z');
const at = (iso) => formatResetIn(iso, now);

// Honest nulls: missing or garbage input renders nothing, not a countdown.
assert.equal(at(null), null);
assert.equal(at(undefined), null);
assert.equal(at(''), null);
assert.equal(at('not-a-date'), null);

// Already elapsed (or exactly now) → "now".
assert.equal(at('2026-07-27T03:34:00Z'), 'now');
assert.equal(at('2026-07-27T03:00:00Z'), 'now');

// Sub-minute / minutes / hours / days — two units max, largest first.
assert.equal(at('2026-07-27T03:34:30Z'), '<1m');
assert.equal(at('2026-07-27T04:16:00Z'), '42m');
assert.equal(at('2026-07-27T05:10:00Z'), '1h 36m');
assert.equal(at('2026-07-27T06:34:00Z'), '3h 0m');
assert.equal(at('2026-07-31T12:59:59Z'), '4d 9h');

// Offset-style ISO (what the daemon actually emits) parses too.
assert.equal(at('2026-07-27T05:10:00.614662+00:00'), '1h 36m');

console.log('usage.test.mjs: all assertions passed');
