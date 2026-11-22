/**
 * Transform the daemon's raw event stream into renderable chat items.
 *
 * Rules (data-honesty: everything derives from the CLI stream or our clocks):
 *  - user_message   -> {kind:'you', text}
 *  - assistant_text -> {kind:'reply', text}
 *  - reasoning      -> {kind:'thinking', text}  one finished thinking block
 *                      (runner-emitted); the pane renders it collapsed
 *  - tool_call      -> {kind:'tool', id, line, raw, err:false, live}
 *                      one dim plain-English mono line via the verb map.
 *  - tool_result    -> append "→ <first line of snippet>" to its call (match by id);
 *                      red (err) when ok===false.
 *  - done (silent)  -> {kind:'turnend', tools, interrupted} when the turn
 *                      produced no reply — the boundary a tool-only turn
 *                      otherwise never shows.
 *  - backup         -> {kind:'tool', err:false, line} one system marker line
 *                      (old answer-once backup turns, kept for old transcripts)
 *  - failover       -> {kind:'tool', err:false, line} the pulse moved to its backup
 *  - fence_breach / runner_error -> {kind:'tool', err:true, line}
 *  - usage / done / session -> NOT rendered as lines.
 *
 * The verb map:
 *  - Read            -> "read <file_path>"
 *  - Edit | Write    -> "edited <file_path>"
 *  - Grep            -> "searched for <pattern>"
 *  - Bash | Task     -> the call's `description` field, verbatim
 *  - fallback        -> "<name> <first arg value>"
 */

import { cardFor, exitCodeFrom } from './toolcards.js';
import { parseTs, fmtBookmarkTime } from './time.js';
import { plainWatchText } from './watches.js';

/** @param {Object} args @returns {any} */
function firstArg(args) {
  if (!args || typeof args !== 'object') return '';
  const skip = new Set(['variant', 'description']);
  for (const [k, v] of Object.entries(args)) {
    if (skip.has(k) || v == null || v === '') continue;
    return v;
  }
  const vals = Object.values(args);
  return vals.length ? vals[0] : '';
}

function pathArg(args) {
  return args.file_path ?? args.target_file ?? args.target_directory
    ?? args.path ?? firstArg(args);
}

/** Description is useful only when it is not just the raw function name. */
function usefulDesc(name, ev, args) {
  const raw = ev.description ?? args.description;
  if (raw == null || raw === '') return '';
  const desc = String(raw);
  // Case-insensitive demotion: Grok/ACP titles and zero-thought descriptions
  // repeat the tool name in any case ('Bash' vs 'bash' — spot-check 2026-08-30).
  if (desc.toLowerCase() === String(name).toLowerCase() || desc.toLowerCase() === 'tool') return '';
  // The local Codex command summarizer can occasionally answer with its own
  // scratch instructions instead of a label. Never render that meta text;
  // falling through shows the command's clipped first line instead.
  const lower = desc.trim().toLowerCase();
  if (/^(?:we need|i need|need)\b|^(?:command|analysis|reasoning):|^<think/.test(lower) ||
      ['you write terse display labels', "user's request", 'users request', 'produce final', 'reply only',
       'plain-english sentence', 'plain english sentence',
       'at most two clauses', 'given a shell command',
       'never meta-commentary'].some((s) => lower.includes(s))) return '';
  return desc;
}

function isPlaceholderToolLine(line) {
  const s = (line || '').trim();
  return !s || s === 'tool';
}

/**
 * Build the plain-English line for a tool_call event.
 * @param {Object} ev  {name, args?, description?}
 * @returns {string}
 */
export function toolCallLine(ev) {
  const rawName = ev.name ?? ev.tool ?? 'tool';
  const args = ev.args ?? ev.input ?? {};
  const desc = usefulDesc(rawName, ev, args);

  // pi (OMP) reports lowercase tool names ("bash", "read"); Claude uses
  // Title Case ("Bash"). Match case-insensitively so every vessel gets the
  // same verb-map treatment (2026-08-29: qwen OMP lines fell through to the
  // raw-args default).
  const name = String(rawName).toLowerCase();

  switch (name) {
    case 'read':
    case 'read_file':
      return `read ${pathArg(args)}`;
    case 'edit':
    case 'write':
    case 'search_replace':
      return `edited ${pathArg(args)}`;
    case 'grep':
      return `searched for ${args.pattern ?? firstArg(args)}`;
    case 'list_dir':
      return `list ${pathArg(args)}`;
    case 'web_fetch':
      return `fetch ${args.url ?? firstArg(args)}`;
    case 'web_search':
      return `searched for ${args.query ?? args.search_term ?? firstArg(args)}`;
    case 'bash':
    case 'task':
    case 'run_terminal_command':
    case 'command_execution': // codex / Luna
    case 'local_shell_call':
    case 'run_shell_command':
      // Use the model's own one-line description verbatim; fall back to a
      // FIRST-LINE summary of the command — never the full raw text (the raw
      // stays available behind the tap-to-expand line).
      if (desc) return desc;
      return summarizeCommand(args.command ?? firstArg(args));
    default:
      if (desc) return desc;
      // ACP titles sometimes arrive as the name ("Read `/path`").
      if (rawName && rawName !== 'tool' && /[\s/`']/.test(rawName)) return String(rawName);
      if (rawName && rawName !== 'tool') {
        const arg = firstArg(args);
        return arg ? `${rawName} ${arg}` : String(rawName);
      }
      const arg = firstArg(args);
      return arg ? String(arg) : 'tool';
  }
}

/** Char cap for the one-line command summary (raw stays behind expand). */
const COMMAND_LINE_MAX = 120;

/**
 * Char cap for the → result summary rendered after a tool call. A tool that
 * returns one long line (e.g., compact JSON with zero newlines) has an overly
 * long "first line" — clip the summary so the tool line stays compact while
 * keeping the full result behind tap-to-expand and in the session log.
 */
export const RESULT_LINE_MAX = 160;

/** First line of a result, clipped to the compact-summary cap. */
export function resultSummaryLine(snippet) {
  const fl = firstLine(snippet);
  return fl.length > RESULT_LINE_MAX ? fl.slice(0, RESULT_LINE_MAX) + '…' : fl;
}

/** `/bin/bash -lc "cmd"` -> `cmd`: the wrapper says nothing to a reader. */
function unwrapShell(command) {
  const c = Array.isArray(command) ? command.join(' ') : String(command ?? '');
  // bash -lc / -c / -ec / -l -c …
  const m = /^\s*(?:\/(?:usr\/)?bin\/)?(?:ba|z)?sh\s+(?:-[a-z]+\s+)*-[a-z]*c\s+([\s\S]*)$/.exec(c);
  if (!m) return c;
  const body = m[1].trim();
  const q = body[0];
  if (!(q === '"' || q === "'") || body.length < 2 || !body.endsWith(q)) return body;
  const inner = body.slice(1, -1);
  // Only one quoted word: `"a" && "b"` is not a single quoted command.
  if (q === "'" ? inner.includes("'") : /(^|[^\\])"/.test(inner)) return body;
  return q === '"' ? inner.replace(/\\(["\\$`])/g, '$1') : inner;
}

