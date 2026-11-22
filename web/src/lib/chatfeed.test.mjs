// The chat feed controller (chatfeed.svelte.js) driven by a fake /live socket,
// fake fetchers and fake timers. This is the gate for the live path: the browser
// scenario (tests/browser/chat_feed.py) disables the WebSocket, so only this file
// exercises pushes, deltas and the timer stand-down.
import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { loadRuneModule } from './runes-loader.mjs';

// Plain node resolves 'svelte' to its server build, where untrack() is a no-op and
// the runtime is a different instance from the one the compiled code uses. The feed's
// effects depend on a working untrack, so point it at the client runtime.
const { createChatFeed } = await loadRuneModule('chatfeed.svelte.js', {
  rewrite: (code) => code.replace(/from (["'])svelte\1/g, 'from "svelte/internal/client"')
});

// `$effect.root` and `$state` are runes, so the helpers are compiled like a module.
const rootDir = mkdtempSync(join(tmpdir(), 'chatfeed-test-'));
writeFileSync(
  join(rootDir, 'root.svelte.js'),
  `export const effectRoot = (fn) => $effect.root(fn);
export function derived(fn) { const d = $derived.by(fn); return () => d; }
export function box(init) { let v = $state(init); return { get v() { return v; }, set v(x) { v = x; } }; }\n`
);
const { effectRoot, box, derived } = await loadRuneModule(join(rootDir, 'root.svelte.js'));

const tick = (ms = 0) => new Promise((r) => setTimeout(r, ms));

const user = (i, text) => ({ type: 'user_message', ts: `2026-10-01T10:00:0${i}Z`, text });
const reply = (i, text) => ({ type: 'assistant_text', ts: `2026-10-01T10:00:0${i}Z`, text });
const done = (i) => ({ type: 'done', ts: `2026-10-01T10:00:0${i}Z` });
const replies = (feed) => feed.items.filter((it) => it.kind === 'reply').map((it) => it.text);

/** A fake setInterval: records every timer, runs them only when told to. */
function fakeTimers() {
  const timers = [];
  const real = { setInterval: globalThis.setInterval, clearInterval: globalThis.clearInterval };
  globalThis.setInterval = (fn, ms) => { timers.push({ fn, ms, live: true }); return timers.length - 1; };
  globalThis.clearInterval = (id) => { if (timers[id]) timers[id].live = false; };
  return {
    timers,
    active: (ms) => timers.filter((t) => t.live && t.ms === ms),
    restore() { Object.assign(globalThis, real); }
  };
}

/** A fake /live: records subscriptions; `connected` is reactive. */
function fakeLive() {
  const connected = box(false);
  const subs = [];
  const tails = [];
  return {
    subs,
    tails,
    set connected(v) { connected.v = v; },
    get connected() { return connected.v; },
    watch(id, tail, cb, partialCb, reasoningCb) {
      const sub = { id, tail, cb, partialCb, reasoningCb, off: false };
      subs.push(sub);
      return () => { sub.off = true; };
    },
    setTail: (id, tail) => tails.push([id, tail]),
    get active() { return subs.filter((s) => !s.off); }
  };
}

/** Build a feed in an effect root. `opts` overrides the starting state. */
function setup(opts = {}) {
  const timers = fakeTimers();
  const live = fakeLive();
  const pulse = box({ id: 'p1', state: 'running', ...(opts.pulse ?? {}) });
  const streamOn = box(opts.streamOn ?? true);
  const reasoningOn = box(opts.reasoningOn ?? false);
  const foldOn = box(false);
  const stuck = box(true);
  const log = { fetches: [], taskFetches: [], partial: [], reasoning: [], anchors: [], errors: [], applied: [], resets: 0 };
  const served = { events: [], offset: 0, unchanged: false, partial: null, reasoning: null, fail: null };
  const deps = {
    live,
    getEvents: async (id, tail, after) => {
      log.fetches.push({ id, tail, after });
      if (served.fail) return { ok: false, error: served.fail };
      return { ok: true, value: { events: served.events, offset: served.offset, unchanged: served.unchanged } };
    },
    getPartial: async (id) => { log.partial.push(id); return { ok: true, value: { partial: served.partial } }; },
    getReasoning: async (id) => { log.reasoning.push(id); return { ok: true, value: { reasoning: served.reasoning } }; },
    getTaskEvents: async (id) => { log.taskFetches.push(id); return { ok: true, value: { events: [] } }; },
    getPulse: () => pulse.v,
    // $derived like the component's: a new pulse object with the same value does not notify.
    getPulseId: null,
    isStopped: null,
    isStreamOn: () => streamOn.v,
    isReasoningEnabled: () => reasoningOn.v,
    getFoldNoise: () => foldOn.v,
    getChatEl: () => null,
    getStuck: () => stuck.v,
    setStuck: (v) => { stuck.v = v; },
    anchor: (keep, toBottom) => log.anchors.push([keep, toBottom]),
    setError: (m) => log.errors.push(m),
    onEvents: (evs) => log.applied.push(evs.length),
    onReset: () => { log.resets++; }
  };
  let feed;
  const destroy = effectRoot(() => {
    deps.getPulseId = derived(() => pulse.v.id);
    deps.isStopped = derived(() => pulse.v.state === 'stopped');
    feed = createChatFeed(deps);
  });
  return {
    feed, live, timers, log, served, destroy, pulse, streamOn, reasoningOn, foldOn, stuck,
    // The $derived-style getters the component passes read these boxes.
    setPulse: (patch) => { pulse.v = { ...pulse.v, ...patch }; },
    end() { destroy(); timers.restore(); }
  };
}

test('first snapshot replaces the window; delta:true appends to it', async () => {
  const t = setup();
  await tick();
  const sub = t.live.active[0];
  assert.equal(sub.id, 'p1');
  assert.equal(sub.tail, 100);
  sub.cb({ events: [user(1, 'hi'), reply(2, 'one'), done(3)], offset: 3 });
  assert.deepEqual(replies(t.feed), ['one']);
  // A second snapshot replaces, it does not append.
  sub.cb({ events: [user(1, 'hi'), reply(2, 'one'), done(3), user(4, 'again'), reply(5, 'two'), done(6)], offset: 6 });
  assert.deepEqual(replies(t.feed), ['one', 'two']);
  // A delta appends.
  sub.cb({ events: [user(7, 'more'), reply(8, 'three'), done(9)], offset: 9, delta: true });
  assert.deepEqual(replies(t.feed), ['one', 'two', 'three']);
  // A delta that repeats an event the window already holds does not duplicate it.
  sub.cb({ events: [reply(8, 'three'), done(9)], offset: 9, delta: true });
  assert.deepEqual(replies(t.feed), ['one', 'two', 'three']);
  // An empty delta is dropped without a render or an onEvents call.
  const applied = t.log.applied.length;
  sub.cb({ events: [], offset: 9, delta: true });
  assert.equal(t.log.applied.length, applied);
  assert.deepEqual(t.log.errors.filter(Boolean), []);
  t.end();
});

test('the 2s timer poll stands down only after a push has landed', async () => {
  const t = setup();
  t.live.connected = true;
  await tick();
  assert.equal(t.log.fetches.length, 1, 'the first poll runs at mount');
  const [timer] = t.timers.active(2000);
  assert.ok(timer, 'the 2s timer is armed');
  // Socket up but no push yet: the timer still fetches.
  await timer.fn();
  assert.equal(t.log.fetches.length, 2);
  // After a push the timer is a no-op ...
  t.live.active[0].cb({ events: [user(1, 'hi')], offset: 1 });
  await timer.fn();
  assert.equal(t.log.fetches.length, 2);
  // ... but a follow poll (just sent) and a scrollback poll still fetch.
  t.served.offset = 5;
  await t.feed.poll(false, true);
  assert.equal(t.log.fetches.length, 3);
  assert.equal(t.log.fetches[2].after, null, 'follow asks for a full tail');
  await t.feed.poll(true);
  assert.equal(t.log.fetches.length, 4);
  // Socket down again: the timer fetches with the push cursor.
  t.live.connected = false;
  await timer.fn();
  assert.equal(t.log.fetches.length, 5);
  assert.equal(t.log.fetches[4].after, 5);
  t.end();
});

test('an unchanged answer keeps the cursor and does not re-render', async () => {
  const t = setup();
  t.served.events = [user(1, 'hi'), reply(2, 'one'), done(3)];
  t.served.offset = 3;
  await tick();
  assert.deepEqual(replies(t.feed), ['one']);
  const items = t.feed.items;
  t.served.unchanged = true;
  t.served.events = [];
  await t.timers.active(2000)[0].fn();
  assert.equal(t.log.fetches.at(-1).after, 3);
  assert.equal(t.feed.items, items);
  t.end();
});

test('disconnect starts the HTTP partial poll, reconnect stops it', async () => {
  const t = setup();
  await tick();
  assert.equal(t.timers.active(500).length, 1, 'socket down: 500ms fallback armed');
  t.live.connected = true;
  await tick();
  assert.equal(t.timers.active(500).length, 0, 'socket up: fallback cleared');
  t.live.connected = false;
  await tick();
  assert.equal(t.timers.active(500).length, 1, 'socket dropped: fallback armed again');
  // The fallback feeds the ghost through the same applyPartial.
  t.served.partial = { text: 'streaming' };
  await t.timers.active(500)[0].fn();
  await tick();
  assert.equal(t.feed.ghostText, 'streaming');
  assert.deepEqual(t.log.partial, ['p1']);
  // The pulse's live subscription was NOT restarted by the socket bounce.
  assert.equal(t.live.subs.length, 1);
  t.end();
});

test('while /live is connected the HTTP partial poll does nothing even if it fires', async () => {
  const t = setup();
  await tick();
  const [pp] = t.timers.active(500);
  t.live.connected = true;
  await pp.fn();
  assert.deepEqual(t.log.partial, []);
  t.end();
});

test('reply ghost: partial shows it, null-partial pulls events, the final block retires it', async () => {
  const t = setup();
  await tick();
  const sub = t.live.active[0];
  sub.cb({ events: [user(1, 'hi')], offset: 1 });
  sub.partialCb({ text: 'hel' });
  assert.equal(t.feed.ghostText, 'hel');
  sub.partialCb({ text: 'hello there' });
  assert.equal(t.feed.ghostText, 'hello there');
  // The runner clears the partial before the final event lands: pull events, ghost stays until they render.
  const before = t.log.fetches.length;
  sub.partialCb({ text: '' });
  assert.equal(t.log.fetches.length, before + 1, 'null partial pulls events');
  assert.equal(t.feed.ghostText, 'hello there', 'no blank gap while the pull is in flight');
  // The final block arrives: the ghost goes with it, and the reply shows once.
  sub.cb({ events: [user(1, 'hi'), reply(2, 'hello there'), done(3)], offset: 3 });
  assert.equal(t.feed.ghostText, null);
  assert.deepEqual(replies(t.feed), ['hello there']);
  t.end();
});

test('reply ghost: a window that already carries the durable copy retires the ghost (ordering race)', async () => {
  const t = setup();
  await tick();
  const sub = t.live.active[0];
  sub.cb({ events: [user(1, 'hi')], offset: 1 });
  sub.partialCb({ text: 'final words' });
  // events push BEFORE the null partial; the net retires on content, not on the flag.
  sub.cb({ events: [user(1, 'hi'), reply(2, 'final words')], offset: 2 });
  assert.equal(t.feed.ghostText, null);
  assert.deepEqual(replies(t.feed), ['final words']);
  t.end();
});

test('reasoning ghost: shows the open block, hands over to its card', async () => {
  const t = setup({ reasoningOn: true });
  await tick();
  const sub = t.live.active[0];
  sub.cb({ events: [user(1, 'hi')], offset: 1 });
  sub.reasoningCb({ block: 'thinking a', text: 'whole trace' });
  assert.equal(t.feed.ghostReasoning, 'thinking a', 'the open block, not the whole trace');
  sub.reasoningCb({ text: 'older runner' });
  assert.equal(t.feed.ghostReasoning, 'older runner', 'no `block` field: the whole text shows');
  sub.reasoningCb({ block: 'thinking a b' });
  const before = t.log.fetches.length;
  sub.reasoningCb({ block: '' });
  assert.equal(t.log.fetches.length, before + 1, 'block ended: events are pulled');
  assert.equal(t.feed.ghostReasoning, 'thinking a b');
  // The durable reasoning event arrives; the card replaces the ghost.
  sub.cb({ events: [user(1, 'hi'), { type: 'reasoning', ts: '2026-10-01T10:00:02Z', text: 'thinking a b' }], offset: 2 });
  assert.equal(t.feed.ghostReasoning, null);
  t.end();
});

test('reasoning ghost stays off when the pulse has reasoning traces off', async () => {
  const t = setup({ reasoningOn: false });
  await tick();
  t.live.active[0].reasoningCb({ block: 'x' });
  assert.equal(t.feed.ghostReasoning, null);
  t.end();
});

test('stream toggle: off shows no ghost and arms no HTTP poll; on arms it; toggling resets the ghost', async () => {
  const t = setup({ streamOn: false });
  await tick();
  t.live.active[0].partialCb({ text: 'nope' });
  assert.equal(t.feed.ghostText, null);
  assert.equal(t.timers.active(500).length, 0, 'stream off: no fallback poll');
  t.streamOn.v = true;
  await tick();
  assert.equal(t.timers.active(500).length, 1, 'stream on: fallback armed');
  // The effect restarted: the live subscription was dropped and re-made.
  assert.equal(t.live.subs.length, 2);
  assert.equal(t.live.subs[0].off, true);
  t.live.active[0].partialCb({ text: 'yes' });
  assert.equal(t.feed.ghostText, 'yes');
  t.streamOn.v = false;
  await tick();
  assert.equal(t.feed.ghostText, null, 'turning streaming off drops the ghost');
  assert.equal(t.timers.active(500).length, 0);
  t.end();
});

test('stopped pulse: one fetch, no timers, no subscription; running again brings them back', async () => {
  const t = setup({ pulse: { state: 'stopped' } });
  await tick();
  assert.equal(t.log.fetches.length, 1);
  assert.equal(t.timers.active(2000).length, 0);
  assert.equal(t.timers.active(500).length, 0);
  assert.equal(t.live.subs.length, 0);
  t.setPulse({ state: 'running' });
  await tick();
  assert.equal(t.timers.active(2000).length, 1);
  assert.equal(t.timers.active(500).length, 1);
  assert.equal(t.live.active.length, 1);
  assert.equal(t.log.fetches.length, 2, 'the flip refetches');
  t.setPulse({ state: 'stopped' });
  await tick();
  assert.equal(t.timers.active(2000).length, 0);
  assert.equal(t.timers.active(500).length, 0);
  assert.equal(t.live.active.length, 0);
  t.end();
});

test('a new pulse object with the same id and state does not restart anything (3s roster churn)', async () => {
  const t = setup();
  await tick();
  t.setPulse({ title: 'renamed' });
  t.setPulse({ title: 'renamed again' });
  await tick();
  assert.equal(t.live.subs.length, 1);
  assert.equal(t.timers.timers.filter((x) => x.ms === 2000).length, 1);
  assert.equal(t.log.fetches.length, 1);
  t.end();
});

test('pulse id change drops the cursor, the ghosts and the loaded window', async () => {
  const t = setup();
  t.served.events = [user(1, 'hi'), reply(2, 'one'), done(3)];
  t.served.offset = 3;
  await tick();
  const sub = t.live.active[0];
  sub.partialCb({ text: 'half a repl' });
  assert.equal(t.feed.ghostText, 'half a repl');
  assert.equal(t.log.resets, 1);
  // A timer poll on the old pulse sends the byte cursor.
  await t.timers.active(2000)[0].fn();
  assert.equal(t.log.fetches.at(-1).after, 3);
  // Switch pulse: the first fetch for the new id asks for a full tail.
  t.setPulse({ id: 'p2' });
  await tick();
  assert.equal(t.log.resets, 2);
  assert.equal(t.feed.ghostText, null);
  assert.deepEqual(t.log.fetches.at(-1), { id: 'p2', tail: 100, after: null });
  assert.equal(sub.off, true, 'the old subscription is gone');
  assert.equal(t.live.active[0].id, 'p2');
  // The new pulse's first push is a snapshot, not merged into the old window.
  t.live.active[0].cb({ events: [user(1, 'other'), reply(2, 'x'), done(4)], offset: 4 });
  assert.deepEqual(replies(t.feed), ['x']);
  t.end();
});

test('a stale delta from the old window cannot collapse the transcript after a pulse flip', async () => {
  // The byte cursor must reset with the window (report 2026-09-14): the poll that
  // follows a flip must not send the old offset.
  const t = setup();
  t.served.offset = 40;
  t.served.events = [user(1, 'hi'), reply(2, 'one'), done(3)];
  await tick();
  t.setPulse({ id: 'p2' });
  await tick();
  assert.equal(t.log.fetches.at(-1).after, null);
  t.end();
});

test('events held while reading back: no render, heldNew lights, catchUp renders them', async () => {
  const t = setup();
  await tick();
  const sub = t.live.active[0];
  sub.cb({ events: [user(1, 'hi'), reply(2, 'one'), done(3)], offset: 3 });
  assert.deepEqual(replies(t.feed), ['one']);
  t.stuck.v = false; // the reader scrolled up
  const shown = t.feed.items;
  sub.cb({ events: [user(1, 'hi'), reply(2, 'one'), done(3), user(4, 'q'), reply(5, 'two'), done(6)], offset: 6 });
  assert.equal(t.feed.items, shown, 'the DOM-facing items are left alone');
  assert.equal(t.feed.heldNew, true);
  t.feed.catchUp();
  assert.deepEqual(replies(t.feed), ['one', 'two']);
  assert.equal(t.feed.heldNew, false);
  assert.equal(t.stuck.v, true);
  assert.deepEqual(t.log.anchors.at(-1), [0, true]);
  t.end();
});

test('widenTo grows the window, widens the pushed tail and refetches a full tail', async () => {
  const t = setup();
  await tick();
  await t.feed.widenTo(2000);
  assert.equal(t.feed.tailLimit, 2000);
  assert.deepEqual(t.live.tails.at(-1), ['p1', 2000]);
  assert.deepEqual(t.log.fetches.at(-1), { id: 'p1', tail: 2000, after: null });
  await t.feed.widenTo(500);
  assert.equal(t.feed.tailLimit, 2000, 'never shrinks');
  t.end();
});

test('growWindow needs a scroll container at the top, not following, with more above', async () => {
  const chatEl = { scrollTop: 0, scrollHeight: 1000 };
  const timers = fakeTimers();
  const live = fakeLive();
  const stuck = box(false);
  const fetches = [];
  let el = chatEl;
  let feed;
  const destroy = effectRoot(() => {
    feed = createChatFeed({
      live,
      getEvents: async (id, tail, after) => { fetches.push({ tail, after }); return { ok: true, value: { events: Array.from({ length: 100 }, (_, i) => user(i % 10, `m${i}`)), offset: 1 } }; },
      getPartial: async () => ({ ok: true, value: {} }),
      getReasoning: async () => ({ ok: true, value: {} }),
      getTaskEvents: async () => ({ ok: true, value: { events: [] } }),
      getPulse: () => ({ id: 'p1', state: 'running' }),
      getPulseId: () => 'p1',
      isStopped: () => false,
      isStreamOn: () => false,
      isReasoningEnabled: () => false,
      getFoldNoise: () => false,
      getChatEl: () => el,
      getStuck: () => stuck.v,
      setStuck: (v) => { stuck.v = v; },
      anchor: () => {},
      setError: () => {}
    });
  });
  await tick();
  assert.equal(fetches.length, 1);
  // 100 events fill the 100-event window: more history above.
  feed.growWindow();
  await tick();
  assert.equal(feed.tailLimit, 500);
  assert.deepEqual(live.tails.at(-1), ['p1', 500]);
  assert.deepEqual(fetches.at(-1), { tail: 500, after: null });
  // Following the tail: nothing grows.
  stuck.v = true;
  el = { scrollTop: 0, scrollHeight: 1000 };
  feed.growWindow();
  assert.equal(feed.tailLimit, 500);
  // Not at the top: nothing grows.
  stuck.v = false;
  el = { scrollTop: 500, scrollHeight: 1000 };
  feed.growWindow();
  assert.equal(feed.tailLimit, 500);
  destroy();
  timers.restore();
});

test('cleanup removes the subscription and both timers', async () => {
  const t = setup();
  await tick();
  assert.equal(t.live.active.length, 1);
  assert.equal(t.timers.active(2000).length, 1);
  assert.equal(t.timers.active(500).length, 1);
  t.destroy();
  assert.equal(t.live.active.length, 0);
  assert.equal(t.timers.active(2000).length, 0);
  assert.equal(t.timers.active(500).length, 0);
  t.timers.restore();
});

test('fold-mode flip re-renders the loaded window in place, anchored from the bottom', async () => {
  const t = setup();
  await tick();
  t.live.active[0].cb({ events: [user(1, 'hi'), reply(2, 'one'), done(3)], offset: 3 });
  const anchors = t.log.anchors.length;
  const items = t.feed.items;
  t.foldOn.v = true;
  await tick();
  assert.notEqual(t.feed.items, items, 'the window was folded again');
  assert.equal(t.log.anchors.length, anchors + 1);
  assert.deepEqual(t.log.anchors.at(-1), [0, true]);
  // Held events: the flip lights the arrow and leaves the DOM alone.
  t.stuck.v = false;
  t.live.active[0].cb({ events: [user(1, 'hi'), reply(2, 'one'), done(3), user(4, 'q')], offset: 4 });
  const shown = t.feed.items;
  t.foldOn.v = false;
  await tick();
  assert.equal(t.feed.items, shown);
  assert.equal(t.feed.heldNew, true);
  t.end();
});

test('an events error reaches the error strip, a later good window clears it', async () => {
  const t = setup();
  t.served.fail = 'boom';
  await tick();
  assert.deepEqual(t.log.errors, ['events: boom']);
  t.live.active[0].cb({ events: [user(1, 'hi')], offset: 1 });
  assert.equal(t.log.errors.at(-1), '');
  t.end();
});

// Speed sweep 2026-11-02: with the socket down, every running pane polled
// its three stream files every 500ms, idle or not.
test('socket down: a busy pulse polls every tick, a quiet one gets one round', async () => {
  const t = setup({ pulse: { live: { socket_state: 'busy' } } });
  await tick();
  const [pp] = t.timers.active(500);
  await pp.fn();
  await pp.fn();
  assert.equal(t.log.partial.length, 2, 'busy: every tick polls');
  t.setPulse({ live: { socket_state: 'idle', heartbeat: { state: 'idle' } } });
  await tick();
  assert.deepEqual(t.timers.active(500), [pp], 'a new pulse prop does not restart the timer');
  await pp.fn();
  assert.equal(t.log.partial.length, 3, 'the busy-to-idle edge gets one round');
  await pp.fn();
  await pp.fn();
  assert.equal(t.log.partial.length, 3, 'quiet: no more polls');
  t.setPulse({ live: { socket_state: 'idle', heartbeat: { state: 'busy' } } });
  await pp.fn();
  assert.equal(t.log.partial.length, 4, 'either busy signal resumes the poll');
  t.end();
});
