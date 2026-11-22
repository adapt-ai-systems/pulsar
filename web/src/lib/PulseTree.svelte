<script>
  import { untrack } from 'svelte';
  import {
    listPulses,
    archivePulse,
    isMonitoringChoice,
    unarchivePulse,
    restorePulse,
    pausePulse,
    deletePulseStopping,
    renamePulse,
    createGrid,
    renameGrid,
    deleteGrid
  } from './api.js';
  import Icon from './Icon.svelte';
  import { modelLabel } from './models.js';
  import { scheduleLabel } from './schedule.js';
  import { confirmArm } from './confirmarm.svelte.js';
  import { runPulseAction } from './pulsetoggle.svelte.js';
  import { childGrids, wsColor } from './wscolor.js';
  import {
    LIT,
    PHASE_LABEL,
    phaseOf as phaseFor,
    schedByPulse as buildSchedByPulse,
    filterPulses,
    gridNameFn,
    sortPulses,
    buildSections,
    reorderGridIds,
    withArchived,
    ARCHIVED_REFRESH_MS
  } from './pulsetree.js';
  import { createLongPressDrag } from './longpress.svelte.js';
  import { dropUnder } from './longpress.js';
  import { seenStore, markSeen } from './seen.svelte.js';
  import { questionStore, subscribeQuestions } from './questionstore.svelte.js';
  import { scheduleStore, subscribeSchedules, refreshSchedules } from './schedulestore.svelte.js';

  /**
   * The pulse list: ALL pulses across every grid as a workspace tree,
   * with a top search field that live-filters, a hide-archived checkbox
   * (default ON), per-row actions and hold-to-drag. Errors surface in a
   * strip — never silent. ONE list for the session drawer and the full-screen
   * pulse window (matching the drawer's behavior).
   *
   * - `head`: snippet rendered first inside the head block (the drawer's
   *   title row).
   * - `activeId`: the pulse currently open; its row is outlined.
   * - `touchBlock`: where the page scroll is blocked while carrying. 'self' =
   *   this component's own element (the drawer's choice); 'document' = the
   *   whole page, for both drags (the pane's choice).
   * - `compact`: the narrow 280px variant for the full-screen pulse window.
   * - `ongridschanged`: also called after a workspace create/rename/delete.
   *
   * @type {{ grids?: Array<{id:string,name:string,parent_id?:string|null}>, activeId?: string|null, compact?: boolean, touchBlock?: 'self'|'document', head?: import('svelte').Snippet, onchanged?: () => void, ongridschanged?: () => void, onopen?: (p: any) => void, onopengrid?: (id: string, created?: boolean) => void, onmovepulse?: (id: string, gridId: string | null) => void, onreorderworkspaces?: (ids: string[]) => void }}
   */
  // Opening the list puts the cursor in its search box.
  function focusOnMount(node) { node.focus(); }
  let { grids = [], rosterPulses = null, activeId = null, compact = false, touchBlock = 'self', head: headRow, onchanged, ongridschanged, onopen, onopengrid, onmovepulse, onreorderworkspaces } = $props();

  // --- hierarchy + attention ---------------------------------------------- //
  // The list is a TREE: workspace → sub-workspace → pulses, every level
  // wearing the most urgent state inside it, so one glance says which project
  // needs a human. Phases come from attention.js; "done" (finished, not yet
  // looked at) from seen.svelte.js; open questions from the question store.
  let tick = $state(Date.now()); // re-evaluates status labels with the poll
  $effect(() => subscribeQuestions());
  const questionIds = $derived(new Set(questionStore.questions.map((q) => q.pulse_id)));
  function phaseOf(p) {
    return phaseFor(p, { questionIds, doneIds: seenStore.done, nowMs: tick });
  }
  // The phases that light up and flash (LIT): working, asking a question, or
  // finished and waiting for eyes. Same work-glow overlay the panes and chips
  // use, colored per phase.
  // Fold state persists across closes and reloads so the same workspaces stay
  // folded as when the user last closed the list.
  const FOLDS_KEY = 'pulsar.drawerFolds';
  function loadFolds() {
    try {
      const v = JSON.parse(localStorage.getItem(FOLDS_KEY) || '{}');
      return v && typeof v === 'object' && !Array.isArray(v) ? v : {};
    } catch { return {}; }
  }
  let collapsed = $state(loadFolds()); // grid id -> true when its branch is folded
  $effect(() => {
    const snap = JSON.stringify(collapsed);
    try { localStorage.setItem(FOLDS_KEY, snap); } catch {}
  });
  function toggleFold(id) {
    collapsed[id] = !collapsed[id];
  }
  // Expand all / collapse all. "All" is every workspace and sub-workspace
  // plus the no-workspace bucket.
  const allFolded = $derived.by(() => {
    const ids = [...grids.map((g) => g.id), '__none'];
    return ids.length > 0 && ids.every((id) => collapsed[id]);
  });
  function foldAll(fold) {
    const next = {};
    if (fold) for (const id of [...grids.map((g) => g.id), '__none']) next[id] = true;
    collapsed = next;
  }

  // --- hold-to-drag a pulse onto a workspace ------------------------------ //
  // Same gesture as the phone overview (Wall.svelte): hold a row ~320ms, then
  // carry it onto a workspace or sub-workspace header (or "no workspace") and
  // let go. Pointer events, so it works for touch and mouse alike; a hold that
  // moves early is a scroll and is left alone.
  // The machine is longpress.svelte.js (LONG_PRESS_MS 320, PRESS_SLOP 9).
  // dropTarget: undefined = nothing; null = no workspace; string = grid id
  let treeEl = $state(null);
  let hotEl = null; // element outside this component lit as the drop target
  function clearHot() {
    hotEl?.removeAttribute('data-drop-hot');
    hotEl = null;
  }
  const lp = createLongPressDrag({
    noTarget: undefined,
    canStart: (e, info) =>
      !!onmovepulse && info.state !== 'archived' && !e.target.closest('button, input, a, select'),
    onStart: (d) => console.debug('[drawer] drag START %s', d.id),
    hitTest: (x, y) => {
      const head = dropUnder(x, y, '[data-ws-drop]');
      // Targets outside the drawer (the workspace tab strip) cannot see our
      // drag state, so they are lit by attribute.
      if (hotEl !== head) {
        hotEl?.removeAttribute('data-drop-hot');
        head?.setAttribute('data-drop-hot', '');
        hotEl = head;
      }
      if (!head) return undefined;
      const v = head.getAttribute('data-ws-drop');
      return v === '' ? null : v;
    },
    onEnd: clearHot,
    onCancel: (d, why) => {
      if (why === 'released') console.debug('[drawer] drag CANCELLED — released on nothing (%s)', d.id);
    },
    onDrop: (target, { id, label }) => {
      const p = pulses.find((x) => x.id === id);
      if (p && (p.grid_id ?? null) === target) return;
      console.debug('[drawer] drop %s (%s) → %s', id, label, target ?? 'no workspace');
      // Apply locally so the row moves at once; the page persists + refreshes.
      pulses = pulses.map((x) => (x.id === id ? { ...x, grid_id: target } : x));
      onmovepulse?.(id, target);
    },
    touchBlockTarget: () => (touchBlock === 'document' ? (lp.drag ? document : null) : treeEl) // block the page scroll while carrying
  });
  const drag = $derived(lp.drag);
  const dropTarget = $derived(lp.target);
  function pressStart(e, p) {
    lp.start(e, { id: p.id, label: p.title, state: p.state });
  }

  // --- hold-to-drag a workspace header to reorder it ---------------------- //
  // Same hold gesture on a header lifts the workspace; carry it over a SIBLING
  // header (top-level over top-level, sub over a sub of the same parent) and
  // let go to drop it above or below that one. The whole order — every
  // workspace with its subs kept together — is handed to the page, which
  // persists it the same way the tab strip does.
  // wsDrop: { id, after } — the sibling header under the finger.
  const wsLp = createLongPressDrag({
    noTarget: null,
    canStart: (e) =>
      !!onreorderworkspaces && !lp.drag && !e.target.closest('button, input, a, select'),
    onStart: (d) => console.debug('[drawer] workspace drag START %s', d.id),
    hitTest: (x, y, d) => {
      const head = dropUnder(x, y, '[data-ws-id]');
      if (!head) return null;
      const id = head.getAttribute('data-ws-id');
      const parent = head.getAttribute('data-ws-parent') || null;
      if (!id || id === d.id || parent !== d.parent) return null;
      const r = head.getBoundingClientRect();
      return { id, after: y > r.top + r.height / 2 };
    },
    onCancel: (d, why) => {
      if (why === 'released') console.debug('[drawer] workspace drag CANCELLED (%s)', d.id);
    },
    onDrop: (target, { id, parent }) => {
      // Reorder the siblings, then flatten the whole tree in tab order.
      const ids = reorderGridIds(grids, id, parent, target);
      if (!ids) return;
      console.debug('[drawer] workspace drop %s → %s %s', id, target.after ? 'after' : 'before', target.id);
      onreorderworkspaces?.(ids);
    },
    touchBlockTarget: () => (touchBlock === 'document' ? (wsLp.drag ? document : null) : treeEl)
  });
  const wsDrag = $derived(wsLp.drag);
  const wsDrop = $derived(wsLp.target);
  function wsPressStart(e, sec) {
    if (!sec.grid) return;
    const g = sec.grid;
    wsLp.start(e, { id: g.id, parent: g.parent_id ?? null, label: g.name });
  }

  /**
   * Tap anywhere on a row (not its buttons/inputs) → jump to that pulse.
   * Archived pulses have no pane to jump to, so their rows stay inert.
   */
  function openRow(e, p) {
    if (p.state === 'archived') return;
    if (e.target.closest('button, input, a, select')) return;
    markSeen(p.id); // looked at
    onopen?.(p);
  }

  let pulses = $state([]);
  let error = $state(''); // drawer-level visible error strip
  let query = $state('');
  let hideArchived = $state(true); // default ON = archived hidden
  // "hide watchers": drops Watchers-shelf pulses and Watcher-titled ones.
  const HIDE_WATCHERS_KEY = 'pulsar.drawer.hideWatchers';
  let hideWatchers = $state(typeof localStorage !== 'undefined' && localStorage.getItem(HIDE_WATCHERS_KEY) === '1');
  $effect(() => {
    if (hideWatchers) localStorage.setItem(HIDE_WATCHERS_KEY, '1');
    else localStorage.removeItem(HIDE_WATCHERS_KEY);
  });
  const SORT_KEY = 'pulsar.drawer.sortBy';
  let sortBy = $state((typeof localStorage !== 'undefined' && localStorage.getItem(SORT_KEY)) || 'recent'); // attention | recent | name | created
  $effect(() => { try { localStorage.setItem(SORT_KEY, sortBy); } catch {} });
  // "my inputs": ONE flat list ordered by when the user last messaged each
  // pulse so rows stay stable instead of jumping on background activity.
  // Pulse activity never moves a row; only their own sends do.
  const BY_INPUT_KEY = 'pulsar.drawer.byInput';
  // Read synchronously at init (not in an effect) so the persist effect
  // below can never run first with `false` and wipe the saved choice.
  let byInput = $state(typeof localStorage !== 'undefined' && localStorage.getItem(BY_INPUT_KEY) === '1');
  $effect(() => {
    if (byInput) localStorage.setItem(BY_INPUT_KEY, '1');
    else localStorage.removeItem(BY_INPUT_KEY);
  });
  // --- select → archive --------------------------------------------------- //
  // A real select-all → archive action in the drawer, replacing the old
  // 'archive idle pulses' number-crunch in the view popover. Selection mode
  // adds a checkbox per row; 'select all' covers every pulse matching the
  // current search/filter (archived rows never select — they are archived).
  // A pulse whose archive the daemon refuses with monitoring_choice_required
  // owns watches/schedules: it is skipped and named in the note, because the
  // keep-watching vs stop choice is per-pulse and not ours to guess.
  let selectMode = $state(false);
  let selectedIds = $state([]);
  let bulkArmed = $state(false);
  let bulkArmTimer = null;
  let bulkNote = $state('');
  function toggleSelectMode(v) {
    selectMode = v;
    selectedIds = [];
    bulkArmed = false;
    bulkNote = '';
    if (bulkArmTimer) {
      clearTimeout(bulkArmTimer);
      bulkArmTimer = null;
    }
  }
  function selectAllVisible() {
    selectedIds = filtered.filter((p) => p.state !== 'archived').map((p) => p.id);
    bulkNote = '';
  }
  async function archiveSelected() {
    if (!bulkArmed) {
      bulkArmed = true;
      clearTimeout(bulkArmTimer);
      bulkArmTimer = setTimeout(() => (bulkArmed = false), 5000);
      return;
    }
    bulkArmed = false;
    clearTimeout(bulkArmTimer);
    bulkArmTimer = null;
    const needChoice = [];
    let archived = 0;
    for (const id of selectedIds) {
      const r = await archivePulse(id, {});
      if (r.ok) {
        archived++;
        continue;
      }
      if (isMonitoringChoice(r)) needChoice.push(id);
      else console.log('[drawer] bulk archive failed', id, r.error);
    }
    selectedIds = [];
    bulkNote = needChoice.length
      ? `archived ${archived} · ${needChoice.length} own watches/schedules — archive them from their pane to choose keep/stop`
      : `archived ${archived}`;
    onchanged?.();
  }

  // Per-row delete confirm; an armed confirm auto-disarms after 5s so a stray
  // tap minutes later can't destroy a pulse (matches ChatPane's pause confirm).
  const pulseDelete = confirmArm({ armed: null }); // armed holds the pulse id
  const armDelete = pulseDelete.arm;
  const disarmDelete = pulseDelete.disarm;

  const gridName = $derived(gridNameFn(grids));

  // --- workspaces (grids) -------------------------------------------------- //
  // Managed here rather than in the nav tabs: the tab strip only creates, and
  // rename/delete had no home in the UI at all until now.
  let wsOpen = $state(false);
  let newWsName = $state('');
  let subFor = $state(null); // top-level grid id with the "+ sub" input open
  let newSubName = $state('');
  let editingWs = $state(null); // grid id being renamed
  let editWsName = $state('');
  const wsDelete = confirmArm({ armed: null });
  const armWsDelete = wsDelete.arm;

  // Live pulse count per grid — the row subtitle, and what the delete confirm
  // says will be moved out. Archived is tracked separately because the list
  // hides archived rows by default, so "1 pulse" with nothing visible to move
  // used to read as a dead end. Deleting no longer waits on emptiness: the
  // pulses are detached for you.
  const wsCounts = $derived.by(() => {
    const m = new Map();
    for (const p of pulses) {
      if (!p.grid_id) continue;
      const c = m.get(p.grid_id) ?? { total: 0, archived: 0 };
      c.total += 1;
      if (p.state === 'archived') c.archived += 1;
      m.set(p.grid_id, c);
    }
    return m;
  });

  /**
   * Reveal exactly the pulses a workspace holds. `gridName` is part of
   * the search haystack, so setting the query to the workspace name filters the
   * list to it; unhiding archived is what actually makes the blockers visible.
   */
  function revealWsPulses(g) {
    hideArchived = false;
    query = g.name;
    wsOpen = false; // collapse the section so the filtered list is on screen
  }

  async function runGrid(label, fn) {
    const r = await fn();
    if (r.ok) {
      error = '';
      ongridschanged?.();
      onchanged?.(); // parent re-fetches the grid list that feeds our `grids` prop
      await refresh();
    } else {
      error = `${label}: ${r.error}`;
      console.log(`[drawer] ${label} failed`, r.error);
    }
    return r;
  }

  async function doCreateWs() {
    const n = newWsName.trim();
    if (!n) return;
    const r = await runGrid('create workspace', () => createGrid(n));
    if (r.ok) newWsName = '';
  }

  // A sub-workspace lives inside ONE top-level workspace (one layer only).
  async function doCreateSub(parentId) {
    const n = newSubName.trim();
    if (!n || !parentId) return;
    const r = await runGrid('create sub-workspace', () => createGrid(n, parentId));
    if (r.ok) {
      newSubName = '';
      subFor = null;
      // Open it, like the tab strip's + does.
      if (r.value?.id) onopengrid?.(r.value.id, true);
    }
  }

  function startRenameWs(g) {
    editingWs = g.id;
    editWsName = g.name;
  }

  async function saveRenameWs() {
    const n = editWsName.trim();
    const id = editingWs;
    if (!n || !id) {
      editingWs = null;
      return;
    }
    const r = await runGrid('rename workspace', () => renameGrid(id, n));
    if (r.ok) editingWs = null;
  }

  async function doDeleteWs(id) {
    wsDelete.disarm();
    // detach: the workspace goes, its pulses don't — they land under ALL
    // (even when an archived pulse is still assigned to it).
    await runGrid('delete workspace', () => deleteGrid(id, { detach: true }));
  }

  // Archived rows are fetched only while shown (box unchecked), searched, or
  // while the WORKSPACES drawer is open so workspace counts see archived rows:
  // polling every archived pulse every 3s slowed down the list.
  // Even then the timer refetches them only every 30s, slim, to keep payload
  // size bounded on large archives. Any other refresh (open, the box,
  // an action here) fetches them at once.
  const needArchived = $derived(!hideArchived || query.trim() !== '' || wsOpen);
  let archivedRows = [];
  let archivedAt = 0;
  async function refresh({ fromTimer = false } = {}) {
    const wantArchived = needArchived;
    const archivedDue = wantArchived && (!fromTimer || Date.now() - archivedAt >= ARCHIVED_REFRESH_MS);
    // The timer takes the live rows from the page's roster (see the effect
    // below); opening, the box and actions here still fetch them.
    const own = !fromTimer || rosterPulses === null;
    const [r, ra] = await Promise.all([
      own ? listPulses() : null,
      archivedDue ? listPulses({ state: 'archived', slim: true }) : null
    ]);
    if (ra?.ok) {
      archivedRows = ra.value ?? [];
      archivedAt = Date.now();
    } else if (ra) {
      console.log('[drawer] list archived pulses failed', ra.error);
    }
    if (r && !r.ok) {
      error = `pulses: ${r.error}`;
      console.log('[drawer] list pulses failed', r.error);
    } else if (r || ra?.ok) {
      // our own fetch, or new archived rows to merge with the roster's
      const rows = r ? (r.value ?? []) : rosterPulses;
      pulses = wantArchived ? withArchived(rows, archivedRows) : rows;
      error = '';
    }
    // Subtitles read the shared schedule store, which the panes already poll
    // every 15s. Opening and actions here ask it for a fresh copy.
    if (!fromTimer) refreshSchedules();
  }

  // The page's roster is the list GET /pulses returns, pushed by /live every
  // 3s (or polled while the socket is down): show each update as it lands.
  // The drawer's own 3s GET /pulses and GET /schedules fetched the same data
  // again, 40 requests and ~490 KB a minute while docked (2026-11-02).
  $effect(() => {
    const rows = rosterPulses;
    if (rows === null) return;
    untrack(() => {
      pulses = needArchived ? withArchived(rows, archivedRows) : rows;
      error = '';
    });
  });
  $effect(() => subscribeSchedules());

  // One enabled schedule per pulse for the subtitle (loops win over wakes).
  const schedByPulse = $derived(buildSchedByPulse(scheduleStore.schedules));

  $effect(() => {
    refresh();
    const id = setInterval(() => {
      tick = Date.now();
      refresh({ fromTimer: true });
    }, 3000);
    return () => {
      clearInterval(id);
    };
  });

  /** Short date for row subtitles: "Jul 12", with 'YY only when not this year. */
  function fmtDate(iso) {
    if (!iso) return '';
    const d = new Date(iso);
    if (isNaN(d.getTime())) return '';
    const opts = { month: 'short', day: 'numeric' };
    if (d.getFullYear() !== new Date().getFullYear()) opts.year = '2-digit';
    return d.toLocaleDateString('en-US', opts);
  }

  /** Date + time ("Jul 12 5:32p") for this year; older stamps drop the time. */
  function fmtStamp(iso) {
    const day = fmtDate(iso);
    if (!day) return '';
    const d = new Date(iso);
    if (d.getFullYear() !== new Date().getFullYear()) return day;
    let h = d.getHours();
    const ap = h >= 12 ? 'p' : 'a';
    h = h % 12 || 12;
    return `${day} ${h}:${String(d.getMinutes()).padStart(2, '0')}${ap}`;
  }

  const modelLabelOf = modelLabel;
  const filtered = $derived.by(() => {
    const list = filterPulses(pulses, { query, hideArchived, hideWatchers, gridName, modelLabelOf });
    return sortPulses(list, byInput ? 'byinput' : sortBy, { phaseOf });
  });

  /**
   * The tree the list renders (pulsetree.js buildSections): one section per
   * top-level workspace, sub-workspaces, then "no workspace".
   */
  const sections = $derived(buildSections(filtered, grids, { query, byInput, modelLabelOf, phaseOf }));

  async function run(label, fn, id) {
    return runPulseAction(label, () => fn(id), {
      onok: async () => {
        error = '';
        await refresh();
        onchanged?.();
      },
      onerr: (m, r) => {
        error = m;
        console.log(`[drawer] ${label} failed`, id, r.error);
      }
    });
  }

  // Inline pulse rename — same shape as the workspace rename above.
  let editingPulse = $state(null); // pulse id being renamed
  let editPulseTitle = $state('');

  function startRenamePulse(p) {
    editingPulse = p.id;
    editPulseTitle = p.title ?? '';
  }

  async function saveRenamePulse() {
    const t = editPulseTitle.trim();
    const id = editingPulse;
    if (!t || !id) {
      editingPulse = null;
      return;
    }
    const r = await run('rename', (pid) => renamePulse(pid, t), id);
    if (r.ok) editingPulse = null;
  }

  const doPause = (id) => run('stop', pausePulse, id);
  // A pulse with live watches/schedules: ask keep/stop in words, never
  // show the daemon's JSON instructions.
  let archiveChoiceId = $state(null);
  async function doArchive(id, monitoring = undefined) {
    archiveChoiceId = null;
    const r = await archivePulse(id, monitoring ? { monitoring } : {});
    if (isMonitoringChoice(r)) {
      error = '';
      archiveChoiceId = id;
      return r;
    }
    return run('archive', () => Promise.resolve(r), id);
  }
  const doUnarchive = (id) => run('unarchive', unarchivePulse, id);
  const doRestore = (id) => run('restore', restorePulse, id);

  // A running pulse is stopped first; one that owns watches asks before
  // they close (the daemon's has_watchers 409, put in words).
  let deleteChoiceId = $state(null);
  async function doDelete(id, closeWatchers = false) {
    disarmDelete();
    deleteChoiceId = null;
    const r = await deletePulseStopping(id, { closeWatchers });
    if (r.code === 'has_watchers') {
      error = '';
      deleteChoiceId = id;
      return r;
    }
    return run('delete', () => Promise.resolve(r), id);
  }
