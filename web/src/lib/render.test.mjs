/**
 * Self-contained tests for render.js — the peer-message split of user_message
 * events (daemon inbox prefix -> kind:'peer' credited to the sender).
 *
 * No test framework is installed in web/, so this is a plain-node assertion
 * script (same pattern as markdown.test.mjs / usage.test.mjs):
 *
 *     node src/lib/render.test.mjs
 */

import assert from 'node:assert/strict';
import { runnerErrorLine as _rel, turnMeta as _tm } from './render.js';
import { setDefaultTimeZone } from './time.js';
import {
  buildItems,
  foldNoise,
  keyItems,
  noiseLabel,
  NOISE_FOLD_MIN,
  firstLine,
  resultSummaryLine,
  RESULT_LINE_MAX,
  pulseStatus,
  thinkingState,
  toolCallLine,
  turnMeta,
  fmtClock,
  PEER_RE,
  WATCH_RE,
  DAEMON_LABEL_RE,
  BRACKET_RE,
  isAutomated,
  isRealYou,
  classifyUserMessage,
  answerLine,
  watchSummary,
  scheduleSummary,
  readableThinking,
  youHistoryRows,
} from './render.js';

let passed = 0;
function t(name, fn) {
  try {
    fn();
    passed++;
  } catch (e) {
    console.error(`FAIL: ${name}\n  ${e.message}`);
    process.exitCode = 1;
  }
}

const you = (text, extra = {}) => ({ type: 'user_message', text, ts: '2026-07-31T12:00:00+0000', ...extra });

t('starting and stopping states get explicit non-thinking phases', () => {
  assert.deepEqual(
    pulseStatus({ socket_state: 'starting', heartbeat_fresh: true }, 0),
    { phase: 'waking', thinking: false, stalled: false, seconds: null, label: 'waking…', evidence: null }
  );
  assert.deepEqual(
    pulseStatus({ heartbeat: { state: 'stopping' }, reachable: true }, 0),
    { phase: 'stopping', thinking: false, stalled: false, seconds: null, label: 'stopping…', evidence: null }
  );
});

t('a stale starting heartbeat is stalled, not waking', () => {
  const s = pulseStatus(
    { socket_state: 'starting', heartbeat: { state: 'starting' }, reachable: false, heartbeat_fresh: false, heartbeat_age_s: 62 },
    0
  );
  assert.equal(s.phase, 'stalled');
  assert.equal(s.evidence, 'no heartbeat for 62s');
});

t('busy outranks a transient state from the other source', () => {
  const s = pulseStatus({ socket_state: 'busy', busy_since: 100, heartbeat: { state: 'starting' } }, 105_000);
  assert.equal(s.phase, 'thinking');
  assert.equal(s.label, 'working · 0:00:05');
});

t('busy status uses the working label and preserves thinkingState', () => {
  const live = { socket_state: 'busy', busy_since: 100 };
  assert.equal(pulseStatus(live, 105_000).label, 'working · 0:00:05');
  assert.deepEqual(thinkingState(live, 105_000), { thinking: true, seconds: 5 });
  assert.equal(
    pulseStatus({ socket_state: 'busy' }, 105_000).label,
    'working'
  );
});

t('idle status has an explicit idle label', () => {
  assert.equal(
    pulseStatus({ socket_state: 'idle', reachable: true, heartbeat_fresh: true }, 0).label,
    'idle'
  );
});

t('turnMeta extracts the newest done timestamp and usage', () => {
  const events = [
    { type: 'usage', in: 10, out: 2, ts: '2026-07-26T14:06:01+0000' },
    { type: 'done', ts: '2026-07-26T14:06:02+0000' },
    { type: 'usage', in: 20, out: 3, ts: '2026-07-26T14:06:03+0000' },
    { type: 'done', ts: '2026-07-26T14:06:04+0000' },
  ];
  // out sums across the turn's usage events; in is the final call's input.
  assert.deepEqual(turnMeta(events), {
    lastDoneTs: Date.parse('2026-07-26T14:06:04+00:00'),
    lastUsage: { inTok: 20, outTok: 5 },
    liveTool: null,
    speed: null,
    lastTurnMs: null,
    lastThinkTok: null,
    lastFailed: false,
  });
});

t('turnMeta reports the newest done event\'s thinking tokens', () => {
  const m = turnMeta([
    { type: 'done', thinking_tokens: 50, ts: '2026-09-23T05:00:00+0000' },
    { type: 'user_message', text: 'next', ts: '2026-09-23T05:01:00+0000' },
    { type: 'done', thinking_tokens: 1234, ts: '2026-09-23T05:02:00+0000' },
  ]);
  assert.equal(m.lastThinkTok, 1234);
  assert.equal(turnMeta([{ type: 'done', ts: '2026-09-23T05:02:00+0000' }]).lastThinkTok, null);
});

t('turnMeta measures the newest completed turn: user_message → its done', () => {
  const m = turnMeta([
    { type: 'user_message', text: 'first', ts: '2026-09-07T05:00:00+0000' },
    { type: 'done', ts: '2026-09-07T05:00:10+0000' },
    { type: 'user_message', text: 'second', ts: '2026-09-07T05:10:00+0000' },
    { type: 'tool_call', id: 'x', name: 'Read', args: {} },
    { type: 'done', ts: '2026-09-07T06:28:27+0000' },
    { type: 'user_message', text: 'still running', ts: '2026-09-07T07:00:00+0000' },
  ]);
  assert.equal(m.lastTurnMs, (78 * 60 + 27) * 1000);
  assert.equal(fmtClock(m.lastTurnMs), '1:18:27');
  assert.equal(fmtClock(5000), '0:00:05');
  assert.equal(fmtClock(NaN), '');
});

t('lastUsage sums out across a multi-call turn, in stays final', () => {
  assert.deepEqual(
    turnMeta([
      { type: 'user_message', text: 'go' },
      { type: 'usage', in: 100_000, out: 50 },
      { type: 'usage', in: 141_000, out: 11 },
      { type: 'done', ts: '2026-08-26T20:18:41+0000' },
    ]).lastUsage,
    { inTok: 141_000, outTok: 61 }
  );
});

t('turnMeta reports a pending live tool with a normalized timestamp', () => {
  const ts = '2026-07-26T14:06:09+0000';
  assert.deepEqual(
    turnMeta([{ type: 'tool_call', id: 'live-1', name: 'Read', args: { file_path: '/tmp/a' }, ts }]).liveTool,
    { line: 'read /tmp/a', ts: Date.parse('2026-07-26T14:06:09+00:00') }
  );
});

t('turnMeta has no live tool after its result or a later done event', () => {
  assert.equal(
    turnMeta([
      { type: 'tool_call', id: 'result-1', name: 'Read', args: { file_path: '/tmp/a' } },
      { type: 'tool_result', id: 'result-1', ok: true },
    ]).liveTool,
    null
  );
  assert.equal(
    turnMeta([
      { type: 'tool_call', id: 'done-1', name: 'Read', args: { file_path: '/tmp/a' } },
      { type: 'done', ts: '2026-07-26T14:06:09+0000' },
    ]).liveTool,
    null
  );
});

t('a later user_message or runner_error closes the live tool', () => {
  assert.equal(
    turnMeta([
      { type: 'tool_call', id: 'orphan-1', name: 'Read', args: { file_path: '/tmp/a' }, ts: '2026-07-26T14:06:09+0000' },
      { type: 'user_message', text: 'new turn' },
    ]).liveTool,
    null
  );
  assert.equal(
    turnMeta([
      { type: 'tool_call', id: 'orphan-2', name: 'Read', args: { file_path: '/tmp/a' }, ts: '2026-07-26T14:06:09+0000' },
      { type: 'runner_error', reason: 'boom' },
    ]).liveTool,
    null
  );
});

