/**
 * Pulsar theme system.
 *
 * The whole UI is driven by CSS custom properties on :root — a theme is a flat
 * token->color map. This module owns:
 *   - the 13 built-in palettes (7 dark + 6 light; Adapt Dark & Adapt Light
 *     defaults match the 7A/7B brand lockups) and the 🎲 random-theme generator,
 *   - a theme PROFILE = { name, colors:{token->hex}, bg:{url,zoom,x,y} },
 *   - applying a profile to the document (setProperty per token + background),
 *   - persistence via the daemon settings KV (GET/PUT /settings/{key}), with a
 *     localStorage cache used ONLY for an instant first paint before the KV
 *     round-trips.
 *
 * Persistence contract (daemon settings KV, enveloped {ok,value}):
 *   key "pulsar.theme.active"   -> the currently-applied profile object
 *   key "pulsar.theme.profiles" -> array of user-saved profile objects
 * localStorage mirrors the active profile under "pulsar-active-theme" for paint.
 *
 * Standing logging rule: every KV failure path console.logs with context; a
 * theme load/save never fails silently, and a KV outage falls back to the cache
 * or the default palette rather than leaving the page unstyled.
 */

import { getSettingForDevice, getDeviceSetting, putDeviceSetting } from './api.js';

// [name, page, bg, surface, surface-warm, border, border-strong, ink, ink-dim,
//  ink-meta, dreamer, user, sprite, bad, matron, warden]
// Thirteen curated palettes — 7 dark first, then 6 light. LIGHT_NAMES below keys the badges.
export const PALETTES = [
  // dark — Adapt Dark matches Logo 7A (deep navy-charcoal, signal orange, cool steel slate).
  ['Adapt Dark', '#0b0e17', '#0f131e', '#161b29', '#1e2536', '#283146', '#3b4762', '#f1f4f9', '#b0bccb', '#7a879b', '#f46b2a', '#6fb1f7', '#3fd896', '#ff5568', '#f5b841', '#a78bfa'],
  ['Nightfall', '#04040c', '#010107', '#0a0b18', '#111225', '#1c1e38', '#2e3156', '#e9ebf8', '#9ba1c6', '#676e97', '#5ee6ff', '#ff5ed2', '#59ffb5', '#ff4d6b', '#ffd166', '#b07dff'],
  ['Obsidian Teal', '#020605', '#010403', '#0a1512', '#0f1f1b', '#1b3a33', '#2d5a4f', '#e8f2f0', '#93bfb7', '#5f8a80', '#3ee6c2', '#ff8a5c', '#7de8a0', '#ff5e5e', '#ffc46b', '#8fb8ff'],
  ['Velvet Dusk', '#080309', '#050208', '#130a18', '#1c1023', '#301c3c', '#4d2f5e', '#f3eaf6', '#c0a5cc', '#83698f', '#e6b45e', '#c88aff', '#6fe0a8', '#ff5470', '#ff9e6b', '#ff8ab8'],
  ['Deep Forest', '#020603', '#010402', '#0a160d', '#0f2114', '#1c3524', '#2e5739', '#e6f2ea', '#96c4aa', '#5f8f74', '#5fe8a0', '#ffb054', '#8ff0b8', '#ff5e5e', '#ffcf6b', '#8fd4ff'],
  ['Charcoal Ember', '#050404', '#030202', '#0d0d0e', '#151517', '#252528', '#3d3d42', '#ededee', '#a0a0a7', '#6d6d74', '#ff9d54', '#5fb8ff', '#7de8a0', '#ff5252', '#ffc46b', '#c9a8ff'],
  ['Slate Harbor', '#111621', '#161d2b', '#1f283b', '#27324a', '#33415e', '#475a80', '#e5ecf6', '#9fb2ce', '#7489ab', '#6db8ff', '#f29e74', '#62dba8', '#f56578', '#ebb95b', '#b59cff'],
  // light — Adapt Light matches Logo 7B (clean white/alabaster, burnt orange, dark slate).
  ['Adapt Light', '#eef1f5', '#f7f9fc', '#ffffff', '#edf1f6', '#d6dde6', '#adb8c7', '#11161f', '#384454', '#5c6a7d', '#c6460c', '#225ea8', '#158052', '#c82838', '#9e6306', '#6946b8'],
  ['Porcelain', '#eef1f7', '#f6f8fb', '#ffffff', '#eef1f8', '#d7deea', '#b4c0d4', '#1c2333', '#4e5c75', '#7c89a0', '#4056c8', '#c8388f', '#148f62', '#cc2f44', '#b57a14', '#7448c8'],
  ['Linen', '#f3efe6', '#faf7f0', '#fffdf8', '#f2ecdf', '#ded4bf', '#bfb096', '#2a231a', '#5e5443', '#8c8170', '#b4551f', '#1f7d8c', '#2f8f5f', '#c23327', '#9c7410', '#8a5fb8'],
  ['Morning Fog', '#e9edf2', '#f2f5f8', '#fbfcfe', '#e9eef4', '#d2dae3', '#adbac9', '#232b36', '#52627a', '#7e8ca0', '#3b6fb0', '#a04fa8', '#2a9470', '#c93b47', '#b0781f', '#5f6fd4'],
  ['Matcha', '#edf2e9', '#f5f8f2', '#fdfefb', '#ecf2e6', '#d5dfcc', '#b0c2a2', '#212b1e', '#52614a', '#7d8c74', '#48822a', '#b45a24', '#2f9455', '#c23838', '#a07b16', '#6a6ac8'],
  ['Seashell', '#f7eeeb', '#fbf5f3', '#fffbfa', '#f6ebe7', '#e6d4cd', '#ccafa4', '#302220', '#66524c', '#92807a', '#c8503c', '#8a4fc8', '#218f66', '#c9303d', '#a5731a', '#3b7fb8']
];

