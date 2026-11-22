<script>
  import { tick } from 'svelte';
  import {
    listInbox,
    searchInbox,
    getInboxUnreadCount,
    listInboxChannels,
    patchInboxChannel,
    markInboxRead,
    markInboxUnread,
    starInboxItem,
    unstarInboxItem,
    dismissInboxResponse,
    scoreInboxItem,
    replyInbox,
    getInboxConversation,
  } from './api.js';
  import { pollWhileVisible } from './pollvisible.js';
  import { renderMarkdown } from './markdown.js';
  import { htmlCards, loadHtmlCards, cardify } from './htmlcards.svelte.js';
  import { parseTs, fmtMailTime } from './time.js';
  import {
    channelColor,
    filterByView,
    inboxQueryForView,
    filterItems,
    mergeOlderItems,
    collapseThreads,
    collapseThreadsForSearch,
    isUnread,
    isReplyable,
    reconcileSelectedItem,
    shouldArchiveOnSwipe,
    swipeGestureIntent,
    renderedReadAllTargets,
    flaggedResponseItems,
    detailThreadMembers,
    muteUntilIso,
    isMuteUntilActive,
    unreadTotal,
    channelCounts,
    previewText,
    threadNeedsResponse,
  } from './inboxlist.js';
  import Icon from './Icon.svelte';

  /**
   * Inbox surface.
   * Email-style triage rail: Inbox/Archive/Starred views, channel chips with
   * unread counts + mute glyphs, thread collapse (⛓ count), search, and a
   * detail pane with the thread conversation, reply composer (pulse-sourced
   * only), needs-response banner, and actions. Opening an item marks it read.
   *
   * Pure list/filter logic lives in inboxlist.js (node-tested); this file is
   * DOM + fetch only.
   *
   * @type {{ onopenpulse?: (pulseId: string) => void, initialItemId?: string|null }}
   */
  let { onopenpulse = () => {}, initialItemId = null } = $props();

  let view = $state('inbox'); // 'inbox' | 'archive' | 'starred'
  let channelFilter = $state(null);
  let search = $state('');
  let items = $state([]); // fetched per view
  let unread = $state({ total: 0, channels: {}, threads: {} });
  let channels = $state([]); // channel settings rows
  // Keep the selected object independent of the current list. Opening an
  // unread item archives it, so a subsequent Inbox refresh no longer returns
  // that row; the detail must remain valid while the user reads it.
  let selectedItem = $state(null);
  let threadSnapshot = $state([]);
  let conversationError = $state('');
  let _conversationEpoch = 0;
  let error = $state('');
  let chansError = $state('');
  let showChans = $state(false);
  let sending = $state(false);
  let draft = $state('');
  let conversationBody = $state(null);
  let now = $state(Date.now());
  // Non-reactive request sequence. A newer fetch (including a view switch)
  // invalidates every older response before it can overwrite the screen.
  let _itemsEpoch = 0;
  // Server-backed whole-archive search: the search box queries /inbox/search
  // over ALL items instead of filtering the loaded view page.
  let searchRunning = $state(false);
  let searchTotal = $state(0);
  let searchCapped = $state(false);
  const SEARCH_PAGE = 100;
  // Infinite scroll: the daemon page is capped at PAGE_SIZE and the archive is
  // far bigger, so the Archive view pages with `before` cursors as the list
  // scrolls. `olderCursor` is the cursor to page from (null ⇒ seen
  // the end); loadingOlder guards concurrent pages; _sentinelAtBottom makes the
  // IntersectionObserver re-fire for the next page once its rows are appended.
  const PAGE_SIZE = 200;
  let olderCursor = $state(null);
  let atEnd = $state(true);
  let loadingOlder = $state(false);
  let _sentinel = null;
  let _sentinelEl = null;
  let _sentinelAtBottom = false;

  // Slow-cadence polls: unread-count (badge + chips), the rendered item list,
  // and channel settings. Item refreshes share refreshItems' epoch guard.
  $effect(() => {
    loadHtmlCards();
    const a = pollWhileVisible(refreshUnread, 15000);
    const b = pollWhileVisible(refreshItems, 15000, { immediate: false });
    const c = pollWhileVisible(refreshChannels, 30000);
    return () => { a(); b(); c(); };
  });

  async function refreshUnread() {
    const r = await getInboxUnreadCount();
    if (r.ok) {
      unread = r.value ?? { total: 0, channels: {}, threads: {} };
      now = Date.now();
    }
    // non-fatal: chips just stay stale; nav badge owns its own errors
  }

  async function refreshChannels() {
    const r = await listInboxChannels();
    if (r.ok) channels = r.value ?? [];
    else chansError = `channels: ${r.error}`;
  }

  // Fetch the item set for the current view. The daemon /inbox is capped at
  // ~200; all view/filter/thread logic (incl. archive narrowing) runs client
  // side in inboxlist.js so it is node-testable.
  async function refreshItems() {
    const epoch = ++_itemsEpoch;
    const requestView = view;
    const requestTerm = search.trim();
    if (requestTerm) {
      // Whole-archive search: the server matches EVERY item, not just the view
      // page; results span both states and ignore the view tab. Channel chips
      // still narrow the result set client-side.
      searchRunning = true;
      const r = await searchInbox(requestTerm);
      searchRunning = false;
      if (epoch !== _itemsEpoch || requestTerm !== search.trim()) return false;
      if (r.ok) {
        items = r.value ?? [];
        selectedItem = reconcileSelectedItem(selectedItem, items);
        searchTotal = items.length;
        searchCapped = items.length >= SEARCH_PAGE;
        olderCursor = null;
        atEnd = true;
        error = '';
      } else {
        error = `search: ${r.error}`;
        console.log('[inbox] search failed', requestTerm, r.error);
      }
      return r.ok;
    }
    const r = await listInbox(inboxQueryForView(requestView));
    if (epoch !== _itemsEpoch || requestView !== view) return false;
    if (r.ok) {
      const nextItems = filterByView(r.value ?? [], requestView);
      selectedItem = reconcileSelectedItem(selectedItem, nextItems);
      items = nextItems;
      error = '';
      // A full page means there is almost certainly more below the cut;
      // anything less and we have already seen the end of the view.
      olderCursor = (r.value ?? []).length >= PAGE_SIZE ? lastCursor(r.value) : null;
      atEnd = olderCursor === null;
      // A live poll replaced the list; re-anchor the sentinel to its bottom.
      _sentinelAtBottom = false;
    } else {
      error = `inbox: ${r.error}`;
      console.log('[inbox] fetch failed', requestView, r.error);
    }
    return r.ok;
  }

  let deepLinkFallbackId = null;
  $effect(() => {
    const targetId = initialItemId;
    refreshItems().then(async (ok) => {
      // Deep link to an item older than the first page (or already read in
      // Archive): page until it shows up; if still missing from unread Inbox,
      // fall back once to Archive.
      if (!ok || search.trim() || !targetId || handledDeepLinkId === targetId) return;
      if (!items.some((i) => i.id === targetId)) {
        await loadOlderUntil(targetId);
      }
      if (
        view === 'inbox'
        && targetId === initialItemId
        && handledDeepLinkId !== targetId
        && deepLinkFallbackId !== targetId
        && !items.some((i) => i.id === targetId)
      ) {
        deepLinkFallbackId = targetId;
        setView('archive');
      }
    });
  });
  // Search box: debounce re-fetches (server-backed whole-archive search —
  // every firing is a request, so waiting 250ms after the last keystroke is
  // load-bearing, not cosmetic).
  let _searchTimer = 0;
  $effect(() => {
    if (typeof window === 'undefined') return;
    search; // re-arm on every change
    window.clearTimeout(_searchTimer);
    _searchTimer = window.setTimeout(() => refreshItems(), 250);
    return () => window.clearTimeout(_searchTimer);
  });
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 30000);
    return () => clearInterval(id);
  });

  // Derived list through the pure helpers: channel + search filters, then
  // thread collapse with the ⛓ count.
  const filtered = $derived.by(() => {
    // Under a search term, matching threads EXPAND to their matching members
    // (collapseThreadsForSearch) so the row count equals the hit count;
    // chips still narrow first.
    const narrowed = search.trim()
      ? filterItems(items, { channel: channelFilter }) // server matched the text
      : filterItems(items, { channel: channelFilter, search });
    return collapseThreadsForSearch(narrowed, search.trim());
  });

  const chips = $derived.by(() => {
    // Union of channels present in the fetched list and in the unread map so
    // a channel chip never flickers between polls.
    const fromUnread = channelCounts(unread).map((c) => c.channel);
    const fromItems = [...new Set(items.map((i) => i.channel).filter(Boolean))];
    const names = [...new Set([...fromUnread, ...fromItems])].sort();
    return names.map((name) => ({
      name,
      count: unread.channels?.[name] ?? 0,
      colorVar: channelColor(name),
    }));
  });

  // The settings modal must list EVERY channel, not just the ones visible in
  // the current view — an empty Inbox tab otherwise renders "no channels yet"
  // over a fully populated /inbox/channels.
  const modalChans = $derived.by(() => {
    const names = [...new Set([...channels.map((c) => c.channel), ...chips.map((c) => c.name)])].sort();
    return names.map((name) => ({
      name,
      count: unread.channels?.[name] ?? 0,
      colorVar: channelColor(name),
    }));
  });

  // Thread conversation for the selected item: all members of its thread (or
  // just the item itself when standalone), newest-first for reading.
  const thread = $derived.by(() => {
    return detailThreadMembers(threadSnapshot, items, selectedItem);
  });

  // The mail list can omit read/older members. Fetch the conversation on its
  // own and poll it while open, including when the selected row leaves Inbox.
  const selectedId = $derived(selectedItem?.id);
  $effect(() => {
    const id = selectedId;
    if (!id) return;
    const stop = pollWhileVisible(() => refreshConversation(id), 15000);
    return () => { stop(); ++_conversationEpoch; };
  });

  async function refreshConversation(id = selectedItem?.id) {
    if (!id) return;
    const epoch = ++_conversationEpoch;
    const r = await getInboxConversation(id);
    if (epoch !== _conversationEpoch || selectedItem?.id !== id) return;
    if (r.ok) {
      threadSnapshot = r.value ?? [];
      const selected = threadSnapshot.find((member) => member.id === id);
      if (selected) selectedItem = selected;
      conversationError = '';
    } else {
      conversationError = `Conversation: ${r.error}`;
    }
  }

  const needsResponse = $derived(selectedItem ? threadNeedsResponse(thread, selectedItem) : false);
  const replyable = $derived(selectedItem ? isReplyable(selectedItem) : false);

  function adoptSelectedResult(r) {
    if (!r.ok || !r.value?.id) return;
    if (selectedItem?.id === r.value.id) selectedItem = r.value;
    if (threadSnapshot.some((member) => member.id === r.value.id)) {
      threadSnapshot = threadSnapshot.map(
        (member) => member.id === r.value.id ? r.value : member,
      );
    }
  }

  function openItem(id) {
    const target = items.find((i) => i.id === id);
    if (!target) return;
    ++_conversationEpoch;
    conversationError = '';
    selectedItem = target;
    threadSnapshot = detailThreadMembers([], items, target);
    // Opening marks ONLY the tapped row read rather than sweeping every unread
    // item of the thread, so opening one update does not quietly archive
    // unread siblings.
    if (isUnread(target)) archiveItems([target], 'read');
  }

  function backToList() {
    selectedItem = null;
    threadSnapshot = [];
  }

  // Swipe-to-archive: a horizontal swipe on an unread row — either direction —
  // archives it (and its unread thread members) without opening. Inbox view
  // only; Archive/Starred rows don't move. touch-action:
  // pan-y on .row keeps vertical list scrolling native while we own X.
  let swipeId = $state(null);   // row currently tracking a horizontal drag
  let swipeDx = $state(0);      // its live horizontal offset (px)
  let swipeDy = $state(0);      // final vertical travel (px), used by the guard
  let swipeGone = $state(null); // row animating out after a committed swipe
  let _swipeStart = null;       // transient gesture origin — not reactive

  async function archiveRow(row) {
    const affected = row.thread_key
      ? items.filter((i) => i.thread_key === row.thread_key && isUnread(i))
      : [row].filter(isUnread);
    await archiveItems(affected, 'archive');
  }

  async function archiveItems(affected, action = 'archive') {
    const results = await Promise.all(affected.map((item) => markInboxRead(item.id)));
    results.forEach(adoptSelectedResult);
    const failed = results.find((result) => !result.ok);
    if (failed) {
      error = `${action}: ${failed.error}`;
      console.log('[inbox] archive failed', failed.error);
    }
    await refreshAfterMutation();
    return !failed;
  }

  function swipeBegin(e, row) {
    if (view !== 'inbox' || swipeGone) return;
    const t = e.touches[0];
    _swipeStart = { x: t.clientX, y: t.clientY, id: row.id };
  }

  function swipeMove(e, row) {
    if (!_swipeStart || _swipeStart.id !== row.id) return;
    const t = e.touches[0];
    const dx = t.clientX - _swipeStart.x;
    const dy = t.clientY - _swipeStart.y;
    swipeDx = dx;
    swipeDy = dy;
    if (_swipeStart.cancelled) return;
    const intent = swipeGestureIntent(dx, dy);
    if (intent === 'vertical') {
      // Once native scrolling wins, this touch can never reclaim the row.
      _swipeStart.cancelled = true;
      swipeId = null;
      swipeDx = 0;
      return;
    }
    if (swipeId !== row.id) {
      // Don't claim the gesture until it reads as horizontal — otherwise a
      // sloppy scroll would drag rows sideways.
      if (intent === 'horizontal') swipeId = row.id;
      return;
    }
  }

  function swipeEnd(row) {
    if (!_swipeStart || _swipeStart.id !== row.id) return;
    const commit = swipeId === row.id && shouldArchiveOnSwipe(swipeDx, swipeDy);
    if (commit) {
      // Slide fully out in the swipe direction, archive, then let the
      // refetch drop the row.
      swipeGone = row.id;
      swipeDx = swipeDx > 0 ? 600 : -600;
      archiveRow(row);
      setTimeout(() => { swipeGone = null; swipeId = null; swipeDx = 0; swipeDy = 0; }, 250);
    } else {
      swipeId = null;
      swipeDx = 0;
      swipeDy = 0;
    }
    _swipeStart = null;
  }

  async function refreshAfterMutation() {
    await refreshItems();
    await refreshUnread();
  }

  // Deep link: when the surface is forced via /?surface=inbox&item=<id>, open
  // that item as soon as its row lands in the fetched set.
  let handledDeepLinkId = null;
  $effect(() => {
    if (!initialItemId || handledDeepLinkId === initialItemId || items.length === 0) return;
    if (items.some((i) => i.id === initialItemId)) {
      openItem(initialItemId);
      handledDeepLinkId = initialItemId;
    }
  });

  async function toggleStar(item) {
    const r = item.starred ? await unstarInboxItem(item.id) : await starInboxItem(item.id);
    if (!r.ok) { error = `star: ${r.error}`; console.log('[inbox] star failed', item.id, r.error); }
    else adoptSelectedResult(r);
    await refreshAfterMutation();
  }

  async function toggleRead(item) {
    // Opposite of current state: unread → mark read; read → mark unread (back to Inbox).
    const r = isUnread(item) ? await markInboxRead(item.id) : await markInboxUnread(item.id);
    if (!r.ok) { error = `read: ${r.error}`; console.log('[inbox] read failed', item.id, r.error); }
    else adoptSelectedResult(r);
    await refreshAfterMutation();
  }

  // Score 1/2/3: teach the daemon how to treat this KIND of message. Clicking
  // the active score again unlearns it. Score 1 archives every unread item of
  // the kind, so the list refresh may drop rows.
  const SCORES = [[1, '1 Hide', 'Never show this kind in the inbox'],
                  [2, '2 OK', 'Inbox only, no notification'],
                  [3, '3 Notify', 'Notification + inbox']];
  async function scoreItem(item, score) {
    const next = item.score === score ? null : score;
    const r = await scoreInboxItem(item.id, next);
    if (!r.ok) { error = `score: ${r.error}`; console.log('[inbox] score failed', item.id, r.error); }
    else {
      adoptSelectedResult({ ok: true, value: r.value.item });
      console.log('[inbox] scored', item.id, next, 'kind', r.value.item.kind_key, 'affected', r.value.affected);
    }
    await refreshAfterMutation();
  }

  async function dismissResponse(item) {
    const flagged = flaggedResponseItems(thread, item);
    const results = await Promise.all(flagged.map((member) => dismissInboxResponse(member.id)));
    results.forEach(adoptSelectedResult);
    const failed = results.find((result) => !result.ok);
    if (failed) { error = `dismiss: ${failed.error}`; console.log('[inbox] dismiss failed', failed.error); }
    await refreshAfterMutation();
  }

  async function sendReply() {
    if (sending || !selectedItem || !replyable) return;
    const text = draft.trim();
    if (!text) return;
    const target = selectedItem;
    const flagged = flaggedResponseItems(thread, target);
    sending = true;
    try {
      const r = await replyInbox(target.id, text);
      if (!r.ok) {
        error = `reply: ${r.error}`;
        console.log('[inbox] reply failed', target.id, r.error);
        return;
      }
      draft = '';
      adoptSelectedResult(r);
      await refreshConversation(target.id);
      await tick();
      if (selectedItem?.id === target.id && conversationBody) {
        conversationBody.scrollTop = 0;
      }
      // Reply clears the target. Clear any older flagged members represented
      // by this thread banner too, otherwise the blocker immediately returns.
      const remaining = flagged.filter((member) => member.id !== target.id);
      const cleared = await Promise.all(remaining.map((member) => dismissInboxResponse(member.id)));
      cleared.forEach(adoptSelectedResult);
      const failed = cleared.find((result) => !result.ok);
      if (failed) {
        error = `reply cleanup: ${failed.error}`;
        console.log('[inbox] reply cleanup failed', failed.error);
      }
      await refreshAfterMutation();
    } finally {
      sending = false;
    }
  }

  async function readAllView() {
    // Explicit per-item mutations are the only safe boundary: the daemon's
    // list is capped, so an open-ended cutoff could archive an older row the
    // user never saw. Thread rows expand only to members present in this page.
    const affected = renderedReadAllTargets(items, { channel: channelFilter, search });
    await archiveItems(affected, 'read-all');
  }

  function setView(v) {
    if (view === v) return;
    _itemsEpoch += 1;
    view = v;
    selectedItem = null;
    threadSnapshot = [];
    olderCursor = null;
    atEnd = true;
  }

  // Newest-first composite cursor of a raw API page (its last row).
  function lastCursor(list) {
    const row = (list ?? []).at(-1);
    return row ? (row.cursor || row.created_at) : null;
  }

  // Append one older page ("load older" / scroll sentinel).
  async function loadOlder() {
    if (!olderCursor || loadingOlder) return false;
    loadingOlder = true;
    const epoch = _itemsEpoch;
    const requestView = view;
    const r = await listInbox({
      ...inboxQueryForView(requestView),
      before: olderCursor,
    });
    loadingOlder = false;
    if (epoch !== _itemsEpoch || requestView !== view) return false;
    if (!r.ok) {
      error = `older: ${r.error}`;
      return false;
    }
    const page = r.value ?? [];
    const nextItems = mergeOlderItems(items, filterByView(page, requestView));
    selectedItem = reconcileSelectedItem(selectedItem, nextItems);
    items = nextItems;
    olderCursor = page.length >= PAGE_SIZE ? lastCursor(page) : null;
    atEnd = olderCursor === null;
    _sentinelAtBottom = false; // re-anchor the sentinel to the new bottom
    return true;
  }

  // Deep-link helper: page until the target item appears or the view runs out.
  async function loadOlderUntil(id) {
    while (!items.some((i) => i.id === id)) {
      if (!olderCursor || loadingOlder) return;
      if (!(await loadOlder())) return;
    }
  }

  // Sentinel element callback: fires when the little footer row scrolls into
  // view; rootMargin pre-loads a page before the user reaches the bottom.
  function sentinel(node) {
    _sentinel?.disconnect();
    _sentinel = null;
    if (!node || typeof IntersectionObserver === 'undefined') return;
    _sentinel = new IntersectionObserver(
      (entries) => {
        if (entries.some((e) => e.isIntersecting)) loadOlder();
      },
      { root: node.closest('.rows'), rootMargin: '0px 0px 320px 0px' },
    );
    _sentinel.observe(node);
  }

  // After an appended page, scroll the sentinel back to the bottom of the
  // list so it can trigger the next page (otherwise it stays in view and
  // silently stops paging).
  function sentinelScrollAnchor(node) {
    if (_sentinelAtBottom || !node || items.length === 0) return;
    _sentinelAtBottom = true;
    node.closest('.rows')?.scrollTo?.({ top: node.offsetTop });
  }

  function openPulse(item) {
    if (item.source_pulse_id) onopenpulse?.(item.source_pulse_id);
  }

  // ---- channel settings -------------------------------------------------- //
  function modeOf(name) {
    return channels.find((c) => c.channel === name)?.mode ?? 'notify';
  }
  function isMuted(name) {
    const c = channels.find((x) => x.channel === name);
    if (!c) return false;
    if (c.mute_until) {
      const until = parseTs(c.mute_until);
      if (Number.isFinite(until) && until > Date.now()) return true;
    }
    return c.mode === 'muted';
  }
  async function setMode(name, mode) {
    const r = await patchInboxChannel(name, { mode });
    if (!r.ok) { chansError = `channel ${name}: ${r.error}`; console.log('[inbox] channel patch failed', name, r.error); }
    await refreshChannels();
  }
  async function setMuteUntil(name, value) {
    if (value === 'active') return;
    const mute_until = muteUntilIso(value);
    const r = await patchInboxChannel(name, { mute_until });
    if (!r.ok) { chansError = `channel ${name}: ${r.error}`; console.log('[inbox] mute patch failed', name, r.error); }
    now = Date.now();
    await refreshChannels();
  }

  // ---- formatting (pure-ish; keeps the mock's compact times) ------------- //
  // Active time zone (owner.timezone or browser); see fmtMailTime in time.js.
  const relTime = (ts) => fmtMailTime(ts, now);

  function chipMuteGlyph(name) {
    if (isMuted(name)) return '🔇';
    if (modeOf(name) === 'quiet') return '◌';
    return '';
  }

  function threadTitle(item) {
    const key = item.thread_key;
    if (!key) return item.title ?? '';
    // The oldest member is the "kickoff" — show its title (minus a "Kickoff:"
    // prefix) so a thread's identity survives its later updates.
    const members = items
      .filter((i) => i.thread_key === key)
      .slice()
      .sort((a, b) => String(a.created_at ?? '').localeCompare(String(b.created_at ?? '')));
    const first = members[0];
    const title = first?.title ?? item.title ?? '';
    return title.replace(/^Kickoff:\s*/i, '');
  }

