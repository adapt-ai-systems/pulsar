/**
 * Tool cards — the "visual tools" chat mode (shows files created, edited
 * snippets, and +N −M stats).
 *
 * Pure functions over the SAME tool_call/tool_result events every vessel
 * emits (events.py). Nothing here depends on which CLI produced the call:
 * tool names and arg keys are normalized (Claude: Edit/file_path, pi:
 * edit/path, codex: command_execution, ...) so a pi edit renders the same
 * diff card a Claude edit does. Anything unrecognized gets kind 'other' and
 * the pane falls back to the plain ToolLine.
 */

/** @typedef {'edit'|'write'|'read'|'shell'|'search'|'fetch'|'agent'|'other'} CardKind */

const NAME_KINDS = {
  edit: 'edit', search_replace: 'edit', str_replace_editor: 'edit', multiedit: 'edit', apply_patch: 'edit',
  write: 'write', write_file: 'write', create_file: 'write',
  read: 'read', read_file: 'read', view: 'read', cat: 'read',
  bash: 'shell', command_execution: 'shell', run_shell_command: 'shell', run_terminal_command: 'shell',
  shell: 'shell', exec: 'shell', execute: 'shell', terminal: 'shell',
  grep: 'search', glob: 'search', search: 'search', find: 'search', list_dir: 'search', ls: 'search',
  web_search: 'search', web_fetch: 'fetch', webfetch: 'fetch', fetch: 'fetch',
  task: 'agent', agent: 'agent'
};

/** @param {any} args */
export function pathOf(args) {
  if (!args || typeof args !== 'object') return '';
  for (const k of ['file_path', 'path', 'filePath', 'filename', 'file', 'target_file', 'notebook_path', 'Path']) {
    if (typeof args[k] === 'string' && args[k]) return args[k];
  }
  return '';
}

/** Short display form: last two path segments. */
export function shortPath(p) {
  if (!p) return '';
  const parts = String(p).replace(/\/+$/, '').split('/');
  return parts.length <= 2 ? parts.join('/') : parts.slice(-2).join('/');
}

function countLines(s) {
  if (!s) return 0;
  const n = s.split('\n').length;
  return s.endsWith('\n') ? n - 1 : n;
}

/**
 * Line diff of two strings via LCS. Edit strings are small (a hunk), so an
 * O(n*m) table is fine; anything past MAX_CELLS degrades to a plain
 * removed-then-added block rather than freezing the pane.
 * @returns {{rows: {t: '+'|'-'|' ', s: string}[], added: number, removed: number}}
 */
export function lineDiff(oldStr, newStr) {
  const a = oldStr ? String(oldStr).split('\n') : [];
  const b = newStr ? String(newStr).split('\n') : [];
  if (a.length && a[a.length - 1] === '' && oldStr.endsWith('\n')) a.pop();
  if (b.length && b[b.length - 1] === '' && newStr.endsWith('\n')) b.pop();
  const MAX_CELLS = 250_000;
  const rows = [];
  if (a.length * b.length > MAX_CELLS) {
    for (const s of a) rows.push({ t: '-', s });
    for (const s of b) rows.push({ t: '+', s });
    return { rows, added: b.length, removed: a.length };
  }
  const n = a.length, m = b.length;
  const L = Array.from({ length: n + 1 }, () => new Uint32Array(m + 1));
  for (let i = n - 1; i >= 0; i--) {
    for (let j = m - 1; j >= 0; j--) {
      L[i][j] = a[i] === b[j] ? L[i + 1][j + 1] + 1 : Math.max(L[i + 1][j], L[i][j + 1]);
    }
  }
  let i = 0, j = 0, added = 0, removed = 0;
  while (i < n && j < m) {
    if (a[i] === b[j]) { rows.push({ t: ' ', s: a[i] }); i++; j++; }
    else if (L[i + 1][j] >= L[i][j + 1]) { rows.push({ t: '-', s: a[i] }); i++; removed++; }
    else { rows.push({ t: '+', s: b[j] }); j++; added++; }
  }
  for (; i < n; i++) { rows.push({ t: '-', s: a[i] }); removed++; }
  for (; j < m; j++) { rows.push({ t: '+', s: b[j] }); added++; }
  return { rows, added, removed };
}

/**
 * Classify one tool_call event into a card. Returns null when the event
 * carries nothing a card can show beyond the plain line.
 * @param {Object} ev {name, args?, description?}
 */
