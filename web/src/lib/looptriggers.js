/**
 * Loop triggers (daemon v25). A loop schedule fires on its timer
 * and/or on events: every handoff, every message the user sends, every N replies
 * the pulse makes, every N tool calls. Wire shape (schedule.triggers) is a
 * list of names: 'timer' | 'handoff' | 'message' | 'replies:N' | 'tools:N'.
 * trigger_mode is 'any' (first trigger fires; all reset) or 'all' (every
 * selected trigger must be due).
 *
 * The form keeps a flat object so checkboxes/inputs bind directly:
 *   { timer: bool, handoff: bool, message: bool, replies: '' | 'N', tools: '' | 'N' }
 * All functions are pure.
 */

export const TRIGGER_MODES = ['any', 'all'];

/** Default form state: a plain timer loop, exactly like pre-v25. */
export function defaultTriggerForm() {
  return { timer: true, handoff: false, message: false, replies: '', tools: '' };
}

/**
 * Wire list -> form object. null/empty/garbage -> timer only (the daemon's
 * own default).
 * @param {any} list
 */
export function triggerFormFromList(list) {
  const f = { timer: false, handoff: false, message: false, replies: '', tools: '' };
  if (!Array.isArray(list)) return defaultTriggerForm();
  for (const raw of list) {
    const t = String(raw ?? '').trim();
    if (t === 'timer' || t === 'handoff' || t === 'message') f[t] = true;
    const m = /^(replies|tools):(\d+)$/.exec(t);
    if (m && Number(m[2]) > 0) f[m[1]] = String(Number(m[2]));
  }
  if (!f.timer && !f.handoff && !f.message && !f.replies && !f.tools) return defaultTriggerForm();
  return f;
}

/**
 * Form object -> wire list, or an error string. Counter fields that are
 * blank are "off"; non-blank must be a positive integer.
 * @param {ReturnType<typeof defaultTriggerForm>} form
 * @returns {{list: string[]}|{error: string}}
 */
export function triggerListFromForm(form) {
  const f = form ?? {};
  const list = [];
  if (f.timer) list.push('timer');
  if (f.handoff) list.push('handoff');
  if (f.message) list.push('message');
  for (const name of ['replies', 'tools']) {
    const raw = String(f[name] ?? '').trim();
    if (raw === '') continue;
    const n = Number(raw);
    if (!Number.isInteger(n) || n <= 0) {
      return { error: `"every N ${name === 'tools' ? 'tool calls' : 'replies'}" needs a positive whole number.` };
    }
    list.push(`${name}:${n}`);
  }
  if (!list.length) return { error: 'A loop needs at least one trigger.' };
  return { list };
}

/** Clamp a mode string to 'any' | 'all'. */
export function normalizeTriggerMode(raw) {
  return raw === 'all' ? 'all' : 'any';
}

/**
 * Human label mirroring the daemon's loop_triggers.label():
 * "every 40 min, every message, every 20 tool calls" (+ " (all)" for all-mode).
 * @param {any} list   wire list (null = timer)
 * @param {number|null|undefined} minutes
 * @param {string} [mode]
 */
export function triggerLabel(list, minutes, mode = 'any') {
  const f = triggerFormFromList(list);
  const bits = [];
  if (f.timer && minutes) bits.push(`every ${minutes} min`);
  if (f.message) bits.push('every message');
  if (f.handoff) bits.push('every handoff');
  if (f.replies) bits.push(`every ${f.replies} replies`);
  if (f.tools) bits.push(`every ${f.tools} tool calls`);
  const joined = bits.join(', ');
  return normalizeTriggerMode(mode) === 'all' && bits.length > 1 ? `${joined} (all)` : joined;
}

/** True when the list is anything other than a plain timer loop. */
export function hasEventTriggers(list) {
  const f = triggerFormFromList(list);
  return !!(f.handoff || f.message || f.replies || f.tools);
}
