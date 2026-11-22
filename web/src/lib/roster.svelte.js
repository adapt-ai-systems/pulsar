import { untrack } from 'svelte';
import {
  listPulses,
  listGrids,
  reorderPulses,
  movePulseToGrid,
  reorderGrids,
  deleteGrid
} from './api.js';
import { reorderableOrder } from './notifpulse.js';
import { moveId } from './pulsetree.js';

/** A restart/reconnect blip rather than a real failure. */
function transientError(e) {
  return /HTTP 50[234]\b|network error|upstream_(?:timeout|unreachable)|not answering right now|took too long/i.test(String(e ?? ''));
}

/**
 * Roster and workspace state machine for +page.svelte.
 *
 * Owns `pulses`, `grids`, `pulsesLoaded`, `gridsLoaded`, `error`, `gridsError`,
 * `connected`, `moveNote`, `binArmed`, the in-flight mutation guards
 * (`reordering`, `moving`, `reorderingGrids`), `applyRoster(list, g)` shared by
 * the `/live` effect and `refresh()`, `poll()` for the page's 3s timer, and
 * the optimistic move/reorder/delete helpers (`movePane`,
 * `movePulseToWorkspace`, `reorderWorkspaces`, `armWorkspaceDelete`,
 * `confirmBinDelete`, `cancelBinDelete`, `refreshGrids`, `onGridsChanged`,
 * `appendPulse`).
 *
 * @param {Object} [opts]
 * @param {() => Array<{id: string}>} [opts.getVisiblePulses]
 * @param {() => {id: string}|null} [opts.getExtraPulse]
 * @param {() => string|null} [opts.getSelectedGrid]
 * @param {(id: string|null) => void} [opts.setSelectedGrid]
 * @param {Object} [opts.api]
 * @param {Object} [opts.live]
 */
