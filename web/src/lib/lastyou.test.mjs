// The YOU strip's wide fallback (2026-11-02): a pulse with no real message in
// its last 2,000 events re-fetched them on every live push. The memo keeps the
// answer, negative ones included, while windows stay contiguous.
import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { lastYouFromWindow, lastYouFromWide, scanLastYou, _resetLastYou } from './lastyou.js';

const ts = (s) => `2026-11-02T22:${String(Math.floor(s / 60)).padStart(2, '0')}:${String(s % 60).padStart(2, '0')}-0800`;
const you = (s, text = 'hi') => ({ type: 'user_message', text, ts: ts(s) });
const watch = (s) => ({ type: 'user_message', text: '[watch: pulsar-wait: x', origin: 'daemon', via: 'condwatch', injected: 'True', ts: ts(s) });
const tool = (s) => ({ type: 'tool_call', name: 'Bash', ts: ts(s) });
const done = (s) => ({ type: 'done', ts: ts(s) });
const win = (from, n, mk = tool) => Array.from({ length: n }, (_, i) => mk(from + i));

test('real message in the window: shown, no fetch', () => {
  _resetLastYou();
  const evs = [you(0, 'do X'), tool(1), done(2), ...win(3, 7)];
  const r = lastYouFromWindow('p1', evs, 10);
  assert.equal(r.fetch, false);
  assert.equal(r.show.text, 'do X');
  assert.equal(r.show.doneTs, ts(2));
});

test('no message anywhere: one wide fetch, then none while windows overlap', () => {
  _resetLastYou();
  const r1 = lastYouFromWindow('p2', win(100, 10, watch), 10);
  assert.deepEqual(r1, { show: undefined, fetch: true });
  // a second window while the fetch runs: no second fetch, strip untouched
  assert.deepEqual(lastYouFromWindow('p2', win(101, 10, watch), 10), { show: undefined, fetch: false });
  assert.equal(lastYouFromWide('p2', win(0, 110, watch)), null);
  // live deltas slide the window forward: remembered, no fetch
  for (let s = 102; s < 140; s++) {
    assert.deepEqual(lastYouFromWindow('p2', win(s, 10, watch), 10), { show: null, fetch: false });
  }
});

test('a gap in the windows (socket down for a while) fetches again', () => {
  _resetLastYou();
  lastYouFromWindow('p3', win(100, 10), 10);
  lastYouFromWide('p3', win(0, 110));
  assert.equal(lastYouFromWindow('p3', win(105, 10), 10).fetch, false);
  // next window starts after everything scanned so far (through = 114)
  assert.equal(lastYouFromWindow('p3', win(200, 10), 10).fetch, true);
});

test('wide hit stays shown after it scrolls out; done found in a later window', () => {
  _resetLastYou();
  lastYouFromWindow('p4', win(100, 10), 10);
  const show = lastYouFromWide('p4', [you(50, 'old ask'), ...win(51, 59)]);
  assert.equal(show.text, 'old ask');
  assert.equal(show.doneTs, null);
  const r = lastYouFromWindow('p4', [...win(105, 5), done(110), ...win(111, 4)], 10);
  assert.equal(r.fetch, false);
  assert.equal(r.show.text, 'old ask');
  assert.equal(r.show.doneTs, ts(110));
});

test('a message seen in the window is remembered when it scrolls out (no fetch)', () => {
  _resetLastYou();
  lastYouFromWindow('p5', [you(10, 'mine'), ...win(11, 9)], 10);
  const r = lastYouFromWindow('p5', win(15, 10), 10);
  assert.equal(r.fetch, false);
  assert.equal(r.show.text, 'mine');
});

test('short window is the whole log: null, no fetch', () => {
  _resetLastYou();
  assert.deepEqual(lastYouFromWindow('p6', win(0, 3, watch), 10), { show: null, fetch: false });
  assert.deepEqual(lastYouFromWindow('p6', [], 10), { show: null, fetch: false });
});

test('failed wide fetch leaves the strip and retries on the next window', () => {
  _resetLastYou();
  assert.equal(lastYouFromWindow('p7', win(100, 10), 10).fetch, true);
  assert.equal(lastYouFromWide('p7', null), undefined);
  assert.equal(lastYouFromWindow('p7', win(101, 10), 10).fetch, true);
});

test('pulses are remembered separately', () => {
  _resetLastYou();
  lastYouFromWindow('a', [you(1, 'for a'), ...win(2, 9)], 10);
  assert.equal(lastYouFromWindow('b', win(5, 10), 10).fetch, true);
  assert.equal(lastYouFromWindow('a', win(5, 10), 10).show.text, 'for a');
});

test('scanLastYou skips watch and schedule messages', () => {
  assert.equal(scanLastYou([watch(1), { type: 'user_message', text: 'x', msg_file: 'sched-1.json', ts: ts(2) }]), null);
});

test('LastYouStrip drops dead open state/aria-expanded and supports keyboard Escape and row activation', () => {
  const src = readFileSync(new URL('./LastYouStrip.svelte', import.meta.url), 'utf8');
  assert.doesNotMatch(src, /aria-expanded/);
  assert.doesNotMatch(src, /\.last-you\.open/);
  assert.match(src, /e\.key === 'Escape'[\s\S]*?closeHistory\(\)/);
  assert.match(src, /class="you-hist-row"[\s\S]*?tabindex="0"[\s\S]*?onkeydown=/);
});
