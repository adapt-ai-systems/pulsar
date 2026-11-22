import assert from 'node:assert/strict';
import { cardFor, lineDiff, turnStats, lastTurnItems, exitCodeFrom, shortPath, foldTodos, todoGroups, mergeTaskEvents, isTaskEvent } from './toolcards.js';

// lineDiff
{
  const d = lineDiff('a\nb\nc', 'a\nB\nc\nd');
  assert.equal(d.added, 2);
  assert.equal(d.removed, 1);
  assert.deepEqual(d.rows.map((r) => r.t + r.s), [' a', '-b', '+B', ' c', '+d']);
  assert.deepEqual(lineDiff('', ''), { rows: [], added: 0, removed: 0 });
  const e = lineDiff('x\n', 'x\ny\n');
  assert.equal(e.added, 1); assert.equal(e.removed, 0);
}

// pi edit (lowercase name, file_path) and Claude Edit (Title case) -> same card
{
  const pi = cardFor({ name: 'edit', args: { file_path: '/a/b/c.py', old_string: 'x = 1\ny = 2', new_string: 'x = 1\ny = 3' } });
  const cl = cardFor({ name: 'Edit', input: { file_path: '/a/b/c.py', old_string: 'x = 1\ny = 2', new_string: 'x = 1\ny = 3' } });
  assert.equal(pi.kind, 'edit'); assert.equal(pi.added, 1); assert.equal(pi.removed, 1); assert.equal(pi.hunks.length, 1);
  assert.deepEqual(pi, cl);
  assert.equal(shortPath(pi.path), 'b/c.py');
}

// pi OMP edit: {path, edits:[{oldText,newText}]} (observed on pi-ds41-flash 2026-09-12)
{
  const m = cardFor({ name: 'edit', args: { path: '/docs/SPEC.md', edits: [
    { oldText: 'a\nb', newText: 'a\nb\nc' },
    { oldText: 'x', newText: 'y\nz' }
  ] } });
  assert.equal(m.kind, 'edit'); assert.equal(m.path, '/docs/SPEC.md');
  assert.equal(m.hunks.length, 2); assert.equal(m.added, 3); assert.equal(m.removed, 1);
  const me = cardFor({ name: 'MultiEdit', args: { file_path: '/f', edits: [{ old_string: 'q', new_string: 'r' }] } });
  assert.equal(me.hunks.length, 1); assert.equal(me.added, 1);
  const empty = cardFor({ name: 'edit', args: { path: '/f' } });
  assert.equal(empty.hunks.length, 0);
}

// pi write uses `path`; Claude Write uses `file_path`
{
  const w = cardFor({ name: 'write', args: { path: '/x/state.json', content: '{\n  "a": 1\n}\n' } });
  assert.equal(w.kind, 'write'); assert.equal(w.lines, 3); assert.equal(w.added, 3); assert.equal(w.path, '/x/state.json');
  const w2 = cardFor({ name: 'Write', args: { file_path: '/x/y', content: 'one' } });
  assert.equal(w2.lines, 1);
}

// shell across vessels
{
  for (const name of ['bash', 'Bash', 'command_execution', 'run_shell_command', 'run_terminal_command']) {
    const c = cardFor({ name, args: { command: 'ls -la', description: 'list' } });
    assert.equal(c.kind, 'shell', name); assert.equal(c.command, 'ls -la'); assert.equal(c.description, 'list');
  }
  assert.equal(cardFor({ name: 'Bash', description: 'top-level desc', args: { command: 'x' } }).description, 'top-level desc');
}

// read / search / unknown
{
  const r = cardFor({ name: 'read', args: { file_path: '/p/app.py', offset: 10, limit: 50 } });
  assert.equal(r.kind, 'read'); assert.equal(r.offset, 10); assert.equal(r.limit, 50);
  const g = cardFor({ name: 'grep', args: { pattern: 'foo|bar', glob: '*.py' } });
  assert.equal(g.kind, 'search'); assert.equal(g.query, 'foo|bar');
  assert.equal(cardFor({ name: 'ScheduleWakeup', args: { delaySeconds: 5 } }), null);
  assert.equal(cardFor({ name: '', args: {} }), null);
}

// exit code parsing for vessels that don't expose it
{
  assert.equal(exitCodeFrom('some output\nExit code: 2'), 2);
  assert.equal(exitCodeFrom('exit status 1'), 1);
  assert.equal(exitCodeFrom('all good'), null);
}

