/**
 * Hub state shared by the sidebar and the Hub surface.
 *
 * - pages: the catalog from GET /hub/pages (loaded on demand when hub.url is set).
 * - pins:  the sidebar tabs, [{slug, title}] in order. Kept by the daemon
 *          (/hub/pins) so phone, desktop and agents share them; polled when
 *          hub.url is configured so a page an agent pins appears without reload.
 * - open:  the page showing in the Hub surface (null = the catalog).
 * - zoom:  per-page zoom, per device (a phone and a monitor want different
 *          zoom for the same page), in localStorage.
 */
import { listHubPages, listHubPins, pinHubPage, unpinHubPage } from './api.js';
import { isHubEnabled } from './appsettings.svelte.js';

export const HUB_PROXY_PREFIX = '/hub-proxy';

const LS_OPEN = 'pulsar.hub.open';
const LS_ZOOM = 'pulsar.hub.zoom';

export const ZOOM_STEPS = [0.33, 0.5, 0.67, 0.75, 0.8, 0.9, 1, 1.1, 1.25, 1.5, 1.75, 2, 2.5];

function lsGet(key, dflt) {
  if (typeof localStorage === 'undefined') return dflt;
  try {
    const v = localStorage.getItem(key);
    return v === null ? dflt : JSON.parse(v);
  } catch {
    return dflt;
  }
}

function lsSet(key, value) {
  try {
    if (value === null) localStorage.removeItem(key);
    else localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode / quota: the in-memory state still works */
  }
}

export const hub = $state({
  pages: [],
  pagesLoaded: false,
  pagesError: '',
  pins: [],
  open: lsGet(LS_OPEN, null),
  zoom: lsGet(LS_ZOOM, {})
});

/** Same-origin proxy URL for a published hub page (proxied to `hub.url`). */
export function pageUrl(slug) {
  return `${HUB_PROXY_PREFIX}/${encodeURIComponent(slug)}/`;
}

export async function loadPages() {
  if (!isHubEnabled()) {
    hub.pages = [];
    hub.pagesError = '';
    hub.pagesLoaded = false;
    return;
  }
  const r = await listHubPages();
  if (r.ok) {
    hub.pages = r.value ?? [];
    hub.pagesError = '';
  } else {
    console.log('[hub] pages load failed', r.error);
    hub.pagesError = r.error;
  }
  hub.pagesLoaded = true;
}

export async function loadPins() {
  if (!isHubEnabled()) {
    hub.pins = [];
    return;
  }
  const r = await listHubPins();
  if (r.ok) {
    hub.pins = r.value ?? [];
  } else {
    console.log('[hub] pins load failed', r.error);
  }
}

// Optimistic: the tab moves at once; the daemon's answer (the whole list,
// agent pins included) replaces it, and a failure puts the old list back.
async function changePins(optimistic, call) {
  if (!isHubEnabled()) return;
  const prev = hub.pins;
  hub.pins = optimistic;
  const r = await call();
  if (r.ok) {
    hub.pins = r.value ?? [];
  } else {
    console.log('[hub] pins save failed', r.error);
    hub.pins = prev;
  }
}

export function pin(slug) {
  if (isPinned(slug)) return;
  const title = titleFor(slug);
  return changePins([...hub.pins, { slug, title }], () => pinHubPage(slug));
}

export function unpin(slug) {
  return changePins(hub.pins.filter((p) => p.slug !== slug), () => unpinHubPage(slug));
}

export function isPinned(slug) {
  return hub.pins.some((p) => p.slug === slug);
}

export function titleFor(slug) {
  return hub.pages.find((p) => p.slug === slug)?.title
    ?? hub.pins.find((p) => p.slug === slug)?.title
    ?? slug;
}

export function togglePin(slug) {
  return isPinned(slug) ? unpin(slug) : pin(slug);
}

export function openPage(slug) {
  hub.open = slug;
  lsSet(LS_OPEN, slug);
}

export function closePage() {
  hub.open = null;
  lsSet(LS_OPEN, null);
}

export function zoomOf(slug) {
  return hub.zoom[slug] ?? 1;
}

/** Step the zoom of one page: dir -1 / +1, or 0 to reset to 100%. */
export function stepZoom(slug, dir) {
  const cur = zoomOf(slug);
  let next = 1;
  if (dir > 0) next = ZOOM_STEPS.find((z) => z > cur + 1e-6) ?? ZOOM_STEPS.at(-1);
  else if (dir < 0) next = [...ZOOM_STEPS].reverse().find((z) => z < cur - 1e-6) ?? ZOOM_STEPS[0];
  const map = { ...hub.zoom };
  if (next === 1) delete map[slug];
  else map[slug] = next;
  hub.zoom = map;
  lsSet(LS_ZOOM, map);
}
