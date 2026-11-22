// Pure helpers behind the hold-to-drag gesture (Wall, drawer, full-screen
// window). The state machine that uses them is longpress.svelte.js.

export const LONG_PRESS_MS = 320;
export const PRESS_SLOP = 9; // px of movement that means "you meant to scroll"
export const SWALLOW_CLICK_MS = 400;

/** Has the pointer moved far enough from where the press began to be a scroll? */
export function movedPastSlop(press, x, y, slop = PRESS_SLOP) {
  return Math.abs(x - press.x) > slop || Math.abs(y - press.y) > slop;
}

/**
 * The release of a drag is also a click on whatever is underneath. Eat the next
 * click in the capture phase; if none follows (a mouse drag off-target does not
 * always produce one) disarm after `ms` so an unrelated tap is not eaten later.
 */
export function swallowNextClick(doc = document, ms = SWALLOW_CLICK_MS) {
  const eat = (e) => {
    e.stopPropagation();
    e.preventDefault();
  };
  doc.addEventListener('click', eat, { capture: true, once: true });
  setTimeout(() => doc.removeEventListener('click', eat, { capture: true }), ms);
}

/**
 * The closest element matching `selector` under the point, or null.
 * `elementFromPoint` is a parameter so tests need no DOM.
 */
export function dropUnder(x, y, selector, elementFromPoint = (px, py) => document.elementFromPoint(px, py)) {
  const el = elementFromPoint(x, y);
  return el?.closest?.(selector) ?? null;
}

/**
 * Block native scroll while `isActive()`. Must be a non-passive listener, which
 * Svelte's ontouchmove attribute does not guarantee, hence the manual wiring.
 * Returns the remover.
 */
export function blockTouchScroll(target, isActive) {
  const block = (e) => {
    if (isActive()) e.preventDefault();
  };
  target.addEventListener('touchmove', block, { passive: false });
  return () => target.removeEventListener('touchmove', block);
}
