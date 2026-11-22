export const PANE_Z_BASE = 60;

/**
 * Return floating panes in stable creation (`win`) order paired with their
 * stacking z-index so raising a background window updates `z-index` without
 * reordering DOM nodes mid-click.
 *
 * @template {{ win: number }} T
 * @param {T[]} panes
 * @param {number} [baseZ]
 * @returns {Array<{ pane: T, z: number }>}
 */
export function panesInDomOrder(panes, baseZ = PANE_Z_BASE) {
  return panes
    .map((pane, i) => ({ pane, z: baseZ + i }))
    .sort((a, b) => a.pane.win - b.pane.win);
}

/**
 * Keep a floating window's head bar comfortably on screen; if a long cascade
 * walks off the edge, wrap back to the top-left default rather than clamping
 * into a pile.
 *
 * @param {{ x: number, y: number }} pos
 * @param {{ width?: number, height?: number }} [viewport]
 * @returns {{ x: number, y: number }}
 */
export function clampSpawn(
  { x, y },
  viewport = typeof window !== 'undefined'
    ? { width: window.innerWidth, height: window.innerHeight }
    : { width: 1280, height: 800 }
) {
  const w = viewport?.width ?? 1280;
  const h = viewport?.height ?? 800;
  if (x > w - 260 || y > h - 160) return { x: 110, y: 84 };
  return { x: Math.max(8, x), y: Math.max(8, y) };
}

/**
 * Floating window stack for +page.svelte:
 *   - `panes` dedupe by pulse `id`, spawn at `lastPanePos` (`{110, 84}`
 *     initially, updated on move/close) or cascade `+36, +30` from the top
 *     pane, clear when workspace or surface changes, close the frontmost pane
 *     on Escape, and stack at z-index `60 + i`.
 *
 * @param {Object} [opts]
 * @param {() => unknown} [opts.getContextKey]  when this reactive dependency changes, `panes` clears
 *     (+page.svelte folds the Pulses, Board and Inbox surfaces into one key:
 *      on desktop those two are a column NEXT TO the grid, so the windows stay)
 * @param {() => { width: number, height: number }} [opts.getViewport]
 */
export function createWindowStack({ getContextKey, getViewport } = {}) {
  let panes = $state([]); // [{ win, id, full, x, y }]
  let winSeq = 0;
  let lastPanePos = { x: 110, y: 84 };

  function spawnAt(top, initialPos) {
    const raw = top ? { x: top.x + 36, y: top.y + 30 } : initialPos;
    return clampSpawn(raw, getViewport?.());
  }

  function frontPane(id) {
    const m = panes.find((e) => e.id === id);
    if (!m || panes[panes.length - 1] === m) return;
    panes = [...panes.filter((e) => e.id !== id), m];
  }

  function openPane(p, full = false) {
    if (panes.some((m) => m.id === p.id)) {
      frontPane(p.id);
      return;
    }
    const top = panes[panes.length - 1];
    const at = spawnAt(top, lastPanePos);
    panes = [...panes, { win: ++winSeq, id: p.id, full, ...at }];
    console.debug('[expand] open %s at %d,%d full=%s (%d windows)', p.id, at.x, at.y, full, panes.length);
  }

  function openFullPane(p) {
    openPane(p, true);
  }

  function switchPane(win, id) {
    panes = panes.map((e) => (e.win === win ? { ...e, id } : e));
  }

  function movePane(id, pos) {
    lastPanePos = pos;
    panes = panes.map((e) => (e.id === id ? { ...e, ...pos } : e));
  }

  function closePane(id) {
    const m = panes.find((e) => e.id === id);
    if (m) lastPanePos = { x: m.x, y: m.y };
    panes = panes.filter((e) => e.id !== id);
  }

  function clearPanes() {
    panes = [];
  }

  function onEscape(e) {
    if (e?.defaultPrevented) return false;
    if (e?.key === 'Escape' && panes.length) {
      closePane(panes[panes.length - 1].id);
      return true;
    }
    return false;
  }

  if (getContextKey) {
    $effect(() => {
      getContextKey();
      panes = [];
    });
  }

  return {
    get panes() {
      return panes;
    },
    paneZ: (i) => PANE_Z_BASE + i,
    openPane,
    openFullPane,
    switchPane,
    movePane,
    frontPane,
    closePane,
    clearPanes,
    onEscape
  };
}
