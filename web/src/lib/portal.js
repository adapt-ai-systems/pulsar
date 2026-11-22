/**
 * Move an element to <body> and keep it there for its lifetime.
 *
 * WHY THIS EXISTS — the mobile deck (MobileDeck.svelte) renders panes inside a
 * `.deck` track carrying `will-change: transform` plus a live `translateX()`.
 * That makes `.deck` a STACKING CONTEXT, which traps the z-index of everything
 * inside it. Measured in the running app: a `z-index: 9999` element inside the
 * deck loses the hit test to a `z-index: 1` element at <body> level. A modal
 * rendered from inside a pane therefore painted underneath the deck's siblings
 * no matter how high its z-index went.
 *
 * Symptom that led here: pressing settings did not show the modal until
 * swiping to another pulse. The stacking trap is verified; the swipe-reveals-it
 * half is most likely iOS Safari not repainting
 * the composited `will-change: transform` layer until the transform changes —
 * that part is NOT reproducible in headless Chrome on Linux, so it is a
 * reasoned explanation rather than a measured one.
 *
 * Portalling to <body> removes the element from both the stacking context and
 * the composited layer, which addresses either cause.
 *
 * NOT the cause, though it looks like it: a transformed ancestor also becomes
 * the containing block for `position: fixed` descendants. Measured here and it
 * did NOT capture them — the modal's coordinates were correct all along. Don't
 * "fix" that again.
 *
 * Modals declared at the top level of +page.svelte were never affected; this is
 * only needed for ones rendered from inside a pane.
 *
 * @param {HTMLElement} node
 * @param {boolean} [enabled] false = leave the node where it is
 */
export function portal(node, enabled = true) {
  if (typeof document === 'undefined') return; // SSR: nothing to move
  if (enabled === false) return; // inline use (e.g. full board docked in the sidebar)
  document.body.appendChild(node);
  return {
    destroy() {
      node.remove();
    }
  };
}