export function cardFor(ev) {
  const rawName = String(ev?.name ?? ev?.tool ?? '').trim();
  const args = ev?.args ?? ev?.input ?? {};
  const kind = NAME_KINDS[rawName.toLowerCase()] ?? 'other';
  if (kind === 'other') return null;
  const path = pathOf(args);
  switch (kind) {
    case 'edit': {
      // Three arg shapes in the wild: Claude Edit {old_string,new_string},
      // pi/OMP edit and Claude MultiEdit {edits:[{oldText,newText} |
      // {old_string,new_string}]}. Normalize to a list of hunks.
      const pairs = [];
      if (Array.isArray(args.edits)) {
        for (const e of args.edits) {
          if (!e || typeof e !== 'object') continue;
          pairs.push([e.old_string ?? e.oldText ?? e.old_str ?? e.old ?? '', e.new_string ?? e.newText ?? e.new_str ?? e.new ?? '']);
        }
      } else {
        const oldS = args.old_string ?? args.old_str ?? args.oldText ?? args.old ?? '';
        const newS = args.new_string ?? args.new_str ?? args.newText ?? args.new ?? '';
        if (oldS || newS) pairs.push([oldS, newS]);
      }
      if (!pairs.length) return { kind, path, hunks: [], added: 0, removed: 0, replaceAll: false };
      const hunks = pairs.map(([o, n]) => lineDiff(String(o), String(n)));
      let added = 0, removed = 0;
      for (const h of hunks) { added += h.added; removed += h.removed; }
      return { kind, path, hunks, added, removed, replaceAll: !!args.replace_all };
    }
    case 'write': {
      const content = String(args.content ?? args.text ?? args.file_text ?? '');
      const lines = countLines(content);
      const PREVIEW = 14;
      const preview = content.split('\n').slice(0, PREVIEW).join('\n');
      return { kind, path, lines, added: lines, removed: 0, preview, more: Math.max(0, lines - PREVIEW) };
    }
    case 'read': {
      const offset = args.offset ?? args.start_line ?? args.start ?? null;
      const limit = args.limit ?? args.end_line ?? args.end ?? null;
      return { kind, path, offset, limit };
    }
    case 'shell': {
      const command = String(args.command ?? args.cmd ?? args.script ?? '');
      return { kind, command, description: typeof ev.description === 'string' ? ev.description : (typeof args.description === 'string' ? args.description : '') };
    }
    case 'search': {
      const query = args.pattern ?? args.query ?? args.search_term ?? args.glob ?? path;
      return { kind, query: query == null ? '' : String(query), scope: args.path ?? args.glob ?? args.include ?? '' , name: rawName.toLowerCase() };
    }
    case 'fetch':
      return { kind, url: String(args.url ?? args.uri ?? '') };
    case 'agent':
      return { kind, description: String(ev.description ?? args.description ?? args.prompt?.slice?.(0, 100) ?? '') };
    default:
      return null;
  }
}

/**
 * Parse an exit code out of a shell result snippet when the vessel did not
 * hand one over (pi: exit_code is always null). Recognizes the common
 * "Exit code: N" / "exit status N" trailers.
 */
export function exitCodeFrom(snippet) {
  if (!snippet) return null;
  const m = /(?:exit code|exit status|exited with(?: code)?)[:\s]+(-?\d+)/i.exec(String(snippet));
  return m ? Number(m[1]) : null;
}

/**
 * Fold the tool items of one turn into the summary strip:
 * files touched (edit/write), lines added/removed, shell commands run and
 * failed. Reads run silently; they never count as "changes".
 * @param {Array<{kind:string, card?:Object|null, err?:boolean}>} items
 */
export function turnStats(items) {
  const files = new Map(); // path -> {added, removed, created}
  let commands = 0, failed = 0, reads = 0, searches = 0, tools = 0;
  for (const it of items) {
    if (it.kind !== 'tool') continue;
    tools++;
    const c = it.card;
    if (!c) { if (it.err) failed++; continue; }
    if (c.kind === 'edit' || c.kind === 'write') {
      const key = c.path || '(unknown)';
      const f = files.get(key) ?? { path: key, added: 0, removed: 0, created: false };
      f.added += c.added ?? 0;
      f.removed += c.removed ?? 0;
      if (c.kind === 'write') f.created = true;
      files.set(key, f);
    } else if (c.kind === 'shell') commands++;
    else if (c.kind === 'read') reads++;
    else if (c.kind === 'search') searches++;
    if (it.err) failed++;
  }
  let added = 0, removed = 0;
  for (const f of files.values()) { added += f.added; removed += f.removed; }
  return { files: [...files.values()], added, removed, commands, failed, reads, searches, tools };
}

/** Items of the most recent turn: everything after the last user/peer message. */
export function lastTurnItems(items) {
  let start = 0;
  for (let i = items.length - 1; i >= 0; i--) {
    if (items[i].kind === 'you' || items[i].kind === 'peer') { start = i + 1; break; }
  }
  return items.slice(start);
}

