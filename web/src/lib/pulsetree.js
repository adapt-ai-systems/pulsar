/**
 * Pure helpers for the pulse list (drawer and full-screen window): phase
 * labels, ordering, the workspace tree, and workspace / id reordering.
 *
 * One behaviour for both lists (the drawer's rules win): archived rows sort by `archived_at`, search also matches the model's label,
 * and a pulse with no / an unknown workspace is named 'no grid' / '—'.
 */
import { attentionOf, rollUp, byAttention } from './attention.js';
import { gridTree } from './wscolor.js';
import { modelLabel } from './models.js';

/** The phases that LIGHT UP and flash: working, asking a question, or finished and unseen. */
export const LIT = new Set(['working', 'question', 'done']);
export const PHASE_LABEL = { question: 'question', stalled: 'stalled', done: 'done', working: 'working', idle: '', stopped: '' };

/**
 * One pulse's attention phase.
 * @param {any} p
 * @param {{ questionIds?: Set<string>, doneIds?: Set<string>, nowMs?: number }} ctx
 */
export function phaseOf(p, { questionIds, doneIds, nowMs } = {}) {
  return attentionOf(p, { questionPulseIds: questionIds, doneIds, nowMs });
}

/** One enabled schedule per pulse for the row subtitle (loops win over wakes). */
export function schedByPulse(schedules) {
  const m = new Map();
  for (const s of schedules) {
    if (!s.enabled) continue;
    const cur = m.get(s.pulse_id);
    if (!cur || (cur.kind !== 'loop' && s.kind === 'loop')) m.set(s.pulse_id, s);
  }
  return m;
}

/**
 * The workspace name the search matches: 'no grid' for a pulse with none,
 * '—' for an id that is not in `grids`.
 * @param {any[]} grids
 * @returns {(id: string|null|undefined) => string}
 */
export function gridNameFn(grids) {
  const m = new Map(grids.map((g) => [g.id, g.name]));
  return (id) => (id ? m.get(id) ?? '—' : 'no grid');
}

/**
 * Does the pulse match the search text? Searches title, model id, the model's
 * label, and the name of its workspace.
 * @param {any} p
 * @param {string} query
 * @param {{ gridName: (id: string|null|undefined) => string, modelLabelOf?: (id: string) => string }} opts
 */
export function matchesQuery(p, query, { gridName, modelLabelOf = modelLabel }) {
  const q = query.trim().toLowerCase();
  if (!q) return true;
  return `${p.title ?? ''} ${p.model ?? ''} ${modelLabelOf(p.model)} ${gridName(p.grid_id)}`.toLowerCase().includes(q);
}

/**
 * Filter pulses for the All Pulses list. An active search query automatically
 * includes archived pulses even while `hideArchived` is checked.
 * @param {any[]} pulses
 * @param {{ query?: string, hideArchived?: boolean, hideWatchers?: boolean,
 *           gridName: (id: string|null|undefined) => string,
 *           modelLabelOf?: (id: string) => string }} opts
 */
export function filterPulses(pulses, { query = '', hideArchived = false, hideWatchers = false, gridName, modelLabelOf = modelLabel }) {
  const hasQuery = query.trim() !== '';
  return pulses.filter(
    (p) =>
      !(hideArchived && !hasQuery && p.state === 'archived') &&
      !(hideWatchers && isWatcher(p)) &&
      matchesQuery(p, query, { gridName, modelLabelOf })
  );
}

/**
 * "Recent" means last touched: for archived rows the archive time, otherwise
 * the last store update.
 */
function recency(p) {
  const iso = p.state === 'archived' ? (p.archived_at ?? p.updated_at) : p.updated_at;
  return Date.parse(iso ?? p.created_at ?? '') || 0;
}

const inputRecency = (p) => Date.parse(p.last_input_at ?? '') || 0;

/**
 * A sorted copy. `sortBy`: 'attention' | 'recent' | 'name' | 'created' |
 * 'byinput' (ordered by when the user last messaged each pulse; pulse
 * activity never moves a row). `phaseOf` is needed for 'attention' only.
 * @param {any[]} list
 * @param {string} sortBy
 * @param {{ phaseOf?: (p: any) => string }} [opts]
 */
export function sortPulses(list, sortBy, { phaseOf: phase } = {}) {
  const out = [...list];
  if (sortBy === 'byinput') {
    out.sort((a, b) => inputRecency(b) - inputRecency(a));
    return out;
  }
  if (sortBy === 'name') {
    out.sort((a, b) => (a.title ?? '').localeCompare(b.title ?? ''));
  } else if (sortBy === 'created') {
    out.sort((a, b) => (Date.parse(b.created_at ?? '') || 0) - (Date.parse(a.created_at ?? '') || 0));
  } else {
    out.sort((a, b) => recency(b) - recency(a));
  }
  // "attention": most urgent first, recency breaking ties (stable sort).
  if (sortBy === 'attention') out.sort(byAttention(phase));
  return out;
}