/** Names of the light built-ins. */
export const LIGHT_NAMES = ['Adapt Light', 'Porcelain', 'Linen', 'Morning Fog', 'Matcha', 'Seashell'];

// Token order matches PALETTES columns 1..15 (index 0 is the name).
export const VARS = [
  '--page', '--bg', '--surface', '--surface-warm', '--border', '--border-strong',
  '--ink', '--ink-dim', '--ink-meta', '--dreamer', '--user', '--sprite', '--bad',
  '--matron', '--warden'
];
export const LABELS = [
  'Page', 'Background', 'Surface', 'Surface warm', 'Border', 'Border strong',
  'Text', 'Text dim', 'Text meta', 'AI accent', 'Your voice', 'Live', 'Alert',
  'Waiting', 'Extra'
];

export const DEFAULT_DARK = 'Adapt Dark';
export const DEFAULT_LIGHT = 'Adapt Light';

// Background images ship with these built-ins; the list is trivially extensible
// (drop a file in static/ and add a line). Generated dark space/nebula set so
// every roll has a place to look at — four new 2026-09-01 (aurora, violet
// nebula, ember static, deep starfield) alongside the original two.
// The space set ships as WebP (3-9% of the old PNGs, which stay in static/ for
// saved profiles that still name them; bgUrl maps those to the WebP). Every
// entry has a small static/bg-thumbs/<name>.webp for the Theme menu pills.
export const BGS = [
  { name: 'Distant Pulsar', url: '/bg-distant-pulsar.webp' },
  { name: 'Radio Rings', url: '/bg-radio-rings.webp' },
  { name: 'Aurora Drift', url: '/bg-aurora-drift.webp' },
  { name: 'Violet Nebula', url: '/bg-violet-nebula.webp' },
  { name: 'Ember Static', url: '/bg-ember-static.webp' },
  { name: 'Deep Starfield', url: '/bg-deep-starfield.webp' },
  { name: 'Cobalt Drift', url: '/bg-cobalt-drift.webp' },
  { name: 'Crimson Veil', url: '/bg-crimson-veil.webp' },
  { name: 'Amethyst Veil', url: '/bg-amethyst-veil.webp' },
  { name: 'Cyan Tide', url: '/bg-cyan-tide.webp' },
  { name: 'Indigo Haze', url: '/bg-indigo-haze.webp' },
  { name: 'Azure Glow', url: '/bg-azure-glow.webp' },
  { name: 'Magenta Flare', url: '/bg-magenta-flare.webp' },
  { name: 'Ember Rift', url: '/bg-ember-rift.webp' },
  { name: 'Violet Storm', url: '/bg-violet-storm.webp' },
  // Void set: ten monochrome backgrounds for the Void family.
  { name: 'Void · Eclipse', url: '/bg-void-eclipse.jpg' },
  { name: 'Void · Porthole', url: '/bg-void-porthole.jpg' },
  { name: 'Void · Brushed Steel', url: '/bg-void-brushed-steel.jpg' },
  { name: 'Void · Contours', url: '/bg-void-contours.jpg' },
  { name: 'Void · Ink Wisp', url: '/bg-void-ink-wisp.jpg' },
  { name: 'Void · Wire Terrain', url: '/bg-void-wire-terrain.jpg' },
  { name: 'Void · Rain Window', url: '/bg-void-rain-window.jpg' },
  { name: 'Void · Oscilloscope', url: '/bg-void-oscilloscope.jpg' },
  { name: 'Void · Concrete Light', url: '/bg-void-concrete-light.jpg' },
  { name: 'Void · Constellations', url: '/bg-void-constellations.jpg' }
];

/** A bg url as painted: a saved `/bg-<name>.png` from before the WebP switch
 * maps to its WebP twin; anything else passes through unchanged. */