// turnStats + lastTurnItems
{
  const items = [
    { kind: 'you', text: 'old' },
    { kind: 'tool', card: { kind: 'edit', path: '/old.py', added: 9, removed: 9 } },
    { kind: 'you', text: 'do it' },
    { kind: 'tool', card: { kind: 'read', path: '/a.py' } },
    { kind: 'tool', card: { kind: 'edit', path: '/a.py', added: 3, removed: 1 } },
    { kind: 'tool', card: { kind: 'edit', path: '/a.py', added: 2, removed: 0 } },
    { kind: 'tool', card: { kind: 'write', path: '/b.py', added: 10, removed: 0 } },
    { kind: 'tool', card: { kind: 'shell', command: 'pytest' }, err: true },
    { kind: 'tool', card: { kind: 'shell', command: 'ls' } },
    { kind: 'tool', card: null, err: true },
    { kind: 'reply', text: 'done' }
  ];
  const s = turnStats(lastTurnItems(items));
  assert.equal(s.files.length, 2);
  assert.equal(s.added, 15); assert.equal(s.removed, 1);
  assert.equal(s.commands, 2); assert.equal(s.failed, 2); assert.equal(s.reads, 1);
  assert.equal(s.files.find((f) => f.path === '/b.py').created, true);
  assert.equal(s.files.find((f) => f.path === '/a.py').created, false);
}

