import test from 'node:test';
import assert from 'node:assert/strict';
import { parseSlashCommand } from './loopcmd.js';
import { runSlashCommand, fmtLead } from './slashrun.js';

const pulse = { id: 'p1', title: 'Alpha' };
const NOW = new Date('2026-10-13T12:00:00.000Z');

function rig({ schedules = [], listOk = true, createOk = true, deleteOk = true, renameOk = true } = {}) {
  const calls = [];
  const api = {
    listSchedules: async (id) => { calls.push(['list', id]); return listOk ? { ok: true, value: schedules } : { ok: false, error: 'boom' }; },
    createSchedule: async (id, body) => { calls.push(['create', id, body]); return createOk ? { ok: true, value: { id: 'new1' } } : { ok: false, error: 'nope' }; },
    deleteSchedule: async (id) => { calls.push(['delete', id]); return deleteOk ? { ok: true } : { ok: false, error: 'nodel' }; },
    renamePulse: async (id, title) => { calls.push(['rename', id, title]); return renameOk ? { ok: true } : { ok: false, error: 'norename' }; },
  };
  const side = { refresh: 0, changed: 0 };
  const deps = { pulse, api, refreshSchedules: () => side.refresh++, onchanged: () => side.changed++, now: () => NOW };
  return { calls, side, run: (text) => runSlashCommand(parseSlashCommand(text), deps) };
}

const loop = (id, prompt, extra = {}) => ({ id, kind: 'loop', prompt, interval_minutes: 5, enabled: true, ...extra });

test('unknown command and usage only speak, never clear the draft', async () => {
  const { run, calls } = rig();
  const u = await run('/wat');
  assert.match(u.notice, /^unknown command \/wat — try \/loop, \/at, \/rename/);
  assert.equal(u.clearDraft, false);
  const us = await run('/loop');
  assert.match(us.notice, /^usage: \/loop <minutes> <prompt>/);
  assert.equal(us.clearDraft, false);
  assert.deepEqual(calls, []);
});

test('/loop list: formats loops, says so when there are none, clears the draft', async () => {
  const r1 = await rig({ schedules: [loop('a1', 'do x'), loop('b2', 'do y', { enabled: false }), { id: 'o', kind: 'once', prompt: 'z' }] }).run('/loop list');
  assert.equal(r1.notice, 'a1 — every 5m — "do x"\nb2 — every 5m (disabled) — "do y"');
  assert.equal(r1.clearDraft, true);
  const r2 = await rig().run('/loop list');
  assert.equal(r2.notice, 'no loops on this pulse');
  const r3 = await rig({ listOk: false }).run('/loop list');
  assert.deepEqual(r3, { error: 'loop list: boom', clearDraft: false });
});

test('/loop stop: one loop, id prefix, ambiguity, none, failure', async () => {
  const one = rig({ schedules: [loop('a1', 'do x')] });
  const r = await one.run('/loop stop');
  assert.equal(r.notice, 'loop stopped — a1 ("do x")');
  assert.equal(r.clearDraft, true);
  assert.deepEqual(one.calls.at(-1), ['delete', 'a1']);
  assert.equal(one.side.refresh, 1);

  const two = rig({ schedules: [loop('ab1', 'x'), loop('ab2', 'y')] });
  assert.equal((await two.run('/loop stop ab1')).notice, 'loop stopped — ab1 ("x")');
  const amb = await two.run('/loop stop ab');
  assert.equal(amb.notice, "'ab' matches several loops — be more specific:\nab1 — \"x\"\nab2 — \"y\"");
  assert.equal(amb.clearDraft, false);
  assert.equal((await two.run('/loop stop zz')).notice, "no loop id starting 'zz' — try /loop list");
  assert.equal((await two.run('/loop stop')).notice, 'several loops — pick one: /loop stop <id>\nab1 — "x"\nab2 — "y"');
  assert.equal((await rig().run('/loop stop')).notice, 'no loops to stop');
  assert.deepEqual(await rig({ schedules: [loop('a1', 'x')], deleteOk: false }).run('/loop stop'), { error: 'loop stop: nodel', clearDraft: false });
  assert.deepEqual(await rig({ listOk: false }).run('/loop stop'), { error: 'loop stop: boom', clearDraft: false });
});

