import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  INBOX_ACCENTS,
  isUnread,
  visibleUnreadTotal,
  isReplyable,
  reconcileSelectedItem,
  shouldArchiveOnSwipe,
  swipeGestureIntent,
  channelColor,
  filterByView,
  inboxQueryForView,
  filterItems,
  mergeOlderItems,
  collapseThreadsForSearch,
  latestRenderedCursor,
  renderedReadAllTargets,
  flaggedResponseItems,
  detailThreadMembers,
  muteUntilIso,
  isMuteUntilActive,
  collapseThreads,
  unreadTotal,
  channelCounts,
  channelsFromItems,
  previewText,
  threadNeedsResponse,
} from './inboxlist.js';

const rawItem = (over = {}) => ({
  id: over.id ?? 'x',
  channel: over.channel ?? 'system',
  thread_key: over.thread_key ?? null,
  source_kind: over.source_kind ?? 'script',
  source_label: over.source_label ?? 'Src',
  title: over.title ?? 'Title',
  body: over.body ?? 'Body',
  priority: over.priority ?? 'notify',
  needs_response: over.needs_response ?? false,
  starred: over.starred ?? false,
  read_at: over.read_at ?? null,
  created_at: over.created_at ?? '2026-08-17T00:00:00+00:00',
  cursor: over.cursor ?? undefined,
});

test('isUnread reads read_at === null', () => {
  assert.equal(isUnread(rawItem()), true);
  assert.equal(isUnread(rawItem({ read_at: '2026-08-17T01:00:00+00:00' })), false);
  assert.equal(isUnread(null), false);
});

test('isReplyable only for pulse-sourced items', () => {
  assert.equal(isReplyable(rawItem({ source_kind: 'pulse' })), true);
  assert.equal(isReplyable(rawItem({ source_kind: 'script' })), false);
  assert.equal(isReplyable(rawItem({ source_kind: 'system' })), false);
  assert.equal(isReplyable(null), false);
});

test('reconcileSelectedItem keeps detail valid when read item leaves Inbox', () => {
  const selected = rawItem({ id: 'selected' });
  const fresh = rawItem({ id: 'selected', starred: true });

  assert.equal(reconcileSelectedItem(null, [fresh]), null);
  assert.equal(reconcileSelectedItem(selected, []), selected);
  assert.equal(reconcileSelectedItem(selected, [rawItem({ id: 'other' })]), selected);
  assert.equal(reconcileSelectedItem(selected, [fresh]), fresh);
});

test('shouldArchiveOnSwipe commits on horizontal swipes either way, never on scrolls', () => {
  assert.equal(shouldArchiveOnSwipe(100, 0), true);   // left-to-right
  assert.equal(shouldArchiveOnSwipe(-100, 0), true);  // right-to-left
  assert.equal(shouldArchiveOnSwipe(40, 0), false);   // too short
  assert.equal(shouldArchiveOnSwipe(80, 70), false);  // diagonal — mostly a scroll
  assert.equal(shouldArchiveOnSwipe(0, 120), false);  // pure scroll
  assert.equal(shouldArchiveOnSwipe(-90, 30), true);  // slight drift is fine
  assert.equal(shouldArchiveOnSwipe(50, 0, 50), true); // custom threshold honored
  // Regression: the gesture can start horizontal and end as a long scroll;
  // the final dy must veto the archive.
  assert.equal(shouldArchiveOnSwipe(100, 120), false);
});

test('swipeGestureIntent makes vertical ownership explicit after slop', () => {
  assert.equal(swipeGestureIntent(10, 3), null);
  assert.equal(swipeGestureIntent(20, 3), 'horizontal');
  assert.equal(swipeGestureIntent(80, 120), 'vertical');
  assert.equal(swipeGestureIntent(20, 20), 'vertical');
});

test('channelColor is deterministic and stable across calls', () => {
  const a = channelColor('benchmarks');
  const b = channelColor('benchmarks');
  assert.equal(a, b);
  assert.ok(INBOX_ACCENTS.includes(a), `got ${a}`);
  // Case-insensitive: same channel in different case maps to the same token.
  assert.equal(channelColor('STOCKS'), channelColor('stocks'));
  // Distinct channels usually differ (collisions possible but unlikely here).
  const set = new Set(['benchmarks', 'stocks', 'projects', 'system', 'stocks2', 'geo', 'meta'].map(channelColor));
  assert.ok(set.size >= 4, `expected spread, got ${[...set].join(',')}`);
});