export function bgUrl(url) {
  if (typeof url !== 'string' || !url.endsWith('.png')) return url;
  const webp = url.slice(0, -4) + '.webp';
  return BGS.some((b) => b.url === webp) ? webp : url;
}

/** The Theme menu thumbnail for a built-in background url, else the url. */
export function bgThumb(url) {
  const m = /^\/(bg-[a-z0-9-]+)\.(?:png|webp|jpg)$/.exec(String(url ?? ''));
  return m ? `/bg-thumbs/${m[1]}.webp` : url;
}

const KV_ACTIVE = 'pulsar.theme.active';
const LS_ACTIVE = 'pulsar-active-theme'; // paint cache only

/** Turn a PALETTES row into a token->hex colors map. */
export function colorsFromPalette(row) {
  return Object.fromEntries(VARS.map((v, k) => [v, row[k + 1]]));
}

/** The built-in profile for a palette name (case-insensitive), or null if unknown. */
export function builtinProfile(name) {
  const needle = typeof name === 'string' ? name.trim().toLowerCase() : '';
  if (!needle) return null;
  const row = PALETTES.find((p) => p[0].toLowerCase() === needle);
  if (!row) return null;
  return {
    name: row[0],
    colors: colorsFromPalette(row),
    bg: emptyBg(),
    text: emptyText()
  };
}

export function emptyBg() {
  return { url: null, zoom: 100, x: 50, y: 50, opacity: 50 };
}

// --- theme randomizer ------------------------------------------------------ //
// The 🎲 button rolls a whole palette from scratch rather than picking a
// built-in. Every knob (tint, depth, accent hues, punch) is random; legibility
// comes from enforced WCAG contrast rules, not fixed bands.

/** #rrggbb from HSL (h 0-360, s/l 0-100). */
export function hslHex(h, s, l) {
  const S = s / 100;
  const L = l / 100;
  const f = (n) => {
    const k = (n + h / 30) % 12;
    const c = L - S * Math.min(L, 1 - L) * Math.max(-1, Math.min(k - 3, 9 - k, 1));
    return Math.round(c * 255).toString(16).padStart(2, '0');
  };
  return `#${f(0)}${f(8)}${f(4)}`;
}

const HUE_WORDS = ['Crimson', 'Ember', 'Amber', 'Gold', 'Lime', 'Jade', 'Teal', 'Cyan', 'Azure', 'Indigo', 'Violet', 'Magenta'];

// The 🎲's voice. A roll used to be named `<Hue> Day/Night` — technically random,
// but every name read the same. Now each roll gets a bespoke, evocative name:
// a hue word dressed in a character adjective (the vibe) and a setting word (the
// place the palette feels like). "Molten Teal Dusk", "Hushed Violet Frost",
// "Golden Jade Mirage" — no two roll names sound alike.
const MOOD_WORDS = ['Electric', 'Velvet', 'Molten', 'Frosted', 'Golden', 'Silken', 'Neon', 'Lucid', 'Radiant', 'Hushed', 'Wild', 'Serene', 'Bold', 'Smoky', 'Luminous', 'Dusky', 'Burning', 'Sleepy', 'Brittle', 'Silent'];
const SETTING_WORDS = ['Dusk', 'Dawn', 'Storm', 'Haze', 'Tide', 'Bloom', 'Frost', 'Fog', 'Mirage', 'Depths', 'Horizon', 'Static', 'Emberfield', 'Voyage', 'Reverie', 'Nocturne', 'Solstice', 'Drift', 'Signal', 'Wander'];

// A one-line tagline that sells the vibe of a roll — the flavor text shown under
// the theme name so a theme feels like a place, not just a palette.
const TAGLINES = [
  'Loud enough to wake the room.',
  'Quiet, until it isn\'t.',
  'A signal in the static.',
  'For the late-night hours.',
  'Born in a neon rainstorm.',
  'Warm like a kerosene lamp.',
  'Cold, clinical, unapologetic.',
  'Chromed and humming.',
  'Rough around the edges.',
  'Soft, like a bed you didn\'t earn.',
  'Tuned for deep focus.',
  'A little unhinged, in a good way.'
];

/**
 * A fun-themed name for a rolled palette. Deterministic in `rand` like the
 * rest of the roll: picks a mood (the character), the hue word (the base tint),
 * and a setting (the place it feels like) — e.g. "Molten Teal Dusk".
 */
export function rollName(h, light, rand = Math.random) {
  const hue = HUE_WORDS[Math.floor(h / 30) % 12];
  const mood = MOOD_WORDS[Math.floor(rand() * MOOD_WORDS.length)];
  const set = SETTING_WORDS[Math.floor(rand() * SETTING_WORDS.length)];
  return `${mood} ${hue} ${set}`;
}

/** A one-line tagline for a rolled theme; deterministic in `rand`. */
export function rollTagline(rand = Math.random) {
  return TAGLINES[Math.floor(rand() * TAGLINES.length)];
}