test('/loop create: arms, refuses a duplicate prompt, reports failure', async () => {
  const ok = rig();
  const r = await ok.run('/loop 10 do x');
  assert.equal(r.notice, 'loop armed — every 10m: "do x" (first fire in ~10m, id new1)');
  assert.equal(r.clearDraft, true);
  assert.deepEqual(ok.calls.at(-1), ['create', 'p1', { kind: 'loop', prompt: 'do x', interval_minutes: 10 }]);
  assert.equal(ok.side.refresh, 1);

  const dup = rig({ schedules: [loop('a1', 'do x')] });
  const d = await dup.run('/loop 10 do x');
  assert.equal(d.notice, 'already looping every 5m ("do x", id a1) — /loop stop a1 to replace');
  assert.equal(d.clearDraft, false);
  assert.equal(dup.calls.some((c) => c[0] === 'create'), false);
  // a disabled loop with the same prompt is not a duplicate
  assert.equal((await rig({ schedules: [loop('a1', 'do x', { enabled: false })] }).run('/loop 10 do x')).clearDraft, true);
  // a failed list does not block the create
  assert.equal((await rig({ listOk: false }).run('/loop 10 do x')).clearDraft, true);
  assert.deepEqual(await rig({ createOk: false }).run('/loop 10 do x'), { error: 'loop: nope', clearDraft: false });
});

test('/at: resolves against the injected clock, refuses a past time, reports failure', async () => {
  const ok = rig();
  const r = await ok.run('/at in 90m check it');
  assert.match(r.notice, /^scheduled — (tomorrow )?.+ \(in 1h30m\): "check it" \(id new1\)$/);
  assert.equal(r.clearDraft, true);
  const body = ok.calls.at(-1)[2];
  assert.equal(body.kind, 'once');
  assert.equal(body.prompt, 'check it');
  assert.equal(body.at_iso, '2026-10-13T13:30:00.000Z');
  assert.equal(ok.side.refresh, 1);
  assert.deepEqual(await rig({ createOk: false }).run('/at in 5m x'), { error: 'at: nope', clearDraft: false });
  // resolveAt returns null for a non-future 'in' time (parse lets '0' through only via direct cmd)
  const past = await runSlashCommand({ kind: 'at', when: 'in 0m', prompt: 'x' }, { pulse, api: {}, now: () => NOW });
  assert.equal(past.notice, 'that time is not in the future — use in <n>m, in <n>h, or a clock time (9:00, 9am, 5:30pm)');
  assert.equal(past.clearDraft, false);
});

test('/rename: renames, skips the same title, reports failure', async () => {
  const ok = rig();
  const r = await ok.run('/rename Beta');
  assert.deepEqual(r, { notice: 'renamed to "Beta"', clearDraft: true });
  assert.deepEqual(ok.calls.at(-1), ['rename', 'p1', 'Beta']);
  assert.equal(ok.side.changed, 1);
  const same = rig();
  assert.deepEqual(await same.run('/rename Alpha'), { notice: 'already named that', clearDraft: false });
  assert.equal(same.calls.length, 0);
  assert.deepEqual(await rig({ renameOk: false }).run('/rename Beta'), { error: 'rename: norename', clearDraft: false });
});

test('fmtLead: minutes under an hour, then hours and minutes', () => {
  assert.equal(fmtLead(0), 'in 1m');
  assert.equal(fmtLead(5 * 60000), 'in 5m');
  assert.equal(fmtLead(60 * 60000), 'in 1h');
  assert.equal(fmtLead(192 * 60000), 'in 3h12m');
});