test('channelColor handles empty/weird input without throwing', () => {
  assert.ok(INBOX_ACCENTS.includes(channelColor('')));
  assert.ok(INBOX_ACCENTS.includes(channelColor(null)));
  assert.ok(INBOX_ACCENTS.includes(channelColor()));
});

test('filterByView inbox/archive/starred', () => {
  const unread = rawItem({ id: 'a' });
  const read = rawItem({ id: 'b', read_at: '2026-08-17T01:00:00+00:00' });
  const starredUnread = rawItem({ id: 'c', starred: true });
  const starredRead = rawItem({ id: 'd', starred: true, read_at: '2026-08-17T01:00:00+00:00' });
  const items = [unread, read, starredUnread, starredRead];

  assert.deepEqual(filterByView(items, 'inbox').map((i) => i.id), ['a', 'c']);
  assert.deepEqual(filterByView(items, 'archive').map((i) => i.id), ['b', 'd']);
  assert.deepEqual(filterByView(items, 'starred').map((i) => i.id), ['c', 'd']);
  // unknown view returns everything unchanged
  assert.equal(filterByView(items, 'bogus').length, 4);
});

test('view query contract includes archived and read-starred API rows', () => {
  const rows = [
    rawItem({ id: 'unread' }),
    rawItem({ id: 'archived', read_at: '2026-08-17T01:00:00+00:00' }),
    rawItem({ id: 'unread-star', starred: true }),
    rawItem({ id: 'read-star', starred: true, read_at: '2026-08-17T01:00:00+00:00' }),
  ];
  // Mirrors the daemon contract: unread_only defaults true unless the client
  // explicitly sends false; starred narrows after that default is applied.
  const daemonList = (query) => rows.filter((item) => {
    if (query.unread_only !== false && !isUnread(item)) return false;
    if (query.starred && !item.starred) return false;
    return true;
  });
  const visible = (view) => filterByView(daemonList(inboxQueryForView(view)), view).map((item) => item.id);
  assert.deepEqual(inboxQueryForView('inbox'), { unread_only: true });
  assert.deepEqual(inboxQueryForView('archive'), { unread_only: false });
  assert.deepEqual(inboxQueryForView('starred'), { unread_only: false, starred: true });
  assert.deepEqual(visible('inbox'), ['unread', 'unread-star']);
  assert.deepEqual(visible('archive'), ['archived', 'read-star']);
  assert.deepEqual(visible('starred'), ['unread-star', 'read-star']);
});

test('filterItems narrows by channel + search (case-insensitive)', () => {
  const items = [
    rawItem({ id: 'a', channel: 'stocks', title: 'NVDA crossed 1300', body: 'Up 4.2%', source_label: 'stock-alerts' }),
    rawItem({ id: 'b', channel: 'projects', title: 'Growth done', body: '3 articles shipped', source_label: 'Geo' }),
  ];
  assert.deepEqual(filterItems(items, { channel: 'stocks' }).map((i) => i.id), ['a']);
  assert.deepEqual(filterItems(items, { channel: 'projects' }).map((i) => i.id), ['b']);
  assert.deepEqual(filterItems(items, { search: 'nvda' }).map((i) => i.id), ['a']);
  assert.deepEqual(filterItems(items, { search: 'ARTICLES' }).map((i) => i.id), ['b']);
  assert.deepEqual(filterItems(items, { search: 'geo' }).map((i) => i.id), ['b']); // source_label
  assert.deepEqual(filterItems(items, { channel: 'stocks', search: 'articles' }).map((i) => i.id), []);
  assert.equal(filterItems(null).length, 0);
});

