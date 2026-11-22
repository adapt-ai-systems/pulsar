/**
 * Event-timestamp formatting, shared by every chat-adjacent component, the
 * Inbox and the Activity log.
 *
 * Every time is shown in the configured `owner.timezone` when set, falling
 * back to the browser's local time zone. Each formatter also takes an optional
 * `zone` (an IANA name) for callers/tests that override the default.
 * Event ts is UTC "…+0000" — normalize the offset for iOS Safari's stricter
 * Date parser. Compact time; date prefix only when the stamp isn't from
 * today (in the active zone); full stamp, with the zone name, for the title
 * attribute.
 */

let defaultZone = null;

/** Set the module-wide fallback IANA time zone (from owner.timezone; null = browser zone). */
export function setDefaultTimeZone(zone) {
  const s = typeof zone === 'string' ? zone.trim() : '';
  if (!s) {
    defaultZone = null;
    return;
  }
  try {
    new Intl.DateTimeFormat('en-US', { timeZone: s });
    defaultZone = s;
  } catch {
    defaultZone = null;
  }
}

/** Current module-wide fallback IANA time zone, or null when using the browser zone. */
export function getDefaultTimeZone() {
  return defaultZone;
}

const cache = new Map();
/** One Intl.DateTimeFormat per (zone, locale, options) — building them is slow. */
function fmt(zone, locale, opts) {
  const z = zone ?? defaultZone ?? undefined;
  const key = `${z ?? ''}|${locale ?? ''}|${JSON.stringify(opts)}`;
  let f = cache.get(key);
  if (!f) {
    f = new Intl.DateTimeFormat(locale, z ? { ...opts, timeZone: z } : opts);
    cache.set(key, f);
  }
  return f;
}

/** Compact clock time for bookmark markers and badges in the active time zone. */
export function fmtBookmarkTime(ts, zone, { withZone = false } = {}) {
  const d = tsDate(ts);
  if (!d) return '';
  const opts = withZone
    ? { hour: 'numeric', minute: '2-digit', timeZoneName: 'short' }
    : { hour: 'numeric', minute: '2-digit' };
  return fmt(zone, 'en-US', opts).format(d);
}

/** "2026-10-14" style key of the calendar day `ms` falls on in `zone`. */
function dayKey(ms, zone) {
  return fmt(zone, 'en-CA', { year: 'numeric', month: '2-digit', day: '2-digit' }).format(ms);
}

export function tsDate(ts) {
  if (!ts) return null;
  const d = new Date(String(ts).replace(/([+-]\d{2})(\d{2})$/, '$1:$2'));
  return isNaN(d.getTime()) ? null : d;
}

const TIME = { hour: 'numeric', minute: '2-digit' };
const DAY = { month: 'short', day: 'numeric' };

/** Compact stamp: time today, "Oct 13 · time" otherwise (viewer's zone). */
export function fmtTs(ts, zone) {
  const d = tsDate(ts);
  if (!d) return '';
  const today = dayKey(Date.now(), zone) === dayKey(d.getTime(), zone);
  const time = fmt(zone, 'en-US', TIME).format(d);
  return today ? time : `${fmt(zone, 'en-US', DAY).format(d)} · ${time}`;
}

/**
 * Compact stamp for dense rows: "9:44a" today, "10/30" earlier this year,
 * "10/30/25" earlier than that, in the active time zone.
 * @param {string} ts
 * @param {string} [zone] IANA name; configured or browser zone when left out
 * @param {number} [nowMs] "today" is judged against this
 */
export function fmtStamp(ts, zone, nowMs = Date.now()) {
  const d = tsDate(ts);
  if (!d) return '';
  const day = dayKey(d.getTime(), zone);
  const today = dayKey(nowMs, zone);
  if (day === today) {
    return fmt(zone, 'en-US', TIME).format(d).replace(/\s*AM$/, 'a').replace(/\s*PM$/, 'p');
  }
  const sameYear = day.slice(0, 4) === today.slice(0, 4);
  return fmt(zone, 'en-US', sameYear
    ? { month: 'numeric', day: 'numeric' }
    : { month: 'numeric', day: 'numeric', year: '2-digit' }).format(d);
}

/** Full stamp for a title attribute, ends with the zone name ("PDT", "GMT+9"). */
export function fmtTsFull(ts, zone) {
  const d = tsDate(ts);
  return d ? fmt(zone, 'en-US', { dateStyle: 'medium', timeStyle: 'long' }).format(d) : '';
}

/**
 * Bare clock time for the Activity log, 24 h by default or 12 h with `h12`,
 * in the viewer's zone. Unparseable input comes back unchanged.
 * @param {string} ts
 * @param {{ zone?: string, h12?: boolean, locale?: string }} [opts]
 */
export function fmtClock(ts, { zone, h12 = false, locale } = {}) {
  if (!ts) return '';
  const ms = parseTs(ts);
  if (!Number.isFinite(ms)) return ts;
  return fmt(zone, locale, { hour: '2-digit', minute: '2-digit', hourCycle: h12 ? 'h12' : 'h23' }).format(ms);
}

/**
 * Inbox row/message time: 24 h time for today, "Oct 4, 07:40" for earlier
 * days, with the year added for earlier years. "Today" and the year are
 * judged in the viewer's zone. Unparseable input comes back unchanged.
 * @param {string} ts
 * @param {number} nowMs
 * @param {{ zone?: string, locale?: string }} [opts]
 */
export function fmtMailTime(ts, nowMs, { zone, locale } = {}) {
  const ms = parseTs(ts);
  if (!Number.isFinite(ms)) return ts ?? '';
  const time = fmt(zone, locale, { hour: '2-digit', minute: '2-digit', hourCycle: 'h23' }).format(ms);
  const day = dayKey(ms, zone);
  const today = dayKey(nowMs, zone);
  if (day === today) return time;
  const date = fmt(zone, locale, day.slice(0, 4) === today.slice(0, 4)
    ? { month: 'short', day: 'numeric' }
    : { month: 'short', day: 'numeric', year: 'numeric' }).format(ms);
  return `${date}, ${time}`;
}

/**
 * Parse an event timestamp to epoch ms, including the daemon's numeric
 * +0000 zone (a numeric zone WITHOUT the colon, which not every Date parser
 * accepts — normalize "+0000" -> "+00:00" first). The ONE copy (sweep
 * 2026-09-02).
 * @param {string} ts
 * @returns {number} epoch ms, or NaN if unparseable
 */
export function parseTs(ts) {
  if (!ts) return NaN;
  const normalized = String(ts).replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  return Date.parse(normalized);
}

/**
 * Compact "how long ago" label. ChatPane ticks a 1s clock — under a second shows
 * 'now', under an hour shows whole seconds ("42s"), then minutes, then hours.
 * @param {number} tsMs
 * @param {number} nowMs
 * @returns {string}
 */
export function agoLabel(tsMs, nowMs) {
  const seconds = Math.max(0, Math.floor((nowMs - tsMs) / 1000));
  if (seconds < 60) return seconds < 1 ? 'now' : `${seconds}s`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes}m`;
  return `${Math.floor(minutes / 60)}h`;
}
