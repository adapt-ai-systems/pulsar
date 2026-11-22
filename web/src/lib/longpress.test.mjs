import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRuneModule } from './runes-loader.mjs';
import {
  LONG_PRESS_MS,
  PRESS_SLOP,
  SWALLOW_CLICK_MS,
  movedPastSlop,
  swallowNextClick,
  dropUnder,
  blockTouchScroll
} from './longpress.js';

const { createLongPressDrag } = await loadRuneModule('longpress.svelte.js');

// `$effect.root` is a rune, so the helper has to be compiled like a module.
const rootDir = mkdtempSync(join(tmpdir(), 'longpress-test-'));
writeFileSync(join(rootDir, 'root.svelte.js'), 'export const effectRoot = (fn) => $effect.root(fn);\n');
const { effectRoot: $effectRoot } = await loadRuneModule(join(rootDir, 'root.svelte.js'));

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

/** A document stand-in that records click listeners. */
function fakeDoc() {
  const listeners = [];
  return {
    listeners,
    addEventListener: (type, fn, opts) => listeners.push({ type, fn, opts }),
    removeEventListener(type, fn) {
      const i = listeners.findIndex((l) => l.type === type && l.fn === fn);
      if (i >= 0) listeners.splice(i, 1);
    }
  };
}

function fakeEl() {
  const el = { captured: [], released: [] };
  el.setPointerCapture = (id) => el.captured.push(id);
  el.releasePointerCapture = (id) => el.released.push(id);
  return el;
}
const ev = (x, y, extra = {}) => ({ pointerId: 1, pointerType: 'touch', button: 0, clientX: x, clientY: y, ...extra });

test('default long-press timings stay at 320ms / 9px slop', () => {
  assert.equal(LONG_PRESS_MS, 320);
  assert.equal(PRESS_SLOP, 9);
  assert.equal(SWALLOW_CLICK_MS, 400);
});

test('movedPastSlop: 9 px is still a hold, 10 px is a scroll', () => {
  const press = { x: 100, y: 100 };
  assert.equal(movedPastSlop(press, 109, 100), false);
  assert.equal(movedPastSlop(press, 100, 91), false);
  assert.equal(movedPastSlop(press, 110, 100), true);
  assert.equal(movedPastSlop(press, 100, 89), true);
  assert.equal(movedPastSlop(press, 105, 100, 4), true);
});

test('dropUnder: closest match for the selector, null when nothing usable', () => {
  const hit = { closest: (sel) => (sel === '[data-ws-drop]' ? 'CHIP' : null) };
  assert.equal(dropUnder(1, 2, '[data-ws-drop]', () => hit), 'CHIP');
  assert.equal(dropUnder(1, 2, '[data-other]', () => hit), null);
  assert.equal(dropUnder(1, 2, '[data-ws-drop]', () => null), null);
  assert.equal(dropUnder(1, 2, '[data-ws-drop]', () => ({})), null);
  const seen = [];
  dropUnder(7, 9, 'x', (x, y) => (seen.push([x, y]), null));
  assert.deepEqual(seen, [[7, 9]]);
});

test('swallowNextClick: eats one click, then disarms after the window', async () => {
  const doc = fakeDoc();
  swallowNextClick(doc, 15);
  assert.equal(doc.listeners.length, 1);
  assert.equal(doc.listeners[0].type, 'click');
  assert.deepEqual(doc.listeners[0].opts, { capture: true, once: true });
  let stopped = 0;
  let prevented = 0;
  doc.listeners[0].fn({ stopPropagation: () => stopped++, preventDefault: () => prevented++ });
  assert.deepEqual([stopped, prevented], [1, 1]);
  await tick(30);
  assert.equal(doc.listeners.length, 0); // the timeout removed it
});

test('swallowNextClick: no click follows, the listener is gone after the window', async () => {
  const doc = fakeDoc();
  swallowNextClick(doc, 10);
  assert.equal(doc.listeners.length, 1);
  await tick(25);
  assert.equal(doc.listeners.length, 0);
});

test('blockTouchScroll: non-passive touchmove, prevents only while active', () => {
  let added;
  let removed;
  const target = {
    addEventListener: (type, fn, opts) => (added = { type, fn, opts }),
    removeEventListener: (type, fn) => (removed = { type, fn })
  };
  let active = false;
  const off = blockTouchScroll(target, () => active);
  assert.equal(added.type, 'touchmove');
  assert.deepEqual(added.opts, { passive: false });
  let prevented = 0;
  added.fn({ preventDefault: () => prevented++ });
  assert.equal(prevented, 0);
  active = true;
  added.fn({ preventDefault: () => prevented++ });
  assert.equal(prevented, 1);
  off();
  assert.equal(removed.fn, added.fn);
});