/** WCAG relative luminance of a #rrggbb color (0 black .. 1 white). */
export function relLuminance(hex) {
  const m = /^#?([0-9a-f]{6})$/i.exec(String(hex ?? '').trim());
  if (!m) return 0;
  const [r, g, b] = [0, 2, 4]
    .map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255)
    .map((c) => (c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4));
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

/** WCAG contrast ratio between two #rrggbb colors (1..21). */
export function contrastRatio(a, b) {
  const x = relLuminance(a);
  const y = relLuminance(b);
  const [hi, lo] = x > y ? [x, y] : [y, x];
  return (hi + 0.05) / (lo + 0.05);
}

/**
 * Walk an HSL color's lightness away from `bg` (up on dark themes, down on
 * light) until it clears `min` contrast. This is the rule that makes a fully
 * random roll safe: any hue/saturation is allowed, legibility is enforced
 * after the fact rather than baked into fixed lightness bands.
 */
function fitContrast(h, s, l, bg, min, lighten) {
  let L = l;
  let hex = hslHex(h, s, L);
  while (contrastRatio(hex, bg) < min && L > 0 && L < 100) {
    L += lighten ? 2 : -2;
    L = Math.max(0, Math.min(100, L));
    hex = hslHex(h, s, L);
  }
  return hex;
}

/**
 * Roll a genuinely random theme profile (no bg/text — the caller keeps the
 * user's current ones). Unlike the first-generation roller (fixed saturation
 * and lightness bands, so every roll was the same theme re-tinted), everything
 * varies per roll — tint depth, base darkness, accent hues and punch — and
 * coherence comes from RULES instead of constants:
 *   - text ≥7:1 on --bg, dim text ≥4.5:1, meta text ≥3:1 (no dark-on-dark),
 *   - every accent ≥3:1 on --bg (fitContrast walks lightness until it clears),
 *   - dreamer/user hues ≥70° apart so AI voice vs your voice always read apart,
 *   - --bad stays in the red zone and --matron in the amber zone (alert and
 *     waiting must still look like alarm and waiting),
 *   - page ≤ bg ≤ surface lightness stays monotonic so elevation still reads.
 *   - some rolls go BOLD (near-full-saturation chrome) or MONOCHROME (near-grey
 *     chrome, accents carry all the color) so rolls land across a wider spread
 *     instead of one family re-tinted.
 * `rand` is injectable so tests can be deterministic.
 */
export function randomProfile(rand = Math.random) {
  const light = rand() < 0.5;
  const h = Math.floor(rand() * 360);
  // Chrome hue may drift a little per token so heavy tints don't look flat.
  const drift = () => (h + (rand() * 36 - 18) + 360) % 360;

  // Neutrals: tint strength and base depth are the roll's personality —
  // anywhere from near-grey to near-full-saturation tint, near-black to
  // near-white depth. The ranges are deliberately wide so rolls span varied,
  // extreme theme parameters. A bold roll goes full-saturation neutrals;
  // a monochrome roll drains almost all color out of the chrome.
  const bold = rand() < 0.35;          // high-tint personality
  const mono = rand() < 0.12;          // near-grey chrome, accents carry the color
  const satN = mono
    ? 2 + rand() * 6
    : light
      ? (bold ? 40 + rand() * 45 : 6 + rand() * 34)
      : (bold ? 40 + rand() * 55 : 6 + rand() * 44);
  const bgL = light ? 86 + rand() * 13 : 2 + rand() * 13;
  const pageL = Math.max(1, bgL - (light ? 2 + rand() * 6 : 1 + rand() * 4));
  const surfL = Math.min(99, bgL + (light ? 2 + rand() * 4 : 2 + rand() * 6));
  const warmL = light ? pageL : Math.min(99, surfL + 2 + rand() * 4);
  const borderL = light ? bgL - (9 + rand() * 10) : surfL + 6 + rand() * 9;
  const strongL = light ? borderL - (9 + rand() * 10) : borderL + 8 + rand() * 9;
  const bg = hslHex(h, satN, bgL);

  // Ink: any tint allowed, contrast guaranteed (7:1 body, 4.5 dim, 3 meta).
  // Ink saturation is wider too so body text reads as a color of its own
  // rather than a grey — mono rolls stay grey, the rest tint toward the base.
  const hInk = mono ? h : drift();
  const satI = mono
    ? 2 + rand() * 6
    : light
      ? 12 + rand() * 40
      : 10 + rand() * 40;
  const colors = {
    '--page': hslHex(h, satN, pageL),
    '--bg': bg,
    '--surface': hslHex(h, satN, surfL),
    '--surface-warm': hslHex(h, satN, warmL),
    '--border': hslHex(h, Math.max(2, satN - 10), borderL),
    '--border-strong': hslHex(h, Math.max(2, satN - 14), strongL),
    '--ink': fitContrast(hInk, satI, light ? 6 + rand() * 16 : 84 + rand() * 14, bg, 7, !light),
    '--ink-dim': fitContrast(hInk, satI, light ? 26 + rand() * 18 : 58 + rand() * 16, bg, 4.5, !light),
    '--ink-meta': fitContrast(hInk, Math.max(4, satI - 12), light ? 40 + rand() * 18 : 42 + rand() * 16, bg, 3, !light)
  };

  // Accents: hue roams free, punch varies per roll across a wider band,
  // legibility enforced. Bold rolls punch harder (higher saturation floor);
  // mono rolls let the accents carry ALL the color, so they stay saturated.
  const sFloor = bold ? 70 : mono ? 82 : 55;
  const accent = (hue, minC = 3) => {
    const s = sFloor + rand() * (100 - sFloor);
    const l = light ? 22 + rand() * 30 : 48 + rand() * 34;
    return fitContrast(hue, s, l, bg, minC, !light);
  };
  const hDreamer = Math.floor(rand() * 360);
  const hUser = (hDreamer + 70 + rand() * 220) % 360;
  Object.assign(colors, {
    '--dreamer': accent(hDreamer),
    '--user': accent(hUser),
    '--sprite': accent(Math.floor(rand() * 360)),
    '--bad': accent((345 + rand() * 30) % 360),
    '--matron': accent(28 + rand() * 22),
    '--warden': accent(Math.floor(rand() * 360))
  });
  const name = rollName(h, light, rand);
  const tagline = rollTagline(rand);
  return { name, tagline, colors, bg: emptyBg(), text: emptyText() };
}

