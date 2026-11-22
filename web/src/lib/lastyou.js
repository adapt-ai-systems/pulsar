/**
 * The pane's "YOU SAID" strip: the user's newest real message and when its
 * turn ended. The pane loads ~100 events; when that window holds no real
 * message, one wide fetch (WIDE_TAIL events) looks further back.
 *
 * The answer is remembered per pulse, across pane remounts and turn flips,
 * and stays valid while each new window overlaps what was already scanned:
 * events only append, so nothing older can turn up later. A wide scan that
 * found NOTHING is remembered too. Without that, a pulse driven by loops,
 * watches or peers re-fetched 2,000 events (~300 KB gzipped) on every live
 * push: 2,513 such fetches from two browsers on 2026-11-02.
 */
import { isRealYou } from './render.js';
import { parseTs } from './time.js';

export const WIDE_TAIL = 2000;
const MAX_PULSES = 200;

/** pulse id -> { hit: {text, ts, attachments, doneTs}|null, through: ms of the newest scanned event } */
const memo = new Map();
const inflight = new Set();

/** Scan a window: newest real message + the first done after it. */
export function scanLastYou(evs) {
  for (let i = evs.length - 1; i >= 0; i--) {
    if (!isRealYou(evs[i])) continue;
    let doneTs = null;
    for (let j = i + 1; j < evs.length; j++) {
      if (evs[j].type === 'done') { doneTs = evs[j].ts ?? null; break; }
    }
    const ev = evs[i];
    return { text: ev.text ?? '', ts: ev.ts, attachments: ev.attachments ?? [], doneTs };
  }
  return null;
}

function edgeMs(evs, fromEnd) {
  for (let k = 0; k < evs.length; k++) {
    const t = parseTs(evs[fromEnd ? evs.length - 1 - k : k]?.ts);
    if (Number.isFinite(t)) return t;
  }
  return null;
}

function remember(pid, hit, evs, prev = null) {
  const last = edgeMs(evs, true);
  const through = last == null ? prev : prev == null ? last : Math.max(prev, last);
  memo.delete(pid); // re-insert: newest last, so the cap drops the stalest pulse
  if (through == null) return;
  memo.set(pid, { hit, through });
  if (memo.size > MAX_PULSES) memo.delete(memo.keys().next().value);
}

/**
 * The strip for one applied event window.
 *
 * `show`: the value to display; undefined = leave the strip as it is (a wide
 * fetch is running). `fetch`: get WIDE_TAIL events for this pulse, then pass
 * them to lastYouFromWide (null on failure) — exactly once per true.
 * @returns {{ show: object|null|undefined, fetch: boolean }}
 */
export function lastYouFromWindow(pid, evs, tailLimit) {
  const hit = scanLastYou(evs);
  if (hit) {
    remember(pid, hit, evs);
    return { show: hit, fetch: false };
  }
  const m = memo.get(pid);
  const first = edgeMs(evs, false);
  if (m && first != null && first <= m.through) {
    // Contiguous with what was scanned before: the remembered message is
    // still the newest. Its turn ends at the first done in this window that
    // precedes any newer user_message.
    let show = m.hit;
    if (show && !show.doneTs) {
      for (const ev of evs) {
        if (ev.type === 'user_message') break;
        if (ev.type === 'done') { show = { ...show, doneTs: ev.ts ?? null }; break; }
      }
    }
    remember(pid, show, evs, m.through);
    return { show, fetch: false };
  }
  // A window short of its limit is the whole log: nothing further back.
  if (evs.length < tailLimit) {
    remember(pid, null, evs);
    return { show: null, fetch: false };
  }
  if (inflight.has(pid)) return { show: undefined, fetch: false };
  inflight.add(pid);
  return { show: undefined, fetch: true };
}

/**
 * Record the wide fetch a `fetch: true` asked for.
 * @param {string} pid
 * @param {any[]|null} evs  the WIDE_TAIL window, or null when the fetch failed
 * @returns {object|null|undefined} the strip; undefined = leave it (failed)
 */
export function lastYouFromWide(pid, evs) {
  inflight.delete(pid);
  if (!evs) return undefined;
  const hit = scanLastYou(evs);
  remember(pid, hit, evs);
  return hit;
}

/** Tests only. */
export function _resetLastYou() {
  memo.clear();
  inflight.clear();
}