/** First line of a command, clipped — the summary form of a raw command. */
function summarizeCommand(command) {
  const line = firstLine(unwrapShell(command));
  return line.length > COMMAND_LINE_MAX
    ? line.slice(0, COMMAND_LINE_MAX) + '…'
    : line;
}

function isByteList(val) {
  return Array.isArray(val) && val.every((n) => Number.isInteger(n) && n >= 0 && n <= 255);
}

function decodeByteList(val) {
  if (!isByteList(val)) return null;
  return new TextDecoder('utf-8').decode(Uint8Array.from(val));
}

/** Unwrap Grok ACP `{type:"Bash", output:[bytes]}` or a JSON dump of that. */
function unwrapToolOutput(obj) {
  if (isByteList(obj)) return decodeByteList(obj);
  if (!obj || typeof obj !== 'object' || Array.isArray(obj)) return null;
  const val = obj.output ?? obj.content ?? obj.result ?? obj.text ?? obj.stdout ?? obj.snippet;
  const decoded = decodeByteList(val);
  if (decoded != null) return decoded;
  if (typeof val === 'string' && obj.type === 'Bash') return val;
  return null;
}

/** Grok ACP stored some bash stdout as a JSON dump of a byte array. */
export function decodeToolSnippet(s) {
  if (s == null) return '';
  if (typeof s !== 'string') {
    const fromObj = unwrapToolOutput(s);
    return fromObj != null ? fromObj : JSON.stringify(s);
  }
  const trimmed = s.trim();
  if (trimmed.startsWith('{') && trimmed.includes('"output"')) {
    try {
      const fromObj = unwrapToolOutput(JSON.parse(trimmed));
      if (fromObj != null) return fromObj;
    } catch {
      // not JSON — leave the original string
    }
  }
  return s;
}

/** First line of an arbitrary snippet/result, trimmed. */
export function firstLine(s) {
  if (s == null) return '';
  const str = decodeToolSnippet(s);
  const nl = str.indexOf('\n');
  return (nl === -1 ? str : str.slice(0, nl)).trim();
}

/**
 * Any event that proves the pending tool_call's turn moved on. Besides the
 * normal closers (assistant_text/done), a user_message means a NEW turn
 * started (a pause/kill/restore can end a turn with no done event at all),
 * and runner_error/fence_breach end the turn abnormally — without these a
 * tool orphaned by a hard kill would pulse "running" forever.
 */
const TURN_CLOSERS = new Set(['assistant_text', 'done', 'user_message', 'runner_error', 'fence_breach', 'pulse_stopped']);

/** Return the newest raw tool_call index that is still open in the loaded tail. */
function liveToolIndex(events) {
  const resultIds = new Set();
  for (const ev of events ?? []) {
    if (ev.type === 'tool_result' && ev.id != null) resultIds.add(ev.id);
  }

  const list = events ?? [];
  for (let i = list.length - 1; i >= 0; i--) {
    const ev = list[i];
    if (ev.type !== 'tool_call') continue;
    const hasResult = ev.id != null && resultIds.has(ev.id);
    const closed = list.slice(i + 1).some((later) => TURN_CLOSERS.has(later.type));
    if (!hasResult && !closed) return i;
  }
  return -1;
}

/**
 * Summarize the newest completion, usage, and currently-running tool in an
 * event tail.
 * @param {Array<Object>} events
 * @returns {{lastDoneTs:number|null, lastUsage:{inTok:number,outTok:number}|null,
 *            liveTool:{line:string,ts:number}|null}}
 */
/**
 * Wall-clock span as H:mm:ss (hours unpadded, no cap) — standard format for
 * turn timers.
 * @param {number} ms
 * @returns {string} '' when ms is not a non-negative number
 */
export function fmtClock(ms) {
  if (!(ms >= 0)) return '';
  const sec = Math.floor(ms / 1000);
  const h = Math.floor(sec / 3600);
  const m = Math.floor((sec % 3600) / 60);
  const s = sec % 60;
  return `${h}:${String(m).padStart(2, '0')}:${String(s).padStart(2, '0')}`;
}

/**
 * Format a pulse stop_reason with a plain next-step hint when the runner
 * failed at start because its agent tool was missing or could not start.
 * @param {string|null|undefined} reason
 * @returns {string}
 */
export function stoppedReasonText(reason) {
  const clean = String(reason ?? '').trim();
  if (!clean) return '';
  if (/vessel failed to start|binary not found|\bnot found\b/i.test(clean) &&
      !/install the tool|pick another model/i.test(clean)) {
    return `${clean}. Install the tool or pick another model in settings.`;
  }
  return clean;
}

/**
 * One readable line for a runner error. Model-server failures get plain
 * words first (a team member cannot act on "stop_reason=error from
 * glmlocal/…"); the technical text stays in the expandable raw view.
 * @param {any} ev
 */
export function runnerErrorLine(ev) {
  const raw = ev?.reason ?? ev?.text ?? ev?.message ?? ev?.type ?? 'error';
  if (ev?.where === 'vessel.start') {
    return stoppedReasonText(raw);
  }
  if (ev?.where === 'provider_error') {
    const err = String(ev.error ?? raw);
    const why = /timed? ?out/i.test(err) ? 'its server timed out'
      : /connect|refused|unreachable|ECONN/i.test(err) ? 'its server could not be reached'
      : /context|max_model_len|too long|tokens/i.test(err) ? 'the conversation is too long for it'
      : 'its server returned an error';
    return `The model didn't answer — ${why}. Send again, or switch this pulse to another model. (${err.slice(0, 120)})`;
  }
  if (ev?.where === 'output_cap') {
    return `The reply was cut off — the model hit its output limit. Ask it to continue, or lower the effort. (${String(raw).slice(0, 120)})`;
  }
  return raw;
}