t('liveTool line survives a folded same-id placeholder update', () => {
  const meta = turnMeta([
    { type: 'tool_call', id: 'fold-1', name: 'Read', args: { file_path: '/tmp/a' }, ts: '2026-07-26T14:06:09+0000' },
    { type: 'tool_call', id: 'fold-1', name: 'tool', args: {}, ts: '2026-07-26T14:06:10+0000' },
  ]);
  assert.equal(meta.liveTool.line, 'read /tmp/a');
});

t('lastUsage belongs to the newest completed turn only', () => {
  // In-flight turn usage must not leak into the chip.
  const events = [
    { type: 'user_message', text: 'turn 1' },
    { type: 'usage', in: 10, out: 2 },
    { type: 'done', ts: '2026-07-26T14:06:02+0000' },
    { type: 'user_message', text: 'turn 2 (in flight)' },
    { type: 'usage', in: 999, out: 999 },
  ];
  assert.deepEqual(turnMeta(events).lastUsage, { inTok: 10, outTok: 2 });
  // Codex trails usage after done — still the completed turn's numbers.
  assert.deepEqual(
    turnMeta([
      { type: 'user_message', text: 'turn 1' },
      { type: 'done', ts: '2026-07-26T14:06:02+0000' },
      { type: 'usage', in: 7, out: 3 },
    ]).lastUsage,
    { inTok: 7, outTok: 3 }
  );
  // No done in the tail = no completed turn = no chip.
  assert.equal(turnMeta([{ type: 'usage', in: 5, out: 1 }]).lastUsage, null);
  // An older turn's usage may not stand in for a turn that emitted none.
  assert.equal(
    turnMeta([
      { type: 'usage', in: 4, out: 1 },
      { type: 'done', ts: '2026-07-26T14:06:01+0000' },
      { type: 'user_message', text: 'turn 2' },
      { type: 'done', ts: '2026-07-26T14:06:05+0000' },
    ]).lastUsage,
    null
  );
});

t('buildItems marks only an unresolved current tool as live', () => {
  const [live] = buildItems([
    { type: 'tool_call', id: 'live-1', name: 'Read', args: { file_path: '/tmp/a' }, ts: '2026-07-26T14:06:09+0000' },
  ]);
  assert.equal(live.live, true);

  const [finished] = buildItems([
    { type: 'tool_call', id: 'done-1', name: 'Read', args: { file_path: '/tmp/a' } },
    { type: 'tool_result', id: 'done-1', ok: true },
  ]);
  assert.equal(finished.live, false);
});

t('buildItems marks the newest pending tool when multiple calls remain open', () => {
  const items = buildItems([
    { type: 'tool_call', id: 'first', name: 'Read', args: { file_path: '/tmp/first' } },
    { type: 'tool_call', id: 'second', name: 'Read', args: { file_path: '/tmp/second' } },
  ]);
  assert.equal(items[0].live, false);
  assert.equal(items[1].live, true);
});

t('a tool-only turn gets a turnend boundary marker', () => {
  const items = buildItems([
    you('do the thing'),
    { type: 'tool_call', id: 'a', name: 'Bash', args: { command: 'x' }, description: 'run x' },
    { type: 'tool_result', id: 'a', ok: true },
    { type: 'tool_call', id: 'b', name: 'ScheduleWakeup', args: {} },
    { type: 'tool_result', id: 'b', ok: true },
    { type: 'done', reason: 'result', subtype: 'success', ts: '2026-08-26T20:18:41+0000' },
  ]);
  const marker = items[items.length - 1];
  assert.deepEqual(
    { kind: marker.kind, tools: marker.tools, interrupted: marker.interrupted },
    { kind: 'turnend', tools: 2, interrupted: false }
  );
});

t('a turn with a reply gets no turnend marker', () => {
  const items = buildItems([
    you('hi'),
    { type: 'tool_call', id: 'a', name: 'Read', args: { file_path: '/tmp/a' } },
    { type: 'assistant_text', text: 'done!' },
    { type: 'done', reason: 'result', ts: '2026-08-26T20:18:41+0000' },
  ]);
  assert.equal(items.some((it) => it.kind === 'turnend'), false);
});

t('turnend honesty: truncated tails and double done stay silent', () => {
  // Tail starts mid-turn — the reply may have been truncated away.
  assert.equal(
    buildItems([
      { type: 'tool_call', id: 'a', name: 'Read', args: { file_path: '/tmp/a' } },
      { type: 'done', reason: 'result', ts: '2026-08-26T20:18:41+0000' },
    ]).some((it) => it.kind === 'turnend'),
    false
  );
  // A trailing second done must not double-mark.
  const items = buildItems([
    you('go'),
    { type: 'done', reason: 'result', ts: '2026-08-26T20:18:41+0000' },
    { type: 'done', reason: 'result', ts: '2026-08-26T20:18:42+0000' },
  ]);
  assert.equal(items.filter((it) => it.kind === 'turnend').length, 1);
});

t('an interrupted turn with a partial reply still gets the interrupted marker', () => {
  const items = buildItems([
    you('go'),
    { type: 'assistant_text', text: 'starting on it' },
    { type: 'tool_call', id: 'a', name: 'Bash', args: { command: 'long' } },
    { type: 'done', reason: 'result', subtype: 'interrupted', ts: '2026-08-26T20:18:41+0000' },
  ]);
  const marker = items[items.length - 1];
  assert.deepEqual(
    { kind: marker.kind, tools: marker.tools, interrupted: marker.interrupted },
    { kind: 'turnend', tools: 1, interrupted: true }
  );
  // A replied turn that was NOT interrupted still shows nothing extra.
  assert.equal(
    buildItems([
      you('go'),
      { type: 'assistant_text', text: 'all done' },
      { type: 'done', reason: 'result', ts: '2026-08-26T20:18:41+0000' },
    ]).some((it) => it.kind === 'turnend'),
    false
  );
});

t('an interrupted silent turn says so', () => {
  const [, marker] = buildItems([
    you('go'),
    { type: 'done', reason: 'interrupted', ts: '2026-08-26T20:18:41+0000' },
  ]);
  assert.deepEqual(
    { kind: marker.kind, tools: marker.tools, interrupted: marker.interrupted },
    { kind: 'turnend', tools: 0, interrupted: true }
  );
});

t('plain user message stays kind you', () => {
  const [item] = buildItems([you('hello there')]);
  assert.equal(item.kind, 'you');
  assert.equal(item.text, 'hello there');
});

t('inbox-prefixed message becomes kind peer with sender credited', () => {
  const [item] = buildItems([
    you("[message from 'Scout' (pulse 11111111222222223333333344444444)] bench round 2 is ready")
  ]);
  assert.equal(item.kind, 'peer');
  assert.equal(item.from, 'Scout');
  assert.equal(item.fromId, '11111111222222223333333344444444');
  assert.equal(item.text, 'bench round 2 is ready');
});

t('daemon watch fire is credited to watcher, not you', () => {
  const [a] = buildItems([you('[watch: Keeper woken] 🔔 condition met — state: stopped.')]);
  assert.equal(a.kind, 'peer');
  assert.equal(a.from, 'watcher');
  assert.equal(a.fromId, null);
  assert.equal(a.text, '[watch: Keeper woken] 🔔 condition met — state: stopped.');
  const [b] = buildItems([you("[delegation watch: 'Ops' (pulse abc)] working")]);
  assert.equal(b.kind, 'peer');
  assert.equal(b.from, 'watcher');
  const [c] = buildItems([you('watch out for [watch: x]')]);
  assert.equal(c.kind, 'you');
});

t('python repr double-quotes a title containing an apostrophe', () => {
  const [item] = buildItems([
    you('[message from "Alex\'s helper" (pulse abc123)] done')
  ]);
  assert.equal(item.kind, 'peer');
  assert.equal(item.from, "Alex's helper");
  assert.equal(item.text, 'done');
});

t('multiline body survives the prefix strip', () => {
  const [item] = buildItems([
    you("[message from 'Ops' (pulse deadbeef)] line one\nline two")
  ]);
  assert.equal(item.kind, 'peer');
  assert.equal(item.text, 'line one\nline two');
});

