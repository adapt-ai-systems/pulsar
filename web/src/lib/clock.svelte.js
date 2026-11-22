/**
 * Shared wall-clock for elapsed chips (working · Ns, finished 12s ago).
 *
 * Every running ChatPane used to own a 1s setInterval(now). On a 29-pane
 * wall that is 29 timers rewriting headers every second. One module-level
 * ticker feeds them all:
 *   - 1s while any subscriber is visible/focused AND the tab is showing
 *   - 5s otherwise (hidden tab, off-screen panes)
 *   - stopped when nobody is subscribed
 */
export const clock = $state({ now: Date.now() });

export const CLOCK_FAST_MS = 1000;
export const CLOCK_SLOW_MS = 5000;

let subscribers = 0;
let focused = 0;
let timer = 0;
/** @type {number} 0 when idle, else the live interval */
let currentMs = 0;

function tabVisible() {
  return typeof document === 'undefined' || document.visibilityState === 'visible';
}

function desiredMs() {
  if (subscribers === 0) return 0;
  return tabVisible() && focused > 0 ? CLOCK_FAST_MS : CLOCK_SLOW_MS;
}

function pulse() {
  clock.now = Date.now();
}

function restart() {
  const ms = desiredMs();
  if (ms === currentMs && (ms === 0 || timer)) return;
  if (timer) {
    clearInterval(timer);
    timer = 0;
  }
  currentMs = ms;
  if (!ms) return;
  pulse();
  timer = setInterval(pulse, ms);
}

function onVisibility() {
  restart();
}

/**
 * Mount-time subscribe. Pass `{ focused: true }` when the pane is on screen
 * (IntersectionObserver / the open wall). Returns unsubscribe for $effect.
 *
 * @param {{ focused?: boolean }} [opts]
 * @returns {() => void}
 */
export function subscribeClock(opts = {}) {
  const isFocused = !!opts.focused;
  subscribers += 1;
  if (isFocused) focused += 1;
  if (subscribers === 1 && typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVisibility);
  }
  restart();
  return () => {
    subscribers -= 1;
    if (isFocused) focused -= 1;
    if (subscribers === 0 && typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVisibility);
    }
    restart();
  };
}

/** Test helper: the live interval, or 0 when the ticker is down. */
export function clockIntervalMs() {
  return currentMs;
}