// --- text scale ----------------------------------------------------------- //
// Multipliers, not absolute sizes: every component keeps its own base px and
// the slider only scales it, so the type hierarchy survives at every setting.
export const TEXT_VARS = ['--tscale-chat', '--tscale-tool', '--tscale-ui', '--iscale'];
export const TEXT_LABELS = ['Chat text', 'Tool lines', 'Interface', 'Icons'];
export const TEXT_MIN = 0.7;
export const TEXT_MAX = 2.0;

export function emptyText() {
  return { chat: 1, tool: 1, ui: 1, icons: 1 };
}

/** Clamp + default one scale value. Garbage (NaN, null, a string) becomes 1. */
export function clampScale(n) {
  const v = Number(n);
  if (!Number.isFinite(v)) return 1;
  return Math.min(TEXT_MAX, Math.max(TEXT_MIN, v));
}

/** Normalize a profile's text block, tolerating absent/partial/legacy values. */
export function normalizeText(text) {
  const t = text ?? {};
  return {
    chat: clampScale(t.chat ?? 1),
    tool: clampScale(t.tool ?? 1),
    ui: clampScale(t.ui ?? 1),
    // Icon scale postdates the three text sliders; an older profile without it
    // normalizes to 1 and renders unchanged.
    icons: clampScale(t.icons ?? 1)
  };
}

/** Paint the text-scale + icon-scale multipliers onto :root. */
export function applyText(text) {
  if (typeof document === 'undefined') return;
  const t = normalizeText(text);
  const root = document.documentElement;
  root.style.setProperty('--tscale-chat', String(t.chat));
  root.style.setProperty('--tscale-tool', String(t.tool));
  root.style.setProperty('--tscale-ui', String(t.ui));
  root.style.setProperty('--iscale', String(t.icons));
}

// --- peer-pulse color ------------------------------------------------------ //
// Messages injected by OTHER pulses render under the sender's name in --peer
// so their voice color stays distinct. It postdates the built-in palettes, so
// an unset token is DERIVED from --warden ("Extra"), which every palette keeps
// distinct from --user and --dreamer — and a profile may pin '--peer' explicitly.
export const PEER_VAR = '--peer';
export const PEER_FALLBACK = '#a78bfa';

/** The peer color a palette implies before any explicit pin. */
export function peerDefault(colors) {
  return (colors && colors['--warden']) || PEER_FALLBACK;
}

/** The pinned peer color, or null when the profile follows the palette. */
export function peerOverride(colors) {
  return (colors && colors[PEER_VAR]) || null;
}

/** What actually renders: the pin when present, else the derivation. */
export function peerColor(colors) {
  return peerOverride(colors) ?? peerDefault(colors);
}

/** Paint --peer onto :root. */
export function applyPeer(colors) {
  if (typeof document === 'undefined') return;
  document.documentElement.style.setProperty(PEER_VAR, peerColor(colors));
}