t('prefix mid-text does not trigger (only a leading prefix counts)', () => {
  const [item] = buildItems([
    you("fyi the format is [message from 'Title' (pulse abc)] like that")
  ]);
  assert.equal(item.kind, 'you');
});

t('timestamps and attachments ride the peer item', () => {
  const atts = [{ filename: 'a.png', url: '/x', size: 1, mime_type: 'image/png' }];
  const [item] = buildItems([
    you("[message from 'Ops' (pulse deadbeef)] see attached", { attachments: atts })
  ]);
  assert.equal(item.ts, '2026-07-31T12:00:00+0000');
  assert.deepEqual(item.attachments, atts);
});

t('reply_to rides the you item as replyTo (Reply button)', () => {
  const ref = { text: 'Option A or B?', ts: '2026-07-31T11:00:00+0000' };
  const [item] = buildItems([you('A', { reply_to: ref })]);
  assert.equal(item.kind, 'you');
  assert.equal(item.text, 'A');
  assert.deepEqual(item.replyTo, ref);
  assert.equal(buildItems([you('plain')])[0].replyTo, null);
});

t('grok read_file line uses the path not the raw name', () => {
  assert.equal(
    toolCallLine({
      name: 'read_file',
      description: 'read_file',
      args: { target_file: '/tmp/a.md' }
    }),
    'read /tmp/a.md'
  );
});

t('grok run_terminal_command prefers the human title', () => {
  assert.equal(
    toolCallLine({
      name: 'run_terminal_command',
      description: 'Execute `ls -la`',
      args: { command: 'ls -la' }
    }),
    'Execute `ls -la`'
  );
});

t('same-id grok updates fold into one line', () => {
  const items = buildItems([
    { type: 'tool_call', id: 'call-1', name: 'read_file',
      description: 'read_file', args: { target_file: '/tmp/a.md' } },
    { type: 'tool_call', id: 'call-1', name: 'Read `/tmp/a.md`',
      description: 'Read `/tmp/a.md`',
      args: { variant: 'ReadFile', target_file: '/tmp/a.md' } },
    { type: 'tool_call', id: 'call-1', name: 'tool', args: {} },
    { type: 'tool_result', id: 'call-1', ok: true, snippet: 'hello' }
  ]);
  const tools = items.filter((i) => i.kind === 'tool');
  assert.equal(tools.length, 1);
  assert.equal(tools[0].line, 'read /tmp/a.md');
  assert.equal(tools[0].res, '→ hello');
});

t('placeholder first then real args upgrades the same row', () => {
  const items = buildItems([
    { type: 'tool_call', id: 'x', name: 'tool', args: {} },
    { type: 'tool_call', id: 'x', name: 'web_fetch', args: { url: 'https://x.test' } }
  ]);
  assert.equal(items.length, 1);
  assert.equal(items[0].line, 'fetch https://x.test');
});

t('empty tool update does not create a placeholder row', () => {
  const items = buildItems([
    { type: 'tool_call', id: 'a', name: 'grep', args: { pattern: 'foo' } },
    { type: 'tool_call', id: 'a', name: 'tool', args: {} },
    { type: 'tool_call', id: 'b', name: 'tool', args: {} }
  ]);
  assert.equal(items.length, 2);
  assert.equal(items[0].line, 'searched for foo');
  assert.equal(items[1].line, 'tool');
});

t('grok bash byte-array snippet decodes to text', () => {
  const bytes = Array.from(new TextEncoder().encode('211 pages indexed\n225 points'));
  const pyDumped = `{"type": "Bash", "output": [${bytes.join(', ')}]}`;
  const items = buildItems([
    { type: 'tool_call', id: 'b', name: 'run_terminal_command',
      description: 'Regenerate and publish three explorer pages' },
    { type: 'tool_result', id: 'b', ok: true, snippet: pyDumped }
  ]);
  assert.equal(items[0].res, '→ 211 pages indexed');
  assert.ok(items[0].raw.includes('211 pages indexed'));
  assert.ok(!items[0].raw.includes('50, 49, 49'));
  assert.equal(firstLine(pyDumped), '211 pages indexed');
});

t('plain tool snippets stay untouched', () => {
  assert.equal(firstLine('hello\nworld'), 'hello');
  assert.equal(firstLine('{"ok": true}'), '{"ok": true}');
});

t('one-line JSON result is clipped to a compact summary', () => {
  // GLM curling the daemon roster: ~8KB of compact JSON, ZERO newlines —
  // firstLine used to hand back the whole blob and flood the pane.
  const roster = JSON.stringify({ ok: true, value: Array.from({ length: 60 }, (_, i) => ({
    id: `id${i}-aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa`, title: `Pulse ${i}`,
    model: 'glm-5.3-flash', vessel: 'pi', state: 'stopped',
  })) });
  assert.ok(roster.length > RESULT_LINE_MAX);
  assert.ok(!roster.includes('\n'));
  const items = buildItems([
    { type: 'tool_call', id: 'r', name: 'bash',
      args: { command: 'curl -s http://127.0.0.1:8726/pulses' } },
    { type: 'tool_result', id: 'r', ok: true, snippet: roster }
  ]);
  const res = items[0].res;
  // Compact: clipped, ellipsed, and NOT the whole blob.
  assert.ok(res.endsWith('…'));
  assert.ok(res.length < 200, `res was ${res.length} chars`);
  assert.ok(!res.includes('Pulse 59'));
  // Full raw stays behind tap-to-expand — nothing is lost.
  assert.ok(items[0].raw.includes(roster));
  // Short results are NOT touched.
  assert.equal(resultSummaryLine('39 pulses remaining\n'), '39 pulses remaining');
  assert.equal(resultSummaryLine('hello'), 'hello');
  // Orphan results (no matching call in the tail window) get the same cap.
  const orphans = buildItems([
    { type: 'tool_result', id: 'orphan', ok: true, snippet: roster }
  ]);
  assert.ok(orphans[0].line.endsWith('…'));
  assert.ok(orphans[0].line.length < 200);
});

t('truncated tool_result copies the flag and keeps the clipped snippet for the collapsed line', () => {
  const items = buildItems([
    { type: 'tool_call', id: 't', name: 'bash', args: { command: 'cat big' } },
    { type: 'tool_result', id: 't', ok: true, snippet: 'hello world', truncated: true }
  ]);
  assert.equal(items[0].truncated, true);
  assert.equal(items[0].resultSnippet, 'hello world');
  assert.ok(items[0].res.includes('hello world'));
});

t('consecutive assistant_text chunks merge into one reply', () => {
  const items = buildItems([
    { type: 'assistant_text', text: 'First thought. ' },
    { type: 'assistant_text', text: 'Second thought.' },
    { type: 'tool_call', id: 'c', name: 'list_dir',
      args: { target_directory: '/tmp' } },
    { type: 'assistant_text', text: 'After the tool.' }
  ]);
  assert.equal(items.map((i) => i.kind).join(','), 'reply,tool,reply');
  assert.equal(items[0].text, 'First thought. Second thought.');
  assert.equal(items[1].line, 'list /tmp');
  assert.equal(items[2].text, 'After the tool.');
});

t('backup event renders one non-error system marker', () => {
  const items = buildItems([
    { type: 'backup', model: 'gpt-5.6-luna', vessel: 'codex',
      reason: 'vessel failed to start', ts: '2026-08-25T12:00:00+0000' },
    { type: 'assistant_text', text: 'here is the answer', backup: true }
  ]);
  assert.equal(items.map((i) => i.kind).join(','), 'tool,reply');
  // The marker is NOT an error — the failure path is runner_error (below).
  assert.equal(items[0].err, false);
  assert.ok(items[0].line.includes('backup'));
  assert.ok(items[0].line.includes('gpt-5.6-luna'));
  assert.ok(items[0].line.includes('vessel failed to start'));
  // No per-bubble chip: the reply is an ordinary bubble.
  assert.equal(items[1].text, 'here is the answer');
});

