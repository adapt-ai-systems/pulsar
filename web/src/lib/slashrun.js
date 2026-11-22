import { resolveAt } from './loopcmd.js';

/**
 * Slash-command execution. The Composer parses the typed command (loopcmd.js parseSlashCommand) and hands the non-literal kinds here.
 * Feedback goes to the local notice / error strips; a command that worked is
 * cleared from the draft like a sent message (`clearDraft`).
 *
 * `api` is the four api.js calls this needs, injected so the table is
 * unit-testable under plain node. `refreshSchedules` re-reads the shared
 * schedule poll, `onchanged` refreshes the roster; both fire at the same point
 * the pane's inline version fired them (before the draft is cleared).
 *
 * @param {{kind: string, [k: string]: any}} cmd  a parseSlashCommand result, not 'literal'
 * @param {{
 *   pulse: {id: string, title?: string},
 *   api: { listSchedules: Function, createSchedule: Function, deleteSchedule: Function, renamePulse: Function },
 *   refreshSchedules?: () => void,
 *   onchanged?: () => void,
 *   now?: () => Date,
 * }} deps
 * @returns {Promise<{notice?: string, error?: string, clearDraft: boolean}>}
 */
export async function runSlashCommand(cmd, { pulse, api, refreshSchedules, onchanged, now = () => new Date() }) {
  if (cmd.kind === 'unknown') {
    return { notice: `unknown command ${cmd.command} — try /loop, /at, /rename ('//text' sends literally)`, clearDraft: false };
  }
  if (cmd.kind === 'usage') return { notice: cmd.text, clearDraft: false };
  if (cmd.kind === 'list') {
    const r = await api.listSchedules(pulse.id);
    if (!r.ok) return { error: `loop list: ${r.error}`, clearDraft: false };
    const loops = r.value.filter((s) => s.kind === 'loop');
    const notice = loops.length
      ? loops.map((s) => `${s.id} — every ${s.interval_minutes}m${s.enabled ? '' : ' (disabled)'} — "${s.prompt}"`).join('\n')
      : 'no loops on this pulse';
    return { notice, clearDraft: true };
  }
  if (cmd.kind === 'stop') {
    const r = await api.listSchedules(pulse.id);
    if (!r.ok) return { error: `loop stop: ${r.error}`, clearDraft: false };
    const loops = r.value.filter((s) => s.kind === 'loop');
    let target = null;
    if (cmd.idPrefix) {
      const matches = loops.filter((s) => s.id.startsWith(cmd.idPrefix));
      if (matches.length === 0) return { notice: `no loop id starting '${cmd.idPrefix}' — try /loop list`, clearDraft: false };
      if (matches.length > 1) {
        return {
          notice: `'${cmd.idPrefix}' matches several loops — be more specific:\n` +
            matches.map((s) => `${s.id} — "${s.prompt}"`).join('\n'),
          clearDraft: false,
        };
      }
      target = matches[0];
    } else if (loops.length === 1) {
      target = loops[0];
    } else if (loops.length === 0) {
      return { notice: 'no loops to stop', clearDraft: false };
    } else {
      return {
        notice: 'several loops — pick one: /loop stop <id>\n' +
          loops.map((s) => `${s.id} — "${s.prompt}"`).join('\n'),
        clearDraft: false,
      };
    }
    const d = await api.deleteSchedule(target.id);
    if (!d.ok) return { error: `loop stop: ${d.error}`, clearDraft: false };
    refreshSchedules?.();
    return { notice: `loop stopped — ${target.id} ("${target.prompt}")`, clearDraft: true };
  }
  if (cmd.kind === 'at') {
    // Relative times must be future ('in 0m' already rejected at parse);
    // the daemon fires a past at_iso on its next tick without complaint,
    // so resolveAt is the only guard (2026-09-15).
    const atIso = resolveAt(cmd.when, now());
    if (!atIso) {
      return { notice: 'that time is not in the future — use in <n>m, in <n>h, or a clock time (9:00, 9am, 5:30pm)', clearDraft: false };
    }
    const r = await api.createSchedule(pulse.id, { kind: 'once', prompt: cmd.prompt, at_iso: atIso });
    if (!r.ok) return { error: `at: ${r.error}`, clearDraft: false };
    const when = new Date(atIso);
    const tomorrow = when.toDateString() === now().toDateString() ? '' : 'tomorrow ';
    refreshSchedules?.();
    return {
      notice: `scheduled — ${tomorrow}${when.toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })} (${fmtLead(when - now().getTime())}): "${cmd.prompt}" (id ${r.value.id})`,
      clearDraft: true,
    };
  }
  if (cmd.kind === 'rename') {
    if (cmd.title === pulse.title) return { notice: 'already named that', clearDraft: false };
    const r = await api.renamePulse(pulse.id, cmd.title);
    if (!r.ok) return { error: `rename: ${r.error}`, clearDraft: false };
    onchanged?.();
    return { notice: `renamed to "${cmd.title}"`, clearDraft: true };
  }
  // create
  const existing = await api.listSchedules(pulse.id);
  if (existing.ok) {
    const dup = existing.value.find((s) => s.kind === 'loop' && s.enabled && s.prompt === cmd.prompt);
    if (dup) {
      return {
        notice: `already looping every ${dup.interval_minutes}m ("${cmd.prompt}", id ${dup.id}) — /loop stop ${dup.id} to replace`,
        clearDraft: false,
      };
    }
  }
  const r = await api.createSchedule(pulse.id, { kind: 'loop', prompt: cmd.prompt, interval_minutes: cmd.minutes });
  if (!r.ok) return { error: `loop: ${r.error}`, clearDraft: false };
  refreshSchedules?.();
  return { notice: `loop armed — every ${cmd.minutes}m: "${cmd.prompt}" (first fire in ~${cmd.minutes}m, id ${r.value.id})`, clearDraft: true };
}

// 'in 3h12m' style lead for the /at confirmation — the resolved local time
// plus the lead is the only guard against 'I meant tonight'.
export function fmtLead(ms) {
  const m = Math.max(1, Math.round(ms / 60000));
  if (m < 60) return `in ${m}m`;
  const h = Math.floor(m / 60), rm = m % 60;
  return `in ${h}h${rm ? `${rm}m` : ''}`;
}