// --- message bubbles ------------------------------------------------------- //
// Customizable background washes and opacities for replies, user messages, and
// peer pulse messages. Derive-until-pinned contract: unpinned, each bubble follows the voice color it always borrowed (--user / --dreamer / peer), at
// the wash strengths ChatPane used to hard-code (7% / 8%; replies now 10% —
// see applyMsg). --bub-reply is the exception: it can NEVER be pinned, because
// the reply's left accent rule mixes from it and must stay the AI's own voice
// color in every palette.
export const MSG_VARS = ['--bub-you', '--bub-reply', '--bub-peer'];
export const MSG_LABELS = ['Your messages', 'Replies', 'Other pulses'];

/** The bubble base colors a palette implies before any explicit pin. */
export function msgDefaults(colors) {
  const c = (v, fb) => (colors && colors[v]) || fb;
  return {
    '--bub-you': c('--user', '#6fb1f7'),
    '--bub-reply': c('--dreamer', '#f46b2a'),
    '--bub-peer': peerColor(colors)
  };
}

/** The bubble tokens a profile pins explicitly (absent = follow the palette). */
/**
 * The bubble tokens a profile pins explicitly (absent = the palette).
 * --bub-reply is deliberately EXCLUDED from what counts as a pin: the reply's
 * left accent rule mixes from it and always follows --dreamer (see msgColors),
 * so surfacing a stale pin would offer a knob that does nothing. Legacy
 * profiles may still carry the key and it still reaches the wash via msgColors.
 */
export function msgOverrides(colors) {
  const out = {};
  for (const v of MSG_VARS) if (colors && colors[v]) out[v] = colors[v];
  delete out['--bub-reply'];
  return out;
}

/**
 * What the appearance controls should show for a profile. Same resolution the
 * document gets EXCEPT the reply color, which the renderer always derives: no
 * swatch is offered for it, and showing an old pin would display a color the
 * pane no longer paints (verifier 2026-09-14: the swatch went red, the rule
 * stayed gray, and reset left the swatch red until the window reopened).
 */
export function msgShownOf(colors) {
  const shown = msgColors(colors);
  delete shown['--bub-reply'];
  return shown;
}

/**
 * What the bubbles actually render: palette derivation, then overrides — except
 * --bub-reply, which is always derived. The reply's left accent rule and its
 * wash both mix from it, so a profile that pinned it to the page ink rendered
 * the accent pale gray on gray (visual verifier 2026-09-14: #e2e2e2 on the
 * 9/12 theme). Pinning --bub-you / --bub-peer still works as before.
 */
export function msgColors(colors) {
  return { ...msgDefaults(colors), ...msgOverrides(colors), '--bub-reply': msgDefaults(colors)['--bub-reply'] };
}

// Wash opacities, percent of the base color mixed over the page (0 = no wash,
// 100 = solid). Fallbacks are the exact strengths ChatPane hard-coded.
export const MSG_NUMS = [
  { key: 'you', var: '--bub-you-fill', label: 'Yours', min: 0, max: 100, step: 1, unit: '%', fallback: 7 },
  { key: 'reply', var: '--bub-reply-fill', label: 'Replies', min: 0, max: 100, step: 1, unit: '%', fallback: 10 },
  { key: 'peer', var: '--bub-peer-fill', label: 'Other pulses', min: 0, max: 100, step: 1, unit: '%', fallback: 8 }
];

export function emptyMsgNums() {
  return Object.fromEntries(MSG_NUMS.map((n) => [n.key, n.fallback]));
}

/** Clamp one opacity to 0..100; absent/garbage falls back (same as the other strengths). */
export function clampMsgNum(key, value) {
  const spec = MSG_NUMS.find((n) => n.key === key);
  if (!spec) return 0;
  if (value === null || value === undefined || value === '') return spec.fallback;
  const v = Number(value);
  if (!Number.isFinite(v)) return spec.fallback;
  return Math.min(spec.max, Math.max(spec.min, Math.round(v)));
}

/** Normalize a profile's msg block, tolerating absent/partial/legacy values. */
export function normalizeMsgNums(nums) {
  const n = nums ?? {};
  return Object.fromEntries(
    MSG_NUMS.map((spec) => [spec.key, clampMsgNum(spec.key, n[spec.key] ?? spec.fallback)])
  );
}

/** Paint the bubble colors + wash strengths onto :root. */
export function applyMsg(colors, nums) {
  if (typeof document === 'undefined') return;
  const resolved = msgColors(colors);
  const n = normalizeMsgNums(nums);
  const root = document.documentElement;
  for (const v of MSG_VARS) root.style.setProperty(v, resolved[v]);
  for (const spec of MSG_NUMS) root.style.setProperty(spec.var, `${n[spec.key]}%`);
}

/** Mix two #rrggbb colors, `pct` (0..1) of `a` over `b` — CSS color-mix in srgb. */
export function mixHex(a, b, pct) {
  const parse = (hex) => {
    const m = /^#?([0-9a-f]{6})$/i.exec(String(hex ?? '').trim());
    return m ? [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16)) : null;
  };
  const x = parse(a);
  const y = parse(b);
  if (!x || !y) return typeof a === 'string' ? a : '#000000';
  const p = Math.min(1, Math.max(0, Number(pct)));
  const chan = (i) => Math.round(x[i] * p + y[i] * (1 - p));
  return '#' + [0, 1, 2].map((i) => chan(i).toString(16).padStart(2, '0')).join('');
}

