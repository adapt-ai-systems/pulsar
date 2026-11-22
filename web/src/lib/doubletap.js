// The double tap that opens the mobile overview. Shared by a pane's title bar
// and the app nav so the two gestures cannot drift apart.
//
// Why this is a module and not two copies of six lines: the hard part is not
// the timing, it is the CLICK that follows the second tap. The overview opens
// on `pointerup`, Svelte mounts the overlay before the browser dispatches the
// compatibility `click`, and that click lands on the overlay — whose whole job
// is "tap outside to close". So the overview opened and shut inside the same
// gesture. swallowNextClick eats exactly that one click.

export const DOUBLE_TAP_MS = 320;
export const DOUBLE_TAP_SLOP = 24; // px between the two taps

// A tap that lands on a control belongs to that control, never to a gesture —
// this is what keeps ⋯, the nav buttons and drag-to-reorder working.
const INTERACTIVE = 'button, a, input, select, textarea, [role="button"]';

/**
 * Eat the next click, capture-phase, before it reaches anything.
 * Returns a cancel function; also self-cancels on a timer so a gesture that
 * never produces a click doesn't leave a trap for an unrelated one later.
 */
export function swallowNextClick(target = globalThis.window) {
  if (!target?.addEventListener) return () => {};
  let timer;
  const kill = (e) => {
    e.stopPropagation();
    e.preventDefault();
    cancel();
  };
  const cancel = () => {
    target.removeEventListener('click', kill, true);
    clearTimeout(timer);
  };
  target.addEventListener('click', kill, true);
  timer = setTimeout(cancel, 700);
  return cancel;
}

/**
 * Build a `pointerup` handler that calls `fire` on the second of two taps
 * landing close together in time and space.
 * @param {(e: any) => void} fire
 * @param {{ swallow?: (t?: any) => void, vibrate?: boolean }} [opts]
 */
export function doubleTap(fire, { swallow = swallowNextClick, vibrate = true } = {}) {
  let last = null;
  return function onPointerUp(e) {
    if (e.target?.closest?.(INTERACTIVE)) {
      last = null;
      return;
    }
    const now = e.timeStamp;
    if (
      last &&
      now - last.t < DOUBLE_TAP_MS &&
      Math.abs(e.clientX - last.x) < DOUBLE_TAP_SLOP &&
      Math.abs(e.clientY - last.y) < DOUBLE_TAP_SLOP
    ) {
      last = null;
      swallow();
      // Haptic where it exists; silently absent on desktop and iOS Safari.
      if (vibrate) globalThis.navigator?.vibrate?.(12);
      fire(e);
      return;
    }
    last = { t: now, x: e.clientX, y: e.clientY };
  };
}
