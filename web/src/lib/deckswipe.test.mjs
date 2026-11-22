import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDeckSwipe, H_THRESHOLD, H_LOCK, V_LOCK } from './deckswipe.js';

const button = { closest: (sel) => (sel.includes('button') ? {} : null) };

function ev(id, x, y, target = null) {
  return { pointerId: id, clientX: x, clientY: y, target };
}

function swipeFrom(start, { id = 7, x = 200, y = 300, target = null, index = 1, count = 3 } = {}) {
  const g = createDeckSwipe();
  const armed = g.down(ev(id, x, y, target));
  const moves = [];
  for (const [dx, dy] of start) {
    moves.push(g.move(ev(id, x + dx, y + dy, target), { index, count }));
  }
  const end = start.length
    ? start[start.length - 1]
    : [0, 0];
  const up = g.up(ev(id, x + end[0], y + end[1], target), { index, count });
  return { g, armed, moves, up };
}

test('down does not consult target.closest (0f651cc INTERACTIVE bail)', () => {
  const g = createDeckSwipe();
  const target = {
    closest() { throw new Error('swipe must not bail on interactive targets'); },
  };
  assert.equal(g.down(ev(1, 0, 0, target)), true);
});

test('start-on-button horizontal swipe navigates', () => {
  // Regression for 0f651cc: INTERACTIVE early-return in onPointerDown meant a
  // swipe that began on a copy button / link / tool-row never armed.
  const { armed, moves, up } = swipeFrom([[-80, 2]], { target: button, index: 1, count: 3 });
  assert.equal(armed, true, 'must arm even when the target is a button');
  assert.equal(moves.at(-1).capture, true, 'horizontal lock captures the pointer');
  assert.equal(moves.at(-1).preventDefault, true);
  assert.equal(up.navigated, true);
  assert.equal(up.to, 2, 'swipe left (negative dx) → next pulse');
});

test('start-on-button swipe left vs right', () => {
  const left = swipeFrom([[-80, 1]], { target: button, index: 1, count: 3 });
  assert.equal(left.up.navigated, true);
  assert.equal(left.up.to, 2);

  const right = swipeFrom([[80, 1]], { target: button, index: 1, count: 3 });
  assert.equal(right.up.navigated, true);
  assert.equal(right.up.to, 0);
});

test('start-on-button tap does not navigate', () => {
  const { armed, moves, up } = swipeFrom([], { target: button, index: 1, count: 3 });
  assert.equal(armed, true);
  assert.equal(moves.length, 0);
  assert.equal(up.navigated, false);
  assert.equal(up.axis, null);
  assert.equal(up.to, 1);
});

test('finger jitter under H_LOCK on a button is still a tap', () => {
  const { moves, up } = swipeFrom([[H_LOCK, 0], [H_LOCK - 1, 1]], {
    target: button, index: 1, count: 3,
  });
  assert.ok(moves.every((m) => !m.capture), 'must not capture under the finger');
  assert.equal(up.navigated, false);
  assert.equal(up.axis, null);
});

test('vertical scroll does not navigate', () => {
  const { moves, up } = swipeFrom([[2, V_LOCK + 20]], { target: button, index: 1, count: 3 });
  assert.ok(moves.every((m) => !m.capture && !m.preventDefault));
  assert.equal(moves.at(-1).axis, 'y');
  assert.equal(up.navigated, false);
  assert.equal(up.axis, 'y');
});

test('diagonal with more vertical than horizontal stays a scroll', () => {
  const { up } = swipeFrom([[3, 30]], { index: 1, count: 3 });
  assert.equal(up.navigated, false);
  assert.equal(up.axis, 'y');
});

test('multi-touch is ignored', () => {
  const g = createDeckSwipe();
  const a = ev(7, 200, 300);
  const b = ev(8, 210, 310);
  assert.equal(g.down(a), true);
  assert.equal(g.down(b), false, 'second pointer must not take over');
  const moveB = g.move(ev(8, 210 - 90, 310), { index: 1, count: 3 });
  assert.equal(moveB.ignored, true);
  const upB = g.up(ev(8, 210 - 90, 310), { index: 1, count: 3 });
  assert.equal(upB.ignored, true);
  // Original pointer can still complete a swipe.
  g.move(ev(7, 200 - 80, 300), { index: 1, count: 3 });
  const upA = g.up(ev(7, 200 - 80, 300), { index: 1, count: 3 });
  assert.equal(upA.navigated, true);
  assert.equal(upA.to, 2);
});

test('below-threshold horizontal swipe snaps back', () => {
  const dx = H_THRESHOLD - 1;
  assert.ok(dx > H_LOCK);
  const { moves, up } = swipeFrom([[-dx, 0]], { index: 1, count: 3 });
  assert.equal(moves.at(-1).axis, 'x');
  assert.equal(up.navigated, false);
  assert.equal(up.snapBack, true);
  assert.equal(up.to, 1);
});

test('cannot swipe past the first or last pulse', () => {
  const atStart = swipeFrom([[80, 0]], { index: 0, count: 3 });
  assert.equal(atStart.up.navigated, false);
  assert.ok(atStart.moves.at(-1).dragDX < 80, 'rubber-band at the start');

  const atEnd = swipeFrom([[-80, 0]], { index: 2, count: 3 });
  assert.equal(atEnd.up.navigated, false);
  assert.ok(atEnd.moves.at(-1).dragDX > -80, 'rubber-band at the end');
});

test('plain pane background swipe still navigates', () => {
  const { armed, up } = swipeFrom([[-80, 0]], { target: null, index: 0, count: 2 });
  assert.equal(armed, true);
  assert.equal(up.navigated, true);
  assert.equal(up.to, 1);
});

test('axis locks to x once and ignores later vertical drift', () => {
  const g = createDeckSwipe();
  g.down(ev(1, 0, 0));
  const first = g.move(ev(1, H_LOCK + 1, 0), { index: 1, count: 3 });
  assert.equal(first.axis, 'x');
  const drifted = g.move(ev(1, H_LOCK + 2, 40), { index: 1, count: 3 });
  assert.equal(drifted.axis, 'x', 'vertical drift after lock must not steal the swipe');
  assert.equal(drifted.capture, true);
});
