import { test } from 'node:test';
import assert from 'node:assert/strict';
import { parseTs, agoLabel, fmtTs, fmtTsFull, fmtClock, fmtMailTime, fmtStamp } from './time.js';

const NOW = Date.parse('2026-07-28T21:00:00+00:00');

test('parseTs handles the daemon +0000 zone form', () => {
  assert.equal(parseTs('2026-07-28T21:00:00+0000'), NOW);
  assert.equal(parseTs('2026-07-28T21:00:00+00:00'), NOW);
  assert.ok(Number.isNaN(parseTs('')));
  assert.ok(Number.isNaN(parseTs('not a time')));
});

test('agoLabel renders seconds/minutes/hours', () => {
  assert.equal(agoLabel(NOW - 42_000, NOW), '42s');
  assert.equal(agoLabel(NOW - 5 * 60_000, NOW), '5m');
  assert.equal(agoLabel(NOW - 59 * 60_000 - 30_000, NOW), '59m');
  assert.equal(agoLabel(NOW - 130 * 60_000, NOW), '2h');
});

// Explicit zone fixture: every test passes the zone, so none of them depends
// on the TZ of the machine that runs them.
const LA = 'America/Los_Angeles';
const TOKYO = 'Asia/Tokyo';
const TS = '2026-07-28T21:05:00+0000'; // 14:05 in LA, 06:05 on Jul 29 in Tokyo

test('fmtClock shows the clock time of the given zone, 24 h or 12 h', () => {
  assert.equal(fmtClock(TS, { zone: LA, locale: 'en-US' }), '14:05');
  assert.equal(fmtClock(TS, { zone: TOKYO, locale: 'en-US' }), '06:05');
  assert.equal(fmtClock(TS, { zone: LA, h12: true, locale: 'en-US' }), '02:05 PM');
  assert.equal(fmtClock('', { zone: LA }), '');
  assert.equal(fmtClock('garbage', { zone: LA }), 'garbage');
});

test('fmtMailTime judges today and the year in the given zone', () => {
  // 18:00 UTC on Jul 28 = 11:00 Jul 28 in LA, 03:00 Jul 29 in Tokyo
  const now = Date.parse('2026-07-28T18:00:00Z');
  assert.equal(fmtMailTime(TS, now, { zone: LA, locale: 'en-US' }), '14:05');
  // same instant, Tokyo's calendar: Jul 29 06:05 vs now = Jul 29 03:00 -> today
  assert.equal(fmtMailTime(TS, now, { zone: TOKYO, locale: 'en-US' }), '06:05');
  // 02:00 UTC Jul 29 is still Jul 28 evening in LA, but Jul 29 in Tokyo
  const early = '2026-07-29T02:00:00+0000';
  assert.equal(fmtMailTime(early, now, { zone: LA, locale: 'en-US' }), '19:00');
  assert.equal(fmtMailTime(early, now, { zone: TOKYO, locale: 'en-US' }), '11:00');
  // Dec 31 / Jan 1 boundary: other zone's year
  const newYear = Date.parse('2027-01-01T03:00:00Z'); // still Dec 31 in LA
  assert.equal(fmtMailTime('2026-12-31T20:00:00+0000', newYear, { zone: LA, locale: 'en-US' }), '12:00');
  assert.equal(fmtMailTime('2026-12-31T20:00:00+0000', newYear, { zone: TOKYO, locale: 'en-US' }), '05:00');
  assert.equal(fmtMailTime('2026-12-30T20:00:00+0000', newYear, { zone: LA, locale: 'en-US' }), 'Dec 30, 12:00');
});

test('fmtTs and fmtTsFull follow the zone and name it', () => {
  assert.equal(fmtTsFull(TS, LA), 'Jul 28, 2026, 2:05:00 PM PDT');
  assert.match(fmtTsFull(TS, TOKYO), /^Jul 29, 2026, 6:05:00 AM (GMT\+9|JST)$/);
  assert.equal(fmtTsFull('', LA), '');
  // not today in either zone -> date prefix
  assert.equal(fmtTs(TS, LA), 'Jul 28 · 2:05 PM');
  assert.equal(fmtTs(TS, TOKYO), 'Jul 29 · 6:05 AM');
  assert.equal(fmtTs('', LA), '');
});

test('time.js and Inbox hard-code no zone', async () => {
  const { readFileSync } = await import('node:fs');
  for (const f of ['time.js', 'Inbox.svelte']) {
    const src = readFileSync(new URL(`./${f}`, import.meta.url), 'utf8');
    assert.doesNotMatch(src, /Los_Angeles|toLocale(Time|Date)String/, f);
  }
});

test('fmtStamp is the tiny board stamp: time today, a short date before that', () => {
  const LA = 'America/Los_Angeles';
  const ts = '2026-07-28T21:05:00+0000';            // 2:05 PM in Los Angeles
  const sameDay = Date.parse('2026-07-28T23:00:00Z');
  assert.equal(fmtStamp(ts, LA, sameDay), '2:05p');
  assert.equal(fmtStamp(ts, LA, Date.parse('2026-11-02T18:00:00Z')), '7/28');
  assert.equal(fmtStamp(ts, LA, Date.parse('2027-01-02T18:00:00Z')), '7/28/26');
  assert.equal(fmtStamp('', LA, sameDay), '');
});
