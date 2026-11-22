/**
 * Pure helper for the top-level surface restore / deep-link decision.
 *
 * +page.svelte owns `surface` ('pulses' | 'inbox' | 'hub'). On load the
 * app must decide between:
 *   - a URL deep link  /?surface=inbox[&item=<id>]  → force that surface and
 *     strip the params from the URL, and
 *   - the persisted surface (localStorage) → the restore gate, which today
 *     honors every surface in VALID_SURFACES.
 *   - otherwise → 'pulses' (the default home).
 *
 * The decision itself is pure so it can be node-tested; the DOM work (actually
 * rewriting the URL) stays in the component.
 */

/** Settings KV key for the phone's last pane: {grid, pulse}. */
export const MOBILE_LOCATION_KEY = 'mobile.location';

/** Restorable top-level surfaces. */
export const VALID_SURFACES = ['pulses', 'inbox', 'hub'];

/** @returns {string|null} the surface when a valid one, else null. */
export function validSurface(v) {
  return VALID_SURFACES.includes(v) ? v : null;
}

/**
 * Parse a location search string (the part after '?') into its inbox-relevant
 * deep-link params. Accepts a URLSearchParams or a raw string.
 * @param {string|URLSearchParams} search
 * @returns {{surface: string|null, item: string|null}}
 */
export function readDeepLink(search) {
  const p = typeof search === 'string' ? new URLSearchParams(search) : search;
  const surface = p?.get('surface') ?? null;
  const item = p?.get('item') ?? null;
  const pulseFocus = (p?.get('pulse-focus') ?? '').trim() || null;
  return {
    surface: validSurface(surface),
    item: item && item.trim() ? item.trim() : null,
    pulseFocus,
  };
}

/**
 * Decide the surface to show on load and whether the URL carries a deep link
 * that must be stripped.
 * @param {{saved: string|null, url: string|null}} input
 *        saved — the persisted surface (raw localStorage value, may be null)
 *        url   — the raw ?surface= value from the URL (may be null)
 * @returns {{surface: ('pulses'|'inbox'|'hub'), fromUrlParam: boolean}}
 */
export function resolveSurface({ saved = null, url = null } = {}) {
  const fromUrl = validSurface(url);
  if (fromUrl) return { surface: fromUrl, fromUrlParam: true };
  const fromSaved = validSurface(saved);
  return { surface: fromSaved ?? 'pulses', fromUrlParam: false };
}

/**
 * Whether the caller should strip 'surface'/'item'/'pulse-focus' from the URL.
 * True when a valid surface param or a pulse-focus link was present on load —
 * we leave unknown params alone.
 * @param {string|URLSearchParams} search
 * @returns {boolean}
 */
export function shouldStripDeepLink(search) {
  const dl = readDeepLink(search);
  return dl.surface != null || dl.pulseFocus != null;
}

/** Optional default saved profile name for embeds unless `&theme=` specifies one. */
export const EMBED_THEME_DEFAULT = null;

/**
 * Single-pulse embed (iframe of one pane).
 * `?pulse=<id>&embed=1` stays on the URL — do not strip it — so a reload
 * keeps the lock. `embed` without a pulse id is ignored.
 * @param {string|URLSearchParams} search
 * @returns {{pulse: string|null, embed: boolean, theme: string|null}}
 */
export function readEmbedLink(search) {
  const p = typeof search === 'string' ? new URLSearchParams(search) : search;
  const pulse = (p?.get('pulse') ?? '').trim();
  const embed = p?.get('embed') === '1' && !!pulse;
  // Optional built-in appearance profile override via `&theme=<name>`. When unset
  // or unmatched, the embed falls back to the device's active theme.
  const theme = embed ? ((p?.get('theme') ?? '').trim() || EMBED_THEME_DEFAULT) : null;
  return {
    pulse: pulse || null,
    embed,
    theme,
  };
}

/**
 * In-app pulse hot link: a chat anchor whose href is same-origin and carries
 * ?pulse-focus=<id> (what POST /pulses returns as `link`). Returns the pulse
 * id so the click can open that pane in place instead of a new tab.
 * @param {string|null} href
 * @param {string} origin — window.location.origin
 * @returns {string|null}
 */
export function pulseLinkTarget(href, origin) {
  if (!href) return null;
  let u;
  try {
    u = new URL(href, origin);
  } catch {
    return null;
  }
  if (u.origin !== origin) return null;
  return (u.searchParams.get('pulse-focus') ?? '').trim() || null;
}
