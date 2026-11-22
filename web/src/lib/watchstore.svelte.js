/**
 * Shared condition-watch roster for the wall.
 *
 * Every ChatPane shows a watch bar; with 30 panes open, each polling on its
 * own would be 30 requests per tick for one list. One module-level poll
 * (while at least one subscriber is mounted) feeds them all; panes filter by
 * pulse id. Deletes update the shared list so every bar agrees at once.
 */
import { listConditionWatches, deleteConditionWatch } from './api.js';
import { sharedPoll } from './pollvisible.js';

const POLL_MS = 15000;

export const watchStore = $state({
  /** @type {import('./api.js').ConditionWatch[]} */
  watches: [],
  loaded: false,
  error: ''
});

export async function refreshWatches() {
  const r = await listConditionWatches(null, 'watching');
  watchStore.loaded = true;
  if (r.ok) {
    watchStore.watches = r.value ?? [];
    watchStore.error = '';
  } else {
    watchStore.error = r.error;
    console.log('[watchstore] poll failed', r.error);
  }
}

/** Mount-time subscribe; returns the unsubscribe for $effect cleanup. Polls only while the tab is visible. */
export const subscribeWatches = sharedPoll(refreshWatches, POLL_MS);

export async function removeWatch(id) {
  const r = await deleteConditionWatch(id);
  if (r.ok) watchStore.watches = watchStore.watches.filter((w) => w.id !== id);
  else console.log('[watchstore] delete failed', id, r.error);
  return r;
}

/** Pure: the open watches owned by one pulse (MET first). */
export function watchesFor(watches, pulseId) {
  return watches
    .filter((w) => w.pulse_id === pulseId)
    .sort((a, b) => (!!b.bad) - (!!a.bad));
}
