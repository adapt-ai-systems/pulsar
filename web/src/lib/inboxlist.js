/**
 * Pure helpers for the Inbox surface (Inbox.svelte).
 *
 * Everything here is a pure function over the daemon's inbox item shape so it
 * can be node-tested (inboxlist.test.mjs) — DOM and fetch stay in the
 * component. Covers the three list responsibilities the mock makes the client
 * own:
 *   - view + channel + search filtering,
 *   - thread collapse (one row per thread_key with a ⛓ count),
 *   - per-channel unread counts + needs-response bookkeeping,
 * and the channel-chip color (deterministic hash of the channel name onto the
 * theme accent tokens).
 *
 * Item shape (daemon /inbox, /inbox/unread-count):
 *   { id, channel, thread_key, source_label, title, body, priority,
 *     needs_response, starred, read_at, created_at, ... }
 * read_at == null  ⇒ Inbox (unread); read_at set ⇒ Archive.
 */

/**
 * The accent tokens the chip palette hashes onto. Kept in sync
 * with app.css/:root. Order matters only for the accent → hex mapping in the
 * component; the hash picks an INDEX into this array.
 */
export const INBOX_ACCENTS = [
  '--dreamer',
  '--user',
  '--sprite',
  '--matron',
  '--warden',
  '--lantern',
];

/** An item is unread while its read_at is null. */
export function isUnread(item) {
  return !!item && !item.read_at;
}

/**
 * The unread number the rail badge should show so it agrees with the visible
 * inbox list. Two sources disagree by construction: /inbox/unread-count totals
 * EVERY unread row, while the list fetch is one capped page and the daemon
 * caps at ~200. The badge therefore shows the number of unread rows the Inbox
 * view can actually reach, never more.
 * @param {object} unread - the /inbox/unread-count value ({total, channels, threads})
 * @param {number} fetchedCount - unread rows the list view actually fetched
 * @returns {number}
 */
export function visibleUnreadTotal(unread, fetchedCount) {
  const total = Number(unread?.total ?? 0) || 0;
  const fetched = Number(fetchedCount ?? 0) || 0;
  // A list page shorter than the total means the rest is beyond the cap: the
  // view shows `fetched`, so that is the badge. An empty list with a nonzero
  // total (a fetch that failed, or rows that went read mid-poll) must show
  // what the view shows — 0 — not the stale total.
  return Math.min(total, fetched);
}

/** @returns {boolean} true when the item is a real sender reply we can reply to. */
export function isReplyable(item) {
  return !!(item && item.source_kind === 'pulse');
}

/**
 * Reconcile the detail selection after a list refresh. Opening an unread item
 * archives it, so the refreshed Inbox list legitimately no longer contains
 * that item. Keep the prior snapshot in that case; if the item is still in the
 * fetched view, prefer the fresh server copy.
 * @param {object|null} selectedItem
 * @param {Array<object>} items
 * @returns {object|null}
 */
export function reconcileSelectedItem(selectedItem, items) {
  if (!selectedItem) return null;
  return (items ?? []).find((item) => item.id === selectedItem.id) ?? selectedItem;
}

/**
 * Decide whether a finished touch gesture archives a row. Either direction
 * counts; the gesture must clear the distance threshold AND be clearly more
 * horizontal than vertical, so list scrolling never archives by accident.
 * @param {number} dx total horizontal travel in px
 * @param {number} dy total vertical travel in px
 * @param {number} [threshold] commit distance in px
 * @returns {boolean}
 */
export function shouldArchiveOnSwipe(dx, dy, threshold = 72) {
  return Math.abs(dx) >= threshold && Math.abs(dx) > Math.abs(dy) * 1.5;
}

/**
 * Decide which axis has won a touch gesture once it clears the intent slop.
 * A vertical win is sticky in the component so a scroll cannot later turn
 * into a destructive swipe.
 * @returns {'horizontal'|'vertical'|null}
 */
export function swipeGestureIntent(dx, dy, slop = 14) {
  const ax = Math.abs(Number(dx) || 0);
  const ay = Math.abs(Number(dy) || 0);
  if (Math.max(ax, ay) <= slop) return null;
  return ax > ay ? 'horizontal' : 'vertical';
}

/**
 * Deterministic channel-name → accent token (CSS var name). A stable
 * FNV-1a-ish hash of the lowercased channel slug picks an index, so a given
 * channel always gets the same chip color across renders AND across theme
 * profiles — the hash selects a TOKEN, not a hex, so chips follow the active
 * theme.
 * @param {string} channel
 * @param {string[]} [accents]
 * @returns {string} one of INBOX_ACCENTS
 */