export function turnMeta(events) {
  const list = events ?? [];
  let lastDoneTs = null;
  let lastDoneIdx = -1;

  for (let i = 0; i < list.length; i++) {
    if (list[i].type !== 'done') continue;
    lastDoneIdx = i;
    const ts = parseTs(list[i].ts);
    if (Number.isFinite(ts)) lastDoneTs = ts;
  }

  // "last turn tokens" means the newest COMPLETED turn — the newest usage at
  // or after the last done but before the next turn's user_message (Codex
  // trails usage after done), or failing that the newest usage before the
  // done (Claude emits it per cycle). A turn currently in flight must never
  // leak its partial numbers into the chip, so no done in the tail = no chip.
  let lastUsage = null;
  if (lastDoneIdx !== -1) {
    let start = 0;
    for (let i = lastDoneIdx; i >= 0; i--) {
      if (list[i].type === 'user_message') { start = i; break; }
    }
    let boundary = list.length;
    for (let i = lastDoneIdx + 1; i < list.length; i++) {
      if (list[i].type === 'user_message') { boundary = i; break; }
    }
    // A turn is many API calls, one usage event each (parse.py emits per
    // assistant message). "out" is summed across the turn so it reflects total
    // generated tokens rather than just the final chunk; "in" stays the final
    // call's input because every call re-reads the full context.
    for (let i = start; i < boundary; i++) {
      const ev = list[i];
      if (ev.type === 'usage' && Number.isFinite(ev.in) && Number.isFinite(ev.out)) {
        lastUsage = { inTok: ev.in, outTok: (lastUsage ? lastUsage.outTok : 0) + ev.out };
      }
    }
  }

  // How long the newest completed turn took: its user_message → that done.
  // null when the tail doesn't reach back to the message (or no done at all).
  let lastTurnMs = null;
  if (lastDoneIdx !== -1) {
    for (let i = lastDoneIdx - 1; i >= 0; i--) {
      if (list[i].type !== 'user_message') continue;
      const startTs = parseTs(list[i].ts);
      if (Number.isFinite(startTs) && lastDoneTs !== null) lastTurnMs = Math.max(0, lastDoneTs - startTs);
      break;
    }
  }

  // Billed hidden-reasoning tokens for the newest completed turn — Claude's
  // result line carries the turn total on its done event.
  let lastThinkTok = null;
  if (lastDoneIdx !== -1 && Number.isFinite(list[lastDoneIdx].thinking_tokens)) {
    lastThinkTok = list[lastDoneIdx].thinking_tokens;
  }

  const liveIndex = liveToolIndex(list);
  let liveTool = null;
  if (liveIndex !== -1) {
    const ts = parseTs(list[liveIndex].ts);
    if (Number.isFinite(ts)) {
      // Same-id folding as buildItems: a later placeholder update ("tool",
      // no args) must not shadow the real verb+arg line of the first call.
      const id = list[liveIndex].id;
      let line = toolCallLine(list[liveIndex]);
      if (isPlaceholderToolLine(line) && id != null) {
        for (const ev of list) {
          if (ev.type !== 'tool_call' || ev.id !== id) continue;
          const candidate = toolCallLine(ev);
          if (!isPlaceholderToolLine(candidate)) { line = candidate; break; }
        }
      }
      liveTool = { line, ts };
    }
  }

  // The newest completed turn FAILED when an error line is its last word:
  // a runner_error after its user_message with no reply text after it
  // (a timed-out model showed a green "just finished").
  let lastFailed = false;
  if (lastDoneIdx !== -1) {
    for (let i = lastDoneIdx - 1; i >= 0; i--) {
      const type = list[i].type;
      if (type === 'assistant_text' || type === 'user_message') break;
      if (type === 'runner_error' || type === 'fence_breach') { lastFailed = true; break; }
    }
  }

  return { lastDoneTs, lastUsage, liveTool, lastTurnMs, lastThinkTok, lastFailed, speed: speedMeta(list) };
}

/**
 * Per-pulse speed from the runner's `metrics` events (meter.py). Numbers shown
 * are medians over trustworthy calls only:
 *  - meter split ghosts (a zero-output stub, or a <0.2s fragment left by a
 *    call boundary landing mid-turn) are dropped;
 *  - >30 min idle gaps: the gap before a call is user think-time, not
 *    model time — the call still counts, with `gapBefore` set;
 *  - medians only average calls the vessel really reported (out_est false)
 *    and that sit under sanity ceilings — a `~` prefix marks that some of
 *    the window was estimated or dropped, and the chip shows nReal/n.
 * `window` is the lookback over raw metrics events.
 * @param {Array<Object>} events
 * @param {number} window  how many recent metric events the window covers
 * @returns {null | Object}  {last, med, nReal, n, excluded, series}
 *   `last`/`med` fields: { genTps, ttftS, prefillTps, inTok, outTok, outEst }
 *   (a field is null when the data behind it is missing)
 */
export const SPEED_SANITY_TPS = 2000;      // no model here decodes faster
export const SPEED_SANITY_PREFILL = 50000; // ditto prefill t/s
const IDLE_GAP_S = 1800;                   // >30min between calls = idle

function parseEventTs(ts) {
  const m = typeof ts === 'string' && ts.match(/^(\d{4})-(\d\d)-(\d\d)T(\d\d):(\d\d):(\d\d)([+-])(\d\d)(\d\d)$/);
  if (!m) return null;
  const off = (Number(m[8]) * 60 + Number(m[9])) * 60 * (m[7] === '+' ? -1 : 1);
  return Date.UTC(+m[1], +m[2] - 1, +m[3], +m[4], +m[5], +m[6]) / 1000 + off;
}

export function speedMeta(events, window = 8) {
  const calls = (events ?? [])
    .filter((e) => e.type === 'metrics' && Number.isFinite(e.gen_s))
    .map((e) => {
      const t = parseEventTs(e.ts);
      const out = Number.isFinite(e.out_tok) ? e.out_tok : null;
      const ttft = Number.isFinite(e.ttft_s) ? e.ttft_s : null;
      const inTok = Number.isFinite(e.in_tok) ? e.in_tok : null;
      const gen = e.gen_s;
      const r = {
        t, out, gen, ttft, inTok, est: !!e.out_est,
        genTps: out !== null && gen > 0 ? out / gen : null,
        prefillTps: ttft !== null && ttft > 0 && inTok !== null ? inTok / ttft : null,
      };
      // a split ghost: zero streamed text and no usable usage, or a fragment
      // so short no real call could complete in it (meter boundary landed
      // mid-turn). Validated on the 2026-09-12 live capture: 77 ghosts,
      // 2,015 real calls.
      const chars = Number.isFinite(e.out_chars) ? e.out_chars : 0;
      r.ghost = (chars === 0 && (out === null || out < 8)) || (chars === 0 && gen < 0.2);
      return r;
    })
    .filter((r) => !r.ghost);
  if (!calls.length) return null;
  for (let i = 1; i < calls.length; i++) {
    const prev = calls[i - 1], cur = calls[i];
    if (cur.t !== null && prev.t !== null && cur.t - (prev.t + (prev.gen || 0)) > IDLE_GAP_S) cur.gapBefore = true;
  }
  const windowed = calls.slice(-window - 4); // over-capture: ghosts shrink back toward `window`
  const rows = windowed.slice(-window);
  const one = (r) => ({
    genTps: r.genTps, ttftS: r.ttft, prefillTps: r.prefillTps,
    inTok: r.inTok, outTok: r.out, outEst: r.est,
  });
  const med = (arr) => {
    if (!arr.length) return null;
    const s = [...arr].sort((a, b) => a - b);
    const m = s.length >> 1;
    return s.length % 2 ? s[m] : (s[m - 1] + s[m]) / 2;
  };
  const okGts = rows.filter((r) => r.genTps !== null && !r.est && r.genTps < SPEED_SANITY_TPS).map((r) => r.genTps);
  const okTtft = rows.filter((r) => r.ttft !== null && !r.gapBefore).map((r) => r.ttft);
  const okPre = rows.filter((r) => r.prefillTps !== null && !r.gapBefore && r.prefillTps < SPEED_SANITY_PREFILL).map((r) => r.prefillTps);
  const last = rows[rows.length - 1];
  return {
    last: one(last),
    med: { genTps: med(okGts), ttftS: med(okTtft), prefillTps: med(okPre) },
    nReal: okGts.length,          // calls the median behind the headline number uses
    n: rows.length,
    excluded: rows.filter((r) => r.est || (r.genTps !== null && r.genTps >= SPEED_SANITY_TPS)).length,
    series: rows.map((r) => ({
      ...one(r), gapBefore: !!r.gapBefore,
      untrustworthy: r.est || r.gapBefore || r.genTps === null || r.genTps >= SPEED_SANITY_TPS,
    })),
  };
}