/** A machine over a fake document; records every callback. */
function make(opts = {}) {
  const doc = fakeDoc();
  globalThis.document = doc;
  const calls = { start: [], drop: [], cancel: [], end: 0, hit: [] };
  const m = createLongPressDrag({
    holdMs: 8,
    hitTest: (x, y, d) => {
      calls.hit.push([x, y, d.id]);
      return opts.hit ? opts.hit(x, y, d) : { x, y };
    },
    onStart: (d) => calls.start.push(d.id),
    onDrop: (t, d) => calls.drop.push([t, d.id]),
    onCancel: (d, why) => calls.cancel.push([d.id, why]),
    onEnd: () => calls.end++,
    ...opts.options
  });
  return { m, calls, doc };
}

test('hold past holdMs lifts the item and captures the pointer on the pressed element', async () => {
  const { m, calls } = make();
  const el = fakeEl();
  m.start({ ...ev(50, 60), currentTarget: el }, { id: 'a', label: 'Alpha', kind: 'pulse' });
  assert.equal(m.drag, null);
  assert.equal(m.pressing, true);
  await tick(25);
  assert.deepEqual(m.drag, { id: 'a', label: 'Alpha', kind: 'pulse', x: 50, y: 60, pointerId: 1 });
  assert.deepEqual(el.captured, [1]);
  assert.equal(m.pressing, false);
  assert.deepEqual(calls.start, ['a']);
});

test('a move past the slop before the hold is a scroll: nothing lifts', async () => {
  const { m, calls } = make();
  m.start({ ...ev(50, 60), currentTarget: fakeEl() }, { id: 'a' });
  m.move(ev(50, 75));
  await tick(25);
  assert.equal(m.drag, null);
  assert.deepEqual(calls.start, []);
});

test('a move inside the slop keeps the hold alive; another pointer is ignored', async () => {
  const { m } = make();
  m.start({ ...ev(50, 60), currentTarget: fakeEl() }, { id: 'a' });
  m.move(ev(55, 64));
  m.move(ev(500, 600, { pointerId: 2 }));
  await tick(25);
  assert.equal(m.drag.id, 'a');
});

test('release before the hold: no drag, no callbacks', async () => {
  const { m, calls } = make();
  m.start({ ...ev(1, 1), currentTarget: fakeEl() }, { id: 'a' });
  m.up();
  await tick(25);
  assert.equal(m.drag, null);
  assert.deepEqual(calls.start, []);
  assert.deepEqual(calls.drop, []);
  assert.deepEqual(calls.cancel, []);
});

test('a right mouse button never starts a hold; canStart can veto', async () => {
  const { m, calls } = make({ options: { canStart: (e, info) => info.id !== 'no' } });
  m.start({ ...ev(1, 1, { pointerType: 'mouse', button: 2 }), currentTarget: fakeEl() }, { id: 'a' });
  m.start({ ...ev(1, 1), currentTarget: fakeEl() }, { id: 'no' });
  await tick(25);
  assert.equal(m.drag, null);
  assert.deepEqual(calls.start, []);
  m.start({ ...ev(1, 1, { pointerType: 'mouse', button: 0 }), currentTarget: fakeEl() }, { id: 'a' });
  await tick(25);
  assert.equal(m.drag.id, 'a');
});

test('moves update the ghost position and the hit-test target; drop calls onDrop once', async () => {
  const { m, calls, doc } = make();
  const el = fakeEl();
  m.start({ ...ev(10, 10), currentTarget: el }, { id: 'a' });
  await tick(25);
  m.move(ev(30, 40));
  assert.equal(m.drag.x, 30);
  assert.equal(m.drag.y, 40);
  assert.deepEqual(m.target, { x: 30, y: 40 });
  assert.deepEqual(calls.hit.at(-1), [30, 40, 'a']);
  m.up();
  assert.deepEqual(calls.drop, [[{ x: 30, y: 40 }, 'a']]);
  assert.deepEqual(calls.cancel, []);
  assert.equal(calls.end, 1);
  assert.equal(m.drag, null);
  assert.equal(m.target, null);
  assert.deepEqual(el.released, [1]);
  // the release click is swallowed
  assert.equal(doc.listeners.filter((l) => l.type === 'click').length, 1);
});

test('release over nothing is a cancel, and still swallows the click', async () => {
  const { m, calls, doc } = make({ options: { noTarget: undefined }, hit: () => undefined });
  m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id: 'a' });
  await tick(25);
  m.move(ev(20, 20));
  assert.equal(m.target, undefined);
  m.up();
  assert.deepEqual(calls.drop, []);
  assert.deepEqual(calls.cancel, [['a', 'released']]);
  assert.equal(doc.listeners.length, 1);
});