t('backup failures reuse the red runner_error line', () => {
  const [item] = buildItems([
    { type: 'runner_error', where: 'backup',
      reason: 'backup: gpt-5.6-luna failed to start: provider unreachable' }
  ]);
  assert.equal(item.kind, 'tool');
  assert.equal(item.err, true);
  assert.ok(item.line.startsWith('backup: '));
});

t('failover event renders one non-error line naming both models and why', () => {
  const [item] = buildItems([
    { type: 'failover', from: 'gpt-6-luna', to: 'claude-opus-5-5',
      reason: 'cli died (rc=1)', resent: ['a.json'], ts: '2026-10-14T12:00:00+0000' }
  ]);
  assert.equal(item.kind, 'tool');
  assert.equal(item.err, false);
  assert.equal(item.line,
    '⇄ failover · gpt-6-luna failed (cli died (rc=1)) — switched to claude-opus-5-5');
});

t('a failed failover is one red runner_error line', () => {
  const [item] = buildItems([
    { type: 'runner_error', where: 'failover',
      reason: 'failover to claude-opus-5-5 failed: respawn: boom — the pulse is stopped.' }
  ]);
  assert.equal(item.err, true);
  assert.ok(item.line.startsWith('failover to claude-opus-5-5 failed'));
});

t('qwen OMP: whitespace-only assistant text next to tool calls is not a reply', () => {
  const items = buildItems([
    { type: 'user_message', text: 'go' },
    { type: 'tool_call', id: 'c1', name: 'bash', args: { command: 'ls' } },
    { type: 'tool_result', id: 'c1', snippet: 'x' },
    { type: 'assistant_text', text: '\n\n\n' },
    { type: 'tool_call', id: 'c2', name: 'bash', args: { command: 'pwd' } },
    { type: 'tool_result', id: 'c2', snippet: 'y' },
    { type: 'assistant_text', text: '\n\n\n' },
    { type: 'done', reason: 'result' }
  ]);
  // Only the two tool lines — no empty REPLY bubbles, and the turn reads as
  // tool-only (turnend marker, since no real reply was produced).
  assert.equal(items.map((i) => i.kind).join(','), 'you,tool,tool,turnend');
});

t('qwen OMP: lowercase pi tool name gets the bash verb-map summary', () => {
  const line = toolCallLine({
    name: 'bash',
    args: { command: 'cd /home/user/pulsar && ls -la\ngrep -rn foo .' }
  });
  // First line only — never the full raw multi-line command.
  assert.equal(line, 'cd /home/user/pulsar && ls -la');
});

t('bash command summary is clipped to one line and capped', () => {
  const long = 'a'.repeat(200);
  const line = toolCallLine({ name: 'bash', args: { command: long } });
  assert.ok(line.length < 200);
  assert.ok(line.endsWith('…'));
});


t('bash description arg becomes the tool line', () => {
  const items = buildItems([
    { type: 'tool_call', id: 'd', name: 'bash',
      args: { command: 'for id in a b c; do delete $id; done',
              description: 'Removing old build artifacts' } },
    { type: 'tool_result', id: 'd', ok: true, snippet: 'done' }
  ]);
  // Meaning, not the raw command.
  assert.equal(items[0].line, 'Removing old build artifacts');
  // A description that just repeats the tool name is still demoted to raw text,
  // in any case ('Bash' vs 'bash').
  const plain = buildItems([
    { type: 'tool_call', id: 'e', name: 'bash',
      args: { command: 'ls', description: 'bash' } },
  ]);
  assert.equal(plain[0].line, 'ls');
  const caps = buildItems([
    { type: 'tool_call', id: 'f', name: 'Bash',
      args: { command: 'ls', description: 'Bash' } },
  ]);
  assert.equal(caps[0].line, 'ls');
});

t('Codex summarizer scratch text is never shown as a tool label', () => {
  const line = toolCallLine({
    name: 'command_execution',
    description: "We need answer user's request. Need produce final only one short plain-English sentence.",
    args: { command: 'ls -la' }
  });
  assert.equal(line, 'ls -la');
  assert.ok(!line.includes("user's request"));
  const prompt = toolCallLine({
    name: 'command_execution',
    description: "You write terse display labels for an AI agent's tool calls.",
    args: { command: 'pwd' }
  });
  assert.equal(prompt, 'pwd');
});


/* --- the noise fold (keeps replies from getting buried under daemon notices) --- */

const watchFire = (text) => you(`[delegation watch] ${text}`);

t('repeated identical runner errors fold into one line', () => {
  const ev = { type: 'runner_error', reason: 'cli exited 1' };
  const items = buildItems([ev, ev, ev, ev]);
  assert.equal(items.length, 1);
  assert.equal(items[0].line, 'cli exited 1 (×4)');
  const two = buildItems([ev, { type: 'runner_error', reason: 'other' }]);
  assert.equal(two.length, 2);
});

t('a context reset leaves a visible marker', () => {
  const items = buildItems([{ type: 'context_reset', ts: '2026-09-24T01:30:00+0000' }]);
  assert.equal(items.length, 1);
  assert.match(items[0].line, /context reset/);
});

t('a bookmark marker draws a divider; a summary press names the period start in configured zone', () => {
  try {
    setDefaultTimeZone('UTC');
    const first = buildItems([{ type: 'bookmark', at: '2026-11-08T20:00:00+00:00', summary_since: null, ts: '2026-11-08T20:00:00+00:00' }]);
    assert.equal(first.length, 1);
    assert.match(first[0].line, /bookmark set/);
    const again = buildItems([{ type: 'bookmark', at: '2026-11-09T01:00:00+00:00', summary_since: '2026-11-08T20:15:00+00:00', ts: '2026-11-09T01:00:00+00:00' }]);
    assert.match(again[0].line, /since 8:15 PM UTC/);
    const cleared = buildItems([{ type: 'bookmark', cleared: true, ts: '2026-11-09T01:05:00+00:00' }]);
    assert.match(cleared[0].line, /bookmark cleared/);
  } finally {
    setDefaultTimeZone(null);
  }
});

t('a spawn marker becomes a spawned item with the new pulse id (2026-11-07)', () => {
  const items = buildItems([
    { type: 'pulse_spawned', pulse_id: 'abc123', title: 'SEO helper', model: 'claude-opus-5', ts: '2026-11-07T20:00:00+0000' }
  ]);
  assert.equal(items.length, 1);
  assert.deepEqual(
    { kind: items[0].kind, pulseId: items[0].pulseId, title: items[0].title, model: items[0].model },
    { kind: 'spawned', pulseId: 'abc123', title: 'SEO helper', model: 'claude-opus-5' }
  );
  // A marker with no id has nothing to link to.
  assert.equal(buildItems([{ type: 'pulse_spawned', title: 'x' }]).length, 0);
  // Mid-turn, between a tool call and its result, the result still lands.
  const mid = buildItems([
    { type: 'user_message', text: 'go' },
    { type: 'tool_call', id: 'c1', name: 'Bash', args: { command: 'curl -X POST /pulses' } },
    { type: 'pulse_spawned', pulse_id: 'p2', title: 'helper' },
    { type: 'tool_result', id: 'c1', ok: true, snippet: '{"ok":true}' }
  ]);
  assert.deepEqual(mid.map((i) => i.kind), ['you', 'tool', 'spawned']);
  assert.equal(mid[1].done, true);
});

t('pulsar handoff notices are credited to pulsar, not you', () => {
  const [a] = buildItems([you('[pulsar] This pulse is switching to another model.')]);
  assert.equal(a.kind, 'peer');
  assert.equal(a.from, 'pulsar');
  const [b] = buildItems([you('[pulsar handoff] You are continuing a previous session')]);
  assert.equal(b.from, 'pulsar');
  assert.equal(a.summary, 'Asked the current model for notes before switching models');
  assert.equal(b.summary, "Fresh session started from the previous session's notes");
  const [c] = buildItems([you('what does [pulsar] mean?')]);
  assert.equal(c.kind, 'you');
});

