import assert from 'node:assert/strict';
import test from 'node:test';
import { loadRuneModule } from './runes-loader.mjs';

const { pulseToggle, runPulseAction, PENDING_HOLD_MS } = await loadRuneModule('pulsetoggle.svelte.js');

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));
const deferred = () => {
  let resolve;
  const promise = new Promise((r) => (resolve = r));
  return { promise, resolve };
};

/** A toggle over a plain pulse object; `write` is controllable per test. */
function make(opts = {}) {
  const pulse = { id: 'p1', settings: { on: false } };
  const calls = { write: [], changed: 0, errors: [], before: [] };
  const t = pulseToggle({
    getPulse: () => pulse,
    read: (p) => !!p.settings.on,
    write: opts.write ?? (async (id, v) => { calls.write.push([id, v]); return { ok: true }; }),
    label: 'stream mode',
    before: (v) => calls.before.push(v),
    onchanged: () => { calls.changed++; },
    onerror: (msg) => calls.errors.push(msg),
    ...opts.options,
  });
  return { t, pulse, calls };
}

test('hold timer matches the pane rule: 6 s', () => {
  assert.equal(PENDING_HOLD_MS, 6000);
});

test('value reads the pulse until a write is pending', async () => {
  const { t, pulse } = make();
  assert.equal(t.value, false);
  pulse.settings.on = true;
  assert.equal(t.value, true);
});

test('hold on: the saved value wins over a stale pulse frame', async () => {
  const { t, calls } = make();
  await t.set(true);
  assert.deepEqual(calls.write, [['p1', true]]);
  // roster still carries the pre-PATCH value
  assert.equal(t.value, true);
  assert.equal(calls.changed, 1);
  assert.equal(t.saving, false);
});

test('hold on: value is already the new one while the write is in flight', async () => {
  const d = deferred();
  const { t } = make({ write: () => d.promise });
  const p = t.set(true);
  assert.equal(t.value, true);
  assert.equal(t.saving, true);
  d.resolve({ ok: true });
  await p;
  assert.equal(t.saving, false);
});

test('hold off: value follows the pulse, no pending value is kept', async () => {
  const { t, pulse, calls } = make({ options: { hold: false } });
  await t.set(true);
  assert.equal(calls.changed, 1);
  assert.equal(t.value, false); // the pulse prop is the only source
  pulse.settings.on = true;
  assert.equal(t.value, true);
});

test('guard on: a second set while saving is ignored', async () => {
  const d = deferred();
  const writes = [];
  const { t } = make({ write: (id, v) => { writes.push(v); return d.promise; } });
  const first = t.set(true);
  await t.set(false); // dropped
  assert.deepEqual(writes, [true]);
  d.resolve({ ok: true });
  await first;
});

test('guard off: a second set goes through and saving is never raised', async () => {
  const d = deferred();
  const writes = [];
  const { t } = make({ write: (id, v) => { writes.push(v); return d.promise; }, options: { guard: false } });
  const first = t.set(true);
  assert.equal(t.saving, false);
  const second = t.set(false);
  assert.deepEqual(writes, [true, false]);
  d.resolve({ ok: true });
  await Promise.all([first, second]);
});

test('before runs with the value ahead of the write', async () => {
  const order = [];
  const { t, calls } = make({
    write: async () => { order.push('write'); return { ok: true }; },
    options: { before: (v) => order.push(`before:${v}`) },
  });
  await t.set(true);
  assert.deepEqual(order, ['before:true', 'write']);
  assert.equal(calls.changed, 1);
});

test('before is skipped when the guard drops the call', async () => {
  const d = deferred();
  const before = [];
  const { t } = make({ write: () => d.promise, options: { before: (v) => before.push(v) } });
  const first = t.set(true);
  await t.set(false);
  assert.deepEqual(before, [true]);
  d.resolve({ ok: true });
  await first;
});

test('error clears the pending value, reports "<label>: <error>", skips onchanged', async () => {
  const { t, pulse, calls } = make({ write: async () => ({ ok: false, error: 'boom' }) });
  pulse.settings.on = true;
  await t.set(false);
  assert.equal(t.value, true); // back to what the pulse says
  assert.deepEqual(calls.errors, ['stream mode: boom']);
  assert.equal(calls.changed, 0);
  assert.equal(t.saving, false);
});

test('error with hold off still reports and releases the guard', async () => {
  const { t, calls } = make({ write: async () => ({ ok: false, error: 'nope' }), options: { hold: false } });
  await t.set(true);
  assert.deepEqual(calls.errors, ['stream mode: nope']);
  assert.equal(t.saving, false);
  await t.set(true); // guard released: this goes through
  assert.equal(calls.errors.length, 2);
});

test('timeout clears only its own value: an older timer leaves a newer one held', async (ctx) => {
  ctx.mock.timers.enable({ apis: ['setTimeout'] });
  const { t, pulse } = make();
  await t.set(true);
  ctx.mock.timers.tick(PENDING_HOLD_MS - 1000);
  await t.set(false); // pending false; its own timer fires 1 s after the first's
  ctx.mock.timers.tick(1000); // first timer: pending (false) !== true -> untouched
  pulse.settings.on = true; // a stale frame
  assert.equal(t.value, false); // still held
  ctx.mock.timers.tick(PENDING_HOLD_MS); // its own timer releases
  assert.equal(t.value, true);
});

test('the pulse is read at call time, so a new pulse object is used', async () => {
  const ids = [];
  let pulse = { id: 'a', settings: { on: false } };
  const t = pulseToggle({
    getPulse: () => pulse,
    read: (p) => !!p.settings.on,
    write: async (id) => { ids.push(id); return { ok: true }; },
    label: 'x',
  });
  await t.set(true);
  pulse = { id: 'b', settings: { on: false } };
  await t.set(false);
  assert.deepEqual(ids, ['a', 'b']);
});

test('runPulseAction: ok runs onok once with the result and returns it', async () => {
  const seen = [];
  const r = await runPulseAction('pause', async () => ({ ok: true, value: 7 }), {
    onok: (res) => seen.push(['ok', res.value]),
    onerr: (m) => seen.push(['err', m]),
  });
  assert.deepEqual(seen, [['ok', 7]]);
  assert.equal(r.ok, true);
});

test('runPulseAction: failure passes "<label>: <error>" and the result to onerr', async () => {
  const seen = [];
  const r = await runPulseAction('pause', async () => ({ ok: false, error: 'busy' }), {
    onok: () => seen.push('ok'),
    onerr: (msg, res) => seen.push([msg, res.error]),
  });
  assert.deepEqual(seen, [['pause: busy', 'busy']]);
  assert.equal(r.ok, false);
});

test('runPulseAction: awaits an async onok before returning', async () => {
  let done = false;
  await runPulseAction('x', async () => ({ ok: true }), {
    onok: async () => { await tick(5); done = true; },
  });
  assert.equal(done, true);
});

test('runPulseAction: callbacks are optional', async () => {
  assert.equal((await runPulseAction('x', async () => ({ ok: true }))).ok, true);
  assert.equal((await runPulseAction('x', async () => ({ ok: false, error: 'e' }))).ok, false);
});
