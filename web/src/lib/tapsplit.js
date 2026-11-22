// Single vs double tap on ONE button (the pane's 🔖: tap = set / summary,
// double tap = clear). A single tap waits DOUBLE_TAP_MS before
// it fires, so the first tap of a double never sends a summary request.
import { DOUBLE_TAP_MS } from './doubletap.js';

/**
 * @param {() => void} single
 * @param {() => void} double
 * @param {{ ms?: number, setTimer?: Function, clearTimer?: Function }} [opts]
 * @returns {() => void} the click handler
 */
export function tapSplit(single, double, {
  ms = DOUBLE_TAP_MS, setTimer = setTimeout, clearTimer = clearTimeout
} = {}) {
  let timer = null;
  return function onClick() {
    if (timer !== null) {
      clearTimer(timer);
      timer = null;
      double();
      return;
    }
    timer = setTimer(() => {
      timer = null;
      single();
    }, ms);
  };
}