export function createRoster({
  getVisiblePulses = () => [],
  getExtraPulse = () => null,
  getSelectedGrid = () => null,
  setSelectedGrid = () => {},
  api = {
    listPulses,
    listGrids,
    reorderPulses,
    movePulseToGrid,
    reorderGrids,
    deleteGrid
  },
  live = { roster: null, grids: null, connected: false }
} = {}) {
  let pulses = $state([]);
  let grids = $state([]);
  let pulsesLoaded = $state(false);
  let gridsLoaded = $state(false);
  let error = $state('');
  let gridsError = $state('');
  let connected = $state(false);

  // True while a drag-reorder POST is in flight. The 3s poll returns the OLD
  // order until the daemon has written it, so applying a poll result mid-flight
  // would snap the dragged pane back and then forward again.
  let reordering = false;
  // Same problem for a workspace drop: the poll would put the pane back in the
  // tab you just dragged it out of until the PATCH lands.
  let moving = false;
  let reorderingGrids = false;

  // Transient "moved" line in the nav's message row; auto-clears.
  let moveNote = $state('');
  let moveNoteTimer;
  function noteMove(text) {
    moveNote = text;
    clearTimeout(moveNoteTimer);
    moveNoteTimer = setTimeout(() => (moveNote = ''), 2600);
  }

  let binArmed = $state(null); // { id, name } waiting on the confirm strip
  let binTimer;

  /**
   * Apply a roster snapshot (from `/live` or `refresh()`), respecting in-flight
   * reorder/move guards. When `g` is provided (from `/live`), also updates
   * `grids` unless a workspace reorder is in flight.
   *
   * @param {Array<any>} list
   * @param {Array<any>|null} [g]
   */
  function applyRoster(list, g = null) {
    if (!reordering && !moving) pulses = list ?? [];
    pulsesLoaded = true;
    if (g !== null && !reorderingGrids) {
      grids = g;
      gridsLoaded = true;
    }
    error = '';
    connected = true;
  }

  // /live push: the daemon sends the roster + grids on its own tick, so while
  // the socket is up the 3s/5s timers are no-ops and this effect is the source.
  $effect(() => {
    const r = live.roster;
    const g = live.grids;
    if (r === null) return;
    untrack(() => {
      applyRoster(r, g);
    });
  });

  // Always fetch — even with the live roster connected. refresh() is the
  // mutation hook (delete/archive/mode/...): waiting for the next 3s roster
  // frame left a deleted pane on screen briefly. One GET per mutation is cheap;
  // the frame that follows agrees.
  async function refresh() {
    const r = await api.listPulses({ include_archived: false });
    if (r.ok) {
      applyRoster(r.value ?? []);
    } else {
      // A restart's 502/503/504 or a dropped connection is not an error to
      // show: the reconnect pill says it without moving the page.
      if (!transientError(r.error)) error = r.error;
      connected = false;
    }
  }

  // The page's 3s timer. A fresh /live frame carries this same list (the
  // daemon builds both with views.roster), so the GET only repeated it: 20 a
  // minute, ~190 KB/min gzip per tab (2026-11-02). Polls resume when frames
  // stop.
  function poll() {
    if (live.rosterFresh?.()) return;
    return refresh();
  }

  async function refreshGrids({ force = false } = {}) {
    if (!force && live.connected && live.grids !== null) return;
    const r = await api.listGrids();
    if (r.ok) {
      if (!reorderingGrids) grids = r.value ?? [];
      gridsLoaded = true;
      gridsError = '';
    } else {
      // Non-fatal: the ALL tab still works; surface it but don't blank the grid.
      // A server restart is transient — no red bar that shifts the layout.
      if (!transientError(r.error)) gridsError = r.error;
      console.log('[grid] list grids failed', r.error);
    }
  }

  // A just-created tab is selected before the grid list (poll or /live push)
  // carries it; the dead-tab reset would throw the view to ALL.
  let pendingGridId = null;
  function onGridsChanged(newId) {
    if (newId) {
      pendingGridId = newId;
      setTimeout(() => { if (pendingGridId === newId) pendingGridId = null; }, 15000);
    }
    refresh();
    return refreshGrids({ force: true });
  }
  const clearPendingGrid = () => { pendingGridId = null; };

  /**
   * Drag-reorder on the wall: drop `fromId` onto `toId`'s slot. Applied locally
   * first so the pane moves under the cursor, then persisted. The daemon
   * redistributes the listed ids over their own position slots, so reordering
   * inside a grid tab leaves the other tabs' pulses alone — mirror that here by
   * only permuting the visible pulses within the slots they already occupy.
   */
  async function movePane(fromId, toId) {
    const extraId = getExtraPulse()?.id ?? null;
    if (fromId === extraId || toId === extraId) {
      console.log('[grid] reorder ignores the notification-only pane', fromId, toId);
      return;
    }
    const order = moveId(reorderableOrder(getVisiblePulses(), extraId), fromId, toId);
    if (!order) return;

    const byId = new Map(pulses.map((p) => [p.id, p]));
    const slots = [];
    pulses.forEach((p, i) => {
      if (byId.has(p.id) && order.includes(p.id)) slots.push(i);
    });
    const next = pulses.slice();
    order.forEach((id, k) => (next[slots[k]] = byId.get(id)));
    pulses = next;

    reordering = true;
    const r = await api.reorderPulses(order);
    reordering = false;
    if (!r.ok) {
      error = `reorder: ${r.error}`;
      console.log('[grid] reorder failed', r.error);
      refresh(); // server is the truth — snap back to it
    }
  }

  /**
   * A pulse was dropped on a workspace: desktop drags a pane's title bar onto a
   * nav chip, mobile long-presses a card in the overview and drags it onto a
   * workspace chip there. `gridId` null = out of every workspace (the app-icon /
   * ALL target). Applied locally first so the pane leaves the tab immediately,
   * then persisted.
   */
  async function movePulseToWorkspace(pulseId, gridId) {
    const p = pulses.find((x) => x.id === pulseId);
    const to = gridId ?? null;
    if (!p || (p.grid_id ?? null) === to) return;

    pulses = pulses.map((x) => (x.id === pulseId ? { ...x, grid_id: to } : x));
    noteMove(
      `${p.title} → ${to === null ? 'no workspace' : grids.find((g) => g.id === to)?.name ?? 'workspace'}`
    );

    moving = true;
    const r = await api.movePulseToGrid(pulseId, to);
    moving = false;
    if (!r.ok) {
      error = `move: ${r.error}`;
      console.log('[grid] move pulse failed', pulseId, to, r.error);
    }
    refresh(); // server is the truth either way
  }

  /** New left-to-right workspace order, applied locally then persisted. */
  async function reorderWorkspaces(ids) {
    const byId = new Map(grids.map((g) => [g.id, g]));
    const next = ids.map((id) => byId.get(id)).filter(Boolean);
    if (next.length !== grids.length) return; // stale list — let the poll win
    grids = next;

    reorderingGrids = true;
    const r = await api.reorderGrids(ids);
    reorderingGrids = false;
    if (!r.ok) {
      gridsError = `reorder workspaces: ${r.error}`;
      console.log('[grid] reorder grids failed', r.error);
    }
    refreshGrids(); // server is the truth either way
  }

  /**
   * A workspace was dropped on the bin. This ARMS the delete rather than doing
   * it: a workspace holding a dozen pulses is a minute of dragging to rebuild,
   * and a drag can miss.
   */
  function armWorkspaceDelete(gridId) {
    const g = grids.find((x) => x.id === gridId);
    if (!g) return;
    binArmed = { id: g.id, name: g.name };
    clearTimeout(binTimer);
    binTimer = setTimeout(() => (binArmed = null), 8000);
    console.debug('[grid] bin drop → armed delete %s (%s)', g.id, g.name);
  }

  function cancelBinDelete() {
    binArmed = null;
    clearTimeout(binTimer);
  }

  async function confirmBinDelete() {
    const g = binArmed;
    binArmed = null;
    clearTimeout(binTimer);
    if (!g) return;
    const r = await api.deleteGrid(g.id, { detach: true });
    if (!r.ok) {
      gridsError = `delete workspace: ${r.error}`;
      console.log('[grid] delete failed', g.id, r.error);
      return;
    }
    if (getSelectedGrid() === g.id) setSelectedGrid(null); // don't sit on a dead tab
    const n = r.value?.detached ?? 0;
    noteMove(`${g.name} deleted${n ? ` · ${n} pulse${n === 1 ? '' : 's'} moved out` : ''}`);
    onGridsChanged();
  }

  /** Optimistically append a newly created pulse before `refresh()` lands. */
  function appendPulse(created) {
    if (!created?.id) return;
    if (!pulses.some((p) => p.id === created.id)) pulses = [...pulses, created];
  }

  return {
    get pulses() {
      return pulses;
    },
    get grids() {
      return grids;
    },
    get pulsesLoaded() {
      return pulsesLoaded;
    },
    get gridsLoaded() {
      return gridsLoaded;
    },
    get error() {
      return error;
    },
    get gridsError() {
      return gridsError;
    },
    get connected() {
      return connected;
    },
    get moveNote() {
      return moveNote;
    },
    get binArmed() {
      return binArmed;
    },
    get pendingGridId() {
      return pendingGridId;
    },
    clearPendingGrid,
    applyRoster,
    refresh,
    poll,
    refreshGrids,
    onGridsChanged,
    movePane,
    movePulseToWorkspace,
    reorderWorkspaces,
    armWorkspaceDelete,
    cancelBinDelete,
    confirmBinDelete,
    noteMove,
    appendPulse
  };
}
