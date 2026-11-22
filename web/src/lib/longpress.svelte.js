// One hold-to-drag state machine (Wall, drawer, full-screen window).
//
// Pointer events, because HTML5 drag-and-drop does not exist on touch. A press
// that is held `holdMs` becomes a drag; a press that moves more than `slop`
// first is a scroll and is left alone. The drag captures the pointer on the
// element that started it: without the capture, move/up retarget to whatever is
// under the finger and the release never reaches the handler.
import { LONG_PRESS_MS, PRESS_SLOP, movedPastSlop, swallowNextClick, blockTouchScroll } from './longpress.js';

/**
 * @param {object} o
 * @param {number} [o.holdMs]
 * @param {number} [o.slop]
 * @param {*} [o.noTarget]  what `target` is while nothing usable is under the
 *   pointer; a drop onto it is a cancel. Drawer and window use `undefined` so
 *   `null` can mean "no workspace"; Wall uses `null`.
 * @param {(e: PointerEvent, info: object) => boolean} [o.canStart]
 * @param {(x: number, y: number, drag: object) => *} o.hitTest
 * @param {(drag: object) => void} [o.onStart]  the hold completed
 * @param {(target: *, drag: object) => void} [o.onDrop]
 * @param {(drag: object, reason: 'released' | 'pointercancel') => void} [o.onCancel]
 * @param {() => void} [o.onEnd]  the drag is over, whatever the outcome
 * @param {() => (EventTarget | null | undefined)} [o.touchBlockTarget]  scroll
 *   is blocked on this element while a drag is live. Reactive: return null to
 *   have no listener (the window only has one while carrying).
 */
export function createLongPressDrag(o) {
  const {
    holdMs = LONG_PRESS_MS,
    slop = PRESS_SLOP,
    canStart,
    hitTest,
    onStart,
    onDrop,
    onCancel,
    onEnd,
    touchBlockTarget
  } = o;
  // Not a destructuring default: an explicit `undefined` is a real choice.
  const noTarget = 'noTarget' in o ? o.noTarget : null;
  let press = null; // { pointerId, x, y, timer }
  let dragEl = null;
  let drag = $state(null); // { ...info, x, y, pointerId }
  let target = $state(noTarget);

  function cancelPress() {
    if (!press) return;
    clearTimeout(press.timer);
    press = null;
  }

  function endDrag() {
    if (dragEl && drag) {
      try {
        dragEl.releasePointerCapture(drag.pointerId);
      } catch {
        /* already released — the pointer is gone, which is the point */
      }
    }
    dragEl = null;
    drag = null;
    target = noTarget;
    onEnd?.();
  }

  /** Arm a long press on `e.currentTarget`. `info` becomes the drag's payload. */
  function start(e, info) {
    if (press || drag) return;
    if (canStart && !canStart(e, info)) return;
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    const el = e.currentTarget;
    const p = { pointerId: e.pointerId, x: e.clientX, y: e.clientY, timer: null };
    p.timer = setTimeout(() => {
      press = null;
      drag = { ...info, x: p.x, y: p.y, pointerId: p.pointerId };
      target = noTarget;
      dragEl = el;
      try {
        el.setPointerCapture(p.pointerId); // every later move/up comes back here
      } catch {
        dragEl = null; // no capture: the drag still works while over the list
      }
      navigator.vibrate?.(12); // the hold has to announce itself somehow
      onStart?.(drag);
    }, holdMs);
    press = p;
  }

  function move(e) {
    if (drag) {
      drag = { ...drag, x: e.clientX, y: e.clientY };
      target = hitTest(e.clientX, e.clientY, drag);
      return;
    }
    if (press?.pointerId !== e.pointerId) return;
    if (movedPastSlop(press, e.clientX, e.clientY, slop)) cancelPress(); // a scroll, not a hold
  }

  function up() {
    cancelPress();
    if (!drag) return;
    const d = drag;
    const t = target;
    endDrag();
    // The release is also a click on whatever is underneath.
    swallowNextClick();
    if (t === noTarget) {
      onCancel?.(d, 'released');
      return;
    }
    onDrop?.(t, d);
  }

  function cancel() {
    cancelPress();
    if (drag) {
      const d = drag;
      endDrag();
      onCancel?.(d, 'pointercancel');
    }
  }

  if (touchBlockTarget) {
    $effect(() => {
      const t = touchBlockTarget();
      if (!t) return;
      return blockTouchScroll(t, () => !!drag);
    });
  }

  return {
    get drag() { return drag; },
    get target() { return target; },
    get pressing() { return !!press; },
    start,
    move,
    up,
    cancel
  };
}