</script>

<div
  class="tree"
  class:compact
  class:dragging={!!drag || !!wsDrag}
  bind:this={treeEl}
>
<div class="drawer-head">
    {#if headRow}{@render headRow()}{/if}

    <div class="search">
      <Icon name="search" size={13} />
      <input
        type="text"
        placeholder="search pulses…"
        bind:value={query}
        aria-label="search pulses"
        use:focusOnMount
      />
      {#if query}
        <button class="clear" onclick={() => (query = '')} title="clear search" aria-label="clear search">✕</button>
      {/if}
    </div>

    <!-- Options in one wrapping row, sort on its own row, fold + hint last
         (the old two-column layout overflowed the panel). -->
    <div class="opts">
      <label class="drawer-filter">
        <input type="checkbox" bind:checked={hideArchived} />
        hide archived
      </label>
      <label class="drawer-filter" title="hide pulses on the Watchers shelf and pulses with Watcher in the title">
        <input type="checkbox" bind:checked={hideWatchers} />
        hide watchers
      </label>
      <label class="drawer-filter by-input" title="one flat list: the pulse you messaged most recently on top; pulse activity never moves a row">
        <input type="checkbox" bind:checked={byInput} />
        my inputs
      </label>
      <label class="drawer-filter select" title="show a checkbox on every row, then archive the selection">
        <input
          type="checkbox"
          checked={selectMode}
          onchange={(e) => toggleSelectMode(e.currentTarget.checked)}
        />
        select
      </label>
    </div>
    <div class="sort" role="group" aria-label="sort pulses">
      <span class="sort-label">sort</span>
      {#each ['attention', 'recent', 'name', 'created'] as k (k)}
        <button class="sort-opt" class:on={sortBy === k} onclick={() => (sortBy = k)}>{k}</button>
      {/each}
    </div>
    <div class="fold-row">
      <button class="sort-opt fold-all" onclick={() => foldAll(!allFolded)} title={allFolded ? 'expand every workspace' : 'collapse every workspace'}>
        {allFolded ? '▸ expand all' : '▾ collapse all'}
      </button>
      {#if selectMode}
        <!-- Select-all → archive: replaces the old 'archive idle' gadget. -->
        <button class="sort-opt" onclick={selectAllVisible} title="select every pulse matching the current search and filters">select all</button>
        {#if selectedIds.length}
          <button
            class="sort-opt archive-sel"
            class:armed={bulkArmed}
            onclick={archiveSelected}
            title="archive every selected pulse (reversible)">
            {bulkArmed ? `confirm archive ${selectedIds.length}?` : `archive ${selectedIds.length}`}
          </button>
        {/if}
      {/if}
      {#if onmovepulse}<span class="hint">hold a pulse to drag it to a workspace</span>{/if}
      {#if selectMode && bulkNote}<span class="hint bulk-note">{bulkNote}</span>{/if}
    </div>
  </div>

  <!-- Workspaces (grids) ---------------------------------------------------- -->
  <div class="ws">
    <button class="ws-toggle" onclick={() => (wsOpen = !wsOpen)} aria-expanded={wsOpen}>
      <span class="chev" class:open={wsOpen}>▸</span>
      WORKSPACES <span class="ws-count">{grids.length}</span>
    </button>

    {#if wsOpen}
      <div class="ws-body">
        {#each grids as g (g.id)}
          {@const held = wsCounts.get(g.id) ?? { total: 0, archived: 0 }}
          <!-- Drop target too (in the flat "my inputs" view there are no
               section headers to drop on). -->
          <div class="wrow" class:sub={!!g.parent_id} class:droppable={!!drag} class:drop-on={!!drag && dropTarget === g.id} data-ws-drop={g.id}>
            {#if editingWs === g.id}
              <input
                class="ws-edit"
                type="text"
                bind:value={editWsName}
                aria-label="workspace name"
                onkeydown={(e) => {
                  if (e.key === 'Enter') saveRenameWs();
                  if (e.key === 'Escape') editingWs = null;
                }}
              />
              <button class="ico ok" title="save name" aria-label="save name" onclick={saveRenameWs}>✓</button>
              <button class="ico" title="cancel" aria-label="cancel rename" onclick={() => (editingWs = null)}>✕</button>
            {:else}
              <div class="winfo">
                <div class="wname" title={g.name}>{g.name}</div>
                {#if held.total}
                  <!-- The count is a BUTTON: it filters the list to this
                       workspace and unhides archived, so what it holds is
                       reachable instead of merely counted. -->
                  <button class="wsub link" onclick={() => revealWsPulses(g)}>
                    {held.total} pulse{held.total === 1 ? '' : 's'}{held.archived
                      ? ` · ${held.archived} archived`
                      : ''} — show
                  </button>
                {:else}
                  <div class="wsub">empty</div>
                {/if}
              </div>
              {#if !g.parent_id}
                <button class="ico" title="add a sub-workspace inside" aria-label={`add sub-workspace in ${g.name}`} onclick={() => { subFor = subFor === g.id ? null : g.id; newSubName = ''; }}>+ sub</button>
              {/if}
              <button class="ico" title="rename" aria-label="rename workspace" onclick={() => startRenameWs(g)}>
                <Icon name="settings" size={13} />
              </button>
              {#if wsDelete.armed === g.id}
                <!-- Says what the delete does to the occupants, because it does
                     NOT delete them: they come out into "no grid". -->
                <button
                  class="ico del armed"
                  title={held.total
                    ? `delete workspace — ${held.total} pulse${held.total === 1 ? '' : 's'} move out, none are deleted`
                    : 'delete workspace'}
                  aria-label="confirm delete workspace"
                  onclick={() => doDeleteWs(g.id)}
                >{held.total ? `confirm? ${held.total} out` : 'confirm?'}</button>
                <button class="ico" title="cancel" aria-label="cancel delete" onclick={() => wsDelete.disarm()}>✕</button>
              {:else}
                <!-- Never disabled: a workspace holding pulses (archived ones
                     included) is deletable — the pulses are detached, not
                     deleted. -->
                <button
                  class="ico del"
                  title={held.total
                    ? `delete workspace — its ${held.total} pulse${held.total === 1 ? '' : 's'} move out, none are deleted`
                    : 'delete workspace'}
                  aria-label="delete workspace"
                  onclick={() => armWsDelete(g.id)}
                ><Icon name="delete" size={13} /></button>
              {/if}
            {/if}
          </div>
          {#if subFor === g.id}
            <div class="ws-new sub-new">
              <!-- svelte-ignore a11y_autofocus -->
              <input
                type="text"
                placeholder={`new sub-workspace in ${g.name}…`}
                bind:value={newSubName}
                aria-label={`new sub-workspace in ${g.name}`}
                autofocus
                onkeydown={(e) => {
                  if (e.key === 'Enter') doCreateSub(g.id);
                  if (e.key === 'Escape') subFor = null;
                }}
              />
              <button class="ws-add" onclick={() => doCreateSub(g.id)} disabled={!newSubName.trim()}>+ add</button>
            </div>
          {/if}
        {/each}

        {#if grids.length === 0}
          <p class="ws-empty">no workspaces yet</p>
        {/if}

        <div class="ws-new">
          <input
            type="text"
            placeholder="new workspace…"
            bind:value={newWsName}
            aria-label="new workspace name"
            onkeydown={(e) => e.key === 'Enter' && doCreateWs()}
          />
          <button class="ws-add" onclick={doCreateWs} disabled={!newWsName.trim()}>+ add</button>
        </div>
      </div>
    {/if}
  </div>

  {#if error}
    <div class="err-strip">⚠ {error}</div>
  {/if}
  {#if deleteChoiceId}
    {@const dp = (pulses ?? []).find((x) => x.id === deleteChoiceId)}
    <div class="err-strip choice" role="group" aria-label="delete choice">
      <span>“{dp?.title ?? 'This pulse'}” owns open watches; deleting it closes them.</span>
      <button title="close its watches, stop it if running, then delete" onclick={() => doDelete(deleteChoiceId, true)}>close watches &amp; delete</button>
      <button title="cancel" aria-label="cancel delete" onclick={() => (deleteChoiceId = null)}>✕</button>
    </div>
  {/if}
  {#if archiveChoiceId}
    {@const cp = (pulses ?? []).find((x) => x.id === archiveChoiceId)}
    <div class="err-strip choice" role="group" aria-label="archive choice">
      <span>“{cp?.title ?? 'This pulse'}” still has watches or reminders.</span>
      <button title="keep its watches and schedules — it moves to the Watchers shelf instead of the archive" onclick={() => doArchive(archiveChoiceId, 'keep')}>keep watching</button>
      <button title="close its watches, disable its schedules, then archive" onclick={() => doArchive(archiveChoiceId, 'stop')}>stop &amp; archive</button>
      <button title="cancel" aria-label="cancel archive" onclick={() => (archiveChoiceId = null)}>✕</button>
    </div>
  {/if}

  <div class="drawer-list">
    {#snippet row(p, lvl)}
      {@const ph = phaseOf(p)}
      {@const stopped = p.state === 'stopped'}
      {@const archived = p.state === 'archived'}
      {@const sched = schedByPulse.get(p.id)}
      <div
        class="srow lvl{lvl} {ph}"
        class:lit={LIT.has(ph)}
        class:work-glow={LIT.has(ph)}
        class:archived
        class:openable={!archived}
        class:active={!!activeId && p.id === activeId}
        class:carrying={drag?.id === p.id}
        role="button"
        tabindex={archived ? -1 : 0}
        onclick={(e) => openRow(e, p)}
        onpointerdown={(e) => pressStart(e, p)}
        onpointermove={lp.move}
        onpointerup={lp.up}
        onpointercancel={lp.cancel}
        onkeydown={(e) => {
          if (e.key === 'Enter' && e.target === e.currentTarget) openRow(e, p);
        }}
      >
        {#if selectMode && !archived}
          <input
            type="checkbox"
            class="row-sel"
            checked={selectedIds.includes(p.id)}
            onclick={(e) => e.stopPropagation()}
            onchange={(e) => {
              selectedIds = e.currentTarget.checked
                ? [...selectedIds, p.id]
                : selectedIds.filter((x) => x !== p.id);
            }}
            aria-label={`select ${p.title}`}
            title={`select ${p.title}`}
          />
        {/if}
        <span class="dot {ph}" title={PHASE_LABEL[ph] || ph}></span>
        <div class="info">
          {#if editingPulse === p.id}
            <div class="trow">
              <input
                class="title-edit"
                type="text"
                bind:value={editPulseTitle}
                aria-label="pulse title"
                onkeydown={(e) => {
                  if (e.key === 'Enter') saveRenamePulse();
                  if (e.key === 'Escape') editingPulse = null;
                }}
              />
              <button class="ico ok" title="save title" aria-label="save title" onclick={saveRenamePulse}>✓</button>
              <button class="ico" title="cancel" aria-label="cancel rename" onclick={() => (editingPulse = null)}>✕</button>
            </div>
          {:else}
            <div class="st" title={p.title}><span class="st-name">{p.title}</span>{#if PHASE_LABEL[ph]}<span class="tag {ph}">{PHASE_LABEL[ph]}</span>{/if}{#if p.settings?.mode}<span class="tag mode" title={p.settings.mode === 'paused' ? 'set aside — off the grid until you bring it back' : 'on the Watchers shelf'}>{p.settings.mode}</span>{/if}</div>
          {/if}
          <!-- Workspace is plain text here; moving a pulse is drag-to-chip on
               the wall (a native select rendered oversized on iOS). -->
          <div class="sm" title={`${p.model} · started ${fmtStamp(p.created_at)}`}>{modelLabel(p.model)}{sched ? ` · ${scheduleLabel(sched)}` : ''} · {byInput ? (p.last_input_at ? `you ${fmtStamp(p.last_input_at)}` : 'no input yet') : fmtStamp(p.created_at)}{archived ? ` · archived ${fmtDate(p.archived_at)}` : ''}</div>
        </div>

        {#if editingPulse !== p.id}
        <div class="acts">
          <button class="ico" title="rename" aria-label="rename pulse" onclick={() => startRenamePulse(p)}>
            <Icon name="rename" size={13} />
          </button>
          <!-- Every live row carries start OR stop — the pair reads as one
               toggle slot. Stop is reversible (play sits in its place after),
               so no confirm. -->
          {#if stopped}
            <button class="ico" title="start" aria-label="start" onclick={() => doRestore(p.id)}><Icon name="play" size={13} /></button>
          {:else if !archived}
            <button class="ico" title="stop" aria-label="stop" onclick={() => doPause(p.id)}><Icon name="pause" size={13} /></button>
          {/if}

          {#if archived}
            <button class="ico" title="unarchive" aria-label="unarchive" onclick={() => doUnarchive(p.id)}>
              <Icon name="unarchive" size={14} />
            </button>
          {:else}
            <!-- Running rows archive directly too — the daemon pauses for us
                 first, same as ChatPane's archive button. -->
            <button class="ico" title="archive" aria-label="archive" onclick={() => doArchive(p.id)}>
              <Icon name="archive" size={14} />
            </button>
          {/if}

          {#if pulseDelete.armed === p.id}
            <button class="ico del armed" title="confirm delete" aria-label="confirm delete" onclick={() => doDelete(p.id)}>confirm?</button>
            <button class="ico" title="cancel" aria-label="cancel delete" onclick={disarmDelete}>✕</button>
          {:else}
            <button class="ico del" title="delete" aria-label="delete" onclick={() => armDelete(p.id)}>
              <Icon name="delete" size={14} />
            </button>
          {/if}
        </div>
        {/if}
      </div>
    {/snippet}

    {#snippet head(sec, lvl)}
      <!-- A workspace header: fold chevron, name (click = open that tab),
           roll-up dot of everything inside, pulse count. -->
      <div
        class="sec-head lvl{lvl} {sec.phase}"
        class:lit={LIT.has(sec.phase)}
        class:work-glow={LIT.has(sec.phase)}
        class:droppable={!!drag}
        class:drop-on={!!drag && dropTarget !== undefined && dropTarget === (sec.grid?.id ?? null)}
        class:carrying={wsDrag?.id === sec.grid?.id && !!sec.grid}
        class:ins-before={!!wsDrop && !wsDrop.after && wsDrop.id === sec.grid?.id}
        class:ins-after={!!wsDrop && wsDrop.after && wsDrop.id === sec.grid?.id}
        data-ws-drop={sec.grid?.id ?? ''}
        data-ws-id={sec.grid?.id ?? ''}
        data-ws-parent={sec.grid?.parent_id ?? ''}
        style={sec.grid ? `--ws-color:${wsColor(sec.grid.id, grids)}` : ''}
        onpointerdown={(e) => wsPressStart(e, sec)}
        onpointermove={wsLp.move}
        onpointerup={wsLp.up}
        onpointercancel={wsLp.cancel}
      >
        <button class="fold" onclick={() => toggleFold(sec.grid?.id ?? '__none')} aria-expanded={!collapsed[sec.grid?.id ?? '__none']} aria-label="fold">
          <span class="chev" class:open={!collapsed[sec.grid?.id ?? '__none']}>▸</span>
        </button>
        <span class="dot {sec.phase}" title={PHASE_LABEL[sec.phase] || sec.phase}></span>
        {#if sec.grid}
          <span class="ws-swatch" aria-hidden="true"></span>
          <button class="sec-name" onclick={() => onopengrid?.(sec.grid.id)} title={`open ${sec.grid.name}`}>{sec.grid.name}</button>
        {:else}
          <span class="sec-name plain">no workspace</span>
        {/if}
        {#if PHASE_LABEL[sec.phase]}<span class="tag {sec.phase}">{PHASE_LABEL[sec.phase]}</span>{/if}
        <span class="sec-count">{sec.count}</span>
      </div>
    {/snippet}

    {#each sections as sec (sec.grid?.id ?? '__none')}
      {#if !sec.flat}{@render head(sec, 0)}{/if}
      {#if sec.flat || !collapsed[sec.grid?.id ?? '__none']}
        {#each sec.pulses as p (p.id)}
          {@render row(p, 1)}
        {/each}
        {#each sec.subs as sub (sub.grid.id)}
          {@render head(sub, 1)}
          {#if !collapsed[sub.grid.id]}
            {#each sub.pulses as p (p.id)}
              {@render row(p, 2)}
            {/each}
            {#if sub.pulses.length === 0}
              <div class="empty lvl2">empty</div>
            {/if}
          {/if}
        {/each}
        {#if sec.count === 0}
          <div class="empty lvl1">empty</div>
        {/if}
      {/if}
    {/each}

    {#if drag}
      <div class="drag-ghost" style="left: {drag.x}px; top: {Math.max(drag.y, 44)}px;">{drag.label}</div>
    {/if}
    {#if wsDrag}
      <div class="drag-ghost ws" style="left: {wsDrag.x}px; top: {Math.max(wsDrag.y, 44)}px;">{wsDrag.label}</div>
    {/if}

    {#if filtered.length === 0 && !error}
      <div class="empty">{query ? 'no pulses match' : 'no pulses'}</div>
    {/if}
  </div>
</div>

<style>
  .tree {
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .drawer-head {
    padding: 14px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .search {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-top: 10px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 6px 10px;
    color: var(--ink-meta);
  }
  .search input {
    flex: 1;
    background: none;
    border: none;
    outline: none;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
  }
  .search input::placeholder { color: var(--ink-meta); }
  .search .clear {
    color: var(--ink-dim);
    font-size: calc(12px * var(--tscale-ui));
    padding: 0 2px;
    line-height: 1;
  }
  .search .clear:hover { color: var(--ink); }

  .opts {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px 14px;
    margin-top: 10px;
  }
  .opts .drawer-filter { white-space: nowrap; }
  .drawer-filter {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-dim);
    font-family: var(--font-mono);
    letter-spacing: 0.06em;
    cursor: pointer;
  }
  .drawer-filter input { accent-color: var(--dreamer); }

  .sort {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    gap: 4px;
    margin-top: 8px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
  }
  .sort-label { color: var(--ink-meta); letter-spacing: 0.08em; margin-right: 2px; }
  .sort-opt {
    color: var(--ink-meta);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 2px 7px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.05em;
    line-height: 1.3;
  }
  .sort-opt:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .sort-opt.on {
    color: var(--dreamer);
    border-color: var(--dreamer);
  }

  .err-strip {
    background: color-mix(in srgb, var(--bad) 16%, var(--surface));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 5px 12px;
    border-bottom: 1px solid color-mix(in srgb, var(--bad) 40%, var(--border));
    flex-shrink: 0;
    word-break: break-word;
  }
  /* A question, not an error. */
  .err-strip.choice { display: flex; flex-wrap: wrap; align-items: center; gap: 6px; color: var(--ink); background: var(--surface); }
  .err-strip.choice button { font: inherit; padding: 2px 8px; min-height: 28px; }

  .drawer-list { flex: 1; overflow-y: auto; }
  .empty {
    padding: 16px 14px;
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
  }

  .srow {
    position: relative;
    display: flex;
    align-items: center;
    gap: 9px;
    padding: 7px 14px;
    border-bottom: 1px solid var(--border);
  }
  .srow.openable { cursor: pointer; }
  .srow:hover { background: var(--surface-warm); }
  /* Archived: dimmed, no strikethrough — the meta line already says
     "archived <date>", and struck titles read as clutter at this density. */
  .srow.archived { opacity: 0.55; }
  .info { min-width: 0; flex: 1; }
  /* The title ellipsizes and the tags keep their room: a normal-length
     title used to push "paused" out of the clipped box. */
  .st {
    display: flex;
    align-items: center;
    font-size: calc(12.5px * var(--tscale-ui));
    font-weight: 500;
    color: var(--ink);
    white-space: nowrap;
    overflow: hidden;
  }
  .st-name { min-width: 0; overflow: hidden; text-overflow: ellipsis; }
  .st .tag { flex-shrink: 0; }
  .trow { display: flex; gap: 5px; align-items: center; }
  .title-edit {
    flex: 1;
    min-width: 0;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    padding: 2px 7px;
    color: var(--ink);
    font-size: calc(11.5px * var(--tscale-ui));
  }
  .title-edit:focus { outline: none; border-color: var(--dreamer); }
  .trow .ico {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 2px 5px;
    font-size: calc(10px * var(--tscale-ui));
    line-height: 1;
    flex-shrink: 0;
  }
  .trow .ico:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .trow .ok:hover { color: var(--sprite); border-color: var(--sprite); }
  .sm {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    letter-spacing: 0.06em;
    margin-top: 2px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }

  .acts {
    display: flex;
    gap: 1px;
    align-items: center;
    flex-shrink: 0;
  }
  /* Ghost buttons: icon only, no borders — five bordered pills per row read
     as clutter and starved the title of width. */
  .acts .ico {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    border: none;
    border-radius: 7px;
    padding: 5px;
    font-size: calc(10px * var(--tscale-ui));
    line-height: 1;
  }
  .acts .ico:hover { color: var(--ink); background: var(--border); }
  /* Actions float over the row's right edge on hover/focus only, so the
     title gets the full row width. */
  @media (hover: hover) {
    .acts {
      position: absolute;
      right: 8px;
      top: 50%;
      transform: translateY(-50%);
      background: var(--surface-warm);
      border-radius: 8px;
      padding: 0 2px;
      box-shadow: -10px 0 10px var(--surface-warm);
      visibility: hidden;
    }
    .srow:hover .acts, .srow:focus-within .acts { visibility: visible; }
  }
  .acts .del:hover { color: var(--bad); }
  .acts .del.armed {
    color: var(--bad);
    border: 1px solid var(--bad);
    border-radius: 999px;
    padding: 3px 7px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
  }


  /* --- tree --------------------------------------------------------------- */
  .sec-head {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 7px 14px 5px;
    border-bottom: 1px solid var(--border);
    background: color-mix(in srgb, var(--surface-warm) 60%, var(--surface));
    position: sticky;
    top: 0;
    z-index: 1;
  }
  .sec-head.lvl1 {
    padding-left: 28px;
    background: var(--surface);
    position: static;
  }
  .sec-head .fold {
    color: var(--ink-meta);
    padding: 0 2px;
    font-size: calc(10px * var(--tscale-ui));
  }
  /* Phone: a finger-sized fold target. */
  @media (max-width: 700px) {
    .sec-head .fold {
      font-size: calc(15px * var(--tscale-ui));
      padding: 4px 8px;
      margin-left: -6px;
    }
    .srow { padding-top: 9px; padding-bottom: 9px; }
  }
  .fold-row {
    display: flex;
    flex-wrap: wrap;
    align-items: center;
    justify-content: space-between;
    gap: 6px 10px;
    margin-top: 8px;
  }
  .fold-row .sort-opt { white-space: nowrap; }
  .fold-all { color: var(--ink-dim); white-space: nowrap; }
  /* Select-all → archive. */
  .sort-opt.archive-sel { color: var(--ink-dim); }
  .sort-opt.archive-sel:hover { color: var(--bad); }
  .sort-opt.archive-sel.armed { color: var(--dreamer); }
  .row-sel {
    flex-shrink: 0;
    margin: 0 2px 0 0;
    accent-color: var(--sprite);
  }
  .hint.bulk-note { white-space: normal; flex: 1 0 100%; }
  .hint {
    font-family: var(--font-mono);
    font-size: calc(8.5px * var(--tscale-ui));
    color: var(--ink-meta);
    letter-spacing: 0.04em;
  }
  /* Hold-to-drag: the carried row fades, every header becomes a target, and
     the one under the finger fills in. */
  .srow.carrying { opacity: 0.35; }
  /* The pulse you're ON (the full-screen window's list) stays readable over the lit tints. */
  .srow.active { outline: 1.5px solid var(--sprite); outline-offset: -1.5px; }
  .tree.dragging {
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
  }
  .sec-head.droppable { outline: 1px dashed var(--sprite); outline-offset: -2px; }
  /* Workspace reorder: the lifted header fades, an insert line shows where
     it will land. */
  .sec-head.carrying { opacity: 0.35; }
  .sec-head.ins-before { box-shadow: inset 0 2px 0 0 var(--sprite); }
  .sec-head.ins-after { box-shadow: inset 0 -2px 0 0 var(--sprite); }
  .sec-head.drop-on,
  .wrow.drop-on {
    outline: 2px solid var(--sprite);
    background: color-mix(in srgb, var(--sprite) 18%, transparent);
  }
  .wrow.droppable { outline: 1px dashed var(--sprite); outline-offset: -1px; }
  .sec-head .chev { display: inline-block; transition: transform 0.15s ease; }
  .sec-head .chev.open { transform: rotate(90deg); }
  .ws-swatch {
    width: 7px;
    height: 7px;
    border-radius: 2px;
    background: var(--ws-color, transparent);
    flex-shrink: 0;
  }
  .sec-name {
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-ui));
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-dim);
    padding: 0;
    text-align: left;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    min-width: 0;
  }
  .sec-head.lvl1 .sec-name { font-size: calc(9.5px * var(--tscale-ui)); letter-spacing: 0.1em; }
  button.sec-name:hover { color: var(--dreamer); }
  .sec-name.plain { color: var(--ink-meta); }
  .sec-count {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
  }
  .srow.lvl1 { padding-left: 28px; }
  .srow.lvl2 { padding-left: 44px; }
  .empty.lvl1 { padding: 4px 14px 6px 28px; }
  .empty.lvl2 { padding: 4px 14px 6px 44px; }

  /* Status: the dot carries the phase; the urgent phases also get a tag after
     the title so the state reads without color. */
  .dot {
    width: 8px;
    height: 8px;
    border-radius: 50%;
    flex-shrink: 0;
    background: var(--border-strong);
  }
  .dot.working {
    background: var(--dreamer);
    animation: drawer-pulse 1.2s ease-in-out infinite;
  }
  .dot.idle { background: color-mix(in srgb, var(--ink-meta) 45%, transparent); }
  .dot.stopped { background: transparent; box-shadow: inset 0 0 0 1.5px var(--border-strong); }
  .dot.done { background: var(--matron); box-shadow: 0 0 6px color-mix(in srgb, var(--matron) 60%, transparent); }
  .dot.question { background: var(--warden); box-shadow: 0 0 7px color-mix(in srgb, var(--warden) 70%, transparent); }
  .dot.stalled { background: var(--bad); }
  @keyframes drawer-pulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.35; }
  }
  @media (prefers-reduced-motion: reduce) { .dot.working { animation: none; } }
  .tag {
    display: inline-block;
    margin-left: 7px;
    padding: 1px 6px;
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: calc(8.5px * var(--tscale-ui));
    letter-spacing: 0.08em;
    vertical-align: 1px;
    border: 1px solid currentColor;
  }
  .tag.working { color: var(--dreamer); }
  .tag.done { color: var(--matron); }
  .tag.question { color: var(--warden); }
  .tag.stalled { color: var(--bad); }
  .srow.stalled { background: color-mix(in srgb, var(--bad) 7%, transparent); }
  /* Lit rows/headers: inset so the ring fits inside the list, a filled tint so
     the row itself lights up, and the shared work-glow flash on top. */
  .srow.lit,
  .sec-head.lit {
    border-radius: 8px;
    margin: 3px 8px;
    border-bottom-color: transparent;
    --work-ring: 1.5px;
    --work-halo: 12px;
    --work-glow: 70%;
  }
  .srow.lit.lvl1, .sec-head.lit.lvl1 { margin-left: 22px; }
  .srow.lit.lvl2 { margin-left: 38px; }
  .srow.lit.lvl1 { padding-left: 12px; }
  .srow.lit.lvl2 { padding-left: 12px; }
  .sec-head.lit.lvl1 { padding-left: 12px; }
  .sec-head.lit { position: static; }
  .lit.working { --work-halo-color: var(--dreamer); background: color-mix(in srgb, var(--dreamer) 14%, transparent); }
  .lit.question { --work-halo-color: var(--warden); --work-speed: 0.6s; background: color-mix(in srgb, var(--warden) 18%, transparent); }
  .lit.done { --work-halo-color: var(--matron); --work-speed: 1.6s; background: color-mix(in srgb, var(--matron) 16%, transparent); }
  .lit.working .st, .lit.working .sec-name { color: var(--dreamer); }
  .lit.question .st, .lit.question .sec-name { color: var(--warden); }
  .lit.done .st, .lit.done .sec-name { color: var(--matron); }
  .wrow.sub { padding-left: 18px; }
  .wrow.sub .wname::before { content: '› '; color: var(--ink-meta); }
  .sub-new { margin: 4px 0 6px 18px; }

  /* --- workspaces ------------------------------------------------------- */
  .ws { border-bottom: 1px solid var(--border); flex-shrink: 0; }
  .ws-toggle {
    display: flex;
    align-items: center;
    gap: 7px;
    width: 100%;
    padding: 9px 14px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.16em;
    color: var(--ink-meta);
    text-align: left;
  }
  .ws-toggle:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .ws-toggle .chev { transition: transform 0.15s ease; display: inline-block; }
  .ws-toggle .chev.open { transform: rotate(90deg); }
  .ws-count { color: var(--border-strong); letter-spacing: 0; }
  .ws-body { padding: 0 14px 12px; }
  .wrow {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 0;
    border-top: 1px solid var(--border);
  }
  .winfo { flex: 1; min-width: 0; }
  .wname {
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .wsub { font-family: var(--font-mono); font-size: calc(9px * var(--tscale-ui)); color: var(--ink-meta); margin-top: 1px; }
  .wsub.link { padding: 0; text-align: left; cursor: pointer; }
  .wsub.link:hover { color: var(--dreamer); text-decoration: underline; }
  .wrow .ico {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 3px 6px;
    font-size: calc(10px * var(--tscale-ui));
    line-height: 1;
    flex-shrink: 0;
  }
  .wrow .ico:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .wrow .ok:hover { color: var(--sprite); border-color: var(--sprite); }
  .wrow .del:hover { color: var(--bad); border-color: var(--bad); }
  .wrow .del:disabled { color: var(--border-strong); border-color: var(--border); cursor: default; }
  .wrow .del:disabled:hover { background: none; color: var(--border-strong); border-color: var(--border); }
  .wrow .del.armed {
    color: var(--bad);
    border-color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
  }
  .ws-edit,
  .ws-new input {
    flex: 1;
    min-width: 0;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    padding: 5px 8px;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
  }
  .ws-edit:focus,
  .ws-new input:focus { outline: none; border-color: var(--dreamer); }
  .ws-new { display: flex; gap: 6px; align-items: center; margin-top: 10px; }
  .ws-add {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    padding: 5px 9px;
    flex-shrink: 0;
  }
  .ws-add:hover:not(:disabled) { color: var(--dreamer); border-color: var(--dreamer); }
  .ws-add:disabled { color: var(--border-strong); border-color: var(--border); cursor: default; }
  .ws-empty { font-family: var(--font-mono); font-size: calc(10px * var(--tscale-ui)); color: var(--ink-meta); padding: 8px 0; }

  /* Compact: the narrow 280px column inside the full-screen pulse window. */
  .tree.compact { width: 280px; }
  .tree.compact .drawer-head { padding: 8px; }
  .tree.compact .srow { padding-left: 10px; padding-right: 8px; }
  .tree.compact .srow.lvl1 { padding-left: 26px; }
  .tree.compact .srow.lvl2 { padding-left: 42px; }
  .tree.compact .sec-head { padding-left: 8px; padding-right: 8px; }
  .tree.compact .sec-head.lvl1 { padding-left: 22px; }
  .tree.compact .ws-toggle { padding-left: 8px; padding-right: 8px; }
  .tree.compact .ws-body { padding: 0 8px 10px; }
</style>
