/**
 * Workspaces hidden from the ALL view (toggled by clicking the color square
 * next to each workspace name). Per device, in localStorage; the
 * workspace's own tab still shows everything.
 */
const KEY = 'pulsar.allHiddenWs';

function read() {
  try {
    const v = JSON.parse(localStorage.getItem(KEY) || '[]');
    return Array.isArray(v) ? v.filter((x) => typeof x === 'string') : [];
  } catch {
    return [];
  }
}

export const wsHidden = $state({ ids: typeof localStorage === 'undefined' ? [] : read() });

export function isWsHidden(gridId) {
  return wsHidden.ids.includes(gridId);
}

export function toggleWsHidden(gridId) {
  wsHidden.ids = isWsHidden(gridId) ? wsHidden.ids.filter((x) => x !== gridId) : [...wsHidden.ids, gridId];
  try { localStorage.setItem(KEY, JSON.stringify(wsHidden.ids)); } catch { /* non-fatal */ }
}
