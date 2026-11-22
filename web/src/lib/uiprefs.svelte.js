/**
 * Cross-pane UI preferences.
 *
 * A shared runes module so every mounted ChatPane reacts to the same flag at
 * once — flipping the checkbox in one pane's menu declutters the whole wall.
 * Persisted in localStorage only: this is a per-browser reading preference,
 * not pulse state, so it deliberately skips the daemon settings KV.
 */

const LS_SHOW_TOOLS = 'pulsar-show-tool-calls';
const LS_SHOW_METRICS = 'pulsar-show-metrics-bar';
const LS_TOOL_CARDS = 'pulsar-tool-cards';
const LS_SHOW_MOB_SIDEBAR = 'pulsar-show-mobile-sidebar';
// Reply-first reading: collapse daemon chatter into expandable one-liners so
// replies stay easy to scan. ON by default — it drops no data, every folded row
// is one tap away.
const LS_FOLD_NOISE = 'pulsar-fold-noise';
const LS_HIDE_PEER = 'pulsar-hide-peer';
const LS_METRICS_HOSTS = 'pulsar-metrics-hosts';
const LS_COLUMNS = 'pulsar.gridColumns';
const LS_HIDE_STOPPED = 'pulsar.hideStopped';
const LS_LIST_DOCKED = 'pulsar.listDocked';
const LS_DICE_LOCKED = 'pulsar.diceLocked';
// Column widths (drag to resize the list and Inbox columns; saved with the rest of the layout state).
const LS_PANE_WIDTHS = 'pulsar.paneWidths';
export const PANE_WIDTH_DEFAULTS = { list: 320, inbox: 1000 };

function readPaneWidths() {
  const out = { ...PANE_WIDTH_DEFAULTS };
  if (typeof localStorage === 'undefined') return out;
  try {
    const saved = JSON.parse(localStorage.getItem(LS_PANE_WIDTHS) || '{}');
    for (const k of Object.keys(out)) {
      const n = Number(saved?.[k]);
      if (Number.isFinite(n) && n > 0) out[k] = Math.round(n);
    }
  } catch {
    // Corrupt value: defaults.
  }
  return out;
}

export const COLUMN_OPTIONS = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10];

function readColumns() {
  if (typeof localStorage === 'undefined') return 3;
  try {
    const saved = Number(localStorage.getItem(LS_COLUMNS));
    return COLUMN_OPTIONS.includes(saved) ? saved : 3;
  } catch {
    return 3;
  }
}

/** @param {string} key */
function readOptInFlag(key) {
  if (typeof localStorage === 'undefined') return false;
  try {
    return localStorage.getItem(key) === '1';
  } catch {
    return false;
  }
}

/** @param {string} key */
function readFlag(key, dflt = true) {
  // SSR / first paint: default to shown (the historical behavior for tools;
  // the metrics bar defaults on so it's visible the moment the feature lands).
  if (typeof localStorage === 'undefined') return dflt;
  try {
    const v = localStorage.getItem(key);
    if (v === null) return dflt;
    return v !== '0';
  } catch {
    return dflt;
  }
}

// Host chips hidden from the metrics bar. The collector's roster can gain hosts,
// so the default is "show all": only ids the user unchecked are stored.
function readHiddenHosts() {
  if (typeof localStorage === 'undefined') return new Set();
  try {
    return new Set(JSON.parse(localStorage.getItem(LS_METRICS_HOSTS) || '[]'));
  } catch {
    return new Set();
  }
}

export const uiPrefs = $state({
  showToolCalls: readFlag(LS_SHOW_TOOLS),
  // Visual tools: diff cards, file previews, command
  // blocks and a per-turn changes strip instead of one-line tool calls.
  // On by default: a new user sees what each tool call did.
  toolCards: readFlag(LS_TOOL_CARDS),
  showMetricsBar: readFlag(LS_SHOW_METRICS),
  // Opt-in: the desktop icon rail on the phone (appearance window checkbox).
  showMobileSidebar: readFlag(LS_SHOW_MOB_SIDEBAR, false),
  // Opt-out: runs of watcher chatter fold to one line (render.js foldNoise).
  foldNoise: readFlag(LS_FOLD_NOISE),
  // Opt-in: hide messages from other pulses in every chat.
  hidePeer: readFlag(LS_HIDE_PEER, false),
  metricsHiddenHosts: readHiddenHosts(),
  columns: readColumns(),
  hideStopped: readOptInFlag(LS_HIDE_STOPPED),
  listDocked: readOptInFlag(LS_LIST_DOCKED),
  diceLocked: readOptInFlag(LS_DICE_LOCKED),
  paneWidths: readPaneWidths()
});

