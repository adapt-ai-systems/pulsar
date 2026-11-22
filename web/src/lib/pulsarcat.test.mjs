import test, { mock } from 'node:test';
import assert from 'node:assert/strict';
import { PulsarCat } from './pulsarcat.js';

test('PulsarCat ignores taps while away and purrs when visible', (t) => {
  mock.timers.enable({ apis: ['setTimeout', 'setInterval'] });
  t.after(() => mock.timers.reset());

  const listeners = new Map();
  const appended = [];
  const fakeCtx = {};
  const fakeCanvas = { style: {}, getContext: () => fakeCtx, remove() {} };
  const origDoc = globalThis.document;
  const origWin = globalThis.window;
  const origRaf = globalThis.requestAnimationFrame;
  const origPerf = globalThis.performance;

  globalThis.document = {
    hidden: true,
    createElement(tag) {
      if (tag === 'canvas') return fakeCanvas;
      return { tag, className: '', textContent: '', style: {}, remove() {} };
    },
    querySelectorAll: () => [],
  };
  globalThis.window = {
    devicePixelRatio: 1,
    addEventListener(type, fn) {
      listeners.set(type, fn);
    },
    removeEventListener(type, fn) {
      if (listeners.get(type) === fn) listeners.delete(type);
    },
  };
  globalThis.requestAnimationFrame = () => 1;
  globalThis.performance = { now: () => 0 };

  const app = {
    clientWidth: 800,
    clientHeight: 600,
    getBoundingClientRect: () => ({ left: 0, top: 0, width: 800, height: 600 }),
    appendChild(el) {
      appended.push(el);
    },
  };

  let cat;
  try {
    cat = PulsarCat(app, { scale: 0.7 });
    const onTap = listeners.get('pointerdown');
    assert.equal(typeof onTap, 'function');
    assert.equal(cat.state.away, true);

    const hitX = cat.state.x;
    const hitY = cat.state.y - 25 * cat.state.S;
    onTap({ clientX: hitX, clientY: hitY });
    mock.timers.tick(600);
    assert.equal(cat.state.purr, 0, 'tapping while away must not trigger purr');
    assert.equal(appended.filter((el) => el.className === 'cat-heart').length, 0);

    cat.state.away = false;
    onTap({ clientX: hitX, clientY: hitY });
    mock.timers.tick(600);
    assert.equal(cat.state.purr, 4, 'tapping while visible triggers purr');
    assert.equal(appended.filter((el) => el.className === 'cat-heart').length, 3);
  } finally {
    cat?.destroy();
    globalThis.document = origDoc;
    globalThis.window = origWin;
    globalThis.requestAnimationFrame = origRaf;
    globalThis.performance = origPerf;
  }
});
