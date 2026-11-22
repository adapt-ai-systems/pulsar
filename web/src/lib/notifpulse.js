/**
 * The pulse a push notification names — how it may live outside the
 * polled/live roster, and what must never reorder with it. Pure + node-tested;
 * +page.svelte owns the DOM.
 */

/**
 * Where a notification tap should land, given the pulse it names.
 *
 * @param {Object|null|undefined} pulse  the roster row for the notified id
 *                 (undefined = the pulse isn't in the current roster)
 * @returns {'pulse'|'none'}
 */
export function notifiedPulseAction(pulse) {
  return pulse ? 'pulse' : 'none';
}

/**
 * A tap-to-open target resolved by id (an archived pulse) is rendered on top
 * of the roster so the next roster update can't take the pane away. It has no
 * server position, so it must never join a reorder payload — otherwise
 * movePane's id/slot mapping desyncs and writes `undefined` into the list.
 *
 * @param {Array<{id: string}>} visible  what the grid/deck currently shows
 * @param {string|null} extraId  the separately resolved pulse id (or null)
 * @returns {string[]} ids, without the injected target
 */
export function reorderableOrder(visible, extraId = null) {
  const ids = visible.map((p) => p.id);
  return extraId ? ids.filter((id) => id !== extraId) : ids;
}

/**
 * The roster is not a fact until it has arrived once — an empty list means
 * "still loading", not "no pulses", and a notification reveal must not decide
 * between those two. Pure so the rule is node-tested rather than prose.
 * @param {Object} input
 * @param {Array<unknown>} input.pulses
 * @param {boolean} input.connected  a roster fetch/socket result has landed
 * @param {boolean} input.errored    the latest roster fetch failed
 * @returns {boolean} true when an empty list really means no pulses
 */
export function rosterLoadedEmpty({ pulses, connected, errored }) {
  return Array.isArray(pulses) && pulses.length === 0 && connected === true
    && errored !== true;
}
