/**
 * The armed-confirm pattern, once.
 *
 * A dangerous action (pause / reset / delete / cross-vessel switch) arms a
 * "confirm?" button that AUTO-DISARMS after 12s — left armed, a stray tap
 * minutes later would fire it (found driving the real UI, 2026-07-26). 5s was
 * too short to read the strip: a careful click landed after it vanished, on
 * whatever icon had slid into its place.
 * Copy-pasted in ChatPane, SettingsModal and SessionDrawer until it was
 * extracted here (sweep 2026-09-02).
 *
 * `armed` is $state: templates read `store.armed` and re-render when arm()
 * fires or the timer disarms. (First cut used a plain variable — the button
 * never un-armed on screen. Runes are why this file is .svelte.js.)
 *
 * The auto-disarm timer is cleared on component unmount — a module-level
 * store must register its own cleanup via the caller's `onDestroy`.
 */
import { onDestroy } from 'svelte';

export const ARM_MS = 12000;

/**
 * @param {{ armed?: any, ms?: number, onDestroy?: (fn: () => void) => void }} [opts]
 *   armed     initial armed value — false for boolean confirms, null for
 *             per-row confirms keyed by id
 *   ms        how long the confirm stays armed (default ARM_MS)
 *   onDestroy cleanup registrar, default svelte's onDestroy (tests pass a noop)
 */
export function confirmArm({ armed = false, ms = ARM_MS, onDestroy: destroy = onDestroy } = {}) {
  let value = $state(armed);
  let timer;

  function disarm() {
    value = armed;
    clearTimeout(timer);
  }

  function arm(id) {
    value = id === undefined ? (armed === null ? null : true) : id;
    clearTimeout(timer);
    timer = setTimeout(disarm, ms);
  }

  destroy(disarm);

  return {
    get armed() {
      return value;
    },
    arm,
    disarm,
  };
}