test('collapseThreadsForSearch expands matched threads; empty term collapses normally', () => {
  const thread = [1, 2, 3].map((n) => rawItem({
    id: `m${n}`, thread_key: 'acme-campaigns',
    title: `routine ${n}`, body: n === 2 ? 'an ACME CORP update' : 'plain',
    created_at: `2026-09-0${n}T10:00:00+00:00`,
  }));
  const plain = rawItem({ id: 'p1', thread_key: null, title: 'Acme Corp: one-off' });
  const list = [...thread, plain];
  // normal: the thread collapses to one row
  assert.equal(collapseThreadsForSearch(list, '').length, 2);
  // search: both matching members (m2 via body, p1 via title) get rows
  const rows = collapseThreadsForSearch(list, 'acme corp');
  assert.deepEqual(rows.map((r) => r.id), ['m2', 'p1']); // newest-first
  assert.equal(rows.find((r) => r.id === 'm2').tcount, 3); // members: all real
  // a term matching nothing collapses normally
  assert.equal(collapseThreadsForSearch(list, 'zzz').length, 2);
});

test('mergeOlderItems appends an older page without duplicates or reorder', () => {
  const page1 = [rawItem({ id: 'a' }), rawItem({ id: 'b' })];
  const page2 = [rawItem({ id: 'b' }), rawItem({ id: 'c' })];
  assert.deepEqual(mergeOlderItems(page1, page2).map((i) => i.id), ['a', 'b', 'c']);
  // existing copies win (fresher state); overlap-only page is a no-op
  const starred = [{ ...page1[0], starred: 1 }];
  const res = mergeOlderItems(starred, [rawItem({ id: 'a' })]);
  assert.equal(res.length, 1);
  assert.equal(res[0].starred, 1);
  assert.equal(mergeOlderItems(starred, []), starred);
  assert.deepEqual(mergeOlderItems(null, page2).map((i) => i.id), ['b', 'c']);
});

test('latestRenderedCursor uses serialized composite cursor and numeric tie-break', () => {
  const at = '2026-08-17T10:00:00.123456+00:00';
  const rows = [
    rawItem({ id: 'seq-9', created_at: at, cursor: `${at}|9` }),
    rawItem({ id: 'seq-10', created_at: at, cursor: `${at}|10` }),
  ];
  assert.equal(latestRenderedCursor(rows), `${at}|10`);
  const legacy = rawItem({ id: 'legacy', created_at: '2026-08-17T12:00:00+00:00' });
  assert.equal(latestRenderedCursor([legacy]), legacy.created_at);
  assert.equal(latestRenderedCursor([]), null);
});

test('renderedReadAllTargets expands only matching rendered rows/threads', () => {
  const matchingSolo = rawItem({ id: 'solo-match', title: 'Urgent standalone' });
  const hiddenSolo = rawItem({ id: 'solo-hidden', title: 'Routine standalone' });
  const matchingThread = rawItem({ id: 'thread-match', thread_key: 't1', title: 'Urgent kickoff' });
  const sameThread = rawItem({ id: 'thread-routine', thread_key: 't1', title: 'Routine update' });
  const otherThread = rawItem({ id: 'other-thread', thread_key: 't2', title: 'Routine other' });
  const readMatch = rawItem({ id: 'read-match', title: 'Urgent but archived', read_at: '2026-08-17T01:00:00+00:00' });
  const targets = renderedReadAllTargets(
    [matchingSolo, hiddenSolo, matchingThread, sameThread, otherThread, readMatch],
    { search: 'urgent' },
  );
  assert.deepEqual(targets.map((item) => item.id).sort(), ['solo-match', 'thread-match', 'thread-routine']);
});

test('renderedReadAllTargets cannot include a capped-off row absent from the fetched page', () => {
  const fetched = Array.from({ length: 200 }, (_, index) => rawItem({ id: `visible-${index}` }));
  const hidden = rawItem({ id: 'hidden-201' });
  const targets = renderedReadAllTargets(fetched);
  assert.equal(targets.length, 200);
  assert.equal(targets.some((item) => item.id === hidden.id), false);
});

test('flaggedResponseItems returns every flagged member of the selected thread', () => {
  const flagged = rawItem({ id: 'old', thread_key: 't', needs_response: true });
  const newest = rawItem({ id: 'new', thread_key: 't', needs_response: false });
  const other = rawItem({ id: 'other', thread_key: 'else', needs_response: true });
  assert.deepEqual(flaggedResponseItems([flagged, newest, other], newest).map((item) => item.id), ['old']);
  assert.deepEqual(flaggedResponseItems([], rawItem({ needs_response: true })).map((item) => item.id), ['x']);
});