/**
 * Fold a flat event list into ordered render items.
 * @param {Array<Object>} events
 * @returns {Array<Object>}
 */
/**
 * Pulse-to-pulse messages arrive as user_message events wearing the daemon's
 * inbox prefix (pulsar/daemon/messages.py):
 *   [message from 'Title' (pulse <id>)] <text>
 * The quotes are Python repr — single normally, double when the title itself
 * contains an apostrophe — hence the alternation. Matching it lets the pane
 * credit the sender instead of labelling the message YOU.
 */
export const PEER_RE = /^\[message from (['"])([\s\S]*?)\1 \(pulse ([0-9a-f-]+)\)\]\s?/;

/**
 * Daemon watch fires (condwatch.py "[watch: note]", watcher.py
 * "[delegation watch: ...]") also arrive as user_message events. They are
 * credited to WATCHER, not YOU. The bracket label stays in the body —
 * it carries the note.
 */
export const WATCH_RE = /^\[(?:delegation )?watch\b[^\]]*\]/;

/**
 * Daemon-injected messages (condition watches, delegation watches, handoff
 * and loop-guard seeds, inbox item pushes) land as user_message events too,
 * so they render as YOU — but the user did not type them directly. Every one
 * starts with a bracketed label; the one bracketed wrapper that IS the user's
 * own words is an Inbox reply (`[<owner> replied via Inbox ...]`).
 * Two tests: the daemon's own label words up front (watch notes can run to
 * hundreds of characters, so no length cap on the label), and a generic
 * "[label]" opener as a fallback for any injector this list doesn't know.
 */
export const DAEMON_LABEL_RE = /^\s*\[(watch\b|delegation watch\b|message from\b|pulsar\b)/i;
export const BRACKET_RE = /^\s*\[[^\]\n]+\]/;

export function isAutomated(text) {
  const t = text ?? '';
  if (/^\s*\[[^\]\n]+\s+replied via Inbox\b/i.test(t)) return false;
  return DAEMON_LABEL_RE.test(t) || BRACKET_RE.test(t);
}

/**
 * Field-first classification: the daemon stamps
 * `origin` + `via` (+ `from_pulse_id`/`from_title` for peers) into the inbox
 * file and the runner copies them onto the user_message event. Returns null
 * when the legacy text rules must decide: no fields (old events), an unknown
 * origin/via pair, and the three rows whose text is whatever the user or the
 * schedule owner typed (user/pane, user/first_message, daemon/schedule).
 */
function classifyByOrigin(ev, text) {
  const { origin, via } = ev ?? {};
  if (origin === 'user') {
    if (via === 'inbox_reply') return { kind: 'you', automated: false, from: null, fromId: null, body: text };
    if (via === 'question') return { kind: 'you', automated: true, from: null, fromId: null, body: text };
  } else if (origin === 'peer') {
    if (via !== 'pulse' && via !== 'first_message' && via !== 'schedule') return null;
    if (typeof ev.from_title !== 'string' || typeof ev.from_pulse_id !== 'string' || !ev.from_pulse_id) return null;
    const m = PEER_RE.exec(text);
    return { kind: 'peer', automated: true, from: ev.from_title, fromId: ev.from_pulse_id, body: m ? text.slice(m[0].length) : text };
  } else if (origin === 'daemon') {
    if (via === 'watch' || via === 'condwatch') {
      return { kind: 'watcher', automated: true, from: 'watcher', fromId: null, body: text };
    }
    if (via === 'handoff' || via === 'loopguard') {
      return { kind: 'you', automated: true, from: null, fromId: null, body: text };
    }
  }
  return null;
}

/**
 * Classify a user_message event into who sent it and whether it is automated.
 * Reads the origin fields when the event carries them, else the text rules.
 * @param {Object} ev
 * @returns {{ kind: 'peer'|'watcher'|'you', automated: boolean, from: string|null, fromId: string|null, body: string }}
 */
export function classifyUserMessage(ev) {
  const text = ev?.text ?? '';
  const byOrigin = classifyByOrigin(ev, text);
  if (byOrigin) return byOrigin;
  if (WATCH_RE.test(text)) {
    return { kind: 'watcher', automated: true, from: 'watcher', fromId: null, body: text };
  }
  const peer = PEER_RE.exec(text);
  if (peer) {
    return { kind: 'peer', automated: true, from: peer[2], fromId: peer[3], body: text.slice(peer[0].length) };
  }
  return { kind: 'you', automated: isAutomated(text), from: null, fromId: null, body: text };
}

export function isRealYou(ev) {
  if (ev?.type !== 'user_message') return false;
  // A schedule's prompt is not something you said.
  if (String(ev.msg_file ?? '').startsWith('sched-')) return false;
  const t = ev.text ?? '';
  if (!t && !(Array.isArray(ev.attachments) && ev.attachments.length)) return false;
  const c = classifyUserMessage(ev);
  return c.kind === 'you' && !c.automated;
}

/**
 * The daemon's own notices (handoff requests "[pulsar] …", successor seeds
 * "[pulsar handoff] …") are credited to PULSAR, not YOU (a model-switch
 * request showed as a YOU bubble the tester never wrote).
 */
export const PULSAR_RE = /^\[pulsar(?: handoff)?\]/;

/**
 * Reasoning summaries as plain lines: Luna sends "**Planning X****Checking
 * Y**" — raw bold marks, run together. One summary per line.
 * @param {string} text
 */
export function readableThinking(text) {
  return String(text ?? '')
    .replace(/\*\*(?=\*\*)/g, '**\n')
    .replace(/\*\*([^*\n]+)\*\*/g, '$1');
}

/**
 * A question-strip answer as people read it: "Alex answered “Pick one” → Blue",
 * not the model-facing "[… answered your question <hex id>] Q: … A: …".
 * Anything else comes back unchanged.
 * @param {string} text
 */
export const ANSWER_RE = /^\[(.+?) answered your question [0-9a-f]+\] Q: ([\s\S]*?)\nA: ([\s\S]*)$/;
export const DISMISS_RE = /^\[(.+?) dismissed your question [0-9a-f]+ without an answer[^\]]*\] Q: ([\s\S]*)$/;
export function answerLine(text) {
  const m = ANSWER_RE.exec(text ?? '');
  if (m) return `${m[1]} answered “${m[2]}” → ${m[3]}`;
  const d = DISMISS_RE.exec(text ?? '');
  return d ? `${d[1]} dismissed “${d[2]}” without an answer` : text;
}

/**
 * One plain line for a Pulsar notice; the full model-facing text stays behind
 * a tap (a switch showed the whole instruction list and a raw JSONL
 * transcript tail with a server path).
 * @param {string} text
 */
/**
 * One line for a daemon watch that fired: "Watch fired: file notes.md". The
 * model-facing text (path, epoch mtime, watch id, mode) stays behind a tap.
 * Other watcher notices keep their text as is.
 * @param {string} text
 */
export function watchSummary(text) {
  const m = /^\[watch: ([^\]]*)\] 🔔 condition met/.exec(text ?? '');
  if (!m) return '';
  // No note: the label is the bare kind, so name the path/url from the body.
  const what = /^(file|log|http|cmd)$/.test(m[1])
    ? /\b(?:path|url|cmd): ([^,]+?)(?:,|\. \(Watch id|$)/.exec(text)?.[1]
    : '';
  return `Watch fired: ${plainWatchText(what ? `${m[1]} ${what}` : m[1])}`;
}

export const SCHED_RE = /^\[pulsar-wait\]/;
/**
 * One line for a scheduled wake: "Reminder: check the photos ticket".
 * The full prompt stays behind a tap.
 * @param {string} text
 */
export function scheduleSummary(text) {
  const t = String(text ?? '');
  // `pulsar-wait in` notes are reminders; loop/daily prompts are scheduled
  // work (email triage is not a "Reminder").
  const label = SCHED_RE.test(t) ? 'Reminder' : 'Scheduled';
  // The note often starts "Reminder:" already (avoid "Reminder: Reminder:").
  const body = t.replace(SCHED_RE, '').trim().split('\n')[0].replace(/^reminder\s*:\s*/i, '');
  const line = body.length > 120 ? `${body.slice(0, 119)}…` : body;
  return line ? `${label}: ${line}` : 'Scheduled wake';
}

/** A command cut off by ■ stop: neutral "stopped", not a red failure.
 *  Only unfinished commands, or ones that died of the kill (137/143). */
function markStopped(t) {
  if (!t.done) {
    t.stopPending = true;
    return;
  }
  const killed = t.exitCode === 137 || t.exitCode === 143;
  const cutOff = t.stopPending && t.err && t.exitCode == null;
  t.stopPending = false;
  if (!killed && !cutOff) return; // finished on its own: keep its real outcome
  t.stopped = true;
  t.err = false;
  t.res = '→ stopped';
}

export function pulsarSummary(text) {
  const t = text ?? '';
  if (t.startsWith('[pulsar handoff]')) {
    return /FORCED/.test(t)
      ? 'Fresh session started from the saved chat history'
      : 'Fresh session started from the previous session\'s notes';
  }
  if (/switching to another model/.test(t)) return 'Asked the current model for notes before switching models';
  if (/fresh session was requested/.test(t)) return 'Asked the pulse for handoff notes — you asked for a fresh session';
  if (/context is nearly full/.test(t)) return 'Asked the pulse for handoff notes — its memory is nearly full';
  return 'Message from Pulsar';
}

/**
 * Fold runs of machine chatter into one expandable item so replies stand out
 * instead of getting buried under repeated daemon watch notices.
 *
 * Three rules keep this honest, and they are why it is a pass over the finished
 * list rather than logic inside the event loop:
 *   - NEVER drops data. Every folded item rides along on `items` and the fold
 *     renders them in order when tapped.
 *   - Only WATCHER rows fold. A named pulse ("Scout", a helper) is a
 *     correspondent, and collapsing its messages would hide delivery content;
 *     tool lines are one dim line already and fold into nothing worth the risk
 *     to the per-turn changes strip.
 *   - NEVER folds an item the pane already renders as an expandable control
 *     (kind 'tool' — ToolLine/ToolCard own their own tap), and it keeps the
 *     `you`/`peer` ordinal stream intact: `turnChatHistory` counts one item per
 *     user_message to line a turn up with its `done`, so folding a NON-watcher
 *     you/peer would misalign the turn clocks in the sent-message history.
 *     Watcher rows keep counting: the fold carries `count`, which the pane adds
 *     back (see the ordinal loop in ChatPane).
 *
 * Runs of NOISE_FOLD_MIN and up become a fold; below that a fold would save no
 * vertical space and would hide that a notice even arrived.
 *
 * @param {Array<Object>} items   finished list from buildItems
 * @param {number} [min]          fold threshold (2 = also fold pairs, for tests)
 * @returns {Array<Object>}        same items, with fold items substituted
 */
export function foldNoise(items, min = NOISE_FOLD_MIN) {
  const out = [];
  let run = [];
  const flush = () => {
    if (run.length >= min) {
      out.push(makeFold(run));
    } else {
      out.push(...run);
    }
    run = [];
  };
  for (const it of items ?? []) {
    if (noiseLabel(it)) {
      run.push(it);
    } else {
      flush();
      out.push(it);
    }
  }
  flush();
  return out;
}

/** Below this many consecutive chatter rows, don't fold — it saves nothing. */
export const NOISE_FOLD_MIN = 3;

/**
 * The row class a folded run contains, or '' when the item must not fold.
 */
export function noiseLabel(item) {
  if (!item) return '';
  // Only daemon watch chatter folds — it is the row class that repeats verbatim.
  // Tool lines are already one dim line each (and "tool detail" off strips
  // them entirely), and folding them would break the per-turn changes strip,
  // so they stay as they are.
  return item.kind === 'peer' && item.from === 'watcher' ? 'watcher' : '';
}

function makeFold(run) {
  const labels = [...new Set(run.map(noiseLabel))];
  const first = run[0];
  const last = run[run.length - 1];
  return {
    kind: 'fold',
    labels,
    count: run.length,
    ts: first.ts,
    lastTs: last.ts,
    items: run,
    // The daemon watch carries its note in the body, so the collapsed line
    // still says WHAT happened, not just how many times.
    head: first.summary || oneLine(first.text),
    err: false
  };
}

/** First line, clipped — a collapsed row must never wrap. */
function oneLine(text) {
  const s = (text ?? '').split('\n')[0].trim();
  return s.length > 90 ? `${s.slice(0, 90)}\u2026` : s;
}

export function buildItems(events) {
  /** @type {Array<Object>} */
  const items = [];
  /** @type {Map<string, Object>} id -> tool item, for result matching */
  const toolsById = new Map();
  const liveIndex = liveToolIndex(events ?? []);

  // Silent-turn boundary: a turn that ends tool-only renders no reply bubble,
  // so a finished command would look indistinguishable from an ignored one.
  // Track each turn from its user_message; when its done arrives with zero
  // replies, emit a dim boundary marker. turnOpen guards two honesty holes:
  // a tail that starts mid-turn may have truncated replies away, and a trailing
  // second done must not double-mark.
  let turnOpen = false;
  let turnReplies = 0;
  let turnTools = 0;
  // Tool items of the open turn: a user stop kills the running command, and
  // its exit 137/143 is the stop, not a failure.
  let turnToolItems = [];
  // Replies to Pulsar's "write your handoff notes" ask fold behind one line:
  // they are notes for the next session, not an answer to you.
  let notesTurn = false;

  let eventIndex = 0;
  for (const ev of events ?? []) {
    if (ev.type === 'user_message') {
      notesTurn = PULSAR_RE.test(ev.text ?? '') && /^Asked /.test(pulsarSummary(ev.text));
      turnOpen = true;
      turnReplies = 0;
      turnTools = 0;
      turnToolItems = [];
    } else if (ev.type === 'assistant_text' && (ev.text ?? '').trim()) {
      turnReplies++;
    } else if (ev.type === 'done') {
      if (turnOpen) {
        const interrupted = ev.reason === 'interrupted' || ev.subtype === 'interrupted';
        // An interrupted turn is marked even when it already replied: a
        // partial reply followed by a kill mid-tool-call must not read as a
        // finished answer.
        if (turnReplies === 0 || interrupted) {
          items.push({ kind: 'turnend', tools: turnTools, replies: turnReplies, interrupted, ts: ev.ts });
        }
      }
      turnOpen = false;
      turnToolItems = [];
    } else if (ev.type === 'pulse_stopped') {
      // A user stop ends the turn with no done event. Only the
      // command running at the stop is its victim: unfinished ones, or the
      // last one if it died of the kill (not earlier real 137s).
      if (turnOpen) {
        for (const t of turnToolItems) if (!t.done) markStopped(t);
        const last = turnToolItems[turnToolItems.length - 1];
        if (last?.done) markStopped(last);
      }
      turnToolItems = [];
      if (turnOpen) items.push({ kind: 'turnend', tools: turnTools, replies: turnReplies, interrupted: true, ts: ev.ts });
      turnOpen = false;
    }
    switch (ev.type) {
      case 'user_message': {
        const attachments = Array.isArray(ev.attachments) ? ev.attachments : [];
        const text = ev.text ?? '';
        const c = classifyUserMessage(ev);
        if (String(ev.msg_file ?? '').startsWith('sched-') || SCHED_RE.test(text)) {
          // A schedule / `pulsar-wait in` wake is not something you typed.
          items.push({ kind: 'peer', from: 'watcher', fromId: null, text, summary: scheduleSummary(text), attachments, ts: ev.ts });
        } else if (c.kind === 'watcher') {
          items.push({ kind: 'peer', from: c.from, fromId: c.fromId, text: c.body, summary: watchSummary(text), attachments, ts: ev.ts });
        } else if (PULSAR_RE.test(text)) {
          items.push({ kind: 'peer', from: 'pulsar', fromId: null, text, summary: pulsarSummary(text), attachments, ts: ev.ts });
        } else if (c.kind === 'peer') {
          items.push({ kind: 'peer', from: c.from, fromId: c.fromId, text: c.body, attachments, ts: ev.ts });
        } else {
          items.push({
            kind: 'you',
            text: answerLine(c.body),
            // Sent while a turn ran and steered into it.
            injected: !!ev.injected,
            // Uploads ride the event: [{filename, url, size, mime_type}] — the
            // pane renders image thumbnails / file chips under the text.
            attachments,
            // Reply button: {text, ts?} of the earlier message the user answered.
            replyTo: ev.reply_to?.text ? ev.reply_to : null,
            ts: ev.ts
          });
        }
        break;
      }

      case 'assistant_text': {
        // Whitespace-only assistant messages (qwen-in-pi emits "\n\n\n" next
        // to every tool call) are not replies — skip them so old recorded
        // events don't render one empty REPLY bubble per tool call either.
        if (!(ev.text ?? '').trim()) break;
        // Grok flushes commentary in sentence-sized chunks; keep one bubble
        // until a tool (or user message) interrupts, matching Fable's blocks.
        const prev = items.length ? items[items.length - 1] : null;
        if (prev && prev.kind === 'reply') {
          prev.text = (prev.text || '') + (ev.text ?? '');
        } else {
          items.push({ kind: 'reply', text: ev.text ?? '', ts: ev.ts, ...(notesTurn ? { notes: true } : {}) });
        }
        break;
      }

      case 'reasoning': {
        // One finished thinking block: sits in order above the reply / tool
        // call it led to. Never merged with a neighbour — each block is its
        // own card. A reply after it starts a new bubble.
        if (!(ev.text ?? '').trim()) break;
        items.push({ kind: 'thinking', text: ev.text ?? '', ts: ev.ts });
        break;
      }

      case 'tool_call': {
        const line = toolCallLine(ev);
        const raw = rawFor(ev);
        const existing = ev.id ? toolsById.get(ev.id) : null;
        // Grok ACP repeats the same toolCallId as status-only updates
        // (name:"tool", no args). Fold those into the first line; never
        // let a placeholder overwrite a real verb+arg.
        if (existing) {
          if (liveIndex === eventIndex) existing.live = true;
          if (isPlaceholderToolLine(existing.line) && !isPlaceholderToolLine(line)) {
            existing.line = line;
          }
          if (raw && raw !== 'tool' && raw.length >= (existing.raw || '').length) {
            existing.raw = raw;
          }
          if (!existing.card) existing.card = cardFor(ev);
          break;
        }
        const item = {
          kind: 'tool',
          id: ev.id ?? null,
          line,
          res: '', // filled by tool_result
          raw,
          // Visual-tools card (toolcards.js): null when the call carries
          // nothing beyond the plain line, so the pane falls back to ToolLine.
          card: cardFor(ev),
          err: false,
          live: liveIndex === eventIndex,
          ts: ev.ts
        };
        items.push(item);
        turnToolItems.push(item);
        if (ev.id) toolsById.set(ev.id, item);
        turnTools++; // folded same-id updates above don't re-count
        break;
      }

      case 'tool_result': {
        const target = ev.id ? toolsById.get(ev.id) : null;
        const snippet = decodeToolSnippet(ev.snippet ?? ev.result ?? ev.output ?? '');
        const fl = resultSummaryLine(snippet);
        if (target) {
          target.res = fl ? `→ ${fl}` : '→ (done)';
          if (ev.ok === false) target.err = true;
          if (snippet) target.raw = (target.raw ? target.raw + '\n' : '') + snippet;
          target.resultSnippet = snippet;
          if (ev.truncated) target.truncated = true;
          if (target.card?.kind === 'shell') {
            // pi never fills exit_code; recover it from the result text.
            target.exitCode = ev.exit_code ?? exitCodeFrom(snippet);
            if (target.exitCode != null && target.exitCode !== 0) target.err = true;
          }
          target.done = true;
          if (target.stopPending) markStopped(target); // result landed after the stop
        } else {
          // Orphan result (no matching call in this tail window) — show it plainly
          // rather than dropping it silently.
          items.push({
            kind: 'tool',
            id: ev.id ?? null,
            line: fl ? `→ ${fl}` : '(result)',
            res: '',
            raw: String(snippet),
            resultSnippet: snippet,
            truncated: !!ev.truncated,
            err: ev.ok === false,
            live: false,
            ts: ev.ts
          });
        }
        break;
      }

      case 'backup': {
        // The marker of the removed answer-once backup engine: the primary
        // was unavailable, so a second model answered the queued messages
        // once, from a digest. Kept so old transcripts still render.
        // Its failures came through runner_error with a 'backup:' prefix.
        const who = ev.model ?? 'backup model';
        const why = ev.reason ? ` — primary unavailable (${ev.reason})` : '';
        items.push({
          kind: 'tool',
          id: null,
          line: `⚡ backup · ${who} answered${why}`,
          res: '',
          raw: JSON.stringify(ev, null, 2),
          err: false,
          live: false,
          ts: ev.ts
        });
        break;
      }

      case 'failover': {
        // The one line for a model failover: the
        // pulse's model failed and the daemon moved it to its backup.
        // Failed failovers come through runner_error (where 'failover').
        const why = ev.reason ? ` (${ev.reason})` : '';
        items.push({
          kind: 'tool',
          id: null,
          line: `⇄ failover · ${ev.from ?? 'the model'} failed${why} — switched to ${ev.to ?? 'the backup model'}`,
          res: '',
          raw: JSON.stringify(ev, null, 2),
          err: false,
          live: false,
          ts: ev.ts
        });
        break;
      }

      case 'pulse_spawned':
        // Daemon marker: this pulse spawned another. The pane draws it as a
        // /?pulse-focus= link, so it never depends on the model's reply.
        if (!ev.pulse_id) break;
        items.push({
          kind: 'spawned',
          pulseId: String(ev.pulse_id),
          title: String(ev.title || ev.pulse_id),
          model: ev.model ? String(ev.model) : '',
          ts: ev.ts
        });
        break;

      case 'bookmark': {
        // Daemon marker for the pane's bookmark button: marks the line where
        // a "what happened since I left" period starts.
        const since = ev.summary_since
          ? fmtBookmarkTime(ev.summary_since, undefined, { withZone: true })
          : '';
        items.push({
          kind: 'tool',
          id: null,
          line: ev.cleared
            ? '🔖 bookmark cleared'
            : since
            ? `🔖 new bookmark — summary asked for everything since ${since}`
            : '🔖 bookmark set — tap 🔖 again when you are back for a summary of everything after this line',
          res: '',
          raw: JSON.stringify(ev, null, 2),
          err: false,
          live: false,
          ts: ev.ts
        });
        break;
      }

      case 'context_reset':
        // Daemon marker for a reset: everything above is gone from
        // the pulse's memory, and the pane must say so.
        items.push({
          kind: 'tool',
          id: null,
          line: '↻ context reset — the pulse starts fresh and no longer remembers anything above',
          res: '',
          raw: JSON.stringify(ev, null, 2),
          err: false,
          live: false,
          ts: ev.ts
        });
        break;

      case 'pulse_stopped':
        // Daemon marker for ■ stop: the pane showed nothing while
        // stopped, and restore picked the cut-off turn up again.
        items.push({
          kind: 'tool',
          id: null,
          line: '■ stopped by you — the running turn was dropped and won\'t re-run on restore',
          res: '',
          raw: JSON.stringify(ev, null, 2),
          err: false,
          live: false,
          ts: ev.ts
        });
        break;

      case 'fence_breach':
      case 'runner_error': {
        // Retries of one failure repeat the same line; one send showed four
        // identical red lines. Fold a repeat into the line above.
        const line = runnerErrorLine(ev);
        const prev = items[items.length - 1];
        if (prev && prev.kind === 'tool' && prev.err && prev.baseLine === line) {
          prev.repeat = (prev.repeat ?? 1) + 1;
          prev.line = `${line} (×${prev.repeat})`;
          prev.ts = ev.ts;
          break;
        }
        items.push({
          kind: 'tool',
          id: null,
          line,
          baseLine: line,
          res: '',
          raw: JSON.stringify(ev, null, 2),
          err: true,
          live: false,
          ts: ev.ts
        });
        break;
      }

      // usage / done / session — not rendered as lines.
      default:
        break;
    }
    eventIndex++;
  }

  return items;
}

function hashText(s) {
  let h = 2166136261;
  const str = String(s ?? '');
  for (let i = 0; i < str.length; i++) {
    h ^= str.charCodeAt(i);
    h = Math.imul(h, 16777619);
  }
  return (h >>> 0).toString(36);
}

function baseItemKey(item) {
  const ts = item.ts ?? '';
  switch (item.kind) {
    case 'tool':
      return item.id != null ? `tool:id:${item.id}` : `tool:${ts}:${hashText(item.baseLine ?? item.line)}`;
    case 'thinking':
      return `thinking:${ts}:${hashText(item.text)}`;
    case 'reply':
      return `reply:${ts}:${item.notes ? 'n' : 'r'}`;
    case 'you':
      return `you:${ts}:${hashText(item.text)}`;
    case 'peer':
      return `peer:${ts}:${item.from ?? ''}:${hashText(item.text)}`;
    case 'spawned':
      return `spawned:${ts}:${item.pulseId ?? ''}`;
    case 'turnend':
      return `turnend:${ts}:${item.interrupted ? 1 : 0}`;
    case 'fold':
      return `fold:${ts}:${hashText(item.head)}`;
    default:
      return `${item.kind ?? 'item'}:${ts}`;
  }
}

/**
 * Attach a stable, unique `key` to each rendered transcript item so prepending
 * older scrollback above the loaded tail preserves open card/fold state.
 * @param {Array<Object>} items
 * @returns {Array<Object>}
 */
export function keyItems(items) {
  const seen = new Map();
  return (items ?? []).map((item) => {
    const base = baseItemKey(item);
    const n = seen.get(base) ?? 0;
    seen.set(base, n + 1);
    return { ...item, key: n === 0 ? base : `${base}#${n}` };
  });
}

/** Raw expand payload for a tool_call (args pretty-printed). */
function rawFor(ev) {
  const parts = [];
  if (ev.name) parts.push(ev.name);
  const args = ev.args ?? ev.input;
  if (args && Object.keys(args).length) parts.push(JSON.stringify(args, null, 2));
  return parts.join(' ');
}

/**
 * Derive a pulse's honest live status from the daemon's polled `live` block
 * (never cry stalled while thinking).
 *
 * Every field here is observable in the polled data — no client-invented
 * states, no faked-progress timers:
 *   - live.socket_state          "starting"|"idle"|"busy"|... (unix socket)
 *   - live.heartbeat.state       same, via the heartbeat file when socket down
 *   - live.busy_since            epoch SECONDS the runner began the current turn
 *                                (or live.heartbeat.busy_since); null/absent idle
 *   - live.reachable             runner socket answered
 *   - live.heartbeat_fresh       heartbeat file written recently
 *   - live.heartbeat_age_s       seconds since heartbeat last written
 *
 * Returns one of six phases:
 *   'thinking' — busy + a live process. label "working · Ns" (N ticks from
 *                busy_since), or plain "working" when busy_since is absent
 *                (old runners predate the field). NEVER "stalled" here.
 *   'stalled'  — real trouble ONLY: running but (not reachable AND heartbeat
 *                stale) → process dead/unresponsive. Carries `evidence`.
 *   'idle'     — running & fresh, not busy. (Caller renders its idle style.)
 *   'offline'  — no live block at all (stopped/archived; caller decides).
 *
 * @param {Object|null} live   the pulse's `live` block (null when not running)
 * @param {number} nowMs       Date.now(), passed in so it ticks with the poll
 * @returns {{phase:'waking'|'thinking'|'stalled'|'idle'|'stopping'|'stopped'|'offline',
 *            thinking:boolean, stalled:boolean,
 *            seconds:number|null, label:string, evidence:string|null}}
 */
export function pulseStatus(live, nowMs) {
  if (!live) {
    return { phase: 'offline', thinking: false, stalled: false, seconds: null, label: '', evidence: null };
  }

  const hb = live.heartbeat ?? null;
  const rawState = live.socket_state ?? hb?.state;
  // cli_dead = the runner recorded a failure (the agent tool did not start or
  // died). A plain 'stopped' beat is a normal stop (pause, reset, handoff).
  if (rawState === 'cli_dead') {
    const evidence = hb?.reason || 'agent tool stopped';
    return { phase: 'stopped', thinking: false, stalled: true, seconds: null, label: 'stopped', evidence };
  }
  // Busy is observed from the live socket, or the heartbeat file if the socket
  // is down. Either signal proving the runner is feeding a turn counts — and
  // it OUTRANKS a starting/stopping reading from the other, older source
  // (probe paths can pair a live socket answer with a stale heartbeat copy).
  const busy = live.socket_state === 'busy' || hb?.state === 'busy';

  if (busy) {
    // busy_since is epoch SECONDS (float). Prefer the socket copy, fall back to
    // the heartbeat copy. Absent on runners that predate the field → no number.
    const sinceSec = live.busy_since ?? hb?.busy_since ?? null;
    if (sinceSec == null) {
      // Never invent a number — just "thinking".
      return { phase: 'thinking', thinking: true, stalled: false, seconds: null, label: 'working', evidence: null };
    }
    const seconds = Math.max(0, Math.round(nowMs / 1000 - sinceSec));
    return { phase: 'thinking', thinking: true, stalled: false, seconds, label: `working · ${fmtClock(seconds * 1000)}`, evidence: null };
  }

  // Not busy. "stalled" is reserved for a dead/unresponsive process: the socket
  // is unreachable AND the heartbeat file has gone stale. Both must fail — a
  // reachable-but-idle process is idle, and a fresh heartbeat means the runner
  // is alive even if the socket momentarily didn't answer.
  const reachable = live.reachable === true;
  const hbFresh = live.heartbeat_fresh === true;
  // starting/stopping are honest only while the process is provably alive —
  // a heartbeat file frozen at "starting" by a dead-at-startup runner must
  // fall through to the stalled check below, not read as calm "waking…".
  if (reachable || hbFresh) {
    const state = live.socket_state ?? hb?.state;
    if (state === 'starting') {
      return { phase: 'waking', thinking: false, stalled: false, seconds: null, label: 'waking…', evidence: null };
    }
    if (state === 'stopping') {
      return { phase: 'stopping', thinking: false, stalled: false, seconds: null, label: 'stopping…', evidence: null };
    }
  }
  if (!reachable && !hbFresh) {
    const ageS = live.heartbeat_age_s;
    const evidence =
      typeof ageS === 'number'
        ? `no heartbeat for ${Math.round(ageS)}s`
        : 'process unreachable, heartbeat stale';
    return { phase: 'stalled', thinking: false, stalled: true, seconds: null, label: 'stalled', evidence };
  }

  return { phase: 'idle', thinking: false, stalled: false, seconds: null, label: 'idle', evidence: null };
}

/**
 * Back-compat thin wrapper for consumers that only need the boolean+seconds
 * (Wall, MobileDeck peek dots). Derives from {@link pulseStatus}, so it honours
 * the heartbeat-file busy fallback too. Elapsed comes from the daemon's
 * busy_since, never a client timer.
 * @param {Object|null} live
 * @param {number} nowMs
 * @returns {{thinking: boolean, seconds: number}}
 */
export function thinkingState(live, nowMs) {
  const s = pulseStatus(live, nowMs);
  return { thinking: s.thinking, seconds: s.seconds ?? 0 };
}

/**
 * Rows for the "YOUR MESSAGES" history overlay: every real message the user
 * sent, newest first, each with its turn clock (send → the first `done` before
 * the next user_message), the tool calls that followed it and the opening of
 * the first reply.
 * @param {Array<Object>} evs raw event window
 * @returns {Array<{text: string, ts: any, attachments: any[], tools: number, reply: string, took: number, n: number}>}
 */
export function youHistoryRows(evs) {
  // Turn clock: user_message → the first `done` before the next
  // user_message. buildItems emits exactly one you/peer item per
  // user_message event, so ordinal k here lines up with the k-th such item.
  const doneTs = [];
  let openIdx = -1;
  for (const ev of evs) {
    if (ev.type === 'user_message') {
      openIdx = doneTs.push(null) - 1;
    } else if (ev.type === 'done' && openIdx >= 0 && doneTs[openIdx] === null) {
      doneTs[openIdx] = ev.ts ?? null;
    }
  }
  const all = buildItems(evs);
  const rows = [];
  let ordinal = -1;
  for (let i = 0; i < all.length; i++) {
    const it = all[i];
    // A fold stands in for N user_message rows (watch fires ride that event
    // type), so the turn ordinal must advance by the whole run or every turn
    // clock after it shifts (render.js foldNoise).
    ordinal += it.kind === 'fold' ? it.count : it.kind === 'you' || it.kind === 'peer' ? 1 : 0;
    if (it.kind !== 'you' || !(it.text || it.attachments?.length)) continue;
    if (classifyUserMessage(it).automated) continue;
    const endTs = doneTs[ordinal];
    const took = endTs && it.ts ? parseTs(endTs) - parseTs(it.ts) : NaN;
    let tools = 0;
    let reply = '';
    for (let j = i + 1; j < all.length; j++) {
      const nx = all[j];
      if (nx.kind === 'you' || nx.kind === 'peer') break;
      if (nx.kind === 'tool') tools++;
      else if (nx.kind === 'reply' && !reply && nx.text) reply = nx.text.trim().slice(0, 200);
    }
    rows.push({ text: it.text ?? '', ts: it.ts, attachments: it.attachments ?? [], tools, reply, took, n: rows.length + 1 });
  }
  rows.reverse();
  return rows;
}