/* ---------------------------------------------------------------------------
 * TODO list (expandable card next to the idle/working strip): fold the
 * vessel's task-list tool calls into one current state.
 *   todo_write / TodoWrite   {todos:[{id, content, status, group?, activeForm?}]}  replaces
 *   update_plan (codex)      {plan:[{step, status}]}                              replaces
 *   TaskCreate (claude)      {subject, description, activeForm}; id from the result
 *                            text "Task #N created"                               appends
 *   TaskUpdate (claude)      {taskId, status?, subject?, activeForm?}             patches
 * Every vessel lands in the same shape; pi gets todo_write from pi_ext/todo.ts.
 * ------------------------------------------------------------------------- */

const TODO_STATUS = { pending: 'pending', in_progress: 'in_progress', completed: 'completed', done: 'completed', cancelled: 'cancelled', canceled: 'cancelled', deleted: 'cancelled' };

function normStatus(s) {
  return TODO_STATUS[String(s ?? 'pending').toLowerCase()] ?? 'pending';
}

/**
 * @param {Array<Object>} events raw daemon events (tool_call / tool_result)
 * @returns {{items: Array<{id:string, content:string, status:string, group:string, activeForm:string}>, ts: string|null, done: number, total: number, active: Object|null} | null}
 */
export function foldTodos(events) {
  // The pulse's task list is a RECORD for the whole chat: nothing is ever
  // dropped. Each CLI session (spawn, handoff, reset) is one
  // segment; `items` is the live list of the newest segment (what the bar
  // shows), `record` is every segment oldest first (the full-screen view).
  // A segment starts on a `session` event with a new session_id, or — for
  // logs without one — when claude's per-session numbering repeats a Task #N.
  const record = []; // [{session, startTs, items: Map(id -> item)}]
  let seg = null;
  let ts = null;
  let touched = false;
  let sessionId = null;
  const pendingCreates = new Map(); // tool_call id -> draft item awaiting "Task #N"
  // Timestamps survive list rewrites (todo_write replaces the whole list every
  // call): keyed by id, falling back to content for tools that omit ids.
  let stampsById = new Map();
  let stampsByContent = new Map();
  const stampsFor = (t) => stampsById.get(t.id) ?? stampsByContent.get(t.content) ?? null;
  const stamp = (t, at) => {
    const prev = stampsFor(t) ?? { createdTs: t.createdTs ?? at ?? null, startedTs: null, doneTs: null };
    if (t.status === 'in_progress' && !prev.startedTs) prev.startedTs = at ?? null;
    if (t.status === 'completed' && !prev.doneTs) prev.doneTs = at ?? null;
    if (t.status !== 'completed') prev.doneTs = null; // reopened
    stampsById.set(t.id, prev); stampsByContent.set(t.content, prev);
    return Object.assign(t, prev);
  };
  // `inferred`: opened by a task before the CLI announced its session id
  // (claude reports the id at the end of the turn, after the first
  // TaskCreate). The session event that follows adopts that segment.
  const newSegment = (at, inferred) => {
    seg = { session: record.length + 1, startTs: at ?? null, items: new Map(), live: [], inferred: !!inferred };
    record.push(seg);
    stampsById = new Map(); stampsByContent = new Map();
  };
  // Record an item in the current segment (insert or refresh in place).
  const put = (t) => { if (!seg) newSegment(t.createdTs ?? null, true); seg.items.set(t.id, t); };
  const replaceWith = (list, at) => {
    if (!seg) newSegment(at, true);
    const next = list.map((t) => stamp(t, at));
    // Items the tool dropped stay in the record with their last known state.
    for (const t of next) put(t);
    seg.live = next;
    touched = true; ts = at ?? ts;
  };
  for (const ev of events ?? []) {
    if (ev.type === 'session') {
      const sid = ev.session_id ?? null;
      if (sid && sid !== sessionId) {
        sessionId = sid;
        // The first session of the log opens the first segment; a changed id
        // closes the previous one. An empty segment (no tasks yet) is reused.
        if (seg && seg.inferred) seg.inferred = false; // late id for the segment a task already opened
        else if (!seg || seg.items.size) newSegment(ev.ts ?? null);
        else seg.startTs = seg.startTs ?? ev.ts ?? null;
      }
      continue;
    }
    if (ev.type === 'tool_result') {
      const draft = ev.id != null ? pendingCreates.get(ev.id) : null;
      if (!draft) continue;
      pendingCreates.delete(ev.id);
      const m = /Task #(\d+)/.exec(String(ev.snippet ?? ''));
      if (m) draft.id = m[1];
      if (ev.ok === false) continue;
      // Claude numbers tasks per session from #1. A number already on the
      // list means a new session began without a session event in the log.
      if (m && seg && seg.items.has(draft.id)) newSegment(ev.ts ?? draft.createdTs ?? null, true);
      const it = stamp(draft, ev.ts ?? draft.createdTs);
      put(it); seg.live = [...seg.live, it];
      touched = true; ts = ev.ts ?? ts;
      continue;
    }
    if (ev.type !== 'tool_call') continue;
    const name = String(ev.name ?? '').toLowerCase();
    const args = ev.args ?? ev.input ?? {};
    if (name === 'todo_write' || name === 'todowrite') {
      if (!Array.isArray(args.todos)) continue;
      replaceWith(args.todos.filter((t) => t && typeof t === 'object').map((t, i) => ({
        id: String(t.id ?? i + 1), content: String(t.content ?? t.title ?? ''), status: normStatus(t.status),
        group: String(t.group ?? t.section ?? ''), activeForm: String(t.activeForm ?? '')
      })), ev.ts ?? null);
    } else if (name === 'update_plan') {
      if (!Array.isArray(args.plan)) continue;
      replaceWith(args.plan.filter((t) => t && typeof t === 'object').map((t, i) => ({
        id: String(i + 1), content: String(t.step ?? t.content ?? ''), status: normStatus(t.status), group: '', activeForm: ''
      })), ev.ts ?? null);
    } else if (name === 'taskcreate') {
      const draft = { id: `new-${pendingCreates.size + (seg?.items.size ?? 0) + 1}`, content: String(args.subject ?? args.description ?? ''), status: 'pending', group: '', activeForm: String(args.activeForm ?? ''), createdTs: ev.ts ?? null };
      if (ev.id != null) pendingCreates.set(ev.id, draft);
      else { const it = stamp(draft, ev.ts ?? null); put(it); seg.live = [...seg.live, it]; touched = true; ts = ev.ts ?? ts; }
    } else if (name === 'taskupdate') {
      const id = String(args.taskId ?? args.id ?? '');
      const it = seg?.items.get(id);
      if (!it) continue;
      if (args.status != null) it.status = normStatus(args.status);
      if (args.subject != null) it.content = String(args.subject);
      if (args.activeForm != null) it.activeForm = String(args.activeForm);
      stamp(it, ev.ts ?? null);
      touched = true; ts = ev.ts ?? ts;
    }
  }
  if (!touched) return null;
  // TodoBar keys its rows by id; a repeated id makes Svelte throw
  // each_key_duplicate and the card cannot open.
  const uniq = (list) => {
    const seen = new Map();
    for (const t of list) {
      const n = seen.get(t.id) ?? 0;
      seen.set(t.id, n + 1);
      if (n) t.id = `${t.id}~${n}`;
    }
    return list;
  };
  const dropped = (t) => t.status === 'cancelled' || t.status === 'deleted';
  const visible = uniq(seg.live.filter((t) => !dropped(t)));
  const done = visible.filter((t) => t.status === 'completed').length;
  const active = visible.find((t) => t.status === 'in_progress') ?? null;
  const segments = record.filter((s) => s.items.size).map((s) => {
    const items = uniq([...s.items.values()]);
    return { session: s.session, startTs: s.startTs, items, done: items.filter((t) => t.status === 'completed').length };
  });
  const recordTotal = segments.reduce((n, s) => n + s.items.length, 0);
  const recordDone = segments.reduce((n, s) => n + s.done, 0);
  return { items: visible, ts, done, total: visible.length, active, record: segments, recordTotal, recordDone };
}