export function channelColor(channel, accents = INBOX_ACCENTS) {
  const s = String(channel ?? '').toLowerCase();
  let h = 2166136261 >>> 0; // FNV-1a 32-bit offset basis
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 16777619) >>> 0;
  }
  const list = accents.length ? accents : INBOX_ACCENTS;
  return list[h % list.length];
}

/**
 * Filter a fetched item list for one of the three list views.
 *   inbox   → unread items only
 *   archive → read items only
 *   starred → starred items only (regardless of read state)
 * @param {Array<object>} items
 * @param {string} view  'inbox' | 'archive' | 'starred'
 * @returns {Array<object>}
 */
export function filterByView(items, view) {
  const list = items ?? [];
  if (view === 'inbox') return list.filter((i) => isUnread(i));
  if (view === 'archive') return list.filter((i) => !isUnread(i));
  if (view === 'starred') return list.filter((i) => i.starred);
  return list;
}

/**
 * Build the GET /inbox query for a top-level view. The daemon defaults
 * unread_only to true, so every view that needs archived rows must opt out.
 */
export function inboxQueryForView(view) {
  if (view === 'starred') return { unread_only: false, starred: true };
  if (view === 'archive') return { unread_only: false };
  return { unread_only: true };
}

/**
 * Narrow a list by the channel chip + free-text search (title/body/sender).
 * Search is case-insensitive substring, mirroring the mock. Returns a new
 * array (never mutates input).
 * @param {Array<object>} items
 * @param {{channel?: string|null, search?: string}} [filters]
 * @returns {Array<object>}
 */
export function filterItems(items, { channel = null, search = '' } = {}) {
  let list = items ?? [];
  if (channel) list = list.filter((i) => (i.channel ?? null) === channel);
  const q = String(search ?? '').trim().toLowerCase();
  if (q) {
    list = list.filter((i) => {
      const hay = `${i.title ?? ''} ${i.body ?? ''} ${i.source_label ?? ''}`.toLowerCase();
      return hay.includes(q);
    });
  }
  return list;
}

/**
 * Maximum stable API cursor among rendered rows. Composite cursors compare by
 * timestamp and numeric cursor_seq; legacy rows fall back to created_at.
 */
export function latestRenderedCursor(items) {
  let max = null;
  let maxAt = '';
  let maxSeq = null;
  for (const item of items ?? []) {
    const fallback = String(item?.created_at ?? '');
    const raw = typeof item?.cursor === 'string' && item.cursor ? item.cursor : fallback;
    if (!raw) continue;
    const split = raw.lastIndexOf('|');
    const parsedSeq = split > 0 ? Number(raw.slice(split + 1)) : NaN;
    const at = split > 0 && Number.isInteger(parsedSeq) ? raw.slice(0, split) : fallback;
    const seq = split > 0 && Number.isInteger(parsedSeq) ? parsedSeq : null;
    if (
      !max
      || at > maxAt
      || (at === maxAt && seq !== null && (maxSeq === null || seq > maxSeq))
    ) {
      max = raw;
      maxAt = at;
      maxSeq = seq;
    }
  }
  return max;
}

/**
 * Append an older page of items (fetched with `before=<cursor>`) under the
 * rows already loaded, keeping the daemon's newest-first order. Rows already
 * present win — the existing copy may be fresher (starred/scored/read since
 * the earlier fetch) — and re-fetching an overlapping page is idempotent.
 * Returns the input list untouched when the page adds nothing new.
 */
export function mergeOlderItems(items, older) {
  const current = items ?? [];
  const seen = new Set(current.map((item) => item?.id));
  const fresh = (older ?? []).filter((item) => item && !seen.has(item.id));
  return fresh.length ? [...current, ...fresh] : current;
}

/**
 * Expand the rows matched by a searched Inbox view back to the unread items
 * that those rendered rows represent. A matched thread row archives the
 * entire unread thread; unrelated rows remain untouched.
 */
export function renderedReadAllTargets(items, filters = {}) {
  const list = items ?? [];
  const rows = collapseThreads(filterItems(list, filters));
  const ids = new Set(rows.map((row) => row.id));
  const threads = new Set(rows.map((row) => row.thread_key).filter(Boolean));
  return list.filter(
    (item) => isUnread(item) && (ids.has(item.id) || (item.thread_key && threads.has(item.thread_key))),
  );
}

