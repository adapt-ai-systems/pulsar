import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { revealPlan } from './reveal.js';
import { fmtMailTime } from './time.js';

const source = (path) => readFileSync(new URL(path, import.meta.url), 'utf8');

test('mail includes local time for today, earlier dates, and earlier years', () => {
  const body = source('./Inbox.svelte').match(/const relTime = \(ts\) => (.*);/)[1];
  assert.equal(body, 'fmtMailTime(ts, now)', 'Inbox formats through time.js in the viewer zone');
  const zone = 'America/Los_Angeles';
  const now = Date.parse('2026-09-05T22:00:00Z'); // 15:00 on Sep 5 in the zone
  const cases = [
    ['2026-09-05T14:40:00Z', /^07:40$/],
    ['2026-09-04T14:40:00Z', /^Sep 4, 07:40$/],
    ['2025-09-04T14:40:00Z', /^Sep 4, 2025, 07:40$/],
  ];
  for (const [ts, want] of cases) assert.match(fmtMailTime(ts, now, { zone, locale: 'en-US' }), want);
  assert.equal(fmtMailTime('invalid', now, { zone }), 'invalid');
});

test('new pulse is available and visible before focus, even with no-op live refresh', () => {
  const created = { id: 'new', grid_id: 'other', state: 'stopped' };
  const plan = revealPlan(created, {
    from: 'new-pulse',
    gridId: created.grid_id,
    hideStopped: true,
    selectedGrid: 'work',
    visibleFamily: new Set(['work']),
  });
  assert.equal(plan.untickHideStopped, true);
  assert.equal(plan.clearGrid, true);
  assert.equal(plan.addOpened, false);
});

test('mobile focus waits for a delayed roster instead of discarding the request', () => {
  const text = source('./MobileDeck.svelte');
  const body = text.match(/\$effect\(\(\) => \{\n    (if \(!ready \|\| !focusId\) return;[\s\S]*?)\n  \}\);/)[1];
  let focused = 0;
  let slot = null;
  const ctx = {
    pulses: [{ id: 'new' }], ready: false, focusId: 'new', console: { debug() {} },
    focusSlot(i) { slot = i; },
    onfocused() { focused++; },
  };
  vm.createContext(ctx);
  vm.runInContext(`(() => {${body}
})()`, ctx);
  assert.equal(focused, 0); // even a matching partial roster must wait
  ctx.ready = true;
  ctx.pulses = [];
  vm.runInContext(`(() => {${body}
})()`, ctx);
  assert.equal(focused, 0); // a missing target also stays pending
  ctx.pulses = [{ id: 'old' }, { id: 'new' }];
  vm.runInContext(`(() => {${body}
})()`, ctx);
  assert.equal(slot, 1);
  assert.equal(focused, 1);
});
