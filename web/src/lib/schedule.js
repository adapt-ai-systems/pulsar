/**
 * Schedule helpers shared by NewPulseModal + SettingsModal (Milestone 3).
 *
 * The user enters LOCAL wall-clock times. The daemon stores/compares in UTC,
 * so we convert here:
 *   - one-shot  -> `at_iso` is a UTC ISO instant
 *   - recurring -> `cron_hhmm` is a UTC "HH:MM"
 *
 * Conversion uses the browser's local timezone via `Date` (rather than
 * hard-coding a fixed offset) so DST is handled correctly.
 */

import { triggerLabel, hasEventTriggers } from './looptriggers.js';
import { getDefaultTimeZone } from './time.js';

const DAY_KEYS = ['sun', 'mon', 'tue', 'wed', 'thu', 'fri', 'sat'];

/** Two-digit zero-pad. */
function pad2(n) {
  return String(n).padStart(2, '0');
}

/**
 * Convert a local `<input type=datetime-local>` value ("YYYY-MM-DDTHH:MM") to a
 * UTC ISO instant string (e.g. "2026-07-27T16:30:00.000Z").
 * @param {string} localValue
 * @returns {string|null} ISO instant, or null if the input is empty/unparseable.
 */
export function localDatetimeToUtcIso(localValue) {
  if (!localValue) return null;
  // `new Date("YYYY-MM-DDTHH:MM")` (no zone) is interpreted as LOCAL time.
  const d = new Date(localValue);
  if (Number.isNaN(d.getTime())) return null;
  return d.toISOString();
}

/**
 * Convert a local "HH:MM" wall-clock time to a UTC "HH:MM" for `cron_hhmm`.
 * Uses today's date so the current UTC offset (incl. DST) is applied. The date
 * component is irrelevant to a daily cron — only the resulting hour/minute.
 * @param {string} localHhmm  "HH:MM" in local time
 * @returns {string|null} "HH:MM" in UTC, or null if unparseable.
 */
export function localHhmmToUtcHhmm(localHhmm) {
  if (!localHhmm) return null;
  const m = /^(\d{1,2}):(\d{2})$/.exec(localHhmm.trim());
  if (!m) return null;
  const hh = Number(m[1]);
  const mm = Number(m[2]);
  if (hh < 0 || hh > 23 || mm < 0 || mm > 59) return null;
  const d = new Date();
  d.setHours(hh, mm, 0, 0); // local wall-clock
  return `${pad2(d.getUTCHours())}:${pad2(d.getUTCMinutes())}`;
}

/**
 * Convert a UTC "HH:MM" (as stored) back to a local "HH:MM" for display/editing.
 * @param {string} utcHhmm
 * @returns {string} local "HH:MM", or the input unchanged if unparseable.
 */
export function utcHhmmToLocalHhmm(utcHhmm) {
  if (!utcHhmm) return '';
  const m = /^(\d{1,2}):(\d{2})$/.exec(utcHhmm.trim());
  if (!m) return utcHhmm;
  const d = new Date();
  d.setUTCHours(Number(m[1]), Number(m[2]), 0, 0);
  return `${pad2(d.getHours())}:${pad2(d.getMinutes())}`;
}

/**
 * Human "wakes …" label for a schedule (drawer subtitle / settings row).
 * All times rendered in the active time zone (configured or browser local).
 * @param {import('./api.js').Schedule} s
 * @returns {string}
 */
/**
 * An `at_iso` as a Date. The daemon reads a zone-less stamp as UTC (scheduler
 * _parse_iso) and `pulsar-wait in` writes zone-less ones, so do the same —
 * `new Date()` would read it as local time (+7h on the shelf in PDT).
 * @param {string|null|undefined} iso
 */
export function parseAtIso(iso) {
  if (!iso) return new Date(NaN);
  const s = String(iso).trim();
  if (/(Z|[+-]\d{2}:?\d{2})$/i.test(s)) return new Date(s);
  // "2026-09-24 09:00" (space) is zone-less too; a bare date is
  // already UTC to Date().
  const m = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2}(?::\d{2}(?:\.\d+)?)?)$/.exec(s);
  return new Date(m ? `${m[1]}T${m[2]}Z` : s);
}

export function scheduleLabel(s) {
  if (!s) return '';
  if (s.kind === 'loop') {
    if (hasEventTriggers(s.triggers) || s.trigger_mode === 'all') {
      return `loop ${triggerLabel(s.triggers, s.interval_minutes, s.trigger_mode)}`;
    }
    return `loop ${s.interval_minutes}m`;
  }
  if (s.kind === 'once') {
    const d = s.at_iso ? parseAtIso(s.at_iso) : null;
    if (!d || Number.isNaN(d.getTime())) return 'wakes once';
    const tz = getDefaultTimeZone();
    const when = d.toLocaleString(undefined, {
      ...(tz ? { timeZone: tz } : {}),
      weekday: 'short',
      hour: 'numeric',
      minute: '2-digit'
    });
    return `wakes ${when}`;
  }
  if (s.kind === 'daily') {
    const local = utcHhmmToLocalHhmm(s.cron_hhmm);
    const timeLabel = formatHhmm(local);
    if (s.days) {
      const days = s.days
        .split(',')
        .map((d) => d.trim())
        .filter(Boolean)
        .map((d) => d.charAt(0).toUpperCase() + d.slice(1))
        .join(' ');
      return `wakes ${days} ${timeLabel}`;
    }
    return `wakes daily ${timeLabel}`;
  }
  return '';
}

/** "13:05" (24h) -> "1:05pm" for friendlier subtitles. */
function formatHhmm(hhmm) {
  const m = /^(\d{1,2}):(\d{2})$/.exec(hhmm || '');
  if (!m) return hhmm || '';
  let hh = Number(m[1]);
  const mm = m[2];
  const ampm = hh >= 12 ? 'pm' : 'am';
  hh = hh % 12 || 12;
  return mm === '00' ? `${hh}${ampm}` : `${hh}:${mm}${ampm}`;
}

export { DAY_KEYS };