/** The default profile (dark). */
export function defaultProfile() {
  return builtinProfile(DEFAULT_DARK);
}

/** Read the colors currently live on :root as a token->hex map. */
export function readLiveColors() {
  if (typeof document === 'undefined') return {};
  const cs = getComputedStyle(document.documentElement);
  return Object.fromEntries(
    VARS.map((v) => [v, (cs.getPropertyValue(v) || '').trim()])
  );
}

/**
 * Apply a theme profile to the document: set every color token, then the
 * background image (with pan/zoom) on every `.app` container.
 * @param {{colors: Record<string,string>, bg?: {url:string|null,zoom:number,x:number,y:number}}} profile
 */
let appliedExtras = new Set();

/** Font tokens click-to-edit mode can set, and the stacks it offers. */
export const FONT_VARS = { '--font-sans': 'Font', '--font-serif': 'Reading font', '--font-mono': 'Code font' };
export const FONT_CHOICES = [
  ['System sans', '-apple-system, "SF Pro Text", ui-sans-serif, system-ui, sans-serif'],
  ['Helvetica', '"Helvetica Neue", Helvetica, Arial, sans-serif'],
  ['Avenir', '"Avenir Next", Avenir, "Segoe UI", sans-serif'],
  ['Verdana', 'Verdana, Geneva, sans-serif'],
  ['System serif', 'ui-serif, "New York", Georgia, Cambria, serif'],
  ['Georgia', 'Georgia, serif'],
  ['Charter', 'Charter, "Bitstream Charter", Georgia, serif'],
  ['Iowan', '"Iowan Old Style", Palatino, Georgia, serif'],
  ['Palatino', 'Palatino, "Palatino Linotype", "Book Antiqua", serif'],
  ['SF Mono', '"SF Mono", ui-monospace, Menlo, monospace'],
  ['Menlo', 'Menlo, Monaco, Consolas, monospace'],
  ['JetBrains Mono', '"JetBrains Mono", "SF Mono", Menlo, monospace'],
  ['Courier', '"Courier New", Courier, monospace']
];

export function applyProfile(profile) {
  if (typeof document === 'undefined' || !profile) return;
  const root = document.documentElement;
  for (const v of VARS) {
    if (profile.colors && profile.colors[v]) root.style.setProperty(v, profile.colors[v]);
  }
  // Extra tokens pinned by click-to-edit mode (any other --var the UI uses,
  // fonts included). Clear the previous profile's extras so a theme switch
  // doesn't leave them stuck on :root.
  const extras = Object.entries(profile.colors ?? {}).filter(([k, val]) => k.startsWith('--') && !VARS.includes(k) && val);
  for (const k of appliedExtras) if (!profile.colors?.[k]) root.style.removeProperty(k);
  appliedExtras = new Set(extras.map(([k]) => k));
  for (const [k, val] of extras) root.style.setProperty(k, val);
  // Same deal for the peer-pulse color: unpinned follows --warden.
  applyPeer(profile.colors);
  // Bubble washes: unpinned colors follow the voices, absent `msg` block =
  // the old hard-coded strengths.
  applyMsg(profile.colors, profile.msg);
  applyBg(profile.bg ?? emptyBg());
  // Profiles saved before text scaling existed have no `text` block;
  // normalizeText turns that into all-1s, i.e. exactly how they looked.
  applyText(profile.text);
}

/** Set/clear a single color token live (swatch editing). */
export function setToken(token, hex) {
  if (typeof document === 'undefined') return;
  document.documentElement.style.setProperty(token, hex);
}

/**
 * Derive a theme-hue colorize tint for the background image, read from the
 * currently-applied --bg token. The built-in background images are near-black
 * nebulas, so a neutral var(--bg) scrim over them just darkens an already-dark
 * image and reads as nothing. Instead we lift the chrome's own hue to a vivid
 * mid-tone and wash that over the image, so the nebula picks up the theme
 * colour and reads as part of the theme rather than a separate stock photo.
 * @returns {string} a #rrggbb hex in the theme's base hue.
 */
