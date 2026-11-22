/**
 * One attention model for every pulse list (drawer, tabs, wall): a list you
 * multitask from needs to say, per pulse, "needs you" / "finished, not looked
 * at yet" / "working" / quiet.
 *
 * Phases, most urgent first:
 *   'question' — an open pulsar-ask question is waiting on the user
 *   'stalled'  — running but dead/unresponsive (render.js pulseStatus)
 *   'done'     — finished a turn and nobody has looked at it since
 *   'working'  — busy right now
 *   'idle'     — running, quiet, already seen
 *   'stopped'  — stopped or archived
 *
 * Pure: `pulseStatus` is injected so this file stays testable without Svelte.
 */
import { pulseStatus } from './render.js';

export const RANK = { question: 5, stalled: 4, done: 3, working: 2, idle: 1, stopped: 0 };

/**
 * @param {any} pulse            roster row (state, live)
 * @param {{questionPulseIds?: Set<string>, doneIds?: Set<string>, nowMs?: number}} ctx
 * @returns {'question'|'stalled'|'done'|'working'|'idle'|'stopped'}
 */
export function attentionOf(pulse, ctx = {}) {
  const { questionPulseIds, doneIds, nowMs = Date.now() } = ctx;
  if (pulse.state === 'archived' || pulse.state === 'stopped') return 'stopped';
  if (questionPulseIds?.has(pulse.id)) return 'question';
  const st = pulseStatus(pulse.live, nowMs);
  if (st.stalled) return 'stalled';
  if (st.thinking) return 'working';
  if (doneIds?.has(pulse.id)) return 'done';
  return 'idle';
}

/** The most urgent phase in a list — what a workspace header wears. */
export function rollUp(phases) {
  let best = null;
  for (const ph of phases) if (best === null || RANK[ph] > RANK[best]) best = ph;
  return best ?? 'stopped';
}

/** Sort key for "attention" order: urgent first, ties keep the caller's order. */
export function byAttention(phaseOf) {
  return (a, b) => RANK[phaseOf(b)] - RANK[phaseOf(a)];
}
