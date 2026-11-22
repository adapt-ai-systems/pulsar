/**
 * Poll `fn` on an interval only while the document is visible.
 *
 * Hidden tabs take the interval down (no 3s/10s wakeups, no fetches).
 * Becoming visible again fetches immediately and restarts the timer.
 * Same idea as FuelRail's panel-open guard, plus visibilityState.
 *
 * @param {() => void} fn
 * @param {number} intervalMs
 * @param {{ immediate?: boolean }} [opts]  immediate=false skips the mount
 *   fetch (when a sibling effect already loads). Visibility-resume still
 *   fetches.
 * @returns {() => void} unsubscribe for $effect cleanup
 */
export function pollWhileVisible(fn, intervalMs, opts = {}) {
  const immediate = opts.immediate !== false;
  let id = 0;

  const visible = () =>
    typeof document === 'undefined' || document.visibilityState === 'visible';

  const start = (reason) => {
    if (id) return;
    if (immediate || reason === 'visible') fn();
    id = setInterval(fn, intervalMs);
  };
  const stop = () => {
    if (!id) return;
    clearInterval(id);
    id = 0;
  };
  const onVis = () => {
    if (visible()) start('visible');
    else stop();
  };

  if (visible()) start('mount');
  if (typeof document !== 'undefined') {
    document.addEventListener('visibilitychange', onVis);
  }
  return () => {
    stop();
    if (typeof document !== 'undefined') {
      document.removeEventListener('visibilitychange', onVis);
    }
  };
}

/**
 * Ref-counted shared poller for the module-level rosters (watches, schedules,
 * questions). The first subscriber starts one `pollWhileVisible` loop, the
 * last one stops it. A hidden tab makes no requests; on return it refreshes
 * once at once.
 *
 * @param {() => void} fn
 * @param {number} intervalMs
 * @returns {() => () => void} subscribe: call it, keep the returned unsubscribe
 */
export function sharedPoll(fn, intervalMs) {
  let subscribers = 0;
  let stop = null;
  return () => {
    subscribers += 1;
    if (subscribers === 1) stop = pollWhileVisible(fn, intervalMs);
    return () => {
      subscribers -= 1;
      if (subscribers === 0 && stop) {
        stop();
        stop = null;
      }
    };
  };
}