/**
 * The tree a list renders: one section per top-level workspace (tab order),
 * each with its own pulses, its sub-workspaces (each with theirs), a roll-up
 * phase and a count — then "no workspace" last. With a search query only
 * branches that hold a match are shown; otherwise every workspace shows, even
 * empty, so the whole structure is visible. `byInput` gives ONE flat section.
 * `pulses` arrive already ordered (see sortPulses) and keep that order.
 * @param {any[]} pulses
 * @param {any[]} grids
 * @param {{ query?: string, byInput?: boolean, modelLabelOf?: (id: string) => string,
 *           phaseOf: (p: any) => string }} opts
 */
export function buildSections(pulses, grids, { query = '', byInput = false, modelLabelOf, phaseOf: phase }) {
  const q = query.trim();
  const gridName = gridNameFn(grids);
  const shown = pulses.filter((p) => matchesQuery(p, query, { gridName, modelLabelOf }));
  if (byInput) return [{ grid: null, flat: true, pulses: shown, subs: [], count: shown.length, phase: rollUp(shown.map(phase)) }];
  const byGrid = new Map();
  const loose = [];
  for (const p of shown) {
    if (!p.grid_id) loose.push(p);
    else (byGrid.get(p.grid_id) ?? byGrid.set(p.grid_id, []).get(p.grid_id)).push(p);
  }
  const branch = (g) => {
    const own = byGrid.get(g.id) ?? [];
    return { grid: g, pulses: own, phase: rollUp(own.map(phase)), count: own.length };
  };
  const out = [];
  for (const { grid, children } of gridTree(grids)) {
    const top = branch(grid);
    const subs = children.map(branch).filter((b) => !q || b.count > 0);
    const count = top.count + subs.reduce((n, b) => n + b.count, 0);
    if (q && count === 0) continue;
    out.push({ ...top, subs, count, phase: rollUp([top.phase, ...subs.map((b) => b.phase)]) });
  }
  if (loose.length) out.push({ grid: null, pulses: loose, subs: [], count: loose.length, phase: rollUp(loose.map(phase)) });
  return out;
}

/**
 * The whole workspace order after dropping workspace `id` (child of `parent`,
 * or null for top level) above or below its sibling `target.id` — every
 * workspace with its subs kept together, in tab order. null when the target
 * is not a sibling in `grids`.
 * @param {any[]} grids
 * @param {string} id
 * @param {string|null} parent
 * @param {{ id: string, after: boolean }} target
 * @returns {string[]|null}
 */
export function reorderGridIds(grids, id, parent, target) {
  const sibs = grids.filter((g) => (g.parent_id ?? null) === parent && g.id !== id);
  let at = sibs.findIndex((g) => g.id === target.id);
  if (at < 0) return null;
  if (target.after) at += 1;
  const moved = grids.find((g) => g.id === id);
  sibs.splice(at, 0, moved);
  const byParent = new Map();
  for (const g of grids) {
    const k = g.parent_id ?? null;
    (byParent.get(k) ?? byParent.set(k, []).get(k)).push(g);
  }
  byParent.set(parent, sibs);
  const ids = [];
  for (const top of byParent.get(null) ?? []) {
    ids.push(top.id);
    for (const c of byParent.get(top.id) ?? []) ids.push(c.id);
  }
  return ids;
}

/**
 * `order` with `from` taken out and put into `to`'s slot. A new array; null
 * when either id is missing or both are the same (nothing to do).
 * @param {string[]} order
 * @param {string} from
 * @param {string} to
 * @returns {string[]|null}
 */
export function moveId(order, from, to) {
  const f = order.indexOf(from);
  const t = order.indexOf(to);
  if (f < 0 || t < 0 || f === t) return null;
  const next = [...order];
  next.splice(t, 0, next.splice(f, 1)[0]);
  return next;
}

/** A watcher pulse: on the Watchers shelf (mode watching) or titled Watcher. */
export function isWatcher(p) {
  return p?.settings?.mode === 'watching' || /\bwatcher\b/i.test(p?.title ?? '');
}

/** How often the drawer's 3s timer refetches the archived rows. */
export const ARCHIVED_REFRESH_MS = 30000;

/**
 * The live rows plus the archived rows from the slower fetch. A live row wins
 * over its archived copy: a pulse unarchived since that fetch shows once.
 */
export function withArchived(live, archived) {
  const ids = new Set(live.map((p) => p.id));
  return [...live, ...archived.filter((p) => !ids.has(p.id))];
}