/** Return every outstanding needs-response member represented by an item. */
export function flaggedResponseItems(allItems, item) {
  if (!item) return [];
  if (!item.thread_key) return item.needs_response ? [item] : [];
  return (allItems ?? []).filter(
    (member) => member.thread_key === item.thread_key && member.needs_response,
  );
}

/**
 * Build the detail conversation while retaining the snapshot captured when a
 * row opened. Fresh current-list copies win by id, and the selected item wins
 * last; when an unread refetch drops the thread, the snapshot remains intact.
 */
export function detailThreadMembers(snapshot, currentItems, selectedItem) {
  if (!selectedItem) return [];
  if (!selectedItem.thread_key) return [selectedItem];
  const key = selectedItem.thread_key;
  const byId = new Map();
  for (const member of snapshot ?? []) {
    if (member?.thread_key === key) byId.set(member.id, member);
  }
  for (const member of currentItems ?? []) {
    if (member?.thread_key === key) byId.set(member.id, member);
  }
  byId.set(selectedItem.id, selectedItem);
  return [...byId.values()].sort(
    (a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')),
  );
}

/**
 * Convert a mute quick-pick value into the API timestamp. The select emits a
 * string, so conversion must happen before arithmetic. The one-day pick means
 * local end-of-day (not a rolling 24 hours).
 */
export function muteUntilIso(value, nowMs = Date.now()) {
  if (value === '' || value === null || value === undefined) return null;
  const duration = Number(value);
  if (!Number.isFinite(duration) || duration <= 0) return null;
  if (duration === 86400000) {
    const end = new Date(nowMs);
    end.setHours(23, 59, 59, 999);
    return end.toISOString();
  }
  return new Date(Number(nowMs) + duration).toISOString();
}

/**
 * Whether a channel's `mute_until` ISO timestamp is still in the future.
 * @param {string|null|undefined} muteUntil
 * @param {number} [nowMs]
 * @returns {boolean}
 */
export function isMuteUntilActive(muteUntil, nowMs = Date.now()) {
  if (!muteUntil) return false;
  const normalized = String(muteUntil).replace(/([+-]\d{2})(\d{2})$/, '$1:$2');
  const ms = Date.parse(normalized);
  return Number.isFinite(ms) && ms > nowMs;
}

/**
 * Collapse a list by thread_key. Standalone items (thread_key null/''),
 * plus the NEWEST item of each thread, appear as one row each. Each returned
 * row is the original item with an added `tcount` = number of items in that
 * thread across the WHOLE supplied list (not just the filtered view), so a
 * thread's ⛓ count stays truthful while only unread members are on screen.
 * @param {Array<object>} items
 * @returns {Array<object>} rows, each { ...item, tcount }
 */
export function collapseThreads(items) {
  const list = items ?? [];
  // group standalone items separately from threaded items
  const threadMembers = new Map(); // thread_key -> items[]
  const standalone = [];
  for (const i of list) {
    if (i.thread_key) {
      if (!threadMembers.has(i.thread_key)) threadMembers.set(i.thread_key, []);
      threadMembers.get(i.thread_key).push(i);
    } else {
      standalone.push(i);
    }
  }
  const rows = [];
  for (const key of threadMembers.keys()) {
    const members = threadMembers.get(key);
    // newest first; the head (members[0]) represents the thread in the list
    const sorted = [...members].sort(
      (a, b) => String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')) || 0,
    );
    rows.push({ ...sorted[0], tcount: members.length });
  }
  return [...standalone, ...rows].sort((a, b) => {
    const starOrder = Number(!!b.starred) - Number(!!a.starred);
    if (starOrder) return starOrder;
  const timeOrder = String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''));
  if (timeOrder) return timeOrder;
  return String(a.id ?? '').localeCompare(String(b.id ?? ''));
  });
}

/**
 * Does an item's text carry the search term (title/body/sender)? Mirrors the
 * server's escaped LIKE over exactly those columns, so the client's expand-
 * on-search decision marks the same rows the daemon matched.
 */
function textMatchesItem(item, term) {
  if (!item || !term) return false;
  const t = String(term).toLowerCase();
  return ['title', 'body', 'source_label'].some((f) =>
    String(item[f] ?? '').toLowerCase().includes(t));
}

/**
 * Collapse threads, EXPANDED under a search term so every matching message
 * is shown. Normally a thread_key collapses to one row with a ⛓ count — under
 * search that hides matching items inside a thread.
 * inside one collapsed row). When a term is active:
 *   - a thread with >=2 matching members expands: every MATCHING member
 *     becomes its own row (non-matching members stay hidden),
 *   - a thread with exactly 1 matching member keeps the collapsed shape —
 *     head = the thread's newest member (tcount = true size), so an
 *     unmatched older head cannot leak in via member order,
 *   - the whole set re-sorts newest-first.
 */
