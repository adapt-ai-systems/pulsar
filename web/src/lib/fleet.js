/**
 * Pure helpers for the sidebar "fleet" panels (Watches, Schedules) — every
 * pulse's watches/schedules in one list. No Svelte here so node can test it.
 */

import { parseAtIso } from './schedule.js';

const DAY_IDX = { sun: 0, mon: 1, tue: 2, wed: 3, thu: 4, fri: 5, sat: 6 };

/**
 * When a schedule fires next, as a Date, or null when it never will
 * (disabled, spent once, malformed). Mirrors the daemon scheduler:
 *   loop  — last_fired_at (or created_at) + interval_minutes
 *   once  — at_iso, only while still in the future
 *   daily — next cron_hhmm (UTC) on an allowed day (days csv, empty = every day)
 * @param {object} s  schedule row
 * @param {Date} [now]
 * @returns {Date|null}
 */
export function nextFireAt(s, now = new Date()) {
  if (!s || !s.enabled) return null;
  if (s.kind === 'loop') {
    const base = new Date(s.last_fired_at ?? s.created_at ?? now);
    if (Number.isNaN(base.getTime()) || !(s.interval_minutes > 0)) return null;
    return new Date(base.getTime() + s.interval_minutes * 60000);
  }
  if (s.kind === 'once') {
    const at = parseAtIso(s.at_iso);
    if (Number.isNaN(at.getTime()) || at <= now) return null;
    return at;
  }
  if (s.kind === 'daily') {
    const m = /^(\d{2}):(\d{2})$/.exec(s.cron_hhmm ?? '');
    if (!m) return null;
    const allowed = (s.days ?? '')
      .split(',')
      .map((d) => DAY_IDX[d.trim().toLowerCase()])
      .filter((d) => d !== undefined);
    for (let i = 0; i < 8; i++) {
      const d = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(), now.getUTCDate() + i, +m[1], +m[2]));
      if (d <= now) continue;
      if (allowed.length && !allowed.includes(d.getUTCDay())) continue;
      return d;
    }
    return null;
  }
  return null;
}

/** "in 4m", "in 2h 05m", "in 3d", or "overdue 12m" for past dates. */
export function fmtUntil(at, now = new Date()) {
  if (!at) return '—';
  let ms = at.getTime() - now.getTime();
  const late = ms < 0;
  ms = Math.abs(ms);
  const mins = Math.round(ms / 60000);
  let s;
  if (mins < 1) s = 'now';
  else if (mins < 60) s = `${mins}m`;
  else if (mins < 60 * 24) s = `${Math.floor(mins / 60)}h ${String(mins % 60).padStart(2, '0')}m`;
  else s = `${Math.floor(mins / 1440)}d`;
  return late ? `overdue ${s}` : mins < 1 ? 'now' : `in ${s}`;
}

/**
 * Sort schedules for the fleet view: enabled first by soonest fire, then
 * disabled by pulse title. Returns [{ s, at }].
 */
export function orderSchedules(schedules, titles, now = new Date(), isDead = () => false) {
  // isDead(s): the owner can never be woken (archived) — sorts with the
  // disabled ones, no fire time, whatever the row's own enabled flag says.
  return schedules
    .map((s) => ({ s, at: isDead(s) ? null : nextFireAt(s, now) }))
    .sort((a, b) => {
      if (!!a.at !== !!b.at) return a.at ? -1 : 1;
      if (a.at && b.at && a.at.getTime() !== b.at.getTime()) return a.at - b.at;
      return (titles[a.s.pulse_id] ?? '').localeCompare(titles[b.s.pulse_id] ?? '');
    });
}

/**
 * Group watches by owner pulse: [{ pulseId, title, watches, met }], owners
 * with a MET watch first, then by title.
 */
export function groupWatches(watches, titles) {
  const by = new Map();
  for (const w of watches) {
    if (!by.has(w.pulse_id)) by.set(w.pulse_id, []);
    by.get(w.pulse_id).push(w);
  }
  return [...by.entries()]
    .map(([pulseId, ws]) => ({
      pulseId,
      title: titles[pulseId] ?? pulseId.slice(0, 8),
      watches: ws,
      met: ws.filter((w) => !!w.bad).length
    }))
    .sort((a, b) => (b.met > 0) - (a.met > 0) || a.title.localeCompare(b.title));
}

/**
 * The Watchers shelf: every pulse that is keeping an eye on something — owns
 * an open condition watch or an enabled schedule — or was shelved with a mode
 * (watching | paused), grouped by owner.
 * Ordering: owners with a MET watch first, then failed deliveries, then
 * watching-mode, then everything else by title. `pulseInfo` maps id ->
 * { title, state, mode } (roster + archived lookups merged by the caller).
 *
 * Each group: { pulseId, title, mode, state, watches, schedules, met,
 *   failed, nextAt (Date|null — soonest schedule fire OR watch check),
 *   lastFiredAt (Date|null — latest watch fire or schedule fire) }.
 */
export function groupMonitoring(watches, schedules, pulseInfo, now = new Date()) {
  const by = new Map();
  const ensure = (id) => {
    if (!by.has(id)) {
      const info = pulseInfo[id] ?? {};
      by.set(id, {
        pulseId: id,
        title: info.title ?? id.slice(0, 8),
        mode: info.mode ?? null,
        state: info.state ?? 'unknown',
        watches: [],
        schedules: [],
        met: 0,
        failed: 0,
        nextAt: null,
        lastFiredAt: null
      });
    }
    return by.get(id);
  };
  const soonest = (g, d) => {
    if (d && !Number.isNaN(d.getTime()) && (!g.nextAt || d < g.nextAt)) g.nextAt = d;
  };
  const latest = (g, iso) => {
    if (!iso) return;
    const d = new Date(iso);
    if (!Number.isNaN(d.getTime()) && (!g.lastFiredAt || d > g.lastFiredAt)) g.lastFiredAt = d;
  };
  for (const w of watches) {
    const g = ensure(w.pulse_id);
    g.watches.push(w);
    if (w.bad) g.met += 1;
    if (typeof w.last_delivery === 'string' && w.last_delivery.startsWith('failed')) g.failed += 1;
    if (w.next_check_at) soonest(g, new Date(w.next_check_at));
    latest(g, w.last_fired_at);
  }
  for (const s of schedules) {
    const g = ensure(s.pulse_id);
    g.schedules.push(s);
    if (g.state !== 'archived') soonest(g, nextFireAt(s, now));
    latest(g, s.last_fired_at);
  }
  // Shelved pulses with nothing registered still belong on the shelf.
  for (const [id, info] of Object.entries(pulseInfo)) {
    // An archived pulse keeps its old mode; it is not on the shelf.
    if (info && info.state !== 'archived' && (info.mode === 'watching' || info.mode === 'paused')) ensure(id);
  }
  const rank = (g) =>
    g.met > 0 ? 0 : g.failed > 0 ? 1 : g.mode === 'watching' ? 2 : g.mode === 'paused' ? 4 : 3;
  return [...by.values()].sort(
    (a, b) => rank(a) - rank(b) || a.title.localeCompare(b.title)
  );
}

/** Watch delivery state for a row: 'met' | 'failed' | 'delivered' | null. */
export function watchDelivery(w) {
  if (w.bad) return 'met';
  const d = w.last_delivery;
  if (typeof d === 'string' && d.startsWith('failed')) return 'failed';
  if (d === 'delivered') return 'delivered';
  return null;
}