t('a run of watcher notices folds to one item carrying the whole run', () => {
  const items = buildItems([
    you('hello'),
    { type: 'assistant_text', text: 'working' },
    watchFire('a is archived'),
    watchFire('b is archived'),
    watchFire('c is archived'),
    { type: 'assistant_text', text: 'the reply you came for' }
  ]);
  assert.deepEqual(items.map((i) => i.kind), ['you', 'reply', 'peer', 'peer', 'peer', 'reply']);
  const folded = foldNoise(items);
  assert.deepEqual(folded.map((i) => i.kind), ['you', 'reply', 'fold', 'reply']);
  const fold = folded[2];
  assert.equal(fold.count, 3);
  assert.deepEqual(fold.labels, ['watcher']);
  assert.equal(fold.items.length, 3);
  assert.equal(fold.items[2].text, '[delegation watch] c is archived');
  assert.ok(fold.head.includes('a is archived'), 'collapsed line keeps the first note');
  assert.equal(fold.ts, items[2].ts, 'fold is stamped with its FIRST row');
  // Nothing lost: unfolded rows survive verbatim, in order.
  assert.deepEqual(
    folded.flatMap((i) => (i.kind === 'fold' ? i.items : [i])),
    items
  );
});

t('below the threshold a watcher notice stays standalone', () => {
  const items = buildItems([watchFire('only one'), watchFire('and two'), you('real message')]);
  assert.equal(NOISE_FOLD_MIN, 3);
  assert.deepEqual(foldNoise(items).map((i) => i.kind), ['peer', 'peer', 'you']);
  // Same run at min=2 does fold — proves the threshold is what gates it.
  assert.deepEqual(foldNoise(items, 2).map((i) => i.kind), ['fold', 'you']);
});

t('a named pulse is never folded, only the daemon watcher is', () => {
  const items = buildItems([
    you("[message from 'Scout' (pulse abc123)] first"),
    you("[message from 'Scout' (pulse abc123)] second"),
    you("[message from 'Scout' (pulse abc123)] third")
  ]);
  assert.deepEqual(foldNoise(items).map((i) => i.kind), ['peer', 'peer', 'peer']);
  assert.equal(noiseLabel(items[0]), '');
  const w = buildItems([watchFire('x')])[0];
  assert.equal(noiseLabel(w), 'watcher');
});

t('a reply between two watcher runs breaks the run; tool rows never fold', () => {
  const items = buildItems([
    watchFire('a'), watchFire('b'), watchFire('c'),
    { type: 'assistant_text', text: 'a reply cuts the run' },
    watchFire('d'), watchFire('e'), watchFire('f'), watchFire('g'),
    { type: 'tool_call', id: 't1', name: 'Read', args: { file_path: '/a' }, ts: 'x' },
    { type: 'tool_result', id: 't1', ok: true, snippet: 'ok' }
  ]);
  const folded = foldNoise(items);
  // two runs, split by the reply — and neither a tool call nor its result echo
  // ever folds, even sitting right behind a watcher run
  assert.deepEqual(folded.map((i) => i.kind), ['fold', 'reply', 'fold', 'tool']);
  assert.equal(folded[0].count, 3);
  assert.equal(folded[2].count, 4);
});

t('the fold advances the turn ordinal by its whole run', () => {
  // turnChatHistory lines a turn up with its `done` by counting you/peer items;
  // a fold replaces N user_message rows, so its count is what must be added.
  // The event side counts EVERY user_message including the three watch fires,
  // so 'turn 2' is event #4 — the fold must land it there, not at #1.
  // Watch fires ride user_message too, so the event-side ordinal counts them.
  const events = [
    you('turn 1'),
    watchFire('a'), watchFire('b'), watchFire('c'),
    you('turn 2'),
    { type: 'assistant_text', text: 'answered' },
    { type: 'done', ts: '2026-07-31T13:00:00+0000' }
  ];
  const doneTs = [];
  let openIdx = -1;
  for (const ev of events) {
    if (ev.type === 'user_message') openIdx = doneTs.push(null) - 1;
    else if (ev.type === 'done' && openIdx >= 0 && doneTs[openIdx] === null) doneTs[openIdx] = ev.ts;
  }
  assert.equal(doneTs.length, 5, 'watch fires are user_message events too');
  let ordinal = -1;
  let sawTurn2At = -1;
  for (const it of foldNoise(buildItems(events))) {
    ordinal += it.kind === 'fold' ? it.count : it.kind === 'you' || it.kind === 'peer' ? 1 : 0;
    if (it.kind === 'you' && it.text === 'turn 2') sawTurn2At = ordinal;
  }
  assert.equal(sawTurn2At, 4, 'turn 2 still maps to its own done');
  assert.equal(doneTs[sawTurn2At], '2026-07-31T13:00:00+0000');
  // And the naive count (folding without `count`) would have landed it at 1 —
  // the wrong turn, the bug this guard exists for.
  let naive = -1;
  for (const it of foldNoise(buildItems(events))) {
    if (it.kind === 'you' || it.kind === 'peer') naive++;
    if (it.kind === 'you' && it.text === 'turn 2') break;
  }
  assert.equal(naive, 1);
});

t('foldNoise on an empty or missing list is inert', () => {
  assert.deepEqual(foldNoise([]), []);
  assert.deepEqual(foldNoise(null), []);
  // an unknown row is left exactly where it is (foldNoise never reorders)
  assert.deepEqual(foldNoise([null, undefined]), [null, undefined]);
});

t('buildItems turns a reasoning event into its own thinking item, in order', () => {
  const items = buildItems([
    { type: 'user_message', text: 'go', ts: '2026-09-14T10:00:00+0000' },
    { type: 'reasoning', text: 'plan the edit', ts: '2026-09-14T10:00:01+0000' },
    { type: 'tool_call', id: 't1', name: 'Read', args: { file_path: '/tmp/a' } },
    { type: 'tool_result', id: 't1', ok: true },
    { type: 'reasoning', text: 'now reply', ts: '2026-09-14T10:00:03+0000' },
    { type: 'assistant_text', text: 'first' },
    { type: 'assistant_text', text: ' second' },
    { type: 'done', reason: 'result' },
  ]);
  assert.deepEqual(items.map((i) => i.kind), ['you', 'thinking', 'tool', 'thinking', 'reply']);
  assert.equal(items[1].text, 'plan the edit');
  assert.equal(items[1].ts, '2026-09-14T10:00:01+0000');
  // adjacent replies still merge across nothing but each other
  assert.equal(items[4].text, 'first second');
  // a thinking block is not a reply: a tool-only turn still gets its marker
  const silent = buildItems([
    { type: 'user_message', text: 'go' },
    { type: 'reasoning', text: 'hmm' },
    { type: 'done', reason: 'result' },
  ]);
  assert.deepEqual(silent.map((i) => i.kind), ['you', 'thinking', 'turnend']);
  // whitespace-only thinking is dropped, like whitespace-only replies
  assert.deepEqual(buildItems([{ type: 'reasoning', text: '\n\n' }]), []);
  // and it never folds as noise
  assert.equal(noiseLabel({ kind: 'thinking', text: 'x' }), '');
});

