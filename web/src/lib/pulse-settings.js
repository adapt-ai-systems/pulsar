/**
 * Pure per-pulse settings predicates and defaults.
 * Kept free of fetch and localStorage so callers and tests can import without side effects.
 */

/**
 * UI gate for live streaming. On unless this pulse explicitly turned it off,
 * so the pane reads partial.json and reasoning.json live instead of waiting
 * for finished blocks.
 * @param {{vessel?: string, model?: string, settings?: {stream_partials?: boolean}}|null|undefined} pulse
 * @returns {boolean}
 */
export function streamPartialsOn(pulse) {
  return pulse?.settings?.stream_partials !== false;
}

/**
 * UI gate for reasoning traces. On unless this pulse explicitly turned them
 * off (the setting is a pure off switch).
 * @param {{vessel?: string, model?: string, settings?: {stream_reasoning?: boolean}}|null|undefined} pulse
 * @returns {boolean}
 */
export function reasoningOn(pulse) {
  const v = pulse?.settings?.stream_reasoning;
  if (v === true || v === false) return v;
  return true;
}

/**
 * UI gate for the live code card (tool-call arguments as the model writes
 * them). On unless this pulse explicitly turned it off.
 * @param {{settings?: {stream_code?: boolean}}|null|undefined} pulse
 * @returns {boolean}
 */
export function codeOn(pulse) {
  return pulse?.settings?.stream_code !== false;
}