test('with noTarget undefined, a null target is a real drop ("no workspace")', async () => {
  const { m, calls } = make({ options: { noTarget: undefined }, hit: () => null });
  m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id: 'a' });
  await tick(25);
  m.move(ev(20, 20));
  m.up();
  assert.deepEqual(calls.drop, [[null, 'a']]);
  assert.deepEqual(calls.cancel, []);
});

test('with the default noTarget null, a null hit-test is a cancel', async () => {
  const { m, calls } = make({ hit: () => null });
  m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id: 'a' });
  await tick(25);
  m.move(ev(20, 20));
  m.up();
  assert.deepEqual(calls.drop, []);
  assert.deepEqual(calls.cancel, [['a', 'released']]);
});

test('release without any move after the hold drops nowhere (target is still noTarget)', async () => {
  const { m, calls } = make();
  m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id: 'a' });
  await tick(25);
  m.up();
  assert.deepEqual(calls.drop, []);
  assert.deepEqual(calls.cancel, [['a', 'released']]);
});

test('pointercancel ends the drag with reason, no drop, no click swallow', async () => {
  const { m, calls, doc } = make();
  const el = fakeEl();
  m.start({ ...ev(10, 10), currentTarget: el }, { id: 'a' });
  await tick(25);
  m.move(ev(20, 20));
  m.cancel();
  assert.equal(m.drag, null);
  assert.deepEqual(calls.drop, []);
  assert.deepEqual(calls.cancel, [['a', 'pointercancel']]);
  assert.deepEqual(el.released, [1]);
  assert.equal(doc.listeners.length, 0);
});

test('pointercancel during a pending press just disarms it', async () => {
  const { m, calls } = make();
  m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id: 'a' });
  m.cancel();
  await tick(25);
  assert.equal(m.drag, null);
  assert.deepEqual(calls.start, []);
  assert.deepEqual(calls.cancel, []);
});

test('a second press while one is pending or live is ignored', async () => {
  const { m, calls } = make();
  m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id: 'a' });
  m.start({ ...ev(99, 99, { pointerId: 2 }), currentTarget: fakeEl() }, { id: 'b' });
  await tick(25);
  assert.equal(m.drag.id, 'a');
  m.start({ ...ev(99, 99, { pointerId: 2 }), currentTarget: fakeEl() }, { id: 'b' });
  await tick(25);
  assert.equal(m.drag.id, 'a');
  assert.deepEqual(calls.start, ['a']);
});

test('setPointerCapture throwing does not stop the drag', async () => {
  const { m } = make();
  const el = fakeEl();
  el.setPointerCapture = () => {
    throw new Error('no such pointer');
  };
  m.start({ ...ev(10, 10), currentTarget: el }, { id: 'a' });
  await tick(25);
  assert.equal(m.drag.id, 'a');
  m.up(); // must not try to release a capture it never got
  assert.deepEqual(el.released, []);
});

test('the machine can be used again after a drop', async () => {
  const { m, calls } = make();
  for (const id of ['a', 'b']) {
    m.start({ ...ev(10, 10), currentTarget: fakeEl() }, { id });
    await tick(25);
    m.move(ev(20, 20));
    m.up();
  }
  assert.deepEqual(calls.drop.map((d) => d[1]), ['a', 'b']);
});

test('touchBlockTarget: scroll is blocked only while carrying, listener removed on destroy', async () => {
  const doc = fakeDoc();
  globalThis.document = doc;
  const added = [];
  const removed = [];
  const target = {
    addEventListener: (type, fn, opts) => added.push({ type, fn, opts }),
    removeEventListener: (type, fn) => removed.push({ type, fn })
  };
  let m;
  const destroy = $effectRoot(() => {
    m = createLongPressDrag({ holdMs: 5, hitTest: () => ({}), touchBlockTarget: () => target });
  });
  await tick(); // effects flush in a microtask
  assert.equal(added.length, 1);
  assert.equal(added[0].type, 'touchmove');
  assert.deepEqual(added[0].opts, { passive: false });
  let prevented = 0;
  added[0].fn({ preventDefault: () => prevented++ });
  assert.equal(prevented, 0); // nothing carried
  assert.equal(m.drag, null);
  m.start({ ...ev(1, 1), currentTarget: fakeEl() }, { id: 'a' });
  await tick(20);
  added[0].fn({ preventDefault: () => prevented++ });
  assert.equal(prevented, 1); // carrying
  m.up();
  added[0].fn({ preventDefault: () => prevented++ });
  assert.equal(prevented, 1); // dropped
  destroy();
  assert.equal(removed.length, 1);
  assert.equal(removed[0].fn, added[0].fn);
});