test('detailThreadMembers retains flagged snapshot after unread refetch drops members', () => {
  const oldFlagged = rawItem({
    id: 'old', thread_key: 't', needs_response: true, created_at: '2026-08-17T09:00:00+00:00',
  });
  const newest = rawItem({
    id: 'new', thread_key: 't', needs_response: false, created_at: '2026-08-17T10:00:00+00:00',
  });
  const snapshot = detailThreadMembers([], [oldFlagged, newest], newest);
  const afterUnreadRefetch = detailThreadMembers(snapshot, [], newest);
  assert.deepEqual(afterUnreadRefetch.map((item) => item.id), ['new', 'old']);
  assert.equal(threadNeedsResponse(afterUnreadRefetch, newest), true);
  assert.deepEqual(flaggedResponseItems(afterUnreadRefetch, newest).map((item) => item.id), ['old']);
});

test('muteUntilIso converts select strings and makes today local end-of-day', () => {
  const now = new Date('2026-08-18T10:15:00.000Z').getTime();
  assert.equal(muteUntilIso('', now), null);
  assert.equal(muteUntilIso('3600000', now), new Date(now + 3600000).toISOString());
  assert.equal(muteUntilIso('604800000', now), new Date(now + 604800000).toISOString());
  const expectedToday = new Date(now);
  expectedToday.setHours(23, 59, 59, 999);
  assert.equal(muteUntilIso('86400000', now), expectedToday.toISOString());
});

test('isMuteUntilActive detects active future mute timestamps only', () => {
  const now = Date.parse('2026-08-18T10:15:00.000Z');
  assert.equal(isMuteUntilActive(null, now), false);
  assert.equal(isMuteUntilActive('', now), false);
  assert.equal(isMuteUntilActive('not-a-date', now), false);
  assert.equal(isMuteUntilActive('2026-08-18T10:14:59.000Z', now), false);
  assert.equal(isMuteUntilActive('2026-08-18T10:15:00.000Z', now), false);
  assert.equal(isMuteUntilActive('2026-08-18T11:15:00.000Z', now), true);
  assert.equal(isMuteUntilActive('2026-08-18T11:15:00+0000', now), true);
});

test('collapseThreads groups by thread_key and flags tcount', () => {
  const a1 = rawItem({ id: 'a1', thread_key: 'bench', title: 'kickoff', created_at: '2026-08-17T10:00:00+00:00' });
  const a2 = rawItem({ id: 'a2', thread_key: 'bench', title: '60%', created_at: '2026-08-17T11:00:00+00:00' });
  const a3 = rawItem({ id: 'a3', thread_key: 'bench', title: 'done', created_at: '2026-08-17T12:00:00+00:00' });
  const solo = rawItem({ id: 's', thread_key: null, title: 'solo' });
  const rows = collapseThreads([a1, a2, a3, solo]);

  assert.equal(rows.length, 2); // one thread row + one standalone
  const threadRow = rows.find((r) => r.thread_key === 'bench');
  assert.equal(threadRow.tcount, 3);
  assert.equal(threadRow.id, 'a3'); // newest represents the thread
  const soloRow = rows.find((r) => r.id === 's');
  assert.equal(soloRow.tcount, undefined); // standalone rows carry no thread count
  assert.equal(collapseThreads([]).length, 0);
  assert.equal(collapseThreads(null).length, 0);
});

test('collapseThreads handles empty-string thread_key as standalone', () => {
  const withEmpty = rawItem({ id: 'e', thread_key: '' });
  const withReal = rawItem({ id: 'r', thread_key: 'geo' });
  const rows = collapseThreads([withEmpty, withReal]);
  assert.equal(rows.find((r) => r.id === 'e').tcount, undefined);
  assert.equal(rows.find((r) => r.id === 'r').tcount, 1);
});

test('collapseThreads sorts starred first, then newest, across standalone and threads', () => {
  const oldStar = rawItem({ id: 'old-star', starred: true, created_at: '2026-08-17T08:00:00+00:00' });
  const newestSolo = rawItem({ id: 'newest-solo', created_at: '2026-08-17T12:00:00+00:00' });
  const threadOld = rawItem({ id: 'thread-old', thread_key: 't', created_at: '2026-08-17T09:00:00+00:00' });
  const threadNew = rawItem({ id: 'thread-new', thread_key: 't', created_at: '2026-08-17T11:00:00+00:00' });
  const tiedB = rawItem({ id: 'b', created_at: '2026-08-17T10:00:00+00:00' });
  const tiedA = rawItem({ id: 'a', created_at: '2026-08-17T10:00:00+00:00' });
  assert.deepEqual(
    collapseThreads([threadOld, tiedB, oldStar, newestSolo, tiedA, threadNew]).map((row) => row.id),
    ['old-star', 'newest-solo', 'thread-new', 'a', 'b'],
  );
});

