/**
 * Usage/FUEL data source (usage meter and FUEL rail).
 *
 * Wired to the daemon's GET /usage (2026-07-27; Grok added 2026-08-13): Claude limit windows via the
 * Anthropic OAuth usage endpoint, Codex via the CLI's rollout rate-limit
 * frames, Grok via the cli-chat-proxy billing endpoint. DATA HONESTY: the
 * daemon returns stale/empty snapshots on any source failure and the rail
 * renders `no data` — never an invented number.
 *
 * Shape:
 *   { vendors: { claude: Snapshot|null, codex: Snapshot|null, grok: Snapshot|null, gemini: Snapshot|null } }
 *   Snapshot = { plan?, fetched_at, windows: Window[], stale_reason? }
 *   Window   = { id, label, used_percent, resets_at }
 */
import { getUsage } from './api.js';

export async function fetchUsage() {
  return getUsage();
}

/** Clamp a percentage into 0..100. */
export function clampPercent(v) {
  return Math.max(0, Math.min(100, v));
}

/** Severity band for a usage percentage (drives rail/bar color). */
export function severityForPercent(pct) {
  if (pct >= 85) return 'hot';
  if (pct >= 60) return 'warn';
  return 'normal';
}

/**
 * Continuous green → yellow → orange → red ramp for a usage percentage, so a
 * meter reddens as it fills and a red one means "about to run out" at a glance.
 *
 * Returns a CSS color-mix() of THEME TOKENS rather than a literal hsl(): the
 * palette is user-editable, so hard-coded hues would ignore the user's theme. Stops
 * are the app's existing semantic colors:
 *
 *   0% sprite (green) → 40% lantern (yellow) → 70% matron (orange) → 100% bad (red)
 *
 * Unequal on purpose — the top of the range is where the distinction earns its
 * keep, so orange→red owns a third of the ramp for the last 30% of usage.
 *
 * @param {number} pct 0..100 (clamped; non-finite input is treated as 0)
 * @returns {string} a CSS color value
 */
export function usageColor(pct) {
  const p = Number.isFinite(pct) ? clampPercent(pct) : 0;
  const STOPS = [
    [0, '--sprite'],
    [40, '--lantern'],
    [70, '--matron'],
    [100, '--bad']
  ];
  let i = 0;
  while (i < STOPS.length - 2 && p > STOPS[i + 1][0]) i++;
  const [lo, loVar] = STOPS[i];
  const [hi, hiVar] = STOPS[i + 1];
  const t = hi === lo ? 0 : (p - lo) / (hi - lo);
  // color-mix takes the FIRST color's share, so invert: t=0 must be all `lo`.
  const share = Math.round((1 - Math.max(0, Math.min(1, t))) * 100);
  return `color-mix(in srgb, var(${loVar}) ${share}%, var(${hiVar}))`;
}

/**
 * Compact time-until-reset for a window's `resets_at` ISO timestamp.
 * Two units max, largest first: "4d 9h", "1h 36m", "42m", "<1m", "now".
 * Returns null when resets_at is missing/unparseable — caller renders nothing
 * rather than an invented countdown (DATA HONESTY).
 */
export function formatResetIn(resetsAtIso, nowMs = Date.now()) {
  if (!resetsAtIso) return null;
  const t = Date.parse(resetsAtIso);
  if (Number.isNaN(t)) return null;
  const s = Math.floor((t - nowMs) / 1000);
  if (s <= 0) return 'now';
  const d = Math.floor(s / 86400);
  const h = Math.floor((s % 86400) / 3600);
  const m = Math.floor((s % 3600) / 60);
  if (d > 0) return `${d}d ${h}h`;
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m`;
  return '<1m';
}