export function collapseThreadsForSearch(items, term) {
  if (!term) return collapseThreads(items);
  const list = items ?? [];
  const direct = list.filter((item) => textMatchesItem(item, term));
  const matchCount = new Map();
  for (const i of direct) {
    if (i.thread_key) matchCount.set(i.thread_key, (matchCount.get(i.thread_key) ?? 0) + 1);
  }
  const expandThreads = new Set(
    [...matchCount.entries()].filter(([, n]) => n >= 2).map(([k]) => k));
  const rows = [];
  const multi = new Map(); // thread_key -> matching members
  const singles = new Map(); // thread_key -> the sole matching member
  const rest = [];
  for (const item of list) {
    const tk = item.thread_key;
    if (tk && expandThreads.has(tk)) {
      if (textMatchesItem(item, term)) {
        if (!multi.has(tk)) multi.set(tk, []);
        multi.get(tk).push(item);
      }
      continue;
    }
    if (tk && matchCount.has(tk)) {
      if (textMatchesItem(item, term)) singles.set(tk, item);
      continue;
    }
    rest.push(item);
  }
  for (const [tk, members] of multi) {
    const sorted = [...members].sort((a, b) =>
      String(b.created_at ?? '').localeCompare(String(a.created_at ?? '')));
    rows.push(...sorted.map((m) => ({ ...m, tcount: members.length })));
  }
  for (const [tk, match] of singles) {
    // collapsed shape: the MATCHING member as head, tcount = true thread size
    const size = list.filter((i) => i.thread_key === tk).length;
    rows.push({ ...match, tcount: size });
  }
  rows.push(...collapseThreads(rest));
  rows.sort((a, b) => {
    const starOrder = Number(!!b.starred) - Number(!!a.starred);
    if (starOrder) return starOrder;
    const timeOrder = String(b.created_at ?? '').localeCompare(String(a.created_at ?? ''));
    if (timeOrder) return timeOrder;
    return String(a.id ?? '').localeCompare(String(b.id ?? ''));
  });
  return rows;
}

/**
 * Total unread across a raw unread-count map ({channels:{}, total, threads:{}}).
 * Handles the daemon's shape where `threads` may include a key "" for
 * non-threaded items — never counts it.
 * @param {object} unread - the /inbox/unread-count value
 * @returns {number}
 */
export function unreadTotal(unread) {
  return Number(unread?.total ?? 0) || 0;
}

/**
 * Per-channel unread counts from the unread-count map as [{channel, count}]
 * sorted by channel name — drives the channel chips' dots/counts.
 * @param {object} unread
 * @returns {Array<{channel: string, count: number}>}
 */
export function channelCounts(unread) {
  const map = unread?.channels ?? {};
  return Object.keys(map)
    .filter((c) => c) // skip a stray empty-string key defensively
    .map((c) => ({ channel: c, count: Number(map[c]) || 0 }))
    .sort((a, b) => a.channel.localeCompare(b.channel));
}

/**
 * The set of channel names present in the current item list (for the chips
 * when unread-count hasn't loaded or to show channels regardless of unread).
 * @param {Array<object>} items
 * @returns {string[]} unique channels, sorted
 */
export function channelsFromItems(items) {
  const set = new Set();
  for (const i of items ?? []) if (i.channel) set.add(i.channel);
  return [...set].sort();
}

/**
 * Build a plain-text preview from markdown-ish body for a list row (strip any
 * markup tags, collapse whitespace, cap length). Mirrors the mock's
 * `body.replace(/<[^>]+>/g,' ').trim().slice(...)`.
 * @param {string} body
 * @param {number} [max]
 * @returns {string}
 */
export function previewText(body, max = 90) {
  const plain = String(body ?? '')
    .replace(/<[^>]+>/g, ' ')
    .replace(/\s+/g, ' ')
    .trim();
  if (plain.length <= max) return plain;
  return plain.slice(0, max).replace(/\s+\S*$/, '') + '…';
}

/**
 * Whether a threaded item's group has any member needing response.
 * @param {Array<object>} allItems
 * @param {object} item
 * @returns {boolean}
 */
export function threadNeedsResponse(allItems, item) {
  if (!item?.thread_key) return !!item?.needs_response;
  return (allItems ?? []).some(
    (i) => i.thread_key === item.thread_key && i.needs_response,
  );
}