test('unreadTotal + channelCounts read the unread-count map', () => {
  const uc = { total: 7, channels: { stocks: 2, system: 3, '': 4 }, threads: { '': 4, bench: 2 } };
  assert.equal(unreadTotal(uc), 7);
  assert.equal(unreadTotal(null), 0);
  assert.deepEqual(channelCounts(uc), [
    { channel: 'stocks', count: 2 },
    { channel: 'system', count: 3 },
  ]);
  const real = channelCounts({ channels: { projects: 5, alpha: 1 } });
  assert.deepEqual(real, [
    { channel: 'alpha', count: 1 },
    { channel: 'projects', count: 5 },
  ]);
});

test('channelsFromItems lists unique sorted channels', () => {
  const items = [
    rawItem({ channel: 'stocks' }),
    rawItem({ channel: 'projects' }),
    rawItem({ channel: 'stocks' }),
    rawItem({ channel: 'system' }),
  ];
  assert.deepEqual(channelsFromItems(items), ['projects', 'stocks', 'system']);
  assert.deepEqual(channelsFromItems([]), []);
});

test('previewText strips tags and caps length', () => {
  const long = 'one two three four five six seven eight nine ten eleven twelve thirteen fourteen fifteen sixteen seventeen eighteen nineteen twenty twenty one fourscore and seven years ago'.repeat(1);
  assert.equal(previewText('<p>Hello <b>world</b></p>'), 'Hello world');
  assert.ok(previewText(long).length <= 93);
  assert.ok(previewText(long).endsWith('…'));
  assert.equal(previewText(''), '');
});

test('threadNeedsResponse reflects any member needing response', () => {
  const all = [
    rawItem({ id: 'a1', thread_key: 'bench', needs_response: false }),
    rawItem({ id: 'a2', thread_key: 'bench', needs_response: true }),
  ];
  assert.equal(threadNeedsResponse(all, all[0]), true);
  assert.equal(threadNeedsResponse(all, rawItem({ thread_key: 'bench' })), true);
  assert.equal(threadNeedsResponse(all, rawItem({ thread_key: 'other' })), false);
  assert.equal(threadNeedsResponse(all, rawItem({ thread_key: null, needs_response: true })), true);
});

test('visibleUnreadTotal never shows more than the list can show (badge bug 2026-09-13)', () => {
  // List page smaller than the total: badge shows the fetched rows, not the
  // unreachable total.
  assert.equal(visibleUnreadTotal({ total: 279 }, 200), 200);
  // The reported symptom: count nonzero, list empty -> badge must be 0.
  assert.equal(visibleUnreadTotal({ total: 24 }, 0), 0);
  // Normal case: everything fits in the page.
  assert.equal(visibleUnreadTotal({ total: 24 }, 24), 24);
  // Missing inputs degrade to 0, never to the stale total.
  assert.equal(visibleUnreadTotal(null, null), 0);
  assert.equal(visibleUnreadTotal(undefined, undefined), 0);
});

test('conversation snapshot keeps sent replies newest-first across inbox filters', () => {
  const first = { id: 'first', thread_key: 'mail', source_kind: 'pulse', created_at: '2026-09-16T12:00:00.000000Z' };
  const sent = { id: 'sent', thread_key: 'mail', source_kind: 'user', read_at: 'sent', created_at: '2026-09-16T12:00:01.000000Z' };
  const last = { id: 'last', thread_key: 'mail', source_kind: 'pulse', created_at: '2026-09-16T12:00:02.000000Z' };
  assert.deepEqual(detailThreadMembers([first, sent, last], [last], last), [last, sent, first]);
  assert.deepEqual(detailThreadMembers([first, sent, last], [], last), [last, sent, first]);
  assert.equal(isUnread(sent), false);
  assert.equal(isReplyable(sent), false);
});
