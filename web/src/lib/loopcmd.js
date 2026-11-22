/**
 * Pure parser + picker model for slash commands typed in a pulse chat.
 * Commands are handled client-side: they NEVER reach
 * the pulse as a message. Execution lives in slashrun.js (the Composer
 * hands it the api.js calls); this module only parses, so it stays unit-testable under
 * plain node.
 *
 * Commands are driven from one TABLE: each row knows its command, its
 * insert-on-accept text, and a keyword rule for type-to-filter. Adding a
 * command = table rows + a parse branch, not another picker if-chain.
 *
 * Grammar:
 *   /loop <minutes> <prompt>   prompt this pulse every N minutes (a daemon
 *                              loop schedule; first fire after N minutes)
 *   /loop list                 show this pulse's loops
 *   /loop stop [id]            stop a loop (id optional when it has exactly one)
 *   /at <when> <prompt>        run a prompt once at a time: 'in 30m',
 *                              'in 2h', 'in 2h 30m', or '9:00'/'17:30'
 *                              (24h local; today, or tomorrow if past)
 *   /rename <title>            rename this pulse
 *   //<text>                   escape: send "<text>" literally (one slash eaten)
 *
 * Any other /word is an UNKNOWN command: the caller shows a local notice and
 * does not send, so a typo never becomes a prompt to the pulse.
 */

/** @typedef {{insert: string, label: string, hint: string, cmd: string, kw: string}} MenuRow */
// kw rules: '' = creator row, visible only while no args are typed (loop's
// is also kept alive while bare digits — the interval — are being typed);
// a word = keyword subcommand, kept while the typed rest is a prefix of it
// or exactly it (trailing space = args still coming).
const COMMAND_ROWS = [
  {
    cmd: 'loop', kw: '',
    insert: '/loop ', label: '/loop <minutes> <prompt>',
    hint: 'prompt this pulse every N minutes',
  },
  {
    cmd: 'loop', kw: 'list',
    insert: '/loop list', label: '/loop list',
    hint: "show this pulse's loops",
  },
  {
    cmd: 'loop', kw: 'stop',
    insert: '/loop stop ', label: '/loop stop [id]',
    hint: 'stop a loop (id optional if it has exactly one)',
  },
  {
    cmd: 'at', kw: '',
    insert: '/at ', label: '/at <when> <prompt>',
    hint: 'run once at a time — in 30m, in 2h, 9:00',
  },
  {
    cmd: 'rename', kw: '',
    insert: '/rename ', label: '/rename <title>',
    hint: 'rename this pulse',
  },
];

/**
 * Picker model: what rows (if any) the menu shows for the current draft.
 * Open on any leading '/' ('//' escape never opens it); the first token
 * narrows by command prefix, '/' alone shows every command. Within a
 * command, rows filter by keyword and close once real args are being typed
 * — the menu is completion, never a re-writer (Enter must RUN a finished
 * command; the caller's accept-guard enforces the same invariant).
 *
 * @param {string} draft  raw composer draft
 * @returns {MenuRow[]|null}
 */
export function slashMenu(draft) {
  if (!draft || !draft.startsWith('/') || draft.startsWith('//')) return null;
  const sp = draft.search(/\s/);
  const token = (sp === -1 ? draft : draft.slice(0, sp)).toLowerCase();
  const name = token.slice(1);

  let rows = COMMAND_ROWS;
  if (name) {
    rows = COMMAND_ROWS.filter((r) => r.cmd.startsWith(name));
    if (!rows.length) return null;
    const exact = rows[0].cmd === name && rows.every((r) => r.cmd === name);
    if (!exact) return rows; // still typing the command word — show candidates
  }

  if (sp === -1) return rows; // no args yet
  const rest = draft.slice(sp + 1).trimStart().toLowerCase();
  if (!rest) return rows; // '/loop ' etc — all forms of this command

  // One command fully typed with args: per-row filtering.
  const items = rows.filter((r) => {
    if (!r.kw) {
      // Creator rows: loop stays while bare interval digits are typed (or
      // the word 'create' is); at/rename close on any arg (freeform args).
      if (r.cmd === 'loop') {
        return /^\d+$/.test(rest) || 'create'.startsWith(rest) || rest.startsWith('create');
      }
      return false;
    }
    if (r.kw === rest) return false; // complete form ('/loop list') — run it
    return r.kw.startsWith(rest) ||
      (rest.startsWith(r.kw) && !rest.slice(r.kw.length).trim());
  });
  return items.length ? items : null;
}

/**
 * @param {string} text  already-trimmed draft text
 * @returns {Object|null}
 *   null                       not a command — send normally
 *   {kind:'literal', text}     '//' escape — send text as a normal message
 *   {kind:'create', minutes, prompt}
 *   {kind:'list'}
 *   {kind:'stop', idPrefix}    idPrefix '' = caller resolves (0, 1 or many loops)
 *   {kind:'at', when, prompt}  when: raw ('in 30m' | '2h 30m' | '9:00'); the
 *                              caller resolves it with resolveAt()
 *   {kind:'rename', title}
 *   {kind:'usage', text}       known command with bad/missing args
 *   {kind:'unknown', command}
 */
export function parseSlashCommand(text) {
  if (!text || !text.startsWith('/')) return null;
  // '//' escape: '//' -> '/' alone is a literal slash message.
  if (text.startsWith('//')) return { kind: 'literal', text: text.slice(1) };

  const sp = text.search(/\s/);
  const token = sp === -1 ? text : text.slice(0, sp);
  const rest = sp === -1 ? '' : text.slice(sp + 1).trim();
  const command = token.slice(1).toLowerCase();

  if (command === 'loop') return parseLoop(rest);
  if (command === 'at') return parseAt(rest);
  if (command === 'rename') {
    if (!rest) {
      return { kind: 'usage', text: 'usage: /rename <title> — rename this pulse' };
    }
    return { kind: 'rename', title: rest };
  }
  return { kind: 'unknown', command: token };
}