t('isAutomated and isRealYou characterize automated vs real user messages', () => {
  // watch and delegation watch
  assert.equal(isAutomated('[watch: Keeper woken] 🔔 condition met'), true);
  assert.equal(isRealYou(you('[watch: Keeper woken] 🔔 condition met')), false);
  assert.equal(isAutomated("[delegation watch: 'Ops' (pulse abc)] working"), true);
  assert.equal(isRealYou(you("[delegation watch: 'Ops' (pulse abc)] working")), false);

  // peer message
  assert.equal(isAutomated("[message from 'x' (pulse abc123)] hello"), true);
  assert.equal(isRealYou(you("[message from 'x' (pulse abc123)] hello")), false);

  // [pulsar ...] daemon injection (handoff, loop-guard, etc.)
  assert.equal(isAutomated('[pulsar] context handoff requested'), true);
  assert.equal(isRealYou(you('[pulsar] context handoff requested')), false);
  assert.equal(isAutomated('[pulsar loop-guard] steer'), true);
  assert.equal(isRealYou(you('[pulsar loop-guard] steer')), false);

  // generic [label] opener (e.g. question answer, inbox item push, or user's own leading bracket)
  assert.equal(isAutomated('[Alex answered your question q1] Q: a? A: yes'), true);
  assert.equal(isRealYou(you('[Alex answered your question q1] Q: a? A: yes')), false);
  assert.equal(isAutomated('[note] user typed a bracketed prefix'), true);
  assert.equal(isRealYou(you('[note] user typed a bracketed prefix')), false);

  // [<owner> replied via Inbox ...] exception — counts as real YOU
  assert.equal(isAutomated('[Alex replied via Inbox] ship it'), false);
  assert.equal(isRealYou(you('[Alex replied via Inbox] ship it')), true);
  assert.equal(isAutomated('[the user replied via Inbox] ship it'), false);
  assert.equal(isRealYou(you('[the user replied via Inbox] ship it')), true);

  // plain text and attachments
  assert.equal(isAutomated('hello there'), false);
  assert.equal(isRealYou(you('hello there')), true);
  assert.equal(isRealYou(you('')), false);
  assert.equal(isRealYou(you('', { attachments: [{ filename: 'a.png' }] })), true);
  assert.equal(isRealYou({ type: 'assistant_text', text: 'hello' }), false);
});

t('classifyUserMessage classifies watcher, peer, and you messages with automated flag', () => {
  assert.deepEqual(
    classifyUserMessage(you('[watch: Keeper woken] 🔔 condition met')),
    { kind: 'watcher', automated: true, from: 'watcher', fromId: null, body: '[watch: Keeper woken] 🔔 condition met' }
  );
  assert.deepEqual(
    classifyUserMessage(you("[delegation watch: 'Ops' (pulse abc)] working")),
    { kind: 'watcher', automated: true, from: 'watcher', fromId: null, body: "[delegation watch: 'Ops' (pulse abc)] working" }
  );
  assert.deepEqual(
    classifyUserMessage(you("[message from 'Scout' (pulse 11111111222222223333333344444444)] bench round 2 is ready")),
    { kind: 'peer', automated: true, from: 'Scout', fromId: '11111111222222223333333344444444', body: 'bench round 2 is ready' }
  );
  assert.deepEqual(
    classifyUserMessage(you('[pulsar] handoff')),
    { kind: 'you', automated: true, from: null, fromId: null, body: '[pulsar] handoff' }
  );
  assert.deepEqual(
    classifyUserMessage(you('[label] something')),
    { kind: 'you', automated: true, from: null, fromId: null, body: '[label] something' }
  );
  assert.deepEqual(
    classifyUserMessage(you('[Alex replied via Inbox] looks good')),
    { kind: 'you', automated: false, from: null, fromId: null, body: '[Alex replied via Inbox] looks good' }
  );
  assert.deepEqual(
    classifyUserMessage(you('plain message')),
    { kind: 'you', automated: false, from: null, fromId: null, body: 'plain message' }
  );
});

// Origin fields first, text rules as the fallback. Each row: the event
// as the runner writes it (origin/via[/from_*] + the text the daemon wrote),
// the same text without fields (what old logs hold), and the result for both
// the bubble (classifyUserMessage + buildItems) and the strip (isRealYou).
const PID = '11111111222222223333333344444444';
const R26_ROWS = [
  // [label, fields, text, {kind, automated, from, fromId, body}, realYou]
  ['user/inbox_reply', { origin: 'user', via: 'inbox_reply' }, '[Alex replied via Inbox] ship it',
    { kind: 'you', automated: false, from: null, fromId: null, body: '[Alex replied via Inbox] ship it' }, true],
  ['user/question', { origin: 'user', via: 'question' }, '[Alex answered your question q1] Q: a? A: yes',
    { kind: 'you', automated: true, from: null, fromId: null, body: '[Alex answered your question q1] Q: a? A: yes' }, false],
  ['peer/pulse', { origin: 'peer', via: 'pulse', from_pulse_id: PID, from_title: 'Scout' }, `[message from 'Scout' (pulse ${PID})] bench ready`,
    { kind: 'peer', automated: true, from: 'Scout', fromId: PID, body: 'bench ready' }, false],
  ['peer/first_message', { origin: 'peer', via: 'first_message', from_pulse_id: PID, from_title: 'Scout' }, `[message from 'Scout' (pulse ${PID})] do the thing`,
    { kind: 'peer', automated: true, from: 'Scout', fromId: PID, body: 'do the thing' }, false],
  ['peer/schedule', { origin: 'peer', via: 'schedule', from_pulse_id: PID, from_title: 'Scout' }, `[message from 'Scout' (pulse ${PID})] check it`,
    { kind: 'peer', automated: true, from: 'Scout', fromId: PID, body: 'check it' }, false],
  ['daemon/watch', { origin: 'daemon', via: 'watch' }, "[delegation watch: 'Ops' (pulse abc)] working",
    { kind: 'watcher', automated: true, from: 'watcher', fromId: null, body: "[delegation watch: 'Ops' (pulse abc)] working" }, false],
  ['daemon/condwatch', { origin: 'daemon', via: 'condwatch' }, '[watch: notes] 🔔 condition met',
    { kind: 'watcher', automated: true, from: 'watcher', fromId: null, body: '[watch: notes] 🔔 condition met' }, false],
  ['daemon/handoff', { origin: 'daemon', via: 'handoff' }, '[pulsar] context handoff requested',
    { kind: 'you', automated: true, from: null, fromId: null, body: '[pulsar] context handoff requested' }, false],
  ['daemon/loopguard', { origin: 'daemon', via: 'loopguard' }, '[pulsar loop-guard] steer',
    { kind: 'you', automated: true, from: null, fromId: null, body: '[pulsar loop-guard] steer' }, false],
];

t('origin fields: each producer row classifies the same with fields and without (legacy text rules)', () => {
  for (const [label, fields, text, want, realYou] of R26_ROWS) {
    assert.deepEqual(classifyUserMessage(you(text, fields)), want, `${label} with fields`);
    assert.equal(isRealYou(you(text, fields)), realYou, `${label} strip, with fields`);
    assert.deepEqual(classifyUserMessage(you(text)), want, `${label} legacy`);
    assert.equal(isRealYou(you(text)), realYou, `${label} strip, legacy`);
    assert.deepEqual(
      buildItems([you(text, fields)]).map(({ ts, ...i }) => i),
      buildItems([you(text)]).map(({ ts, ...i }) => i),
      `${label} bubble`
    );
  }
});

t('origin fields: the fields decide when the text alone would not', () => {
  // title with both quote kinds: the text regex gets the repr escapes, the field is exact
  const title = `Alex's "pulse"`;
  const c = classifyUserMessage(you(`[message from ${JSON.stringify(title)} (pulse ${PID})] hi`,
    { origin: 'peer', via: 'pulse', from_pulse_id: PID, from_title: title }));
  assert.equal(c.from, title);
  // peer text without the prefix is still a peer
  assert.deepEqual(
    classifyUserMessage(you('no prefix here', { origin: 'peer', via: 'pulse', from_pulse_id: PID, from_title: 'Scout' })),
    { kind: 'peer', automated: true, from: 'Scout', fromId: PID, body: 'no prefix here' }
  );
  // handoff / question with a non-bracketed text are automated by field
  assert.equal(classifyUserMessage(you('seed text', { origin: 'daemon', via: 'handoff' })).automated, true);
  assert.equal(classifyUserMessage(you('plain answer', { origin: 'user', via: 'question' })).automated, true);
  assert.equal(isRealYou(you('plain answer', { origin: 'user', via: 'question' })), false);
  // inbox reply without the bracket is still the user's
  assert.equal(isRealYou(you('ship it', { origin: 'user', via: 'inbox_reply' })), true);
  // buildItems: a field-only watcher / peer row
  const w = buildItems([you('plain note', { origin: 'daemon', via: 'watch' })])[0];
  assert.equal(w.kind, 'peer');
  assert.equal(w.from, 'watcher');
});

