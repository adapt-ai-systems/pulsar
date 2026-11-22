// The sending browser showed its own message twice. The follow poll
// after send loads a full tail that already ends in the user_message; the
// next /live delta (its own, older cursor) carries that same event again.
import test from 'node:test';
import assert from 'node:assert/strict';
import { mergeDelta } from './evmerge.js';
import { buildItems } from './render.js';

const ev = (type, ts, extra = {}) => ({ type, ts, ...extra });
const history = [
  ev('assistant_text', '2026-09-24T03:54:00.000001+00:00', { text: 'earlier' }),
  ev('done', '2026-09-24T03:54:00.000002+00:00'),
];
const you = ev('user_message', '2026-09-24T03:55:00.123456+00:00', { text: "I'm Alex. What's due" });
const ack = ev('assistant_text', '2026-09-24T03:55:02.000000+00:00', { text: 'on it' });

// Fresh copies, as the HTTP body and the WS frame are parsed separately.
const clone = (x) => JSON.parse(JSON.stringify(x));
const youRows = (evs) => buildItems(evs).filter((it) => it.kind === 'you').length;

test('delta repeating the follow-poll tail does not duplicate the sent message', () => {
  const polled = clone([...history, you]);          // follow poll after send
  const merged = mergeDelta(polled, clone([you, ack]), 100); // late /live delta
  assert.equal(youRows(merged), 1);
  assert.deepEqual(merged.map((e) => e.type), ['assistant_text', 'done', 'user_message', 'assistant_text']);
});

test('the old blind concat is what doubled it', () => {
  const polled = clone([...history, you]);
  assert.equal(youRows(polled.concat(clone([you, ack]))), 2);
});

test('delta wholly inside the loaded window appends nothing', () => {
  const polled = clone([...history, you, ack]);
  const merged = mergeDelta(polled, clone([you]), 100);
  assert.equal(merged.length, 4);
  assert.equal(youRows(merged), 1);
});

test('non-overlapping delta appends in full, trimmed to the tail limit', () => {
  const merged = mergeDelta(clone(history), clone([you, ack]), 3);
  assert.deepEqual(merged.map((e) => e.ts), [history[1].ts, you.ts, ack.ts]);
});

test('a genuinely new message with the same text still appears', () => {
  const again = { ...you, ts: '2026-09-24T03:56:00.000000+00:00' };
  const merged = mergeDelta(clone([...history, you, ack]), clone([again]), 100);
  assert.equal(youRows(merged), 2);
});

test('empty delta returns the window unchanged', () => {
  const polled = clone(history);
  assert.equal(mergeDelta(polled, [], 100), polled);
});