export function setShowMobileSidebar(v) {
  uiPrefs.showMobileSidebar = !!v;
  writeFlag(LS_SHOW_MOB_SIDEBAR, !!v);
}

/**
 * @param {string} key
 * @param {boolean} v
 */
function writeFlag(key, v) {
  try {
    localStorage.setItem(key, v ? '1' : '0');
  } catch {
    // Storage full/blocked: the session still honors the toggle.
  }
}

function persistHiddenHosts() {
  try {
    localStorage.setItem(LS_METRICS_HOSTS, JSON.stringify([...uiPrefs.metricsHiddenHosts]));
  } catch {
    // Storage full/blocked: the session still honors the toggle.
  }
}

/** @param {boolean} v */
export function setShowToolCalls(v) {
  uiPrefs.showToolCalls = !!v;
  writeFlag(LS_SHOW_TOOLS, !!v);
}

/** @param {boolean} v */
export function setToolCards(v) {
  uiPrefs.toolCards = !!v;
  writeFlag(LS_TOOL_CARDS, !!v);
}

/** @param {boolean} v */
export function setFoldNoise(v) {
  uiPrefs.foldNoise = !!v;
  writeFlag(LS_FOLD_NOISE, !!v);
}

/** @param {boolean} v */
export function setShowMetricsBar(v) {
  uiPrefs.showMetricsBar = !!v;
  writeFlag(LS_SHOW_METRICS, !!v);
}

/** @param {string} hostId */
export function isMetricsHostHidden(id) {
  return uiPrefs.metricsHiddenHosts.has(id);
}

/** @param {string} hostId @param {boolean} hidden */
export function setMetricsHostHidden(id, hidden) {
  const s = new Set(uiPrefs.metricsHiddenHosts);
  if (hidden) s.add(id); else s.delete(id);
  uiPrefs.metricsHiddenHosts = s;
  persistHiddenHosts();
}

/** @param {boolean} v */
export function setHidePeer(v) {
  uiPrefs.hidePeer = !!v;
  writeFlag(LS_HIDE_PEER, !!v);
}

/** @param {number} n */
export function setColumns(n) {
  const next = COLUMN_OPTIONS.includes(Number(n)) ? Number(n) : 3;
  uiPrefs.columns = next;
  try {
    localStorage.setItem(LS_COLUMNS, String(next));
  } catch {
    // Storage full/blocked: the session still honors the toggle.
  }
}

/** @param {boolean} v */
export function setHideStopped(v) {
  const on = !!v;
  uiPrefs.hideStopped = on;
  try {
    if (on) localStorage.setItem(LS_HIDE_STOPPED, '1');
    else localStorage.removeItem(LS_HIDE_STOPPED);
  } catch {
    // Storage full/blocked: the session still honors the toggle.
  }
}

/** @param {boolean} v */
export function setListDocked(v) {
  const on = !!v;
  uiPrefs.listDocked = on;
  writeFlag(LS_LIST_DOCKED, on);
}

/** @param {boolean} v */
export function setDiceLocked(v) {
  const on = !!v;
  uiPrefs.diceLocked = on;
  writeFlag(LS_DICE_LOCKED, on);
}

/**
 * Set one column width. `save=false` while dragging (no storage write per
 * pointer move); `save=true` on release.
 * @param {'list'|'inbox'} key @param {number} px @param {boolean} [save]
 */
export function setPaneWidth(key, px, save = true) {
  if (!(key in PANE_WIDTH_DEFAULTS)) return;
  uiPrefs.paneWidths = { ...uiPrefs.paneWidths, [key]: Math.round(px) };
  if (!save) return;
  try {
    localStorage.setItem(LS_PANE_WIDTHS, JSON.stringify(uiPrefs.paneWidths));
  } catch {
    // Storage full/blocked: the session still honors the width.
  }
}

/** @param {'list'|'inbox'} key */
export function resetPaneWidth(key) {
  setPaneWidth(key, PANE_WIDTH_DEFAULTS[key]);
}

/** Re-read view preferences from localStorage (SSR hydration / tests). */
export function hydrateViewPrefs() {
  uiPrefs.columns = readColumns();
  uiPrefs.hideStopped = readOptInFlag(LS_HIDE_STOPPED);
  uiPrefs.listDocked = readOptInFlag(LS_LIST_DOCKED);
  uiPrefs.diceLocked = readOptInFlag(LS_DICE_LOCKED);
  uiPrefs.paneWidths = readPaneWidths();
}

