import { test } from 'node:test';
import assert from 'node:assert/strict';
import { ghostSupersededBy } from './ghostnet.js';

const at = (text, id = undefined) => ({ type: 'assistant_text', text, id });
const think = (text, id = undefined) => ({ type: 'reasoning', text, id });
const BLOCK = 'Applied from here on. The full reply.';

test('retire: window carries the finished block the ghost shows', () => {
  assert.equal(ghostSupersededBy([at(BLOCK)], BLOCK), true);
});

test('retire: ghost tail longer than the block (provider normalized)', () => {
  assert.equal(ghostSupersededBy([at(BLOCK)], BLOCK + ' **bolded tail**'), true);
});

test('no early retire: ghost is a live PREFIX of this turn block', () => {
  assert.equal(ghostSupersededBy([at(BLOCK)], 'Applied from h'), false);
});

test('anchored match: a stream that STARTS verbatim like an older full block retires', () => {
  // The surviving edge after the bidirectional removal: an old block that is a
  // VERBATIM PREFIX of the new stream (the model opens a new turn by repeating
  // an old reply exactly, then continues) matches the anchored predicate.
  // Accepted: bounded to one push refresh, same class as the "Done." habit.
  const old = 'On it. Copying now.';
  assert.equal(
    ghostSupersededBy([at(old)], old + ' Then verifying checksums and copying files.'),
    true,
  );
});

test('retire: identical text IS treated as a duplicate (anchored full block)', () => {
  // Includes the accepted blink edge: a habitual "Done." re-stream. Bounded to
  // one push refresh, and the safe direction is to trust the durable block.
  assert.equal(ghostSupersededBy([at('Done.')], 'Done.'), true);
});

test('no ghost, no retire (whole-block push / stream off / remount)', () => {
  assert.equal(ghostSupersededBy([at(BLOCK)], null), false);
  assert.equal(ghostSupersededBy([at(BLOCK)], ''), false);
});

test('empty durable text never retires', () => {
  assert.equal(ghostSupersededBy([at('')], 'anything'), false);
});

test('scan window: the newest 3 assistant_text events only', () => {
  const fresh = at(BLOCK);
  const evs = [fresh];
  for (let i = 0; i < 3; i++) evs.push({ type: 'tool_call', id: `t${i}` });
  for (let i = 0; i < 2; i++) evs.push(at(`noise ${i}`));
  // fresh block is 3rd-newest assistant_text — still seen
  assert.equal(ghostSupersededBy(evs, BLOCK), true);
  // a 4th newest assistant_text pushes it out of the window
  evs.push(at('newest'));
  assert.equal(ghostSupersededBy(evs, BLOCK), false);
  assert.equal(ghostSupersededBy([...evs, at('one more')], BLOCK), false);
});

test('thinking ghost: card text starts the live block — retire', () => {
  const card = think('I should check the disk first.');
  assert.equal(ghostSupersededBy([card], card.text, 'reasoning'), true);
  assert.equal(ghostSupersededBy([card], card.text + ' then copy', 'reasoning'), true);
});

test('thinking ghost: live prefix of a future card — keep', () => {
  assert.equal(ghostSupersededBy([think('I should check the disk first.')], 'I should ch', 'reasoning'), false);
});

test('thinking ghost: never match the reverse direction', () => {
  assert.equal(ghostSupersededBy([think('step one. step two. step three.')], 'step two', 'reasoning'), false);
});

test('thinking ghost: no ghost, no retire', () => {
  assert.equal(ghostSupersededBy([think('x')], null, 'reasoning'), false);
});
