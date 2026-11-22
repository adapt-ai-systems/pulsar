import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createChatScroll } from './chatscroll.js';
function harness() {
  const listeners = new Map(), frames = new Map();
  let top = 600, following = true, catches = 0, resize, serial = 0;
  const node = {
    scrollHeight: 1000, clientHeight: 400,
    get scrollTop() { return top; },
    set scrollTop(v) { top = Math.max(0, Math.min(v, this.scrollHeight - this.clientHeight)); },
    addEventListener: (t, f) => listeners.set(t, f),
    removeEventListener: (t) => listeners.delete(t),
  };
  const controller = createChatScroll(node, {}, {
    follows: () => following, setFollowing: (v) => { following = v; },
    catchUp: () => { catches++; following = true; controller.anchor(); },
    requestFrame: (f) => { frames.set(++serial, f); return serial; },
    cancelFrame: (id) => frames.delete(id),
    Observer: class { constructor(f) { resize = f; } observe() {} disconnect() {} },
    viewport: null,
  });
  return { node, controller, resize: () => resize(), listeners,
    event: (t, e = {}) => listeners.get(t)?.({ target: node, ...e }),
    flush() { const batch = [...frames.values()]; frames.clear(); batch.forEach((f) => f()); },
    follows: () => following, catches: () => catches,
  };
}
test('stream growth and layout scroll cannot turn off following', () => {
  const h = harness(); h.node.scrollHeight += 500; h.event('scroll');
  assert.equal(h.follows(), true); h.resize(); h.flush();
  assert.equal(h.node.scrollTop, 1100);
});
test('ghost shrink then durable reply growth stays at the tail', () => {
  const h = harness(); h.node.scrollHeight = 500; h.node.scrollTop = 600;
  h.event('scroll'); h.resize(); h.flush(); h.node.scrollHeight = 1600;
  h.event('scroll'); h.resize(); h.flush(); h.flush();
  assert.equal(h.follows(), true); assert.equal(h.node.scrollTop, 1200);
});
test('bar/keyboard resize and late content growth re-pin', () => {
  const h = harness(); h.node.clientHeight = 200; h.resize(); h.flush();
  assert.equal(h.node.scrollTop, 800);
  h.node.scrollHeight += 900; h.resize(); h.flush(); assert.equal(h.node.scrollTop, 1700);
});
test('wheel-up cancels pending pins and keeps history still during growth', () => {
  const h = harness(); h.controller.anchor(); h.event('wheel', { deltaY: -120 });
  h.node.scrollTop = 350; h.flush(); h.flush();
  assert.equal(h.follows(), false); assert.equal(h.node.scrollTop, 350);
  h.node.scrollHeight += 500; h.resize(); h.flush(); assert.equal(h.node.scrollTop, 350);
});
test('touch, keyboard and scrollbar input release pending pins', () => {
  for (const input of ['touch', 'key', 'scrollbar']) {
    const h = harness(); h.controller.anchor();
    if (input === 'touch') {
      h.event('touchstart', { touches: [{ clientY: 100 }] });
      h.event('touchmove', { touches: [{ clientY: 140 }] });
    } else if (input === 'key') h.event('keydown', { key: 'PageUp', target: { closest: () => null } });
    else h.event('pointerdown', { pointerType: 'mouse' });
    h.flush(); assert.equal(h.follows(), false, input);
  }
});
test('scrolling down to bottom catches up and resumes streaming', () => {
  const h = harness(); h.controller.release(); h.node.scrollTop = 200; h.event('scroll');
  h.node.scrollTop = 580; h.event('scroll'); h.flush();
  assert.equal(h.catches(), 1); assert.equal(h.follows(), true); assert.equal(h.node.scrollTop, 600);
});
test('unchanged position near bottom does not undo an upward gesture', () => {
  const h = harness(); h.event('wheel', { deltaY: -10 }); h.event('scroll'); assert.equal(h.follows(), false);
});
test('new anchors supersede stale history restores', () => {
  const h = harness(); h.controller.anchor(800, false); h.controller.anchor(); h.flush(); h.flush();
  assert.equal(h.node.scrollTop, 600);
});
test('history restore keeps bottom-relative position without following', () => {
  const h = harness(); h.controller.release(); h.node.scrollHeight = 1800;
  h.controller.anchor(800, false); h.flush(); assert.equal(h.node.scrollTop, 1000); assert.equal(h.follows(), false);
});
// Regression test for duplicate-looking replies at the turn boundary. A
// following pane must stop re-pinning after it settles, so late reflow of the
// row that just rendered cannot scroll the finished reply away and leave only
// the streaming ghost (the
// same text) on screen.
test('pin loop stops after settling; late reflow does not drag the view down', () => {
  const h = harness();
  h.controller.anchor();
  h.flush();
  assert.equal(h.node.scrollTop, 600);
  // The loop must keep scheduling for a while (a real flush needs frames)...
  h.flush(); h.flush();
  assert.equal(h.node.scrollTop, 600);
  // ...and must be BOUNDED: run frames past the cap, then grow content with no
  // new anchor/resize. An endless loop would follow it; a settled one will not.
  for (let i = 0; i < 200; i++) h.flush();
  h.node.scrollHeight += 2000;
  h.flush();
  assert.equal(h.node.scrollTop, 600, 'settled pin must not chase late reflow');
  // A genuine resize still re-arms a settle (existing behaviour).
  h.resize();
  h.flush();
  assert.equal(h.node.scrollTop, 2600);
});
test('destroy removes listeners and cancels verify frame', () => {
  const h = harness(); h.controller.anchor(); h.flush(); h.controller.destroy();
  h.node.scrollHeight += 200; h.flush(); assert.equal(h.node.scrollTop, 600); assert.equal(h.listeners.size, 0);
});