// foldTodos — todo_write replaces (qwen/grok/codex/pi), TaskCreate/TaskUpdate patch (claude)
{
  assert.equal(foldTodos([{ type: 'tool_call', name: 'bash', args: {} }]), null);
  const t = foldTodos([
    { type: 'tool_call', name: 'todo_write', ts: 't1', args: { todos: [{ id: 'a', content: 'one', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', ts: 't2', args: { todos: [
      { id: 'a', content: 'one', status: 'completed', group: 'Core' },
      { id: 'b', content: 'two', status: 'in_progress', group: 'Core' },
      { id: 'c', content: 'three', status: 'pending', group: 'Docs' },
      { id: 'd', content: 'gone', status: 'cancelled' }
    ] } }
  ]);
  assert.equal(t.total, 3); assert.equal(t.done, 1); assert.equal(t.active.id, 'b'); assert.equal(t.ts, 't2');
  const g = todoGroups(t);
  assert.deepEqual(g.map((x) => [x.name, x.items.length, x.done]), [['Core', 2, 1], ['Docs', 1, 0]]);

  const c = foldTodos([
    { type: 'tool_call', name: 'TaskCreate', id: 'x1', ts: 't1', args: { subject: 'Fix it', activeForm: 'Fixing it' } },
    { type: 'tool_result', id: 'x1', ok: true, snippet: 'Task #7 created successfully: Fix it' },
    { type: 'tool_call', name: 'TaskCreate', id: 'x2', args: { subject: 'Test it' } },
    { type: 'tool_result', id: 'x2', ok: true, snippet: 'Task #8 created successfully: Test it' },
    { type: 'tool_call', name: 'TaskUpdate', ts: 't3', args: { taskId: '7', status: 'in_progress' } },
    { type: 'tool_call', name: 'TaskUpdate', args: { taskId: '8', status: 'deleted' } }
  ]);
  assert.equal(c.total, 1); assert.equal(c.items[0].id, '7'); assert.equal(c.active.activeForm, 'Fixing it');

  const p = foldTodos([{ type: 'tool_call', name: 'update_plan', args: { plan: [{ step: 's1', status: 'completed' }, { step: 's2', status: 'in_progress' }] } }]);
  assert.equal(p.done, 1); assert.equal(p.active.content, 's2');
}

// timestamps survive list rewrites: created on first sight, started on first
// in_progress, done on first completed, cleared if reopened
{
  const t = foldTodos([
    { type: 'tool_call', name: 'todo_write', ts: 'T1', args: { todos: [{ id: 'a', content: 'one', status: 'pending' }, { id: 'b', content: 'two', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', ts: 'T2', args: { todos: [{ id: 'a', content: 'one', status: 'in_progress' }, { id: 'b', content: 'two', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', ts: 'T3', args: { todos: [{ id: 'a', content: 'one', status: 'in_progress' }, { id: 'b', content: 'two', status: 'pending' }, { id: 'c', content: 'three', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', ts: 'T4', args: { todos: [{ id: 'a', content: 'one', status: 'completed' }, { id: 'b', content: 'two', status: 'in_progress' }, { id: 'c', content: 'three', status: 'pending' }] } }
  ]);
  const a = t.items.find((x) => x.id === 'a'), b = t.items.find((x) => x.id === 'b'), c = t.items.find((x) => x.id === 'c');
  assert.deepEqual([a.createdTs, a.startedTs, a.doneTs], ['T1', 'T2', 'T4']);
  assert.deepEqual([b.createdTs, b.startedTs, b.doneTs], ['T1', 'T4', null]);
  assert.deepEqual([c.createdTs, c.startedTs, c.doneTs], ['T3', null, null]);
  assert.equal(t.ts, 'T4');
  // no ids: carried by content
  const u = foldTodos([
    { type: 'tool_call', name: 'todo_write', ts: 'T1', args: { todos: [{ content: 'x', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', ts: 'T2', args: { todos: [{ content: 'y', status: 'pending' }, { content: 'x', status: 'completed' }] } }
  ]);
  assert.equal(u.items.find((i) => i.content === 'x').createdTs, 'T1');
  assert.equal(u.items.find((i) => i.content === 'x').doneTs, 'T2');
  // claude: started/done stamped from TaskUpdate events
  const cl = foldTodos([
    { type: 'tool_call', name: 'TaskCreate', id: 'x1', ts: 'T1', args: { subject: 'Fix it' } },
    { type: 'tool_result', id: 'x1', ok: true, snippet: 'Task #7 created successfully: Fix it', ts: 'T1b' },
    { type: 'tool_call', name: 'TaskUpdate', ts: 'T2', args: { taskId: '7', status: 'in_progress' } },
    { type: 'tool_call', name: 'TaskUpdate', ts: 'T3', args: { taskId: '7', status: 'completed' } }
  ]);
  assert.deepEqual([cl.items[0].createdTs, cl.items[0].startedTs, cl.items[0].doneTs], ['T1', 'T2', 'T3']);
}

// claude: a new session numbers its tasks from #1 again; the live list is the
// new session's, updates hit the new task, every id is unique — and the old
// session's items stay in the record (the task list is a permanent record)
{
  const t = foldTodos([
    { type: 'tool_call', name: 'TaskCreate', id: 'a1', ts: 'T1', args: { subject: 'old one' } },
    { type: 'tool_result', id: 'a1', ok: true, snippet: 'Task #1 created successfully: old one' },
    { type: 'tool_call', name: 'TaskCreate', id: 'a2', ts: 'T2', args: { subject: 'old two' } },
    { type: 'tool_result', id: 'a2', ok: true, snippet: 'Task #2 created successfully: old two' },
    { type: 'tool_call', name: 'TaskUpdate', ts: 'T3', args: { taskId: '1', status: 'completed' } },
    { type: 'tool_call', name: 'TaskCreate', id: 'b1', ts: 'T4', args: { subject: 'new one' } },
    { type: 'tool_result', id: 'b1', ok: true, snippet: 'Task #1 created successfully: new one' },
    { type: 'tool_call', name: 'TaskUpdate', ts: 'T5', args: { taskId: '1', status: 'in_progress' } }
  ]);
  assert.deepEqual(t.items.map((i) => [i.id, i.content, i.status]), [['1', 'new one', 'in_progress']]);
  assert.deepEqual([t.items[0].createdTs, t.items[0].startedTs, t.items[0].doneTs], ['T4', 'T5', null]);
  assert.equal(t.record.length, 2);
  assert.deepEqual(t.record[0].items.map((i) => [i.id, i.content, i.status]), [['1', 'old one', 'completed'], ['2', 'old two', 'pending']]);
  assert.deepEqual(t.record[1].items.map((i) => [i.id, i.content, i.status]), [['1', 'new one', 'in_progress']]);
  assert.deepEqual([t.recordTotal, t.recordDone, t.total, t.done], [3, 1, 1, 0]);
  // repeated ids from a todo_write list still render: ids are made unique
  const d = foldTodos([{ type: 'tool_call', name: 'todo_write', args: { todos: [
    { id: '1', content: 'a', status: 'pending' }, { id: '1', content: 'b', status: 'pending' }, { id: '1', content: 'c', status: 'pending' }
  ] } }]);
  assert.deepEqual(d.items.map((i) => i.id), ['1', '1~1', '1~2']);
}

// history + tail merge: no duplicates, order kept, timestamps from history win
{
  const h = [
    { type: 'tool_call', name: 'todo_write', id: 'c1', ts: 'T1', args: { todos: [{ id: 'a', content: 'one', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', id: 'c2', ts: 'T2', args: { todos: [{ id: 'a', content: 'one', status: 'completed' }] } }
  ];
  const tail = [
    { type: 'assistant_text', text: 'x' },
    { type: 'tool_call', name: 'todo_write', id: 'c2', ts: 'T2', args: { todos: [{ id: 'a', content: 'one', status: 'completed' }] } },
    { type: 'tool_call', name: 'todo_write', id: 'c3', ts: 'T3', args: { todos: [{ id: 'a', content: 'one', status: 'completed' }, { id: 'b', content: 'two', status: 'pending' }] } }
  ];
  const m = mergeTaskEvents(h, tail);
  assert.deepEqual(m.map((e) => e.id), ['c1', 'c2', 'c3']);
  const t = foldTodos(m);
  assert.equal(t.items.find((i) => i.id === 'a').doneTs, 'T2');
  assert.equal(t.items.find((i) => i.id === 'b').createdTs, 'T3');
  assert.equal(isTaskEvent(tail[2]), true); assert.equal(isTaskEvent(tail[0]), false);
  assert.equal(isTaskEvent({ type: 'tool_call', name: 'TaskUpdate' }), true);
}

// record: session events open segments; todo_write rewrites never lose an
// item; an empty session (no tasks) is not a segment; segments carry startTs
{
  const t = foldTodos([
    { type: 'session', session_id: 's1', ts: 'S1' },
    { type: 'tool_call', name: 'todo_write', ts: 'T1', args: { todos: [{ id: 'a', content: 'one', status: 'in_progress' }, { id: 'b', content: 'two', status: 'pending' }] } },
    { type: 'tool_call', name: 'todo_write', ts: 'T2', args: { todos: [{ id: 'a', content: 'one', status: 'completed' }] } }, // 'two' dropped by the tool
    { type: 'session', session_id: 's1', ts: 'S1b' }, // same session re-announced (every turn) — no new segment
    { type: 'session', session_id: 's2', ts: 'S2' }, // handoff
    { type: 'session', session_id: 's3', ts: 'S3' }, // s2 never made a task: reused, not an empty segment
    { type: 'tool_call', name: 'TaskCreate', id: 'c1', ts: 'T3', args: { subject: 'after handoff' } },
    { type: 'tool_result', id: 'c1', ok: true, snippet: 'Task #1 created successfully: after handoff' }
  ]);
  assert.deepEqual(t.items.map((i) => i.content), ['after handoff']);
  assert.equal(t.record.length, 2);
  assert.deepEqual(t.record.map((s) => s.startTs), ['S1', 'S2']);
  assert.deepEqual(t.record[0].items.map((i) => [i.content, i.status]), [['one', 'completed'], ['two', 'pending']]);
  assert.equal(t.record[0].done, 1);
  assert.deepEqual([t.recordTotal, t.recordDone], [3, 1]);
  // session events count as task events and survive the history/tail merge
  assert.equal(isTaskEvent({ type: 'session', session_id: 's9' }), true);
  const m = mergeTaskEvents([{ type: 'session', session_id: 's1', ts: 'S1' }], [{ type: 'session', session_id: 's1', ts: 'S1' }, { type: 'session', session_id: 's2', ts: 'S2' }]);
  assert.deepEqual(m.map((e) => e.session_id), ['s1', 's2']);
}

// claude announces its session id at the END of the first turn: the first
// TaskCreate after a handoff opens the segment (by Task #1 repeating), and
// the late session event adopts it instead of opening a third segment
{
  const t = foldTodos([
    { type: 'session', session_id: 's1', ts: 'S1' },
    { type: 'tool_call', name: 'TaskCreate', id: 'a1', ts: 'T1', args: { subject: 'old' } },
    { type: 'tool_result', id: 'a1', ok: true, snippet: 'Task #1 created successfully: old' },
    { type: 'tool_call', name: 'TaskCreate', id: 'b1', ts: 'T2', args: { subject: 'new' } },
    { type: 'tool_result', id: 'b1', ok: true, snippet: 'Task #1 created successfully: new' },
    { type: 'session', session_id: 's2', ts: 'S2' },
    { type: 'tool_call', name: 'TaskCreate', id: 'b2', ts: 'T3', args: { subject: 'newer' } },
    { type: 'tool_result', id: 'b2', ok: true, snippet: 'Task #2 created successfully: newer' }
  ]);
  assert.equal(t.record.length, 2);
  assert.deepEqual(t.record[1].items.map((i) => i.content), ['new', 'newer']);
  assert.deepEqual(t.items.map((i) => i.content), ['new', 'newer']);
  // a log that starts with tasks before any session event: one segment
  const u = foldTodos([
    { type: 'tool_call', name: 'todo_write', ts: 'T1', args: { todos: [{ id: 'a', content: 'x', status: 'pending' }] } },
    { type: 'session', session_id: 's1', ts: 'S1' }
  ]);
  assert.equal(u.record.length, 1);
}

console.log('toolcards.test.mjs: ok');

// agy (Antigravity) write_file sends the path as `Path` (observed 2026-11-10)
{
  const c = cardFor({ name: 'write_file', args: { Path: '/a/b/notes.md' } });
  assert.equal(c.kind, 'write');
  assert.equal(shortPath(c.path), 'b/notes.md');
}
