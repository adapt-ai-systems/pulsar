/**
 * Mobile-deck swipe state machine.
 *
 * Extracted from MobileDeck.svelte so the axis-lock / threshold math can be
 * node-tested without a browser. The component owns DOM (window listeners,
 * pointer capture, transforms); this module decides when a gesture is a
 * swipe, a scroll, or a tap.
 *
 * A pointerdown on a button/link/input MUST still arm tracking. The control
 * keeps its own tap because we do not capture or preventDefault until
 * horizontal intent exceeds H_LOCK. 0f651cc bailed out of onPointerDown for
 * interactive targets (`button, a, input, select, textarea, [role="button"]`),
 * which made any swipe that began on a control a dead zone — the transcript
 * is full of those (code-block copy, links, tool-call rows).
 */

export const H_THRESHOLD = 60; // px horizontal to change pulse
export const H_LOCK = 4; // px horizontal to commit to a deck swipe
export const V_LOCK = 8; // px vertical to commit to scrolling

/**
 * @typedef {{ pointerId: number, clientX: number, clientY: number, type?: string, target?: any }} PtrEvent
 * @typedef {{ index: number, count: number }} DeckPos
 */

export function createDeckSwipe() {
  let pointerId = null;
  let startX = 0;
  let startY = 0;
  let axis = null; // 'x' | 'y' | null
  let dragging = false;
  let dragDX = 0;

  function snapshot() {
    return { pointerId, startX, startY, axis, dragging, dragDX };
  }

  /**
   * Arm tracking. Ignores a second pointer (pinch/scroll belongs to the
   * browser). Does NOT inspect e.target — interactive starts are allowed.
   * @param {PtrEvent} e
   * @returns {boolean} true if this pointer is now the tracked gesture
   */
  function down(e) {
    if (pointerId !== null) return false;
    pointerId = e.pointerId;
    startX = e.clientX;
    startY = e.clientY;
    dragDX = 0;
    axis = null;
    dragging = false;
    return true;
  }

  /**
   * @param {PtrEvent} e
   * @param {DeckPos} pos
   * @returns {{ ignored: boolean, axis: string|null, dragging: boolean, dragDX: number, capture: boolean, preventDefault: boolean }}
   */
  function move(e, { index, count } = { index: 0, count: 1 }) {
    if (pointerId !== e.pointerId) {
      return { ignored: true, axis, dragging, dragDX, capture: false, preventDefault: false };
    }
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;

    if (axis === null) {
      const ax = Math.abs(dx);
      const ay = Math.abs(dy);
      if (ax > H_LOCK && ax > ay) {
        axis = 'x';
        dragging = true;
      } else if (ay > V_LOCK) {
        axis = 'y';
      } else {
        return { ignored: false, axis, dragging, dragDX, capture: false, preventDefault: false };
      }
    }

    if (axis === 'x') {
      let d = dx;
      if ((index === 0 && d > 0) || (index === count - 1 && d < 0)) d *= 0.35;
      dragDX = d;
      return { ignored: false, axis, dragging, dragDX, capture: true, preventDefault: true };
    }
    return { ignored: false, axis, dragging, dragDX, capture: false, preventDefault: false };
  }

  /**
   * @param {PtrEvent} e
   * @param {DeckPos} pos
   * @returns {{ ignored: boolean, navigated: boolean, to: number, axis: string|null, settledDX: number, dx: number, dy: number, snapBack: boolean }}
   */
  function up(e, { index, count } = { index: 0, count: 1 }) {
    if (pointerId !== e.pointerId) {
      return {
        ignored: true, navigated: false, to: index, axis, settledDX: dragDX,
        dx: 0, dy: 0, snapBack: false,
      };
    }
    const dx = e.clientX - startX;
    const dy = e.clientY - startY;
    const finishedAxis = axis;
    const settledDX = dragDX;

    pointerId = null;
    axis = null;
    dragging = false;
    dragDX = 0;

    if (finishedAxis === 'x') {
      if (settledDX <= -H_THRESHOLD && index < count - 1) {
        return {
          ignored: false, navigated: true, to: index + 1, axis: 'x',
          settledDX, dx, dy, snapBack: false,
        };
      }
      if (settledDX >= H_THRESHOLD && index > 0) {
        return {
          ignored: false, navigated: true, to: index - 1, axis: 'x',
          settledDX, dx, dy, snapBack: false,
        };
      }
      return {
        ignored: false, navigated: false, to: index, axis: 'x',
        settledDX, dx, dy, snapBack: true,
      };
    }
    return {
      ignored: false, navigated: false, to: index, axis: finishedAxis,
      settledDX, dx, dy, snapBack: false,
    };
  }

  function reset() {
    pointerId = null;
    startX = 0;
    startY = 0;
    axis = null;
    dragging = false;
    dragDX = 0;
  }

  return { down, move, up, reset, snapshot };
}
