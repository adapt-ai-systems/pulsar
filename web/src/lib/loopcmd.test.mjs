/**
 * Tests for loopcmd.js — pure slash-command parsing + picker model for
 * pulse chat. Plain-node assertion script, matches the web/src/lib test
 * pattern:
 *
 *     node src/lib/loopcmd.test.mjs
 *
 * resolveAt cases pin the timezone to America/Los_Angeles (TZ guard below,
 * same contract as schedule.test.mjs) and use fixed `now` dates, so they are
 * deterministic regardless of the host clock.
 */

import assert from 'node:assert/strict';
import { parseSlashCommand, slashMenu, resolveAt } from './loopcmd.js';

// --- parseSlashCommand -----------------------------------------------------

// not commands -> null (sent to the pulse as messages)
assert.equal(parseSlashCommand(''), null);
assert.equal(parseSlashCommand('hello'), null);
assert.equal(parseSlashCommand(' /loop 5 x'), null); // leading space = literal

// '//' escape
assert.deepEqual(parseSlashCommand('//hi'), { kind: 'literal', text: '/hi' });
assert.deepEqual(parseSlashCommand('//'), { kind: 'literal', text: '/' });
assert.deepEqual(parseSlashCommand('///'), { kind: 'literal', text: '//' });

// create
assert.deepEqual(parseSlashCommand('/loop 10 do x'), { kind: 'create', minutes: 10, prompt: 'do x' });
assert.deepEqual(parseSlashCommand('/LOOP 5 x'), { kind: 'create', minutes: 5, prompt: 'x' });
assert.deepEqual(parseSlashCommand('/loop 10'), { kind: 'usage', text: 'Missing the prompt. usage: /loop <minutes> <prompt> — prompt this pulse every N minutes\n' +
  "       /loop list — show this pulse's loops\n" +
  '       /loop stop [id] — stop a loop (id optional if it has exactly one)' });
assert.equal(parseSlashCommand('/loop x y').kind, 'usage');
assert.equal(parseSlashCommand('/loop 0 x').kind, 'usage');
assert.equal(parseSlashCommand('/loop').kind, 'usage');

// list / stop
assert.deepEqual(parseSlashCommand('/loop list'), { kind: 'list' });
assert.deepEqual(parseSlashCommand('/loop LIST'), { kind: 'list' });
assert.deepEqual(parseSlashCommand('/loop stop'), { kind: 'stop', idPrefix: '' });
assert.deepEqual(parseSlashCommand('/loop stop abc123'), { kind: 'stop', idPrefix: 'abc123' });
assert.deepEqual(parseSlashCommand('/loop stop  abc '), { kind: 'stop', idPrefix: 'abc' });

// newline/tab after the command word still parses as /loop (split on /\s/)
assert.deepEqual(parseSlashCommand('/loop\n10 do x'), { kind: 'create', minutes: 10, prompt: 'do x' });

// unknown commands
assert.deepEqual(parseSlashCommand('/foo'), { kind: 'unknown', command: '/foo' });
assert.deepEqual(parseSlashCommand('/foo bar'), { kind: 'unknown', command: '/foo' });