t('origin fields: raw-text rows (user/pane, user/first_message, daemon/schedule) keep the legacy text result', () => {
  const texts = [
    '[label] user typed brackets',
    '[watch: x]',
    `[message from 'x' (pulse ab12)] hi`,
    '[Alex replied via Inbox] typed by hand',
    'plain words',
  ];
  for (const fields of [
    { origin: 'user', via: 'pane' },
    { origin: 'user', via: 'first_message' },
    { origin: 'daemon', via: 'schedule' },
  ]) {
    for (const text of texts) {
      assert.deepEqual(classifyUserMessage(you(text, fields)), classifyUserMessage(you(text)), `${fields.via} ${text}`);
      assert.equal(isRealYou(you(text, fields)), isRealYou(you(text)), `${fields.via} ${text} strip`);
    }
  }
});

t('origin fields: unknown or incomplete fields fall back to the text rules', () => {
  const peerText = `[message from 'Scout' (pulse ${PID})] hi`;
  const legacy = classifyUserMessage(you(peerText));
  for (const fields of [
    { origin: 'peer' },                                  // via missing
    { origin: 'peer', via: 'carrier-pigeon', from_pulse_id: PID, from_title: 'Scout' },
    { origin: 'peer', via: 'pulse' },                    // from_* missing
    { origin: 'mars', via: 'pulse' },
    { via: 'pulse' },                                    // origin missing
  ]) {
    assert.deepEqual(classifyUserMessage(you(peerText, fields)), legacy, JSON.stringify(fields));
  }
  assert.deepEqual(
    classifyUserMessage(you('[watch: x] hi', { origin: 'daemon', via: 'mystery' })),
    classifyUserMessage(you('[watch: x] hi'))
  );
  assert.deepEqual(classifyUserMessage(you('hello', { origin: 'user' })), classifyUserMessage(you('hello')));
});

t('a turn that ended on a provider error is marked failed', () => {
  const ev = (type, extra = {}) => ({ type, ts: '2026-09-24T01:00:00Z', ...extra });
  const failed = _tm([ev('user_message'), ev('runner_error', { where: 'provider_error' }), ev('done')]);
  assert.equal(failed.lastFailed, true);
  const ok = _tm([ev('user_message'), ev('runner_error'), ev('assistant_text', { text: 'hi' }), ev('done')]);
  assert.equal(ok.lastFailed, false);
  const later = _tm([ev('user_message'), ev('runner_error'), ev('done'), ev('user_message'), ev('assistant_text'), ev('done')]);
  assert.equal(later.lastFailed, false);
});