</script>

<div class="surface inbox-surface">
  <section class="listpane" aria-label="inbox list">
    <div class="list-head">
      <div class="viewtabs" role="tablist">
        {#each [['inbox', 'Inbox'], ['archive', 'Archive'], ['starred', 'Starred']] as [v, label] (v)}
          <button
            class="viewtab"
            class:active={view === v}
            role="tab"
            aria-selected={view === v}
            onclick={() => setView(v)}>{label}</button>
        {/each}
      </div>
      <div class="searchrow">
        <input
          class="search"
          placeholder="Search title, body, sender…"
          bind:value={search}
          aria-label="search inbox"
        />
        <button class="chan-gear" title="Channel settings" aria-label="channel settings" onclick={() => (showChans = true)}>
          <Icon name="settings" size={13} />
        </button>
      </div>
      {#if chips.length > 0 || channelFilter}
        <div class="chips">
          <button
            class="chip"
            class:active={channelFilter === null}
            onclick={() => (channelFilter = null)}>
            <span class="dot" style="background: var(--ink-meta)"></span>All
            {#if unreadTotal(unread) > 0}<span class="cnt">{unreadTotal(unread)}</span>{/if}
          </button>
          {#each chips as c (c.name)}
            <button
              class="chip"
              class:active={channelFilter === c.name}
              style="--chip-c: var({c.colorVar})"
              onclick={() => (channelFilter = channelFilter === c.name ? null : c.name)}>
              <span class="dot" style="background: var({c.colorVar})"></span>{c.name}
              {#if chipMuteGlyph(c.name)}<span class="muted-ic">{chipMuteGlyph(c.name)}</span>{/if}
              {#if c.count > 0}<span class="cnt">{c.count}</span>{/if}
            </button>
          {/each}
        </div>
      {/if}
      <div class="row-tools">
        {#if search.trim()}
          <span class="count-label">
            {searchRunning ? 'searching…' : `${filtered.length}${searchCapped ? '+' : ''} of ${searchTotal} matches`}
          </span>
        {:else}
          <span class="count-label">{filtered.length}{atEnd ? '' : '+'} {view}</span>
          {#if view === 'inbox' && unreadTotal(unread) > 0}
            <button class="readall" onclick={readAllView}>read all</button>
          {/if}
        {/if}
      </div>
    </div>

    {#if error}
      <div class="err-strip">⚠ {error}</div>
    {/if}

    <div class="rows">
      {#if filtered.length === 0}
        <div class="empty">
          {search || channelFilter
            ? 'nothing matches'
            : view === 'inbox'
              ? 'Inbox zero. Nice.'
              : view === 'starred'
                ? 'No starred items yet.'
                : 'Archive is empty.'}
        </div>
      {:else}
        {#each filtered as row (row.id)}
          <button
            class="row"
            class:unread={isUnread(row)}
            class:sel={row.id === selectedItem?.id}
            style="--rc: var({channelColor(row.channel)})"
            style:transform={swipeId === row.id || swipeGone === row.id ? `translateX(${swipeDx}px)` : null}
            style:transition={swipeGone === row.id ? 'transform 0.22s ease-out, opacity 0.22s ease-out' : null}
            style:opacity={swipeGone === row.id ? 0 : null}
            ontouchstart={(e) => swipeBegin(e, row)}
            ontouchmove={(e) => swipeMove(e, row)}
            ontouchend={() => swipeEnd(row)}
            ontouchcancel={() => { swipeId = null; swipeDx = 0; swipeDy = 0; _swipeStart = null; }}
            onclick={() => { if (swipeId !== row.id && swipeGone !== row.id) openItem(row.id); }}>
            <div class="row-top">
              <span class="r-chan">{row.channel}</span>
              <span class="r-src">{row.source_label}</span>
              <span class="r-time">{relTime(row.created_at)}</span>
            </div>
            <div class="r-title">
              <span class="udot"></span>
              {#if row.starred}<span class="star-ic">★</span>{/if}
              {#if row.score}<span class="score-ic" class:s1={row.score === 1} class:s3={row.score === 3}>{row.score}</span>{/if}
              <span class="r-title-text">{row.title}</span>
              {#if threadNeedsResponse(items, row)}<span class="needs">NEEDS RESPONSE</span>{/if}
              {#if row.tcount > 1}<span class="thread-pill">⛓ {row.tcount}</span>{/if}
            </div>
            <div class="r-prev">{previewText(row.body, 90)}</div>
          </button>
        {/each}
        {#if !atEnd}
          <div class="load-more" bind:this={_sentinelEl} use:sentinelScrollAnchor>
            <button class="readall" onclick={loadOlder} disabled={loadingOlder}>
              {loadingOlder ? 'loading…' : 'load older'}
            </button>
            <span use:sentinel class="sentinel-anchor"></span>
          </div>
        {/if}
      {/if}
    </div>
  </section>

  <!-- CSS owns the responsive breakpoint. On mobile only a real selection
       earns .open, so an empty/stale detail can never become an overlay. -->
  <section class="detailpane" class:open={!!selectedItem} aria-label="inbox item detail">
    {#if selectedItem}
      <div class="d-head">
        <button class="d-back" onclick={backToList}>← Inbox</button>
        <div class="d-toprow">
          <span class="d-chanchip" style="--rc: var({channelColor(selectedItem.channel)})">{selectedItem.channel}</span>
          <span class="d-title">{threadTitle(selectedItem)}</span>
          <div class="d-actions">
            <button
              class="act"
              class:star-on={selectedItem.starred}
              onclick={() => toggleStar(selectedItem)}>
              {selectedItem.starred ? '★ Starred' : '☆ Star'}
            </button>
            <button class="act" onclick={() => toggleRead(selectedItem)}>
              {isUnread(selectedItem) ? 'Mark read' : 'Mark unread'}
            </button>
            <span class="score-group" title="Score this kind of message">
              {#each SCORES as [n, label, tip] (n)}
                <button
                  class="act score"
                  class:score-on={selectedItem.score === n}
                  class:s1={n === 1} class:s3={n === 3}
                  title={tip}
                  onclick={() => scoreItem(selectedItem, n)}>{label}</button>
              {/each}
            </span>
          </div>
        </div>
        <div class="d-meta">
          <span>from <b style="color: var(--peer)">{selectedItem.source_label}</b></span>
          {#if selectedItem.thread_key}
            <span class="d-thread-id">thread: {selectedItem.thread_key}</span>
          {/if}
          {#if selectedItem.source_pulse_id}
            <button class="openpulse" onclick={() => openPulse(selectedItem)}>Open pulse ↗</button>
          {/if}
        </div>
      </div>

      {#if needsResponse}
        <div class="needs-banner">
          ⚑ This sender is blocked on your response.
          <button onclick={() => dismissResponse(selectedItem)}>Dismiss</button>
        </div>
      {/if}

      <div class="d-body" bind:this={conversationBody}>
        {#if conversationError}<p role="alert">{conversationError}</p>{/if}
        {#each thread as m (m.id)}
          <div class="msg" class:from-pulse={m.source_kind === 'pulse'} class:from-user={m.source_kind === 'user'} class:from-sys={m.source_kind !== 'pulse' && m.source_kind !== 'user'}>
            <div class="msg-head">
              <span class="msg-who">{m.source_label}</span>
              <span class="msg-t">{relTime(m.created_at)}</span>
              {#if isUnread(m)}<span class="msg-new">NEW</span>{/if}
            </div>
            <div class="msg-card" use:cardify={{ on: htmlCards.on, text: m.body }}>{@html renderMarkdown(m.body)}</div>
          </div>
        {/each}
      </div>

      <div class="reply-note">
        {replyable
          ? 'Reply is delivered to the pulse’s queue; its answer returns to this thread. For live chat, open the pulse.'
          : 'Sender is a server script — replies are disabled.'}
      </div>
      <div class="composer" class:disabled={!replyable || sending}>
        <textarea
          bind:value={draft}
          placeholder={replyable ? `Reply to ${selectedItem.source_label}…` : 'Replies disabled for this sender'}
          disabled={!replyable || sending}
          onkeydown={(e) => { if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); sendReply(); } }}
          rows="2"></textarea>
        <button class="send" disabled={!replyable || sending} onclick={sendReply}>Reply</button>
      </div>
    {:else}
      <div class="d-empty">
        <div class="big">✉</div>
        <div>Select an item</div>
      </div>
    {/if}
  </section>
</div>

{#if showChans}
  <div class="scrim" onclick={() => (showChans = false)} role="presentation">
    <div class="modal" onclick={(e) => e.stopPropagation()}>
      <h2>Channel notifications</h2>
      <div class="sub">
        Channels are created automatically when a sender first uses one.
        <span class="m-notify">notify</span> = highlighted ·
        <span class="m-quiet">quiet</span> = badge only ·
        <span class="m-muted">muted</span> = filed silently
      </div>
      {#if chansError}<div class="err-strip">⚠ {chansError}</div>{/if}
      <div class="chanrows">
        {#each modalChans as c (c.name)}
          {@const ch = channels.find((x) => x.channel === c.name)}
          {@const mutedUntil = isMuteUntilActive(ch?.mute_until, now)}
          <div class="chan-row" style="--cc: var({c.colorVar})">
            <div class="chan-name"><span class="dot"></span>{c.name}</div>
            <div class="seg">
              {#each [['notify', 'notify'], ['quiet', 'quiet'], ['muted', 'muted']] as [m, cls] (m)}
                <button
                  class="{modeOf(c.name) === m ? 'on m-' + cls : ''}"
                  onclick={() => setMode(c.name, m)}>{m}</button>
              {/each}
            </div>
            <div class="mute-until">
              mute until
              <select
                value={mutedUntil ? 'active' : ''}
                onchange={(e) => setMuteUntil(c.name, e.currentTarget.value)}>
                <option value="" selected={!mutedUntil}>—</option>
                {#if mutedUntil}
                  <option value="active" selected>{relTime(ch.mute_until)}</option>
                {/if}
                <option value="3600000">1 hour</option>
                <option value="86400000">today</option>
                <option value="604800000">1 week</option>
              </select>
            </div>
          </div>
        {/each}
        {#if modalChans.length === 0}
          <div class="empty chan-empty">no channels yet</div>
        {/if}
      </div>
      <button class="modal-close" onclick={() => (showChans = false)}>Done</button>
    </div>
  </div>
{/if}



<style>
  .surface { display: flex; flex: 1; min-height: 0; }
  .listpane {
    width: 430px;
    min-width: 340px;
    border-right: 1px solid var(--border);
    display: flex;
    flex-direction: column;
    background: var(--bg);
  }
  .detailpane { flex: 1; min-width: 0; display: flex; flex-direction: column; background: var(--page); }

  .list-head { padding: calc(12px * var(--tscale-ui)) calc(14px * var(--tscale-ui)) 0; flex-shrink: 0; }
  .viewtabs {
    display: flex; gap: 2px; background: var(--surface); border: 1px solid var(--border);
    border-radius: calc(9px * var(--tscale-ui)); padding: 3px; width: max-content;
  }
  .viewtab {
    padding: calc(5px * var(--tscale-ui)) calc(14px * var(--tscale-ui)); border-radius: calc(6px * var(--tscale-ui));
    font-size: calc(12.5px * var(--tscale-ui)); color: var(--ink-dim);
  }
  .viewtab:hover { color: var(--ink); }
  .viewtab.active {
    background: var(--surface-warm); color: var(--ink);
    box-shadow: inset 0 0 0 1px var(--border-strong);
  }
  .searchrow { display: flex; gap: calc(8px * var(--tscale-ui)); margin: calc(10px * var(--tscale-ui)) 0; }
  .search {
    flex: 1; background: var(--surface); border: 1px solid var(--border); border-radius: calc(8px * var(--tscale-ui));
    padding: calc(7px * var(--tscale-ui)) calc(11px * var(--tscale-ui)); font-size: calc(12.5px * var(--tscale-ui)); color: var(--ink);
  }
  .search::placeholder { color: var(--ink-meta); }
  .search:focus { outline: none; border-color: var(--border-strong); }
  .chan-gear {
    width: calc(34px * var(--tscale-ui)); border: 1px solid var(--border); border-radius: calc(8px * var(--tscale-ui));
    color: var(--ink-dim); display: flex; align-items: center; justify-content: center;
  }
  .chan-gear:hover { color: var(--ink); border-color: var(--border-strong); }

  .chips { display: flex; gap: calc(6px * var(--tscale-ui)); flex-wrap: wrap; padding-bottom: calc(11px * var(--tscale-ui)); }
  .chip {
    display: inline-flex; align-items: center; gap: calc(5px * var(--tscale-ui));
    font-size: calc(11px * var(--tscale-ui)); padding: calc(4px * var(--tscale-ui)) calc(9px * var(--tscale-ui));
    border-radius: calc(20px * var(--tscale-ui)); border: 1px solid var(--border); color: var(--ink-dim);
  }
  .chip:hover { border-color: var(--border-strong); }
  .chip.active {
    color: var(--ink);
    border-color: var(--chip-c, var(--border-strong));
    background: color-mix(in srgb, var(--chip-c, var(--border-strong)) 13%, transparent);
  }
  .chip .dot { width: calc(6px * var(--tscale-ui)); height: calc(6px * var(--tscale-ui)); border-radius: 3px; }
  .chip .cnt { color: var(--ink-meta); font-size: calc(10px * var(--tscale-ui)); }
  .chip .muted-ic { font-size: calc(10px * var(--tscale-ui)); opacity: 0.8; }

  .row-tools {
    display: flex; align-items: center; gap: calc(10px * var(--tscale-ui));
    padding-bottom: calc(8px * var(--tscale-ui));
  }
  .count-label {
    font-family: var(--font-mono); font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta); text-transform: uppercase; letter-spacing: 0.1em;
  }
  .readall {
    margin-left: auto; font-family: var(--font-mono); font-size: calc(10px * var(--tscale-ui));
    color: var(--dreamer); text-transform: uppercase; letter-spacing: 0.08em;
  }
  .readall:hover { text-decoration: underline; }

  .err-strip {
    background: color-mix(in srgb, var(--bad) 16%, var(--surface)); color: var(--bad);
    font-family: var(--font-mono); font-size: calc(10px * var(--tscale-ui));
    padding: calc(5px * var(--tscale-ui)) calc(12px * var(--tscale-ui)); flex-shrink: 0;
  }
  .rows { flex: 1; overflow-y: auto; border-top: 1px solid var(--border); }
  .load-more {
    display: flex; align-items: center; gap: 10px;
    padding: calc(10px * var(--tscale-ui)) calc(14px * var(--tscale-ui));
  }
  .sentinel-anchor { display: block; width: 1px; height: 1px; }
  .row {
    display: block; width: 100%; text-align: left;
    padding: calc(11px * var(--tscale-ui)) calc(14px * var(--tscale-ui));
    border-bottom: 1px solid var(--border); position: relative;
    /* pan-y: vertical scrolling stays native; horizontal is the
       swipe-to-archive gesture's. */
    touch-action: pan-y;
  }
  .row:hover { background: var(--surface); }
  .row.sel { background: var(--surface-warm); }
  .row.sel::before {
    content: ''; position: absolute; left: 0; top: 0; bottom: 0;
    width: 2px; background: var(--dreamer);
  }
  .row-top { display: flex; align-items: center; gap: calc(7px * var(--tscale-ui)); margin-bottom: calc(3px * var(--tscale-ui)); }
  .r-chan {
    font-size: calc(9.5px * var(--tscale-ui)); letter-spacing: 0.06em; text-transform: uppercase;
    font-weight: 600; color: var(--rc, var(--ink-meta));
  }
  .r-src { font-size: calc(11px * var(--tscale-ui)); color: var(--ink-meta); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .r-time {
    margin-left: auto; font-size: calc(10.5px * var(--tscale-ui)); color: var(--ink-meta);
    flex-shrink: 0; font-family: var(--font-mono);
  }
  .r-title { font-size: calc(13px * var(--tscale-ui)); color: var(--ink-dim); display: flex; align-items: center; gap: calc(7px * var(--tscale-ui)); }
  .r-title-text { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .row.unread .r-title { color: var(--ink); font-weight: 600; }
  .row.unread .r-time { color: var(--dreamer); }
  .r-prev {
    font-size: calc(11.5px * var(--tscale-ui)); color: var(--ink-meta); margin-top: calc(2px * var(--tscale-ui));
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
  }
  .udot { width: calc(7px * var(--tscale-ui)); height: calc(7px * var(--tscale-ui)); border-radius: 4px; background: var(--dreamer); flex-shrink: 0; }
  .row:not(.unread) .udot { display: none; }
  .thread-pill {
    font-size: calc(9.5px * var(--tscale-ui)); color: var(--ink-meta); border: 1px solid var(--border-strong);
    border-radius: calc(9px * var(--tscale-ui)); padding: 1px calc(7px * var(--tscale-ui));
    font-family: var(--font-mono); flex-shrink: 0;
  }
  .needs {
    font-size: calc(9px * var(--tscale-ui)); letter-spacing: 0.05em; font-weight: 700;
    color: var(--page); background: var(--lantern); border-radius: 4px;
    padding: calc(2px * var(--tscale-ui)) calc(5px * var(--tscale-ui)); flex-shrink: 0;
  }
  .star-ic { color: var(--lantern); font-size: calc(11px * var(--tscale-ui)); flex-shrink: 0; }
  .empty { padding: calc(40px * var(--tscale-ui)) calc(20px * var(--tscale-ui)); text-align: center; color: var(--ink-meta); font-size: calc(12.5px * var(--tscale-ui)); }
  .chan-empty { padding: calc(16px * var(--tscale-ui)); }

  /* ---- detail ---- */
  .d-head {
    padding: calc(14px * var(--tscale-ui)) calc(20px * var(--tscale-ui)) calc(12px * var(--tscale-ui));
    border-bottom: 1px solid var(--border); flex-shrink: 0;
  }
  .d-back { display: none; color: var(--dreamer); font-size: calc(12.5px * var(--tscale-ui)); margin-bottom: calc(8px * var(--tscale-ui)); }
  .d-toprow { display: flex; align-items: center; gap: calc(9px * var(--tscale-ui)); flex-wrap: wrap; }
  .d-title { font-size: calc(16.5px * var(--tscale-ui)); font-weight: 650; }
  .d-meta {
    display: flex; align-items: center; gap: calc(10px * var(--tscale-ui)); margin-top: calc(7px * var(--tscale-ui));
    font-size: calc(11.5px * var(--tscale-ui)); color: var(--ink-meta); flex-wrap: wrap;
  }
  .d-thread-id { font-family: var(--font-mono); font-size: calc(10px * var(--tscale-ui)); }
  .d-chanchip {
    font-size: calc(9.5px * var(--tscale-ui)); letter-spacing: 0.06em; text-transform: uppercase; font-weight: 700;
    color: var(--rc);
    border: 1px solid color-mix(in srgb, var(--rc) 40%, transparent);
    background: color-mix(in srgb, var(--rc) 10%, transparent);
    border-radius: calc(5px * var(--tscale-ui)); padding: calc(2px * var(--tscale-ui)) calc(7px * var(--tscale-ui));
  }
  .openpulse {
    color: var(--warden); font-size: calc(11.5px * var(--tscale-ui)); text-decoration: none;
    border: 1px solid color-mix(in srgb, var(--warden) 35%, transparent); border-radius: calc(7px * var(--tscale-ui));
    padding: calc(3.5px * var(--tscale-ui)) calc(9px * var(--tscale-ui));
  }
  .openpulse:hover { background: color-mix(in srgb, var(--warden) 12%, transparent); }
  .d-actions { margin-left: auto; display: flex; gap: calc(6px * var(--tscale-ui)); }
  .act {
    font-size: calc(11.5px * var(--tscale-ui)); color: var(--ink-dim); border: 1px solid var(--border);
    border-radius: calc(7px * var(--tscale-ui)); padding: calc(4px * var(--tscale-ui)) calc(10px * var(--tscale-ui));
  }
  .act:hover { color: var(--ink); border-color: var(--border-strong); }
  .act.star-on { color: var(--lantern); border-color: color-mix(in srgb, var(--lantern) 40%, transparent); }
  .score-group { display: inline-flex; gap: calc(3px * var(--tscale-ui)); margin-left: calc(4px * var(--tscale-ui)); }
  .act.score { padding-left: calc(7px * var(--tscale-ui)); padding-right: calc(7px * var(--tscale-ui)); }
  .act.score-on { color: var(--ink); border-color: var(--border-strong); background: color-mix(in srgb, var(--ink) 8%, transparent); }
  .act.score-on.s1 { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 45%, transparent); }
  .act.score-on.s3 { color: var(--lantern); border-color: color-mix(in srgb, var(--lantern) 45%, transparent); }
  .score-ic {
    flex-shrink: 0; font-size: calc(10px * var(--tscale-ui)); font-weight: 700; line-height: 1;
    color: var(--ink-dim); border: 1px solid var(--border); border-radius: calc(4px * var(--tscale-ui));
    padding: calc(1px * var(--tscale-ui)) calc(4px * var(--tscale-ui));
  }
  .score-ic.s1 { color: var(--bad); }
  .score-ic.s3 { color: var(--lantern); }

  .needs-banner {
    display: flex; align-items: center; gap: calc(9px * var(--tscale-ui));
    margin: calc(12px * var(--tscale-ui)) calc(20px * var(--tscale-ui)) 0;
    padding: calc(9px * var(--tscale-ui)) calc(13px * var(--tscale-ui));
    border: 1px solid color-mix(in srgb, var(--lantern) 40%, transparent); border-radius: calc(9px * var(--tscale-ui));
    background: color-mix(in srgb, var(--lantern) 9%, transparent);
    font-size: calc(12px * var(--tscale-ui)); color: var(--lantern);
  }
  .needs-banner button {
    margin-left: auto; font-size: calc(11px * var(--tscale-ui)); color: var(--ink-dim);
    border: 1px solid var(--border-strong); border-radius: calc(6px * var(--tscale-ui)); padding: calc(3px * var(--tscale-ui)) calc(9px * var(--tscale-ui));
  }

  .d-body { flex: 1; overflow-y: auto; padding: calc(16px * var(--tscale-ui)) calc(20px * var(--tscale-ui)) calc(20px * var(--tscale-ui)); }
  .msg { max-width: 760px; margin-bottom: calc(14px * var(--tscale-ui)); }
  .msg-head { display: flex; align-items: baseline; gap: calc(9px * var(--tscale-ui)); margin-bottom: calc(5px * var(--tscale-ui)); font-size: calc(11px * var(--tscale-ui)); }
  .msg-who { font-weight: 650; }
  .msg.from-user { margin-left: auto; }
  .msg.from-user .msg-who { color: var(--ink); }
  .msg.from-user .msg-card { background: color-mix(in srgb, var(--ink) 8%, var(--surface)); }
  .msg.from-pulse .msg-who { color: var(--peer); }
  .msg.from-sys .msg-who { color: var(--matron); }
  .msg-t { color: var(--ink-meta); font-family: var(--font-mono); font-size: calc(10px * var(--tscale-ui)); }
  .msg-new { color: var(--dreamer); font-size: calc(9px * var(--tscale-ui)); letter-spacing: 0.08em; }
  .msg-card {
    border: 1px solid var(--border); border-radius: calc(11px * var(--tscale-ui));
    padding: calc(12px * var(--tscale-ui)) calc(15px * var(--tscale-ui));
    font-size: calc(13px * var(--tscale-chat)); line-height: 1.55; color: var(--ink);
  }
  .msg.from-pulse .msg-card { background: color-mix(in srgb, var(--peer) 8%, var(--surface)); }
  .msg.from-sys .msg-card { background: color-mix(in srgb, var(--matron) 7%, var(--surface)); }
  :global(.inbox-surface .msg-card p) { margin-bottom: calc(8px * var(--tscale-chat)); }
  :global(.inbox-surface .msg-card p:last-child) { margin-bottom: 0; }
  /* HTML cards (htmlcards.svelte.js) in Inbox mail too: the live frame
     replaces the code until "source". */
  :global(.inbox-surface .msg-card iframe.html-card) { display: block; width: 100%; height: 120px; border: 0; border-radius: 6px; background: var(--bg); }
  :global(.inbox-surface .msg-card .code-wrap.carded:not(.show-src) pre) { display: none; }
  :global(.inbox-surface .msg-card .src-btn) { background: none; border: 0; color: inherit; font: inherit; cursor: pointer; opacity: 0.7; margin-left: auto; margin-right: 6px; }
  :global(.inbox-surface .msg-card code) {
    font-family: var(--font-mono); font-size: calc(11.5px * var(--tscale-chat));
    background: var(--surface-warm); border: 1px solid var(--border);
    border-radius: 4px; padding: 1px calc(5px * var(--tscale-chat));
  }
  :global(.inbox-surface .msg-card ul) { margin: calc(4px * var(--tscale-chat)) 0 calc(8px * var(--tscale-chat)) 18px; }

  .reply-note {
    font-size: calc(10.5px * var(--tscale-ui)); color: var(--ink-meta);
    padding: 0 calc(20px * var(--tscale-ui)) calc(10px * var(--tscale-ui)); background: var(--bg);
  }
  .composer {
    border-top: 1px solid var(--border); padding: calc(12px * var(--tscale-ui)) calc(20px * var(--tscale-ui));
    display: flex; gap: calc(9px * var(--tscale-ui)); flex-shrink: 0; background: var(--bg);
  }
  .composer textarea {
    flex: 1; background: var(--surface); border: 1px solid var(--border); border-radius: calc(10px * var(--tscale-ui));
    padding: calc(10px * var(--tscale-ui)) calc(13px * var(--tscale-ui));
    font-size: calc(13px * var(--tscale-chat)); resize: none; height: calc(44px * var(--tscale-ui)); line-height: 1.4;
  }
  .composer textarea:focus { outline: none; border-color: var(--border-strong); }
  .send {
    background: color-mix(in srgb, var(--user) 16%, transparent); color: var(--user);
    border: 1px solid color-mix(in srgb, var(--user) 45%, transparent); border-radius: calc(10px * var(--tscale-ui));
    padding: 0 calc(18px * var(--tscale-ui)); font-size: calc(13px * var(--tscale-ui)); font-weight: 600;
  }
  .send:hover:not(:disabled) { background: color-mix(in srgb, var(--user) 26%, transparent); }
  .composer.disabled, .composer :disabled { opacity: 0.45; pointer-events: none; }

  .d-empty {
    flex: 1; display: flex; align-items: center; justify-content: center; flex-direction: column;
    gap: calc(10px * var(--tscale-ui)); color: var(--ink-meta); font-size: calc(13px * var(--tscale-ui));
  }
  .d-empty .big { font-size: calc(34px * var(--tscale-ui)); opacity: 0.35; }

  /* ---- channel settings modal ---- */
  .scrim {
    position: fixed; inset: 0; background: color-mix(in srgb, var(--page) 72%, transparent); backdrop-filter: blur(3px);
    display: flex; align-items: center; justify-content: center; z-index: 90;
  }
  .modal {
    width: min(560px, 92vw); max-height: 84dvh; overflow-y: auto; background: var(--surface);
    border: 1px solid var(--border-strong); border-radius: calc(15px * var(--tscale-ui));
    padding: calc(20px * var(--tscale-ui)) calc(22px * var(--tscale-ui));
  }
  .modal h2 { font-size: calc(14px * var(--tscale-ui)); letter-spacing: 0.04em; margin-bottom: calc(4px * var(--tscale-ui)); }
  .modal .sub { font-size: calc(11.5px * var(--tscale-ui)); color: var(--ink-meta); margin-bottom: calc(16px * var(--tscale-ui)); }
  .m-notify { color: var(--sprite); } .m-quiet { color: var(--lantern); } .m-muted { color: var(--bad); }
  /* flex-wrap: on phone widths the mute-until select otherwise clips off the
     modal's right edge — it wraps under instead. */
  .chan-row { display: flex; flex-wrap: wrap; align-items: center; gap: calc(11px * var(--tscale-ui)); padding: calc(11px * var(--tscale-ui)) 0; border-top: 1px solid var(--border); }
  .chan-name { display: flex; align-items: center; gap: calc(8px * var(--tscale-ui)); width: 130px; font-size: calc(13px * var(--tscale-ui)); flex-shrink: 0; }
  .chan-name .dot { width: calc(8px * var(--tscale-ui)); height: calc(8px * var(--tscale-ui)); border-radius: 4px; background: var(--cc); }
  .seg { display: flex; background: var(--bg); border: 1px solid var(--border); border-radius: calc(8px * var(--tscale-ui)); padding: 2px; }
  .seg button { padding: calc(4.5px * var(--tscale-ui)) calc(11px * var(--tscale-ui)); font-size: calc(11px * var(--tscale-ui)); border-radius: calc(6px * var(--tscale-ui)); color: var(--ink-meta); }
  .seg button.on { color: var(--ink); background: var(--surface-warm); box-shadow: inset 0 0 0 1px var(--border-strong); }
  .seg button.on.m-notify { color: var(--sprite); }
  .seg button.on.m-quiet { color: var(--lantern); }
  .seg button.on.m-muted { color: var(--bad); }
  .mute-until { margin-left: auto; font-size: calc(10.5px * var(--tscale-ui)); color: var(--ink-meta); }
  .mute-until select {
    background: var(--bg); border: 1px solid var(--border); border-radius: calc(6px * var(--tscale-ui));
    padding: calc(3px * var(--tscale-ui)) calc(6px * var(--tscale-ui)); font-size: calc(10.5px * var(--tscale-ui)); color: var(--ink-dim);
  }
  .modal-close {
    margin-top: calc(16px * var(--tscale-ui)); width: 100%; padding: calc(9px * var(--tscale-ui));
    border: 1px solid var(--border-strong); border-radius: calc(9px * var(--tscale-ui));
    font-size: calc(12.5px * var(--tscale-ui)); color: var(--ink-dim);
  }
  .modal-close:hover { color: var(--ink); }

  /* ---- responsive (mock's 820px breakpoint) ---- */
  @media (max-width: 820px) {
    /* The desktop 340px minimum pushed the entire Inbox off a 320px phone and
       clipped every row on the right. In phone mode the list owns the exact
       available width, whatever that width is. */
    .listpane { width: 100%; min-width: 0; border-right: none; }
    .detailpane {
      display: none; position: fixed; inset: calc(var(--nav-h, 48px) + 1px) 0 0 0;
      background: var(--page); z-index: 20;
    }
    .detailpane.open { display: flex; }
    .d-back { display: inline-block; }
    /* Keep .surface flex here: block breaks the height chain (listpane goes
       content-height, .rows loses its bound and the list can't scroll — the
       shell's overflow:hidden just clips it). The detailpane doesn't need
       block either; it's position:fixed below this breakpoint. */
  }
</style>