/** Group todo items by their `group` field, preserving first-seen order. */
export function todoGroups(todo) {
  const out = [];
  const byName = new Map();
  for (const t of todo?.items ?? []) {
    const g = t.group || '';
    let grp = byName.get(g);
    if (!grp) { grp = { name: g, items: [], done: 0 }; byName.set(g, grp); out.push(grp); }
    grp.items.push(t);
    if (t.status === 'completed') grp.done++;
  }
  return out;
}

/** Is this event one the TODO fold cares about? */
export function isTaskEvent(ev) {
  if (!ev) return false;
  if (ev.type === 'session') return true; // segment boundary for the record
  if (ev.type !== 'tool_call') return false;
  const n = String(ev.name ?? '').toLowerCase();
  return n === 'todo_write' || n === 'todowrite' || n === 'update_plan' || n === 'taskcreate' || n === 'taskupdate';
}

/**
 * Merge the whole-log task events (GET /task-events) with the pane's tail:
 * history first, then any tail event not already present (same type, name,
 * id, and ts). Order is preserved so foldTodos sees one timeline.
 */
export function mergeTaskEvents(history, tail) {
  const key = (e) => `${e.type}|${e.name ?? ''}|${e.id ?? e.session_id ?? ''}|${e.ts ?? ''}`;
  const seen = new Set((history ?? []).map(key));
  const out = [...(history ?? [])];
  for (const e of tail ?? []) {
    if (e.type !== 'tool_call' && e.type !== 'tool_result' && e.type !== 'session') continue;
    const k = key(e);
    if (seen.has(k)) continue;
    seen.add(k);
    out.push(e);
  }
  return out;
}