function parseLoop(rest) {
  if (!rest || rest.toLowerCase() === 'help') return usageLoop();

  if (rest.toLowerCase() === 'list') return { kind: 'list' };

  if (rest.toLowerCase() === 'stop' ||
      rest.toLowerCase().startsWith('stop ')) {
    const idPrefix = rest.length > 4 ? rest.slice(5).trim() : '';
    return { kind: 'stop', idPrefix };
  }

  // create: first token is integer minutes, the rest is the prompt.
  const msp = rest.search(/\s/);
  const numTok = msp === -1 ? rest : rest.slice(0, msp);
  const prompt = msp === -1 ? '' : rest.slice(msp + 1).trim();
  if (!/^\d+$/.test(numTok) || parseInt(numTok, 10) < 1) {
    const u = usageLoop();
    return { kind: 'usage', text: `${numTok ? `'${numTok}' is not a valid interval. ` : ''}${u.text}` };
  }
  if (!prompt) {
    const u = usageLoop();
    return { kind: 'usage', text: `Missing the prompt. ${u.text}` };
  }
  return { kind: 'create', minutes: parseInt(numTok, 10), prompt };
}

function parseAt(rest) {
  if (!rest || rest.toLowerCase() === 'help') {
    return {
      kind: 'usage',
      text: 'usage: /at <when> <prompt> — run once at a time\n' +
            "       when: 'in 30m' · 'in 2h' · 'in 2h 30m' · '9:00' · '9am' · '5:30pm' (local; clock times today or tomorrow if past)",
    };
  }
  const lower = rest.toLowerCase();
  if (lower.startsWith('in')) {
    // 'in <n><unit>[ <n>m]' — bare number means minutes.
    const m = rest.slice(2).trim().match(/^(\d+)\s*([mh])?(?:\s*(\d+)\s*m)?([\s,]+([\s\S]+))?$/i);
    if (!m) return atUsage('Bad "in" time.');
    const n1 = parseInt(m[1], 10);
    const unit = (m[2] || 'm').toLowerCase();
    const n2 = m[3] !== undefined ? parseInt(m[3], 10) : 0;
    const minutes = unit === 'h' ? n1 * 60 + n2 : n1;
    if (minutes < 1) return atUsage('That is not a future time.');
    const prompt = (m[5] || '').trim();
    if (!prompt) return atUsage('Missing the prompt.');
    return { kind: 'at', when: `in ${minutes}m`, prompt };
  }
  // Clock times: 24h requires the colon ('9' alone is usage, not 09:00);
  // am/pm accepted bare ('9am', '5:30pm') — that is what gets typed on a
  // phone (2026-09-15).
  const am = rest.match(/^(\d{1,2})(?::(\d{2}))?\s*(am|pm)(?:[\s,]+([\s\S]+))?$/i);
  if (am) {
    let hh = parseInt(am[1], 10);
    if (hh < 1 || hh > 12) return atUsage('Bad clock time.');
    hh = hh % 12 + (am[3].toLowerCase() === 'pm' ? 12 : 0);
    const mm = am[2] ? parseInt(am[2], 10) : 0;
    if (mm > 59) return atUsage('Bad clock time.');
    const prompt = (am[4] || '').trim();
    if (!prompt) return atUsage('Missing the prompt.');
    return { kind: 'at', when: `${String(hh).padStart(2, '0')}:${String(mm).padStart(2, '0')}`, prompt };
  }
  const m24 = rest.match(/^(\d{1,2}):(\d{2})(?:[\s,]+([\s\S]+))?$/);
  if (m24) {
    const hh = parseInt(m24[1], 10), mm = parseInt(m24[2], 10);
    if (hh > 23 || mm > 59) return atUsage('Bad clock time.');
    const prompt = (m24[3] || '').trim();
    if (!prompt) return atUsage('Missing the prompt.');
    return { kind: 'at', when: `${String(hh).padStart(2, '0')}:${m24[2]}`, prompt };
  }
  return atUsage('Bad time.');
}

function atUsage(prefix) {
  return {
    kind: 'usage',
    text: `${prefix} usage: /at <when> <prompt> — when: 'in 30m' · 'in 2h' · 'in 2h 30m' · '9:00' (24h local, today or tomorrow if past)`,
  };
}

/**
 * Resolve a /at `when` to a UTC ISO instant.
 *   'in <N>m'  → now + N minutes (caller passed normalized 'in <N>m')
 *   'HH:MM'    → today local at HH:MM, tomorrow if that is already past
 * Returns null for a non-future 'in' time; clock times always resolve.
 * Pure apart from the passed `now` so tests pin the clock.
 *
 * @param {string} when
 * @param {Date} now
 * @returns {string|null}
 */
export function resolveAt(when, now) {
  const inM = when.match(/^in (\d+)m$/);
  if (inM) {
    const t = new Date(now.getTime() + parseInt(inM[1], 10) * 60000);
    return t > now ? t.toISOString() : null;
  }
  const hm = when.match(/^(\d{2}):(\d{2})$/);
  if (hm) {
    const t = new Date(now);
    t.setHours(parseInt(hm[1], 10), parseInt(hm[2], 10), 0, 0);
    if (t <= now) t.setDate(t.getDate() + 1);
    return t.toISOString();
  }
  return null;
}

function usageLoop() {
  return {
    kind: 'usage',
    text: 'usage: /loop <minutes> <prompt> — prompt this pulse every N minutes\n' +
          "       /loop list — show this pulse's loops\n" +
          '       /loop stop [id] — stop a loop (id optional if it has exactly one)',
  };
}