function bgTintHex() {
  const cs = getComputedStyle(document.documentElement);
  const fallback = '#0f131e';
  const m = /^#?([0-9a-f]{6})$/i.exec((cs.getPropertyValue('--bg') || fallback).trim());
  if (!m) return fallback;
  const rgb = [0, 2, 4].map((i) => parseInt(m[1].slice(i, i + 2), 16) / 255);
  const max = Math.max(...rgb);
  const min = Math.min(...rgb);
  const l = (max + min) / 2;
  const d = max - min;
  let h = 0;
  let s = 0;
  if (d) {
    s = l > 0.5 ? d / (2 - max - min) : d / (max + min);
    if (max === rgb[0]) h = (rgb[1] - rgb[2]) / d + (rgb[1] < rgb[2] ? 6 : 0);
    else if (max === rgb[1]) h = (rgb[2] - rgb[0]) / d + 2;
    else h = (rgb[0] - rgb[1]) / d + 4;
    h *= 60;
  }
  // Keep the chrome's own saturation (a mono roll stays neutral) but lift the
  // lightness so the tint is actually visible over a near-black image. Dark
  // themes get a glowing mid-tone; light themes a soft pastel of the same hue.
  const sat = Math.max(10, Math.round(s * 100));
  const lPct = l > 0.5 ? 78 : 55;
  return hslHex(Math.round(h), sat, lPct);
}

/**
 * Paint the background image + pan/zoom onto every `.app` element. Mirrors the
 * mockup's applyBg() but the scrim is a theme-hue colourize (bgTintHex) so the
 * image tints toward the theme's colours instead of a neutral dark wash.
 * @param {{url:string|null, zoom:number, x:number, y:number}} bg
 */
export function applyBg(bg) {
  if (typeof document === 'undefined') return;
  const apps = document.querySelectorAll('.app');
  apps.forEach((a) => {
    if (!bg || !bg.url) {
      a.classList.remove('has-bg');
      a.style.backgroundImage = '';
      return;
    }
    a.classList.add('has-bg');
    // Colourize tint: wash the theme hue over the image (34%) so the nebula
    // inherits the theme colour and is visible. The tint reads through the
    // translucent chrome in the gutters while the reading panes stay legible.
    const tint = bgTintHex();
    a.style.backgroundImage =
      "linear-gradient(color-mix(in srgb," + tint + " 34%,transparent),color-mix(in srgb," + tint + " 34%,transparent)),url('" +
      bgUrl(bg.url) + "')";
    // 100 = cover (fills the viewport, upscales small images). Above 100 the
    // image grows past the viewport; below 100 it renders at that % of the
    // viewport width and tiles, so a small nebula stays crisp instead of
    // being stretched 2-3x.
    a.style.backgroundSize = bg.zoom === 100 ? 'cover' : bg.zoom + '% auto';
    a.style.backgroundRepeat = bg.zoom < 100 ? 'repeat' : 'no-repeat';
    a.style.backgroundPosition = bg.x + '% ' + bg.y + '%';
    // Background opacity: how strongly the image shows through the chrome.
    // Higher opacity = thinner theme-colour veil over the image, so the image
    // reads more clearly behind the panes and the chat text (main field).
    a.style.setProperty('--bg-wash', (100 - (bg.opacity ?? 50)) + '%');
  });
}

// --- persistence --------------------------------------------------------- //

/** Cache the active profile in localStorage for the next instant paint. */
function cacheActive(profile) {
  if (typeof localStorage === 'undefined') return;
  try {
    localStorage.setItem(LS_ACTIVE, JSON.stringify(profile));
  } catch (e) {
    console.log('[theme] cache active failed', e?.message ?? e);
  }
}

/** Read the paint cache (may be null). Never throws. */
export function readCachedActive() {
  if (typeof localStorage === 'undefined') return null;
  try {
    const raw = localStorage.getItem(LS_ACTIVE);
    return raw ? JSON.parse(raw) : null;
  } catch (e) {
    console.log('[theme] read cache failed', e?.message ?? e);
    return null;
  }
}

/**
 * Load the active profile from the KV. Returns the profile (falling back to the
 * cache, then the default) — never throws, always resolves to something usable.
 * @returns {Promise<object>}
 */
export async function loadActiveProfile() {
  // A device with no per-device pick yet (it last saved before per-device
  // themes) keeps what it last painted instead of snapping to the shared KV.
  const cached = readCachedActive();
  if (getDeviceSetting(KV_ACTIVE) === undefined && cached && cached.colors) {
    putDeviceSetting(KV_ACTIVE, cached);
    return cached;
  }
  const r = await getSettingForDevice(KV_ACTIVE); // this device's pick, else shared
  if (r.ok && r.value && r.value.colors) {
    cacheActive(r.value);
    return r.value;
  }
  if (!r.ok) {
    console.log('[theme] load active from KV failed', r.error, '→ falling back to cache/default');
  }
  return readCachedActive() ?? defaultProfile();
}

/** Persist the active profile to the KV + paint cache. */
export async function saveActiveProfile(profile) {
  cacheActive(profile);
  // Per device: the shared KV value stays as the fallback for new devices.
  if (!putDeviceSetting(KV_ACTIVE, profile)) {
    console.log('[theme] save active to device failed');
    return { ok: false, error: 'device storage unavailable' };
  }
  return { ok: true, value: null };
}
