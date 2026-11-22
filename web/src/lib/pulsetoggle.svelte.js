/**
 * Pulse settings toggles and pulse actions, once.
 *
 * `pulseToggle` is "write a pulse setting, and hold the value against a stale
 * /live frame". The roster push can carry a snapshot from before the PATCH
 * and flip the box back for a moment (seen in the browser test, ~3 s), so a
 * saved value is held for PENDING_HOLD_MS. `value` is `pending ?? read(pulse)`.
 *
 * Options:
 *   hold   keep the saved value for PENDING_HOLD_MS; off for stream and reasoning
 *   guard  ignore set() while a write is in flight and expose `saving`
 *   before runs with the value right before the write
 *
 * `runPulseAction` is "run one pulse action, report `<label>: <error>`".
 * Each caller supplies its own onok / onerr; the drift between callers is
 * theirs to keep or fix.
 *
 * `.svelte.js` because `pending` and `saving` are $state: markup reads
 * `toggle.value` / `toggle.saving` and re-renders.
 */

export const PENDING_HOLD_MS = 6000;

/**
 * @param {object} o
 * @param {() => any} o.getPulse        current pulse (read at call time)
 * @param {(pulse: any) => any} o.read  value the pulse reports
 * @param {(id: string, v: any) => Promise<{ok: boolean, error?: string}>} o.write
 * @param {string} o.label              error prefix, e.g. 'streaming'
 * @param {boolean} [o.hold]
 * @param {boolean} [o.guard]
 * @param {(v: any) => void} [o.before]
 * @param {() => void} [o.onchanged]    called once on ok
 * @param {(message: string) => void} [o.onerror]
 */
export function pulseToggle({ getPulse, read, write, label, hold = true, guard = true, before, onchanged, onerror }) {
  let pending = $state(/** @type {any} */ (null));
  let saving = $state(false);

  async function set(v) {
    if (guard && saving) return;
    if (guard) saving = true;
    if (hold) pending = v;
    before?.(v);
    const r = await write(getPulse().id, v);
    if (guard) saving = false;
    if (r.ok) {
      onchanged?.();
      if (hold) setTimeout(() => { if (pending === v) pending = null; }, PENDING_HOLD_MS);
    } else {
      if (hold) pending = null;
      onerror?.(`${label}: ${r.error}`);
    }
  }

  return {
    get value() {
      return pending ?? read(getPulse());
    },
    get saving() {
      return saving;
    },
    set,
  };
}

/**
 * @template {{ok: boolean, error?: string}} R
 * @param {string} label
 * @param {() => Promise<R>} fn
 * @param {{ onok?: (r: R) => any, onerr?: (message: string, r: R) => any }} [cb]
 * @returns {Promise<R>}
 */
export async function runPulseAction(label, fn, { onok, onerr } = {}) {
  const r = await fn();
  if (r.ok) await onok?.(r);
  else await onerr?.(`${label}: ${r.error}`, r);
  return r;
}