// --- /at -------------------------------------------------------------------
assert.deepEqual(parseSlashCommand('/at in 30m check loops'), { kind: 'at', when: 'in 30m', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at in 2h check loops'), { kind: 'at', when: 'in 120m', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at in 2h 30m check loops'), { kind: 'at', when: 'in 150m', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at in 2h30m check loops'), { kind: 'at', when: 'in 150m', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at in 90m check loops'), { kind: 'at', when: 'in 90m', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at in 45 check loops'), { kind: 'at', when: 'in 45m', prompt: 'check loops' }); // bare number after 'in' = minutes
// 24h requires the colon; '9' alone is usage (never guess 09:00)
assert.equal(parseSlashCommand('/at 45 check loops').kind, 'usage');
// am/pm — phone typing
assert.deepEqual(parseSlashCommand('/at 9am check loops'), { kind: 'at', when: '09:00', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at 5:30pm check loops'), { kind: 'at', when: '17:30', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at 12am check loops'), { kind: 'at', when: '00:00', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at 12pm check loops'), { kind: 'at', when: '12:00', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at 9:00 check loops'), { kind: 'at', when: '09:00', prompt: 'check loops' });
assert.deepEqual(parseSlashCommand('/at 17:30 check loops'), { kind: 'at', when: '17:30', prompt: 'check loops' });
assert.equal(parseSlashCommand('/at in 0m x').kind, 'usage');
assert.equal(parseSlashCommand('/at 25:00 x').kind, 'usage');
assert.equal(parseSlashCommand('/at 13am x').kind, 'usage');
assert.equal(parseSlashCommand('/at 9:00').kind, 'usage'); // missing prompt
assert.equal(parseSlashCommand('/at').kind, 'usage');
assert.equal(parseSlashCommand('/at tomorrow x').kind, 'usage'); // not a grammar we ship

// --- /rename ---------------------------------------------------------------
assert.deepEqual(parseSlashCommand('/rename New Title'), { kind: 'rename', title: 'New Title' });
assert.equal(parseSlashCommand('/rename').kind, 'usage');
assert.equal(parseSlashCommand('/rename   ').kind, 'usage');

// --- slashMenu (picker model) ----------------------------------------------
// closed: no draft, not a command, '//' escape, unknown command prefix
assert.equal(slashMenu(''), null);
assert.equal(slashMenu('hello'), null);
assert.equal(slashMenu('//literal'), null);
assert.equal(slashMenu('/foo'), null);
// '/' alone shows EVERY command's rows
assert.equal(slashMenu('/').length, 5);
// typing the command word narrows to candidates
assert.deepEqual(slashMenu('/l'), slashMenu('/loop'));
assert.equal(slashMenu('/l').length, 3);
assert.equal(slashMenu('/r').length, 1);
assert.equal(slashMenu('/r')[0].cmd, 'rename');
assert.equal(slashMenu('/a').length, 1);
assert.equal(slashMenu('/a')[0].cmd, 'at');
// '/loop ' shows all three forms
assert.equal(slashMenu('/loop ').length, 3);
// typing the number keeps only the create row
assert.deepEqual(slashMenu('/loop 1'), [slashMenu('/loop ')[0]]);
// prompt started — menu hides
assert.equal(slashMenu('/loop 10 do x'), null);
assert.equal(slashMenu('/loop 10 '), null);
// complete forms hide so Enter runs instead of completing
assert.equal(slashMenu('/loop list'), null);
assert.equal(slashMenu('/loop stop'), null);
// keyword filtering
assert.deepEqual(slashMenu('/loop li'), [slashMenu('/loop ')[1]]);
assert.deepEqual(slashMenu('/loop s'), [slashMenu('/loop ')[2]]);
assert.deepEqual(slashMenu('/loop c'), [slashMenu('/loop ')[0]]);
assert.equal(slashMenu('/loop z'), null);
// trailing space keeps the row visible (id/args still coming)
assert.deepEqual(slashMenu('/loop list '), [slashMenu('/loop ')[1]]);
assert.deepEqual(slashMenu('/loop stop '), [slashMenu('/loop ')[2]]);
// args beyond the keyword close the menu — Enter must RUN, not shorten the
// draft back to the bare form
assert.equal(slashMenu('/loop stop abc123'), null);
assert.equal(slashMenu('/loop list foo'), null);
// at/rename: row visible bare or with the command word + trailing space,
// closed once any arg is typed (freeform args — menu is noise)
assert.equal(slashMenu('/at ').length, 1);
assert.equal(slashMenu('/at in 30m x'), null);
assert.equal(slashMenu('/at x'), null);
assert.equal(slashMenu('/rename ').length, 1);
assert.equal(slashMenu('/rename New Title'), null);
assert.equal(slashMenu('/rename x'), null);

// --- resolveAt (TZ-pinned: America/Los_Angeles via guard below) ------------
// fixed now: 2026-09-15 06:00:00 local (13:00 UTC during PDT)
const NOW = new Date(2026, 8, 15, 6, 0, 0);
// 'in' form: now + N minutes, ISO out
assert.equal(resolveAt('in 30m', NOW), new Date(NOW.getTime() + 30 * 60000).toISOString());
// zero/negative lead refuses
assert.equal(resolveAt('in 0m', NOW), null);
// clock form: later today -> today local
const today9 = resolveAt('09:00', NOW);
assert.equal(new Date(today9).getDate(), 15);
assert.equal(new Date(today9).getHours(), 9);
// clock form: already past -> tomorrow
const past = resolveAt('05:00', NOW);
assert.equal(new Date(past).getDate(), 16);
assert.equal(new Date(past).getHours(), 5);
// exactly now -> tomorrow (a prompt at the same instant is not useful)
assert.equal(new Date(resolveAt('06:00', NOW)).getDate(), 16);
// DST crossing: Nov 1 2026 10:00 PDT + '/at 09:00' ->
// Nov 2 09:00 PST — 23 wall hours later, not 24. setDate, never +24h.
const DST_NOW = new Date(2026, 10, 1, 10, 0, 0); // Nov 1, still PDT (UTC-7)
const dstT = new Date(resolveAt('09:00', DST_NOW));
assert.equal(dstT.getDate(), 2);
assert.equal(dstT.getHours(), 9);
assert.equal(Math.round((dstT - DST_NOW) / 3600000), 23);
// unknown grammar
assert.equal(resolveAt('tomorrow', NOW), null);

// Guard: resolveAt tests only make sense in Pacific time. Re-exec under TZ
// if needed (same pattern as schedule.test.mjs).
if (process.env.TZ !== 'America/Los_Angeles') {
  const { spawnSync } = await import('node:child_process');
  const r = spawnSync(process.execPath, [new URL(import.meta.url).pathname], {
    stdio: 'inherit',
    env: { ...process.env, TZ: 'America/Los_Angeles' },
  });
  process.exit(r.status ?? 0);
}

console.log('loopcmd.test.mjs: all assertions passed');
