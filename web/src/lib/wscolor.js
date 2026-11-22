/**
 * One color per workspace, so the ALL view can outline panes by workspace
 * and the tabs can wear the same color, making workspaces visually distinct
 * on one screen. Color follows the workspace's slot in the
 * tab order, so it is stable across reloads and the same on every device.
 */
export const WS_PALETTE = [
  '#5ec8f2', // sky
  '#f2a65e', // orange
  '#8ee08e', // green
  '#e07de0', // magenta
  '#f2e05e', // yellow
  '#7d8ff2', // indigo
  '#f25e7a', // rose
  '#5ef2d3', // mint
  '#c9a06b', // tan
  '#b58ef2'  // violet
];

/**
 * @param {string|null|undefined} gridId
 * @param {Array<{id: string}>} grids  in tab order
 * @returns {string|null}  null when the pulse has no workspace
 */
export function wsColor(gridId, grids) {
  if (!gridId) return null;
  const i = (grids ?? []).findIndex((g) => g.id === gridId);
  if (i < 0) return null;
  return WS_PALETTE[i % WS_PALETTE.length];
}

/**
 * Order pulses by workspace (tab order), pulses with no workspace last;
 * relative order inside a workspace is preserved.
 * @template {{grid_id?: string|null}} P
 * @param {P[]} pulses
 * @param {Array<{id: string}>} grids
 * @returns {P[]}
 */
export function groupByWorkspace(pulses, grids) {
  const rank = new Map((grids ?? []).map((g, i) => [g.id, i]));
  const key = (p) => (p.grid_id && rank.has(p.grid_id) ? rank.get(p.grid_id) : Number.MAX_SAFE_INTEGER);
  return pulses
    .map((p, i) => ({ p, i }))
    .sort((a, b) => key(a.p) - key(b.p) || a.i - b.i)
    .map((x) => x.p);
}

/**
 * Sub-workspaces: a grid may carry `parent_id`. The daemon already returns grids in tab order — parent, then its children — so these
 * are just views over that flat list.
 */

/** Top-level grids only (a child whose parent is missing counts as top). */
export function topGrids(grids) {
  const ids = new Set((grids ?? []).map((g) => g.id));
  return (grids ?? []).filter((g) => !g.parent_id || !ids.has(g.parent_id));
}

/** Direct sub-workspaces of `gridId`, in tab order. */
export function childGrids(gridId, grids) {
  return (grids ?? []).filter((g) => g.parent_id === gridId);
}

/** Tree: [{ grid, children: [grid, ...] }] in tab order. */
export function gridTree(grids) {
  return topGrids(grids).map((g) => ({ grid: g, children: childGrids(g.id, grids) }));
}

/**
 * The ids a selected tab shows: the grid itself plus, for a parent, every
 * sub-workspace inside it. A sub-workspace tab shows only itself.
 */
export function gridFamily(gridId, grids) {
  const ids = new Set([gridId]);
  for (const c of childGrids(gridId, grids)) ids.add(c.id);
  return ids;
}

/** The top-level grid a tab belongs to (itself when it is top level). */
export function topOf(gridId, grids) {
  const g = (grids ?? []).find((x) => x.id === gridId);
  if (!g) return gridId;
  const parent = g.parent_id && (grids ?? []).find((x) => x.id === g.parent_id);
  return parent ? parent.id : g.id;
}
