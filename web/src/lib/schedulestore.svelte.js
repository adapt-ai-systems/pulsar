/**
 * Shared schedule roster for the wall.
 *
 * Every ChatPane shows a loop-armed indicator; with 30 panes open, each
 * polling GET /pulses/{id}/schedules on its own was ~217 req/min for data
 * that changes rarely. One module-level poll (while at least one subscriber
 * is mounted) feeds them all; panes filter by pulse id. Same pattern as
 * watchstore.svelte.js.
 */
import { listAllSchedules } from './api.js';
import { sharedPoll } from './pollvisible.js';

const POLL_MS = 15000;

export const scheduleStore = $state({
  /** @type {import('./api.js').Schedule[]} */
  schedules: [],
  loaded: false,
  error: ''
});

export async function refreshSchedules() {
  // Enabled only: panes need just the armed loop, and the full list is
  // ~1 MB of spent one-shots every 15 s (2026-11-02, slow on the phone).
  const r = await listAllSchedules(true);
  scheduleStore.loaded = true;
  if (r.ok) {
    scheduleStore.schedules = r.value ?? [];
    scheduleStore.error = '';
  } else {
    scheduleStore.error = r.error;
    console.log('[schedulestore] poll failed', r.error);
  }
}

/** Mount-time subscribe; returns the unsubscribe for $effect cleanup. Polls only while the tab is visible. */
export const subscribeSchedules = sharedPoll(refreshSchedules, POLL_MS);

/** Pure: the schedules attached to one pulse. */
export function schedulesFor(schedules, pulseId) {
  return schedules.filter((s) => s.pulse_id === pulseId);
}