t('provider errors read in plain words', () => {
  const line = _rel({ where: 'provider_error', reason: 'provider error: x', error: 'Request timed out.' });
  assert.match(line, /^The model didn't answer — its server timed out\./);
  assert.equal(_rel({ reason: 'cli exited 1' }), 'cli exited 1');
});

t('answerLine: a strip answer reads as a sentence, no hex id', () => {
  const raw = '[Sam answered your question 55555555666666667777777788888888] Q: QA1 strip test: pick one\nA: Blue';
  assert.equal(answerLine(raw), 'Sam answered “QA1 strip test: pick one” → Blue');
  const items = buildItems([{ type: 'user_message', text: raw, ts: 1 }]);
  assert.equal(items.find((i) => i.kind === 'you').text, 'Sam answered “QA1 strip test: pick one” → Blue');
  assert.equal(answerLine('plain words'), 'plain words');
  assert.equal(answerLine('[Sam dismissed your question 9b2c without an answer — decide yourself or move on] Q: Pick one'),
    'Sam dismissed “Pick one” without an answer');
});

t('a user stop closes the turn with a marker; mid-turn sends are tagged', () => {
  const items = buildItems([
    { type: 'user_message', text: 'send X', ts: 1 },
    { type: 'tool_use', id: 'a', name: 'Bash', input: { command: 'sleep 9' }, ts: 2 },
    { type: 'user_message', text: 'also Y', injected: true, ts: 3 },
    { type: 'pulse_stopped', by: 'user', ts: 4 },
  ]);
  assert.equal(items.filter((i) => i.kind === 'you')[1].injected, true);
  assert.equal(items.filter((i) => i.kind === 'you')[0].injected, false);
  assert.ok(items.some((i) => i.kind === 'turnend' && i.interrupted));
  assert.ok(items.some((i) => i.kind === 'tool' && /stopped by you/.test(i.line)));
});

t('readableThinking: no bold marks, one summary per line', () => {
  assert.equal(readableThinking('**Planning to inspect image****Prioritizing the request**'),
    'Planning to inspect image\nPrioritizing the request');
  assert.equal(readableThinking('plain text'), 'plain text');
});


t('buildItems folds the reply to a handoff-notes ask, not the next answer', () => {
  const items = buildItems([
    { type: 'user_message', text: '[pulsar] A fresh session was requested. Write your handoff notes.' },
    { type: 'assistant_text', text: 'Notes: working on X' },
    { type: 'done' },
    { type: 'user_message', text: 'hi' },
    { type: 'assistant_text', text: 'Hello' },
    { type: 'done' }
  ]);
  const replies = items.filter((i) => i.kind === 'reply');
  assert.equal(replies[0].notes, true);
  assert.equal(replies[1].notes, undefined);
});

t('watchSummary: a fired watch reads as one line', () => {
  const raw = '[watch: pulsar-wait: file /home/user/notes.md] 🔔 condition met — exists: True, mtime: 1790217000. (Watch id abc, mode edge.)';
  assert.equal(watchSummary(raw), 'Watch fired: file notes.md');
  assert.equal(watchSummary('[delegation watch] x is archived'), '');
  assert.equal(watchSummary('[watch: file] 🔔 condition met — exists: True, path: /var/lib/app/flag.txt, mtime: 1790. (Watch id a, mode edge.)'),
    'Watch fired: file flag.txt');
});

t('a scheduled wake is a Reminder line, not a YOU bubble', () => {
  assert.equal(scheduleSummary('[pulsar-wait] check the photos ticket'), 'Reminder: check the photos ticket');
  assert.equal(scheduleSummary('[pulsar-wait] Reminder: check it'), 'Reminder: check it');
  const items = buildItems([
    { type: 'user_message', text: '[pulsar-wait] check the photos ticket', msg_file: 'sched-20260924T043600Z-ab.json', ts: 't1' },
    { type: 'user_message', text: 'Check the backlog', msg_file: 'sched-20260924T050000Z-cd.json', ts: 't2' },
    { type: 'user_message', text: 'hello', msg_file: '20260924T043700Z-ef.json', ts: 't3' }
  ]);
  const kinds = items.filter((i) => i.kind === 'peer' || i.kind === 'you').map((i) => [i.kind, i.summary ?? i.text]);
  assert.deepEqual(kinds, [
    ['peer', 'Reminder: check the photos ticket'],
    ['peer', 'Scheduled: Check the backlog'],
    ['you', 'hello']
  ]);
});

t('a command killed by ■ stop reads stopped, not failed', async () => {
  const { turnStats } = await import('./toolcards.js');
  const call = (id) => ({ type: 'tool_call', id, name: 'Bash', args: { command: 'sleep 99' } });
  const killed = (id) => ({ type: 'tool_result', id, ok: false, snippet: 'Exit code 137', exit_code: 137 });
  for (const order of ['result-first', 'stop-first']) {
    const evs = [{ type: 'user_message', text: 'go' }, call('a'),
      { type: 'tool_result', id: 'a', ok: false, snippet: 'Exit code 2', exit_code: 2 }, call('b')];
    if (order === 'result-first') evs.push(killed('b'), { type: 'pulse_stopped' });
    else evs.push({ type: 'pulse_stopped' }, killed('b'));
    const tools = buildItems(evs).filter((i) => i.kind === 'tool' && i.id);
    const b = tools.find((i) => i.id === 'b');
    assert.equal(b.stopped, true, order);
    assert.equal(b.err, false, order);
    assert.equal(tools.find((i) => i.id === 'a').err, true, order); // a real failure stays
    assert.equal(turnStats(tools).failed, 1, order);
  }
});

t('a codex/Luna shell call reads as its command, no wrapper', () => {
  assert.equal(toolCallLine({ type: 'tool_call', name: 'command_execution', args: { command: '/bin/bash -lc "mail-cli search invoice"' } }),
    'mail-cli search invoice');
  assert.equal(toolCallLine({ type: 'tool_call', name: 'bash', args: { command: 'ls -la' } }), 'ls -la');
});

t('stop marks only the command it killed; a finished turn is untouched', () => {
  const call = (id) => ({ type: 'tool_call', id, name: 'Bash', args: { command: 'x' } });
  const res = (id, code) => ({ type: 'tool_result', id, ok: code === 0, snippet: `Exit code ${code}`, exit_code: code });
  // An earlier real OOM (137) in the stopped turn stays a failure.
  let tools = buildItems([{ type: 'user_message', text: 'go' }, call('a'), res('a', 137), call('b'), res('b', 137), { type: 'pulse_stopped' }])
    .filter((i) => i.kind === 'tool' && i.id);
  assert.equal(tools.find((i) => i.id === 'a').err, true);
  assert.equal(tools.find((i) => i.id === 'b').stopped, true);
  // Stop after the turn already finished: nothing is relabelled.
  tools = buildItems([{ type: 'user_message', text: 'go' }, call('c'), res('c', 137), { type: 'done' }, { type: 'pulse_stopped' }])
    .filter((i) => i.kind === 'tool' && i.id);
  assert.equal(tools.find((i) => i.id === 'c').err, true);
  assert.ok(!tools.find((i) => i.id === 'c').stopped);
});

t('shell unwrap handles flag variants and leaves multi-quote commands alone', () => {
  const line = (command) => toolCallLine({ type: 'tool_call', name: 'command_execution', args: { command } });
  assert.equal(line('bash -ec "make test"'), 'make test');
  assert.equal(line('/bin/bash -l -c "echo \\"hi\\""'), 'echo "hi"');
  assert.equal(line('bash -lc "a" && "b"'), '"a" && "b"');
});

t('youHistoryRows: newest first, turn clock, tool count, reply opening, numbering', () => {
  const ev = (type, ts, extra = {}) => ({ type, ts: `2026-09-01T10:${ts}:00+0000`, ...extra });
  const rows = youHistoryRows([
    ev('user_message', '00', { text: 'first ask' }),
    ev('tool_call', '01', { id: 'a', name: 'Bash', args: { command: 'ls' } }),
    ev('tool_result', '01', { id: 'a', ok: true, snippet: 'x' }),
    ev('assistant_text', '02', { text: '  opening of reply  ' }),
    ev('assistant_text', '03', { text: 'second part' }),
    ev('done', '05'),
    ev('user_message', '10', { text: 'second ask', attachments: [{ filename: 'a.png', url: '/u/a.png' }] }),
  ]);
  assert.equal(rows.length, 2);
  // newest first, n counts oldest = 1
  assert.deepEqual(rows.map((r) => r.n), [2, 1]);
  assert.deepEqual(rows.map((r) => r.text), ['second ask', 'first ask']);
  assert.equal(rows[1].tools, 1);
  assert.equal(rows[1].reply.startsWith('opening of reply'), true);
  assert.equal(rows[1].took, 5 * 60 * 1000);
  // no done after the second ask: never finished -> NaN, no reply
  assert.ok(Number.isNaN(rows[0].took));
  assert.equal(rows[0].tools, 0);
  assert.equal(rows[0].reply, '');
  assert.equal(rows[0].attachments.length, 1);
});

t('youHistoryRows: automated and peer messages are skipped but keep later turn clocks aligned', () => {
  const ev = (type, ts, extra = {}) => ({ type, ts: `2026-09-01T10:${ts}:00+0000`, ...extra });
  const rows = youHistoryRows([
    ev('user_message', '00', { text: '[message from \'Scout\' (pulse abc-123)] hi' }),
    ev('done', '01'),
    ev('user_message', '02', { text: '[watch: tests] fired' }),
    ev('done', '03'),
    ev('user_message', '04', { text: '[pulsar] model switch' }),
    ev('done', '05'),
    ev('user_message', '06', { text: 'the real one' }),
    ev('done', '09'),
  ]);
  assert.equal(rows.length, 1);
  assert.equal(rows[0].text, 'the real one');
  assert.equal(rows[0].n, 1);
  assert.equal(rows[0].took, 3 * 60 * 1000); // its own done, not an earlier one
});

t('youHistoryRows: a reply stops at the next inbound message; empty-text no-attachment rows are dropped', () => {
  const ev = (type, ts, extra = {}) => ({ type, ts: `2026-09-01T10:${ts}:00+0000`, ...extra });
  const rows = youHistoryRows([
    ev('user_message', '00', { text: 'one' }),
    ev('user_message', '01', { text: '' }),
    ev('user_message', '02', { text: 'two' }),
    ev('assistant_text', '03', { text: 'only for two' }),
  ]);
  assert.deepEqual(rows.map((r) => r.text), ['two', 'one']);
  assert.equal(rows[1].reply, '');
  assert.equal(rows[0].reply, 'only for two');
  assert.deepEqual(youHistoryRows([]), []);
});

t('keyItems preserves keys for tail items when older events are prepended', () => {
  const mk = (i) => [
    { type: 'user_message', text: `msg ${i}`, ts: `2026-09-01T10:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}+0000` },
    { type: 'reasoning', text: `THINKING_${i}`, ts: `2026-09-01T10:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}+0000` },
    { type: 'tool_call', id: `tc_${i}`, name: 'Bash', args: { command: `echo ${i}` }, ts: `2026-09-01T10:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}+0000` },
    { type: 'tool_result', id: `tc_${i}`, ok: true, snippet: String(i), ts: `2026-09-01T10:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}+0000` },
    { type: 'assistant_text', text: `reply ${i}`, ts: `2026-09-01T10:${String(Math.floor(i / 60)).padStart(2, '0')}:${String(i % 60).padStart(2, '0')}+0000` },
  ];
  const older = Array.from({ length: 10 }, (_, i) => mk(i)).flat();
  const tail = Array.from({ length: 5 }, (_, i) => mk(10 + i)).flat();
  const tailItems = keyItems(buildItems(tail));
  const fullItems = keyItems(buildItems([...older, ...tail]));
  assert.deepEqual(
    fullItems.slice(-tailItems.length).map((it) => it.key),
    tailItems.map((it) => it.key)
  );
  // Every key in the full list is unique.
  assert.equal(new Set(fullItems.map((it) => it.key)).size, fullItems.length);
});

t('keyItems disambiguates identical events in the same second and keeps reply/tool keys stable as they grow', () => {
  const ts = '2026-09-01T10:00:00+0000';
  const dup = keyItems(buildItems([
    { type: 'reasoning', text: 'same', ts },
    { type: 'reasoning', text: 'same', ts },
  ]));
  assert.notEqual(dup[0].key, dup[1].key);
  const reply1 = keyItems(buildItems([{ type: 'assistant_text', text: 'hello', ts }]));
  const reply2 = keyItems(buildItems([
    { type: 'assistant_text', text: 'hello', ts },
    { type: 'assistant_text', text: ' world', ts: '2026-09-01T10:00:01+0000' },
  ]));
  assert.equal(reply1[0].key, reply2[0].key);
  const toolPending = keyItems(buildItems([{ type: 'tool_call', id: 'c1', name: 'Bash', args: { command: 'ls' }, ts }]));
  const toolDone = keyItems(buildItems([
    { type: 'tool_call', id: 'c1', name: 'Bash', args: { command: 'ls' }, ts },
    { type: 'tool_result', id: 'c1', ok: true, snippet: 'a.txt', ts: '2026-09-01T10:00:01+0000' },
  ]));
  assert.equal(toolPending[0].key, toolDone[0].key);
});

console.log(`render.test: ${passed} passed`);
