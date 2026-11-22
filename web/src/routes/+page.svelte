<script>
  import { untrack } from 'svelte';
  import {
    getInboxUnreadCount,
    listInbox
  } from '$lib/api.js';
  import { visibleUnreadTotal } from '$lib/inboxlist.js';
  import ChatPane from '$lib/ChatPane.svelte';
  import NoToolsNotice from '$lib/NoToolsNotice.svelte';
  import { toolsLoaded, anyToolAvailable } from '$lib/models.js';
  import { live } from '$lib/live.svelte.js';
  import NewPulseModal from '$lib/NewPulseModal.svelte';
  import GridTabs from '$lib/GridTabs.svelte';
  import { trackDone, markSeen } from '$lib/seen.svelte.js';
  import SessionDrawer from '$lib/SessionDrawer.svelte';
  import Icon from '$lib/Icon.svelte';
  import MobileDeck from '$lib/MobileDeck.svelte';
  import FuelRail from '$lib/FuelRail.svelte';
  import ThemeModal from '$lib/ThemeModal.svelte';
  import AppSettingsModal from '$lib/AppSettingsModal.svelte';
  import ThemePicker from '$lib/ThemePicker.svelte';
  import { appSettings, loadAppSettings, isHubEnabled, isMetricsEnabled } from '$lib/appsettings.svelte.js';
  import ReasoningPanel from '$lib/ReasoningPanel.svelte';
  import Inbox from '$lib/Inbox.svelte';
  import Hub from '$lib/Hub.svelte'; // built-in pages hub (removable)
  import PulsarCatLayer from '$lib/PulsarCat.svelte'; // the cat who lives on the lines
  import { hub, openPage, closePage, loadPins } from '$lib/hubstore.svelte.js';
  import PaneModal from '$lib/PaneModal.svelte';
  import Wall from '$lib/Wall.svelte';
  import MetricsBar from '$lib/MetricsBar.svelte';
  import { wsColor, gridFamily } from '$lib/wscolor.js';
  import { visiblePulsesFor } from '$lib/pulsefilter.js';
  import { wsHidden } from '$lib/wshidden.svelte.js';
  import Clock from '$lib/Clock.svelte';
  import SideRail from '$lib/SideRail.svelte';
  import SidePanel from '$lib/SidePanel.svelte';
  import WatchersFleet from '$lib/WatchersFleet.svelte';
  import { listConditionWatches, getSetting } from '$lib/api.js';
  import {
    uiPrefs,
    COLUMN_OPTIONS,
    setColumns,
    setHideStopped,
    setListDocked,
    setDiceLocked,
    setPaneWidth,
    resetPaneWidth,
    hydrateViewPrefs
  } from '$lib/uiprefs.svelte.js';
  import ColResize from '$lib/ColResize.svelte';
  import { pollWhileVisible } from '$lib/pollvisible.js';
  import { readCachedActive, applyProfile, builtinProfile, loadActiveProfile, randomProfile, saveActiveProfile } from '$lib/theme.js';
  import { doubleTap } from '$lib/doubletap.js';
  import { startTapLog } from '$lib/taplog.js';
  import { isPulseDrag, PULSE_MIME } from '$lib/dnd.js';
  import { readDeepLink, readEmbedLink, resolveSurface, MOBILE_LOCATION_KEY } from '$lib/surface-handler.js';
  import { createNotifReveal } from '$lib/notifreveal.svelte.js';
  import { createWindowStack, panesInDomOrder } from '$lib/windowstack.svelte.js';
  import { createRoster } from '$lib/roster.svelte.js';
  import { revealPlan } from '$lib/reveal.js';

  // True once the daemon's /api/models response confirms zero supported agent
  // tools were found on this machine.
  const noToolsFound = $derived(toolsLoaded() && !anyToolAvailable());

  // Theme: paint the cached profile instantly, then reconcile with
  // the daemon settings KV (the source of truth). localStorage is paint-only.
  let showTheme = $state(false);
  let showSettings = $state(false);

  $effect(() => {
    loadAppSettings();
  });

  // Bulk archiving lives in the session drawer (select-all → archive).
  $effect(() => {
    if (embedMode && embedDec.theme) {
      const named = builtinProfile(embedDec.theme);
      if (named?.colors) {
        applyProfile(named);
        return;
      }
      console.log('[theme] embed profile not found', embedDec.theme, '→ device pick');
    }
    const cached = readCachedActive();
    if (cached) applyProfile(cached); // instant paint before the KV round-trip
    (async () => {
      const active = await loadActiveProfile();
      applyProfile(active);
    })();
  });

  // 🎲 nav button: roll a random theme (see randomProfile's contrast rules),
  // keep the current bg image + sizes, persist so a reload keeps the roll.
  async function rollTheme() {
    const active = await loadActiveProfile();
    const p = {
      ...randomProfile(),
      bg: active?.bg ?? undefined,
      text: active?.text ?? undefined,
      msg: active?.msg
    };
    applyProfile(p);
    await saveActiveProfile(p);
  }

  // Double-tap the dice to LOCK it (prevents accidental theme rolls on mobile).
  // A single tap only rolls after a 300ms pause proves no second tap is coming;
  // two taps inside the window toggle the lock and never roll. Persisted per device.
  $effect(() => {
    hydrateViewPrefs();
  });
  let diceTapTimer = 0;
  function diceTap() {
    if (diceTapTimer) {
      clearTimeout(diceTapTimer);
      diceTapTimer = 0;
      setDiceLocked(!uiPrefs.diceLocked);
      return;
    }
    diceTapTimer = setTimeout(() => {
      diceTapTimer = 0;
      if (!uiPrefs.diceLocked) rollTheme();
    }, 300);
  }

  // Mobile mode: viewport < 700px -> one pulse full-screen + swipe deck.
  // Desktop (>= 700px) keeps the exact grid behavior, no regressions.
  let isMobile = $state(false);
  $effect(() => {
    if (typeof window === 'undefined') return;
    const mq = window.matchMedia('(max-width: 699px)');
    const apply = () => {
      isMobile = mq.matches;
      console.debug('[layout] mode=%s (innerWidth=%d)', isMobile ? 'mobile' : 'desktop', window.innerWidth);
    };
    apply();
    mq.addEventListener('change', apply);
    return () => mq.removeEventListener('change', apply);
  });

  const roster = createRoster({
    live,
    getVisiblePulses: () => visiblePulses,
    getExtraPulse: () => notifiedExtraPulse,
    getSelectedGrid: () => selectedGrid,
    setSelectedGrid: (id) => {
      selectedGrid = id;
    }
  });
  const pulses = $derived(roster.pulses);
  const grids = $derived(roster.grids);
  const pulsesLoaded = $derived(roster.pulsesLoaded);
  const gridsLoaded = $derived(roster.gridsLoaded);
  const navigationReady = $derived(pulsesLoaded && gridsLoaded);
  const error = $derived(roster.error);
  const gridsError = $derived(roster.gridsError);
  const connected = $derived(roster.connected);
  const moveNote = $derived(roster.moveNote);
  const binArmed = $derived(roster.binArmed);
  const {
    refresh,
    poll,
    refreshGrids,
    onGridsChanged,
    movePane,
    movePulseToWorkspace,
    reorderWorkspaces,
    armWorkspaceDelete,
    cancelBinDelete,
    confirmBinDelete
  } = roster;

  // Workspace attention badges: a workspace chip flashes while any of its
  // pulses is working, then goes SOLID once a pulse finishes a turn and the
  // workspace hasn't been opened. Completed turns are detected client-side
  // from the 3s poll (busy→idle crossing inside a grid); the mark clears when
  // that workspace (or ALL) is opened.
  let gridMarks = $state({});          // grid id -> true when it has unseen work
  let prevBusy = new Map();          // pulse id -> was-busy on the last poll
  // The workspace ids the selected tab puts on screen: a parent tab shows its
  // sub-workspaces too; null = ALL.
  const visibleFamily = $derived(selectedGrid === null ? null : gridFamily(selectedGrid, grids));
  function gridOnScreen(gridId) {
    if (visibleFamily === null) return !wsHidden.ids.includes(gridId);
    return visibleFamily.has(gridId);
  }
  $effect(() => {
    const next = new Map();
    for (const p of pulses) {
      const busy = !!(p.live && (p.live.socket_state === 'busy' || p.live.heartbeat?.state === 'busy'));
      next.set(p.id, busy);
      if (
        prevBusy.get(p.id) === true && // it was working last poll…
        !busy &&                       // …and now it isn't → a turn finished
        p.grid_id &&                   // it lives in a workspace…
        !gridOnScreen(p.grid_id)       // …that isn't the one on screen
      ) {
        if (!gridMarks[p.grid_id]) gridMarks[p.grid_id] = true;
      }
    }
    prevBusy = next;
  });
  // Per-pulse "done but not seen" for the pulse list (seen.svelte.js): a
  // pulse on screen in the selected workspace is being looked at.
  $effect(() => {
    trackDone(pulses, (p) => surface === 'pulses' && !shelved(p) && gridOnScreen(p.grid_id));
  });

  // Opening a workspace counts as "looking" — clear its attention mark (and
  // its sub-workspaces'). ALL shows every pulse, so opening it clears every
  // mark at once.
  function selectGrid(id) {
    if (id) for (const gid of gridFamily(id, grids)) if (gridMarks[gid]) delete gridMarks[gid];
    selectedGrid = id;
  }
  function clearAllGridMarks() {
    if (Object.keys(gridMarks).length) gridMarks = {};
  }
  let showNew = $state(false);
  let showDrawer = $state(false);
  // Desktop: the list is a docked navigation column that stays open while you
  // work, and comes back open after a reload. On phones it is an overlay and
  // is never remembered.
  let dockRestored = false;
  $effect(() => {
    if (isMobile || dockRestored) return;
    dockRestored = true;
    showDrawer = uiPrefs.listDocked;
  });
  $effect(() => {
    const on = showDrawer;
    if (isMobile || !dockRestored) return;
    setListDocked(on);
  });
  // The app icon carries activity: it breathes while any pulse is working and
  // dims when the daemon is unreachable.
  const anyWorking = $derived(pulses.some((p) => p.live?.busy_since));
  // Selected grid tab: null = the implicit "ALL" tab. Persisted to
  // localStorage so backgrounding the app (iOS evicts the page) comes back to
  // the tab you were on, not ALL. Storage is a convenience cache only — a
  // missing/stale value just means starting on ALL.
  const GRID_TAB_KEY = 'pulsar.selectedGrid';
  let selectedGrid = $state(null);
  $effect(() => {
    // One-time restore (runs before the persist effect below, declaration order).
    const saved = localStorage.getItem(GRID_TAB_KEY);
    if (saved) selectedGrid = saved;
  });
  $effect(() => {
    if (selectedGrid === null) localStorage.removeItem(GRID_TAB_KEY);
    else localStorage.setItem(GRID_TAB_KEY, selectedGrid);
  });
  // If the remembered grid was deleted, fall back to ALL — but only once the
  // grid list has actually loaded (an empty list during startup/fetch-failure
  // must not clobber the restored tab).
  $effect(() => {
    if (roster.pendingGridId && grids.some((g) => g.id === roster.pendingGridId)) roster.clearPendingGrid();
    if (selectedGrid !== null && selectedGrid === roster.pendingGridId) return;
    if (selectedGrid !== null && gridsLoaded && !grids.some((g) => g.id === selectedGrid)) {
      console.log('[grid] restored tab no longer exists, falling back to ALL', selectedGrid);
      selectedGrid = null;
    }
  });
  // Mobile deck focus request from the activity sidebar (pulse id or null).
  let focusPulseId = $state(null);

  // The overview (wall) is owned by the page — see the block below the pulse
  // filtering, which is what its active-card index is derived from.
  let showWall = $state(false);
  // Which pulse the deck is showing, so the wall can mark that card.
  let deckPulseId = $state(null);

  // Desktop wall column count (`pulsar.gridColumns`) and running-only filter
  // (`pulsar.hideStopped`) live in `uiprefs.svelte.js`.
  const columns = $derived(uiPrefs.columns);
  const hideStopped = $derived(uiPrefs.hideStopped);
  const diceLocked = $derived(uiPrefs.diceLocked);

  // Second click on a pane the list brought up clears it.
  function dismissFromDesk(id) {
    markSeen(id);
    if (openedIds.has(id)) openedIds = new Set([...openedIds].filter((x) => x !== id));
    if (revealedShelvedIds.has(id)) revealedShelvedIds = new Set([...revealedShelvedIds].filter((x) => x !== id));
  }

  // Top-level surface: the pulse grid, Inbox rail, or optional Hub.
  // Persisted like the grid tab so backgrounding the app comes back to it.
  // On load, a URL deep link  /?surface=inbox[&item=<id>]  FORCES the surface
  // and gets stripped from the URL; otherwise the persisted surface restores.
  // A notification tap naming a pulse arrives as /?pulse-focus=<id>: the app
  // opens home and focuses that pulse. The link keeps ?item=<id> as the
  // fallback target — captured here and all three params are stripped at once.
  const SURFACE_KEY = 'pulsar.surface';
  const MOBILE_SURFACE_KEY = 'pulsar.surface.mobile';
  const surfDec = (() => {
    if (typeof window === 'undefined')
      return { surface: 'pulses', fromUrlParam: false, item: null, pulseFocus: null, fallbackItem: null };
    const search = window.location.search;
    const dl = readDeepLink(search);
    // Phone keeps its own surface key because desktop shows Inbox beside panes
    // while phone switches full-screen surfaces.
    const phone = window.matchMedia?.('(max-width: 699px)').matches ?? false;
    const decision = resolveSurface({ saved: localStorage.getItem(phone ? MOBILE_SURFACE_KEY : SURFACE_KEY), url: dl.surface });
    const item = decision.fromUrlParam ? dl.item : null;
    // A pulse-focus link's item is the fallback for an unresolvable pulse.
    const fallbackItem = dl.pulseFocus ? dl.item : null;
    if (decision.fromUrlParam || dl.pulseFocus) {
      // Strip the deep-link params now that we've consumed them.
      try {
        const u = new URL(window.location.href);
        u.searchParams.delete('surface');
        u.searchParams.delete('item');
        u.searchParams.delete('pulse-focus');
        window.history.replaceState(null, '', u.toString());
      } catch {
        /* history may be unavailable in some embedded contexts; non-fatal */
      }
    }
    return { ...decision, item, pulseFocus: dl.pulseFocus, fallbackItem };
  })();
  // Cold start: the inbox item the notification named (fallback target).
  const coldFallbackItem = surfDec.fallbackItem;
  const embedDec = (() => {
    if (typeof window === 'undefined') return { pulse: null, embed: false, theme: null };
    return readEmbedLink(window.location.search);
  })();
  const embedMode = embedDec.embed;
  const embedPulseId = embedDec.pulse;

  let surface = $state(surfDec.surface); // 'pulses' | 'inbox'
  // The deep link's optional &item=<id> — captured BEFORE the params were
  // stripped, handed to Inbox so it opens that item on load.
  let inboxInitialItem = $state(surfDec.item);

  $effect(() => {
    const key = isMobile ? MOBILE_SURFACE_KEY : SURFACE_KEY;
    if (surface === 'pulses') localStorage.removeItem(key);
    else localStorage.setItem(key, surface);
  });

  // Nav inbox unread badge — polls /inbox/unread-count on a slow cadence (the
  // count changes rarely; the 3s grid poll shouldn't pay for it).
  let inboxUnread = $state(0);
  async function refreshInboxUnread() {
    const r = await getInboxUnreadCount();
    if (!r.ok) { console.log('[inbox] unread poll failed', r.error); return; }
    const counts = r.value ?? { total: 0, channels: {}, threads: {} };
    // The badge must equal the number of unread items the Inbox view can
    // actually show. /inbox/unread-count counts EVERY unread row, while the
    // list is a single capped page — fold unseen overflow into per-channel
    // counts so the badge and rendered rows agree, and let the list's own
    // "N+" pill say when there is more.
    const l = await listInbox({ unread_only: true });
    inboxUnread = visibleUnreadTotal(
      counts,
      l.ok ? (l.value ?? []).filter((i) => !i.read_at).length : 0,
    );
    // The Home Screen app badge is the true total — a push counter says how
    // much mail EXISTS, not how many rows one page renders. This is the ONLY
    // place that sets it (D7), so it cannot flip to the capped visible count.
    if (typeof navigator !== 'undefined' && 'setAppBadge' in navigator) {
      if (counts.total > 0) navigator.setAppBadge(counts.total);
      else navigator.clearAppBadge();
    }
  }
  $effect(() => pollWhileVisible(refreshInboxUnread, 15000));
  // Sidebar: the rail's Watches tab opens a side panel; Pulses opens the
  // existing drawer; the rest are surfaces. The watches badge polls slowly to
  // show whether any pulse is waiting on a watch or a condition is met.
  let railPanel = $state(null); // 'watches' | null
  let watchOpen = $state(0);
  let watchMet = $state(0);
  async function refreshWatchBadge() {
    const r = await listConditionWatches(null, 'watching');
    if (!r.ok) {
      console.log('[watches] badge poll failed', r.error);
      return;
    }
    const ws = r.value ?? [];
    watchOpen = ws.length;
    watchMet = ws.filter((w) => !!w.bad).length;
  }
  $effect(() => {
    if (embedMode || (isMobile && !uiPrefs.showMobileSidebar)) return;
    return pollWhileVisible(refreshWatchBadge, 30000);
  });
  const railActive = $derived(railPanel ?? (surface !== 'pulses' ? surface : showDrawer && !isMobile ? 'pulses' : null));
  // The rail owns destinations, brand and usage. The compact workspace
  // header remains on both desktop and sidebar-enabled phones/PWAs.
  const railMode = $derived(!isMobile || uiPrefs.showMobileSidebar);
  function onRailSelect(id) {
    if (id === 'pulses') {
      railPanel = null;
      showDrawer = isMobile ? true : !showDrawer; // desktop: the rail button toggles the dock
      return;
    }
    if (id === 'watches') {
      railPanel = railPanel === id ? null : id;
      return;
    }
    railPanel = null;
    if (id === 'hub' && !isHubEnabled()) return;
    // Hub with a page open: the Hub button goes back to the catalog first.
    if (id === 'hub' && surface === 'hub' && hub.open) {
      closePage();
      return;
    }
    surface = surface === id ? 'pulses' : id;
  }
  // A pinned Hub page tab in the rail: show that page in the content area.
  function onHubPage(slug) {
    if (!isHubEnabled()) return;
    railPanel = null;
    openPage(slug);
    surface = 'hub';
  }
  // Polled only when hub.url is configured, so a page an agent pins appears without a reload.
  $effect(() => {
    if (embedMode || !railMode || !isHubEnabled()) return;
    return pollWhileVisible(loadPins, 15000);
  });
  // If hub is unconfigured, leave any restored 'hub' surface back on pulses.
  $effect(() => {
    if (appSettings.loaded && !isHubEnabled() && surface === 'hub') {
      surface = 'pulses';
    }
  });
  function openFromPanel(pulseId) {
    railPanel = null;
    revealPulse(pulseId, { from: 'panel' });
  }
  // Watchers shelf: watching/paused pulses leave the grid so the workspace
  // only holds live work. They stay in the roster/drawer.
  let revealedShelvedIds = $state(new Set());
  const shelved = (p) => !!p.settings?.mode && !revealedShelvedIds.has(p.id);

  // Deep-link reveal: a pulse named on cold start from /?pulse-focus=<id> (or
  // clicked via an in-app hot link) is retried against the roster until it
  // lands; a pulse outside the current roster (e.g. archived) is resolved by id
  // once before falling back to the inbox item.
  const notifReveal = createNotifReveal({
    initialPulseId: surfDec.pulseFocus,
    initialFallbackItem: coldFallbackItem,
    getRoster: () => ({ pulses, connected, errored: !!error }),
    onReveal: (pulseId) => {
      revealPulse(pulseId, { from: 'panel' });
      showWall = false;
    },
    onFallback: (itemId) => {
      if (itemId) inboxInitialItem = itemId;
      surface = 'inbox';
    },
    onExtraFound: (found) => {
      if (hideStopped && found.state !== 'running') setHideStopped(false);
    }
  });
  const notifiedExtraPulse = $derived(notifReveal.notifiedExtraPulse);
  const { onPulseLinkClick } = notifReveal;

  // The app icon is also the "out of every workspace" drop target — dropping a
  // pane on ALL detaches it, which otherwise needed the drawer's select.
  let brandDrop = $state(false);
  function onBrandDragOver(e) {
    if (!isPulseDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    brandDrop = true;
  }
  function onBrandDrop(e) {
    brandDrop = false;
    if (!isPulseDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    const id = e.dataTransfer.getData(PULSE_MIME) || e.dataTransfer.getData('text/plain');
    if (id) movePulseToWorkspace(id, null);
  }

  // A notification target resolved outside the roster (an archived pulse) is
  // appended, so the pane the tap opened stays on screen across roster
  // refreshes (the polled/live roster never carries archived rows).
  const renderedPulses = $derived(
    notifiedExtraPulse && !pulses.some((p) => p.id === notifiedExtraPulse.id)
      ? [...pulses, notifiedExtraPulse]
      : pulses
  );
  const renderedGridPulses = $derived(
    renderedPulses.filter(
      (p) => !shelved(p) && (!hideStopped || p.state === 'running')
    )
  );
  // ALL view: panes grouped by workspace in tab order so one screen shows
  // every workspace as a colored block; wsColor() gives each pane its outline.
  // Manual/notification opens must not bypass a named workspace's membership.
  // focusPulse switches to ALL when opening a pulse outside the selected tab.
  const visiblePulses = $derived(
    visiblePulsesFor({
      selectedGrid,
      visibleFamily,
      renderedGridPulses,
      grids,
      wsHidden,
      openedIds,
      notifiedExtraPulse
    })
  );

  // Never render more columns than there are panes — picking 10 with 4 open
  // would otherwise leave 6 dead tracks and squeeze the panes for nothing.
  const effectiveColumns = $derived(Math.max(1, Math.min(columns, visiblePulses.length || 1)));

  // --- expanded pane windows ---------------------------------------------- //
  // On a multi-row grid every pane is squeezed, so each header grows an expand
  // button that pops that pane out into a floating, draggable window — no
  // scrim, so several can be open at once over the live grid. Array order is
  // stacking order: interact with a window and it moves to the end (front).
  const multiRow = $derived(visiblePulses.length > effectiveColumns);
  // The desktop Inbox is a COLUMN NEXT TO the grid, so the panes
  // are still on screen there and the window button must still work.
  const gridSurface = $derived(
    surface === 'pulses' || (!isMobile && surface === 'inbox')
  );
  const windowStack = createWindowStack({
    getContextKey: () => [selectedGrid, gridSurface ? 'grid' : surface]
  });
  const {
    openPane: openExpanded,
    openFullPane: openFullExpanded,
    movePane: moveExpanded,
    frontPane: frontExpanded,
    closePane: closeExpanded
  } = windowStack;

  // App full-screen mode: a sidebar toggle outside the window stack so trips
  // to Hub or Inbox do not close it; back on the pulse surface the same pulse
  // stays full screen. Mode and pulse persist per browser.
  const FS_KEY = 'pulsar.fsMode';
  function loadFs() {
    try {
      return JSON.parse(localStorage.getItem(FS_KEY) ?? 'null') ?? { on: false, id: null };
    } catch {
      return { on: false, id: null };
    }
  }
  let fs = $state(loadFs());
  $effect(() => {
    try {
      localStorage.setItem(FS_KEY, JSON.stringify({ on: fs.on, id: fs.id }));
    } catch {
      /* private mode: session only */
    }
  });
  const fsPulse = $derived(
    fs.on ? (pulses.find((p) => p.id === fs.id) ?? visiblePulses[0] ?? pulses[0] ?? null) : null
  );
  function enterFs(p) {
    fs = { on: true, id: p?.id ?? fs.id };
    // The Inbox column lives beside the full-screen window, so only surfaces
    // that replace the grid switch back to pulses.
    if (!gridSurface) surface = 'pulses';
  }
  function toggleFsMode() {
    if (fs.on) fs = { ...fs, on: false };
    else enterFs(null);
  }

  function onExpandedKey(e) {
    // A composer can own Ctrl/⌘+Enter.
    if (e.defaultPrevented) return;
    windowStack.onEscape(e);
    // Desktop keyboard: Ctrl+Enter (⌘+Enter) opens the new pulse window.
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter' && !isMobile && !embedMode && !showNew) {
      e.preventDefault();
      showNew = true;
    }
  }

  // --- the overview (wall) ------------------------------------------------ //
  // Lives here rather than inside MobileDeck because the app nav opens it too
  // even when no deck or title bar is mounted on screen.
  // Double-tapping the nav anywhere off a button toggles it from any surface.
  const wallActiveIndex = $derived(visiblePulses.findIndex((p) => p.id === deckPulseId));

  const detectNavDoubleTap = doubleTap(() => {
    showWall = !showWall;
    console.debug('[wall] %s — double-tap nav', showWall ? 'OPEN' : 'CLOSE');
  });

  function onNavPointerUp(e) {
    // Desktop has the grid and the workspace chips already; the wall is the
    // phone's navigation surface.
    if (isMobile) detectNavDoubleTap(e);
  }

  // By id, not index: the roster refreshes every few seconds and can reorder
  // or add cards between paint and tap, so an index could name the wrong pulse.
  function pickFromWall(id) {
    const p = visiblePulses.find((x) => x.id === id);
    showWall = false;
    if (!p) return;
    surface = 'pulses'; // the deck only exists on the pulse surface
    focusPulseId = p.id;
    console.debug('[wall] card tapped → focus pulse %s', p.id);
  }

  // Grid poll every 3s.
  $effect(() => {
    // Startup is intentionally untracked. refresh()/refreshGrids() synchronously
    // read live.connected before their first await; tracking that here makes
    // every socket open tear this effect down, disconnect it, and immediately
    // start another one — a reconnect storm that starves mobile rendering.
    untrack(() => {
      live.connect();
      refresh();
      refreshGrids();
    });
    // poll() skips the GET while /live roster frames are fresh.
    const id = setInterval(poll, 3000);
    const gid = setInterval(refreshGrids, 5000);
    return () => {
      clearInterval(id);
      clearInterval(gid);
      live.disconnect();
    };
  });

  /**
   * Bring a pulse on screen and focus it. Make sure the pane exists first: the
   * pulse surface, and a workspace tab that includes it. Desktop → scroll the
   * pane into view + brief highlight; mobile → focus it in the deck (reuse
   * MobileDeck's focus mechanism).
   *
   * `gridId` is the pulse's own workspace, for callers that already know it —
   * a pulse created a moment ago may not have landed in `pulses` yet.
   */
  // Every pulse opened from the list is shown unconditionally (shelved, hidden
  // workspace, or stopped under "running only"), and manually opened panes
  // accumulate until explicitly dismissed.
  let openedIds = $state(new Set());
  // TEMPORARY: trace phone taps to the daemon (see taplog.js).
  $effect(() => { if (isMobile) startTapLog(); });
  let mobileLocationRestored = $state(false);
  // The daemon copy of the phone's last pane (MobileDeck writes it). iOS can
  // drop localStorage, and Safari and the home-screen app do not share it.
  // undefined = still asking; a slow daemon gives up after 2s.
  let serverLocation = $state(undefined);
  $effect(() => {
    if (!isMobile || serverLocation !== undefined) return;
    const giveUp = setTimeout(() => { if (serverLocation === undefined) serverLocation = null; }, 2000);
    getSetting(MOBILE_LOCATION_KEY).then((r) => {
      if (serverLocation === undefined) serverLocation = r.ok && r.value?.pulse ? r.value : null;
    });
    return () => clearTimeout(giveUp);
  });
  $effect(() => {
    if (!isMobile || !navigationReady || mobileLocationRestored || serverLocation === undefined) return;
    mobileLocationRestored = true;
    // Explicit notification/embed links win. Normal launch restores the last
    // pane even if it was manually opened outside the workspace's filters.
    if (embedMode || surfDec.pulseFocus || surfDec.fromUrlParam || surface !== 'pulses') return;
    untrack(() => {
      let saved;
      try { saved = localStorage.getItem(`pulsar.deckPulse.${selectedGrid ?? 'all'}`); }
      catch { saved = null; }
      if (!pulses.some((p) => p.id === saved) && serverLocation) saved = serverLocation.pulse;
      const target = pulses.find((p) => p.id === saved);
      if (target) {
        revealPulse(target.id, { gridId: target.grid_id, from: 'drawer' });
      }
    });
  });

  function revealPulse(pulseId, { gridId, from = 'focus', target: targetOverride } = {}) {
    surface = 'pulses'; // the pane only exists on the pulse surface
    // Full-screen mode: the opened pulse takes over the full-screen window.
    if (fs.on && !isMobile && pulses.some((x) => x.id === pulseId)) fs = { on: true, id: pulseId };
    const target = targetOverride !== undefined ? targetOverride : (pulses.find((x) => x.id === pulseId) ?? null);
    const plan = revealPlan(target, {
      from,
      gridId,
      hideStopped,
      revealedShelvedIds,
      selectedGrid,
      visibleFamily
    });
    if (plan.addOpened && !openedIds.has(pulseId)) {
      openedIds = new Set([...openedIds, pulseId]);
    }
    if (plan.revealShelved) {
      revealedShelvedIds = new Set([...revealedShelvedIds, pulseId]); // off the Watchers shelf
    }
    if (plan.untickHideStopped) {
      setHideStopped(false);
    }
    markSeen(pulseId); // looking at it now
    if (plan.clearGrid) {
      selectedGrid = null; // reveal it on the ALL tab
    }

    if (isMobile) {
      focusPulseId = pulseId; // deck picks it up, clears via onfocused
      return;
    }

    // Desktop: wait a tick for any tab switch to render the pane, then scroll +
    // highlight. Retry briefly in case the pane hasn't mounted yet.
    const tryFocus = (attempt = 0) => {
      const el = document.querySelector(`[data-pulse-id="${CSS.escape(pulseId)}"]`);
      if (!el) {
        if (attempt < 5) {
          setTimeout(() => tryFocus(attempt + 1), 60);
        } else {
          console.log('[%s] pane not found to focus', from, pulseId);
        }
        return;
      }
      el.scrollIntoView({ behavior: 'smooth', block: 'nearest', inline: 'nearest' });
      el.classList.add('pulse-focus-flash');
      console.debug('[%s] desktop focus → pane %s', from, pulseId);
      setTimeout(() => el.classList.remove('pulse-focus-flash'), 2500);
      // Opened from the list: focus the composer so a command can be typed immediately.
      if (plan.focusComposer) el.querySelector('.composer textarea')?.focus({ preventScroll: true });
    };
    setTimeout(() => tryFocus(0), 0);
  }

  // Untick "running only" if the target is stopped (mirrors the drawer's
  // onopen guard), then focus the pulse's pane / jump the mobile deck to it.
  function onPulseOpen(pulseId) {
    revealPulse(pulseId, { from: 'panel' });
  }

  function openFromMetrics(pulseId) {
    showWall = false;
    if (isMobile) showDrawer = false;
    railPanel = null;
    windowStack.clearPanes();
    revealPulse(pulseId, { from: 'metrics' });
  }

  // Use the create response immediately: refresh() may be a no-op while the
  // live roster is connected, and its next update can arrive after this callback.
  async function onPulseCreated(created) {
    if (!created?.id) return;
    roster.appendPulse(created);
    showWall = false;
    revealPulse(created.id, { gridId: created.grid_id ?? null, from: 'new-pulse', target: created });
    await refresh();
  }

</script>

<div class="app desktop-app" class:embed={embedMode}>
  {#snippet brandMark()}
    <!-- 22px nav mark: inline SVG instead of the 1.46MB PNG. -->
    <svg class="brand-mark" width="22" height="22" viewBox="0 0 32 32" aria-hidden="true" focusable="false">
      <rect width="32" height="32" rx="7" fill="#07061a"/>
      <line x1="4.2" y1="27.8" x2="27.8" y2="4.2" stroke="#3ecbff" stroke-width="1.5" stroke-linecap="round"/>
      <ellipse cx="16" cy="16" rx="9.2" ry="4.3" transform="rotate(-32 16 16)" fill="none" stroke="#ff3dad" stroke-width="1.4"/>
      <circle cx="16" cy="16" r="2.5" fill="#f4f7ff"/>
    </svg>
  {/snippet}
  <!-- Rail mode keeps a compact workspace header on desktop AND phone.
       Brand/usage/actions remain in the rail, without duplicate controls.
       The no-sidebar phone keeps its classic bar. -->
  {#if !embedMode}
  <div
    class="nav"
    class:chips-only={railMode}
    onpointerup={onNavPointerUp}
    role="presentation"
  >
    <!-- Icon-only brand = the ALL button (tapping the app icon goes home to
         all pulses). Rail mode renders it in brandSlot instead. -->
    {#if !railMode}
    <button
      class="brand"
      class:home-on={surface === 'pulses' && selectedGrid === null}
      class:working={anyWorking}
      class:offline={!connected}
      class:drop-on={brandDrop}
      onclick={() => {
        surface = 'pulses';
        selectedGrid = null;
        clearAllGridMarks();
      }}
      ondragover={onBrandDragOver}
      ondragleave={() => (brandDrop = false)}
      ondrop={onBrandDrop}
      title={!connected ? 'daemon unreachable' : anyWorking ? 'working — all pulses' : 'all pulses (drop a pulse here to take it out of its workspace)'}
      aria-label="all pulses"
    >{@render brandMark()}</button>
    {/if}
    <!-- Destinations the rail already carries (drawer, inbox,
         dice, appearance) — only the no-sidebar phone keeps them here. -->
    {#if isMobile && !railMode}
    <button class="chrome-btn" onclick={() => (showDrawer = true)} title="all pulses" aria-label="all pulses">
      <Icon name="drawer" size={16} stroke={2} />
    </button>
    <button
      class="chrome-btn inbox-icon"
      class:surface-on={surface === 'inbox'}
      onclick={() => (surface = surface === 'inbox' ? 'pulses' : 'inbox')}
      title="Inbox — async comms rail"
      aria-label="Inbox"
      aria-pressed={surface === 'inbox'}
    >
      <Icon name="inbox" size={16} stroke={1.8} />
      {#if inboxUnread > 0}
        <span class="inbox-badge" aria-label={`${inboxUnread} unread`}>{inboxUnread > 99 ? '99+' : inboxUnread}</span>
      {/if}
    </button>
    {/if}
    <!-- Workspaces have no rail home: keep their header when the phone
         sidebar is enabled, just like desktop. -->
    {#if railMode}
      <GridTabs
        {grids}
        {pulses}
        unseen={gridMarks}
        selected={selectedGrid}
        onselect={selectGrid}
        oncreated={onGridsChanged}
        onmovepulse={movePulseToWorkspace}
        onreorder={reorderWorkspaces}
        ondelete={armWorkspaceDelete}
      />
    {/if}
    <div class="spacer"></div>
    <!-- Theme randomizer: one tap rolls + persists a whole random palette.
         The appearance window's 🎲 RANDOM pill stays too — same roll. -->
    {#if isMobile && !railMode}
    <button
      class="chrome-btn dice-icon"
      class:locked={diceLocked}
      onclick={diceTap}
      title={diceLocked ? 'dice locked — double-tap to unlock' : 'random theme — double-tap to lock'}
      aria-label={diceLocked ? 'dice locked, double-tap to unlock' : 'random theme'}
      aria-pressed={diceLocked}
      data-testid="theme-dice">
      <Icon name="dice" size={16} stroke={1.8} />
      {#if diceLocked}<span class="dice-lock" aria-hidden="true">🔒</span>{/if}
    </button>
    <button class="chrome-btn" onclick={() => (showTheme = true)} title="appearance" aria-label="appearance">
      <Icon name="settings" size={16} stroke={1.8} />
    </button>
    <button class="chrome-btn" onclick={() => (showSettings = true)} title="settings" aria-label="settings">
      <Icon name="gear" size={16} stroke={1.8} />
    </button>
    {/if}
    <!-- The clock is a flex child of the nav row (on no-sidebar phone) so it
         never overlaps adjacent controls and collapses with the row. In rail
         mode it lives in the rail footer alongside the meter. -->
    {#if !railMode}
    <Clock />
    {/if}
    <!-- Usage pips ride inline in the nav row on no-sidebar phones; in rail
         mode both the meter and the + button live in the rail. -->
    {#if !railMode}
      <FuelRail />
      <button class="new-session" onclick={() => (showNew = true)} title="new pulse" aria-label="new pulse">
        <Icon name="new" size={16} stroke={2.4} />
      </button>
    {/if}
  </div>
  {/if}

  <!-- Optional metrics stat strip: shown only when `metrics.url` is configured
       and enabled in Appearance. -->
  {#if uiPrefs.showMetricsBar && isMetricsEnabled()}
    <MetricsBar {pulses} onopenpulse={openFromMetrics} />
  {/if}

  {#if error}
    <div class="top-err">⚠ grid: {error}</div>
  {/if}
  {#if gridsError}
    <div class="top-err">⚠ workspaces: {gridsError}</div>
  {/if}
  {#if pulsesLoaded && !connected && !error}
    <div class="reconnect-pill" role="status">Reconnecting…</div>
  {/if}
  {#if moveNote}
    <div class="top-note">↦ {moveNote}</div>
  {/if}
  {#if binArmed}
    <div class="top-confirm">
      <span>delete workspace “{binArmed.name}”? its pulses move out — none are deleted</span>
      <button class="confirm-yes" onclick={confirmBinDelete}>delete</button>
      <button class="confirm-no" onclick={cancelBinDelete}>cancel</button>
    </div>
  {/if}

  <!-- Everything below the nav. It is the wall's positioning context, so the
       overview covers the surface but leaves the nav reachable underneath the
       gesture that opened it. -->
  <div class="body-wrap">
    <!-- Desktop always; phone when the “Sidebar on phone” checkbox is on.
         The sidebar carries the destinations, the brand and the usage meter;
         the + floats over the content's right edge. -->
    {#if (!isMobile || uiPrefs.showMobileSidebar) && !embedMode}
      <!-- The app icon: ALL/home button, moved out of the header. Same
           handlers/drag-drop as the nav brand it replaces. -->
      {#snippet railBrand()}
        <button
          class="rail-brand"
          class:home-on={surface === 'pulses' && selectedGrid === null}
          class:working={anyWorking}
          class:offline={!connected}
          class:drop-on={brandDrop}
          onclick={() => {
            surface = 'pulses';
            selectedGrid = null;
            clearAllGridMarks();
          }}
          ondragover={onBrandDragOver}
          ondragleave={() => (brandDrop = false)}
          ondrop={onBrandDrop}
          title={!connected ? 'daemon unreachable' : anyWorking ? 'working — all pulses' : 'all pulses (drop a pulse here to take it out of its workspace)'}
          aria-label="all pulses"
        >{@render brandMark()}</button>
      {/snippet}
      <!-- The usage meter, out of the header into the rail's bottom cluster.
           Same FuelRail component — tap still opens the full panel. -->
      {#snippet railFuel()}
        <FuelRail />
      {/snippet}
      <!-- Meter and clock share one home in the rail. -->
      {#snippet railClock()}
        <Clock stack={true} />
      {/snippet}
      <SideRail
        active={railActive}
        {inboxUnread}
        {watchOpen}
        {watchMet}
        {diceLocked}
        onselect={onRailSelect}
        ondice={diceTap}
        onnew={() => (showNew = true)}
        fsMode={fs.on}
        onfsmode={isMobile ? null : toggleFsMode}
        hubEnabled={isHubEnabled()}
        hubOpen={surface === 'hub' ? hub.open : null}
        onhubpage={onHubPage}
        brandSlot={railBrand}
        fuelSlot={railFuel}
        clockSlot={railClock}
        detached={isMobile}
      >
        {#snippet viewMenu(closeView)}
          <!-- Hide non-running pulses everywhere, ALL tab included. The same
               toggle lives in the mobile overview; both drive the one persisted
               state. -->
          <label class="hide-stopped" title="hide pulses that are not running">
            <input type="checkbox" checked={hideStopped} onchange={(e) => setHideStopped(e.currentTarget.checked)} />
            <span>running only</span>
          </label>
          {#if !isMobile}
            <div class="view-row">
              <span class="view-lbl">columns</span>
              <div class="col-picker" role="group" aria-label="wall columns">
                {#each COLUMN_OPTIONS as n (n)}
                  <button
                    class="col-btn"
                    class:on={columns === n}
                    type="button"
                    onclick={() => setColumns(n)}
                    aria-pressed={columns === n}
                    aria-label="{n} column{n === 1 ? '' : 's'}"
                  >{n}</button>
                {/each}
              </div>
            </div>
          {/if}
          <button class="view-btn" onclick={() => { closeView?.(); showTheme = true; }}>
            <Icon name="settings" size={13} stroke={1.8} /> appearance…
          </button>
          <button class="view-btn" onclick={() => { closeView?.(); showSettings = true; }}>
            <Icon name="gear" size={13} stroke={1.8} /> settings…
          </button>
        {/snippet}
      </SideRail>
      {#if showDrawer && !embedMode && !isMobile}
        {@render pulseList(true)}
        <ColResize label="resize pulse list" width={uiPrefs.paneWidths.list} min={240}
                   onresize={(w, save) => setPaneWidth('list', w, save)} onreset={() => resetPaneWidth('list')} />
      {/if}
    {/if}
    <div class="body-main">
    {#if embedMode}
      {@const embedPulse = pulses.find((p) => p.id === embedPulseId)}
      {#if embedPulse}
        <ChatPane pulse={embedPulse} onchanged={refresh} />
      {:else if !error}
        <div class="grid-empty">that pulse isn’t here</div>
      {/if}
    {:else if surface === 'inbox' && isMobile}
      <Inbox onopenpulse={onPulseOpen} initialItemId={inboxInitialItem} />
    {:else if surface === 'hub'}
      <Hub />
    {:else if isMobile}
      <MobileDeck
        pulses={visiblePulses}
        ready={navigationReady && mobileLocationRestored}
        noTools={pulses.length === 0 && noToolsFound}
        onchanged={refresh}
        focusId={focusPulseId}
        onfocused={() => (focusPulseId = null)}
        gridKey={selectedGrid ?? 'all'}
        onoverview={() => (showWall = true)}
        onactive={(id) => { if (id !== deckPulseId) console.debug('[deck] active → %s', id); deckPulseId = id; }}
        onopenpulse={onPulseOpen}
      />
    {:else}
      <!-- On desktop the inbox is a column capped at 1000px beside the pulse grid. -->
      <div class="inbox-split">
      <!-- Drag the divider to resize; widths live in uiPrefs.paneWidths. -->
      {#if surface === 'inbox'}
        <div class="inbox-col" style="flex-basis: {uiPrefs.paneWidths.inbox}px;"><Inbox onopenpulse={onPulseOpen} initialItemId={inboxInitialItem} /></div>
        <ColResize label="resize inbox" width={uiPrefs.paneWidths.inbox} min={360}
                   onresize={(w, save) => setPaneWidth('inbox', w, save)} onreset={() => resetPaneWidth('inbox')} />
      {/if}
      <!-- max-width caps each column at --col-max so one or two panes on an
           ultrawide monitor stay readable instead of stretching to 2000px; the
           grid centres itself (margin-inline: auto) once the cap bites. -->
      <!-- The pane area. Full-screen mode lives INSIDE it: one pane fills
           the space the grid has, and the tabs, metrics bar, rail, pulse
           list and Inbox column all stay where they are. The grid stays
           mounted under the stage (hidden, inert) so its panes keep their
           scroll and drafts for the way back. -->
      <div class="pane-area">
      <div
        class="grid-body"
        class:under-fs={!!fsPulse}
        inert={!!fsPulse}
        style="grid-template-columns: repeat({effectiveColumns}, minmax(0, 1fr));
               max-width: calc({effectiveColumns} * var(--col-max) + {effectiveColumns - 1} * 8px);"
      >
        {#each visiblePulses as pulse, i (pulse.id)}
          <ChatPane
            {pulse}
            ws={(selectedGrid === null || visibleFamily.size > 1) && pulse.grid_id ? { name: grids.find((g) => g.id === pulse.grid_id)?.name ?? '', color: wsColor(pulse.grid_id, grids), first: i > 0 && i % effectiveColumns !== 0 && visiblePulses[i - 1].grid_id !== pulse.grid_id } : null}
            onchanged={refresh}
            onreorder={movePane}
            onexpand={multiRow ? openExpanded : undefined}
            onfullscreen={enterFs}
            onopenpulse={onPulseOpen}
              />
        {/each}
        {#if visiblePulses.length === 0 && !error}
          <div class="grid-empty">
            {#if pulses.length === 0 && noToolsFound}
              <NoToolsNotice />
            {:else}
              {pulses.length === 0
                ? 'No pulses yet. Tap ＋ to start one.'
                  : hideStopped
                  ? 'No running pulses here. Untick “running only” to see the rest.'
                  : 'No pulses in this workspace. Tap ＋ or tap the app icon for all pulses.'}
            {/if}
          </div>
        {/if}
      </div>
      {#if fsPulse}
        <div class="fs-stage">
          {#key fsPulse.id}
            <ChatPane
              pulse={fsPulse}
              fsStage
              onchanged={refresh}
              onfullscreen={toggleFsMode}
              onopenpulse={onPulseOpen}
            />
          {/key}
        </div>
      {/if}
      </div>
      </div>
    {/if}

    </div>
    {#if isMobile && showWall && !embedMode}
      <Wall
        pulses={visiblePulses}
        {grids}
        {selectedGrid}
        onselectgrid={(id) => (selectedGrid = id)}
        {hideStopped}
        ontogglehidestopped={() => setHideStopped(!hideStopped)}
        onmovepulse={movePulseToWorkspace}
        onreorderpulses={movePane}
        onreordergrids={reorderWorkspaces}
        ondeletegrid={armWorkspaceDelete}
        activeIndex={wallActiveIndex}
        onpick={pickFromWall}
        onnew={() => { showWall = false; showNew = true; }}
        onclose={() => { showWall = false; console.debug('[wall] CLOSE — tap-outside'); }}
      />
    {/if}
  </div>
</div>

<svelte:window onkeydown={onExpandedKey} />
<svelte:document onclick={onPulseLinkClick} />

{#if gridSurface && !isMobile && !embedMode}
  <!-- Expanded pane windows float over the grid, newest on top. A window whose
       pulse left the list (archived, deleted) simply renders nothing. -->
  {#each panesInDomOrder(windowStack.panes) as { pane: m, z } (m.win)}
    {@const mp = pulses.find((p) => p.id === m.id)}
    {#if mp}
      <PaneModal
        pulse={mp}
        x={m.x}
        y={m.y}
        {z}
        full={m.full ?? false}
        onchanged={refresh}
        onclose={() => closeExpanded(m.id)}
        onfront={() => frontExpanded(m.id)}
        onmove={(pos) => moveExpanded(m.id, pos)}
        onopenpulse={onPulseOpen}
      />
    {/if}
  {/each}
{/if}

{#if showNew && !embedMode}
  <NewPulseModal
    {grids}
    activeGrid={selectedGrid}
    onclose={() => (showNew = false)}
    oncreated={onPulseCreated}
  />
{/if}

{#if railPanel && (!isMobile || uiPrefs.showMobileSidebar) && !embedMode}
  <SidePanel title="Watchers" onclose={() => (railPanel = null)}>
    <WatchersFleet {pulses} onopenpulse={openFromPanel} onchanged={refresh} />
  </SidePanel>
{/if}

{#snippet pulseList(docked)}
  <SessionDrawer
    {docked}
    rosterPulses={pulses}
    width={uiPrefs.paneWidths.list}
    onmovepulse={movePulseToWorkspace}
    onreorderworkspaces={reorderWorkspaces}
    onopengrid={(id, created) => {
      if (created) onGridsChanged(id); // keep the new tab through the dead-tab reset
      selectGrid(id);
      surface = 'pulses';
      if (!docked) showDrawer = false;
    }}
    {grids}
    side={isMobile ? 'right' : 'left'}
    onclose={() => (showDrawer = false)}
    onchanged={refresh}
    ongridschanged={onGridsChanged}
    onopen={(p) => {
      if (!docked) showDrawer = false;
      // Full-screen mode: the click switches the full-screen pulse.
      if (fs.on && gridSurface && !isMobile) {
        enterFs(p);
        return;
      }
      // The list click is a toggle: a pane the list brought up goes away on
      // the second click, even when shelved or in a hidden workspace.
      const onScreen = document.querySelector(`[data-pulse-id="${CSS.escape(p.id)}"]`);
      if (onScreen && openedIds.has(p.id)) {
        dismissFromDesk(p.id);
        return;
      }
      revealPulse(p.id, { gridId: p.grid_id, from: 'drawer' });
    }}
  />
{/snippet}

{#if showDrawer && !embedMode && isMobile}
  {@render pulseList(false)}
{/if}

<ThemePicker />
{#if showTheme && !embedMode}
  <ThemeModal onclose={() => (showTheme = false)} />
{/if}
{#if showSettings && !embedMode}
  <AppSettingsModal onclose={() => (showSettings = false)} />
{/if}

<!-- The reasoning panel is opened from a pane's working status strip or a
     wall card's working label; it owns its own open/close state. Rendered in
     embed mode too, so a locked single-pulse pane can still show its trace. -->
<ReasoningPanel />

<PulsarCatLayer />

<style>
  .app {
    --rail-w: 44px;
    --nav-h: 48px;
    background: var(--bg);
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    overflow: hidden;
    position: relative;
  }
  .app:has(> .nav.chips-only) { --nav-h: 34px; }
  .desktop-app {
    margin: 0 auto;
    /* dvh, not vh: on mobile 100vh is the LARGE viewport (browser chrome
       retracted), which is taller than what you can actually see — the shell
       overflowed the screen and the document became a scroller. */
    height: 100dvh;
    display: flex;
    flex-direction: column;
  }
  /* Embedded iframe mode locks one pulse full-bleed. */
  .app.embed {
    border: none;
    border-radius: 0;
  }
  .app.embed .body-wrap :global(.pane) {
    flex: 1;
    border: none;
    border-radius: 0;
    min-height: 0;
  }
  /* Desktop is edge-to-edge, so the shell's frame would just be a hairline
     against the window border — drop it. Mobile keeps the rounded frame. */
  @media (min-width: 700px) {
    .app.desktop-app { border: none; border-radius: 0; }
  }
  /* Holds everything under the nav, and is what the overview stretches over
     (position:absolute inset:0) — so the nav stays visible and tappable while
     the overview is up. */
  .body-wrap {
    flex: 1;
    display: flex;
    /* Row: [SideRail | body-main]. On the phone there is no rail unless the
       appearance-window "Sidebar on phone" checkbox is on. */
    flex-direction: row;
    min-height: 0;
    position: relative;
  }
  .inbox-split {
    flex: 1;
    display: flex;
    gap: 8px;
    min-height: 0;
    min-width: 0;
  }
  .inbox-col {
    flex: 0 1 1000px;
    min-width: 0;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  .pane-area { flex: 1; min-width: 0; min-height: 0; display: flex; position: relative; }
  .pane-area > .grid-body { min-width: 0; }
  .pane-area > .grid-body.under-fs { visibility: hidden; }
  .fs-stage { position: absolute; inset: 0; display: flex; padding: 8px 0; }
  .fs-stage :global(.pane) { flex: 1; min-width: 0; }
  .body-main {
    flex: 1;
    display: flex;
    flex-direction: column;
    min-height: 0;
    min-width: 0;
    position: relative;
  }
  /* View popover contents (rendered inside the rail, styled here because the
     snippet is this file's markup). */
  .view-row { display: flex; align-items: center; justify-content: space-between; gap: 10px; }
  .view-lbl {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .view-btn {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 5px 8px;
    border-radius: 6px;
    border: 1px solid var(--border);
    color: var(--ink-dim);
    font-size: calc(12px * var(--tscale-ui));
  }
  .view-btn:hover { background: var(--surface-warm); color: var(--ink); }
  .nav {
    display: flex;
    align-items: center;
    gap: 14px;
    height: 48px;
    padding: 0 12px;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    flex-shrink: 0;
    /* The bar carries the double-tap that opens the overview: without these
       the gesture brings its own mess — double-CLICK selects a word and
       double-TAP asks the browser to zoom. */
    user-select: none;
    -webkit-user-select: none;
    touch-action: manipulation;
  }
  /* Compact workspace header, including phones/PWAs with the sidebar on. */
  .nav.chips-only { height: 34px; }
  .brand {
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 3px;
    border-radius: 8px;
    flex-shrink: 0;
  }
  .brand.home-on {
    box-shadow: 0 0 0 1px var(--dreamer), 0 0 10px color-mix(in srgb, var(--dreamer) 45%, transparent);
  }
  /* A pulse is hovering over the app icon: dropping takes it out of its
     workspace. */
  .brand.drop-on {
    box-shadow: 0 0 0 1px var(--sprite), 0 0 10px color-mix(in srgb, var(--sprite) 55%, transparent);
  }
  .brand-mark { border-radius: 5px; flex-shrink: 0; display: block; }
  /* Working: the icon flashes toward white. Colour only — no transform, so it
     does not nudge adjacent nav items. */
  .brand.working .brand-mark { animation: brand-flash 1.8s ease-in-out infinite; }
  @keyframes brand-flash {
    0%, 100% { filter: none; }
    50% {
      filter: brightness(2.4) saturate(0.35)
              drop-shadow(0 0 6px rgb(255 255 255 / 0.55));
    }
  }
  /* Daemon unreachable: drain the icon instead of blinking a red dot. */
  .brand.offline .brand-mark { filter: grayscale(1) brightness(0.55); animation: none; }
  @media (prefers-reduced-motion: reduce) {
    .brand.working .brand-mark {
      animation: none;
      filter: brightness(1.5) drop-shadow(0 0 5px rgb(255 255 255 / 0.4));
    }
  }
  .nav .spacer { flex: 1; }
  /* Phone nav: tighter gaps and padding keep every control on screen. */
  @media (max-width: 700px) {
    .nav { gap: 5px; padding: 0 8px; }
    .nav .chrome-btn { padding: 4px 2px; }
    .nav .new-session { flex-shrink: 0; }
  }
  @media (max-width: 340px) {
    /* Keep the rightmost new-pulse button inside older/narrow phone screens. */
    .nav { gap: 3px; padding-inline: 5px; }
  }
  .chrome-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    padding: 4px 6px;
    border-radius: 6px;
  }
  .chrome-btn:hover { color: var(--ink-dim); background: var(--surface-warm); }
  /* Nav icons carry their own hue, muted toward ink-meta so the bar stays
     quiet; hover lifts to the full palette color (same rule as the pane head). */
  /* Inbox nav icon: magenta (--user) so it reads as "your messages", with a
     live unread-count badge. Hover lifts to the full accent. */
  .inbox-icon { color: color-mix(in srgb, var(--user) 60%, var(--ink-meta)); position: relative; }
  .inbox-icon:hover { color: var(--user); }
  .inbox-badge {
    position: absolute;
    top: -2px;
    right: -3px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 17px;
    height: 17px;
    padding: 0 5px;
    border-radius: 9px;
    background: var(--user);
    color: var(--page);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    font-weight: 700;
    line-height: 1;
  }
  .dice-icon { color: color-mix(in srgb, var(--user) 60%, var(--ink-meta)); position: relative; }
  .dice-icon:hover { color: var(--user); }
  .dice-icon.locked { color: var(--ink-meta); opacity: 0.55; }
  .dice-lock {
    position: absolute;
    right: -1px;
    bottom: -2px;
    font-size: 8px;
    line-height: 1;
  }
  .hide-stopped {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .hide-stopped:hover { color: var(--ink-dim); }
  .hide-stopped input { accent-color: var(--dreamer); }
  .hide-stopped:has(input:checked) { color: var(--dreamer); }
  .col-picker { display: flex; align-items: center; gap: 2px; flex-shrink: 0; }
  .col-btn {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-meta);
    padding: 3px 5px;
    min-width: 18px;
    border-radius: 4px;
    border: 1px solid transparent;
  }
  .col-btn:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .col-btn.on {
    color: var(--dreamer);
    border-color: color-mix(in srgb, var(--dreamer) 55%, transparent);
    background: color-mix(in srgb, var(--dreamer) 10%, transparent);
  }
  .chrome-btn.surface-on {
    color: var(--dreamer);
    box-shadow: 0 0 0 1px var(--dreamer), 0 0 10px color-mix(in srgb, var(--dreamer) 45%, transparent);
  }

  /* Brief highlight when a pane is focused from the activity sidebar (desktop). */
  :global(.pane.pulse-focus-flash) {
    animation: paneFlash 1.4s ease;
  }
  @keyframes paneFlash {
    0%, 100% { box-shadow: none; }
    15% { box-shadow: 0 0 0 2px var(--dreamer), 0 0 22px color-mix(in srgb, var(--dreamer) 55%, transparent); }
    60% { box-shadow: 0 0 0 2px var(--dreamer), 0 0 18px color-mix(in srgb, var(--dreamer) 40%, transparent); }
  }
  .new-session {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 30px;
    height: 30px;
    font-size: calc(16px * var(--tscale-ui));
    color: var(--bg);
    background: var(--sprite);
    border-radius: 999px;
    font-weight: 700;
  }
  /* Floats, so a reconnect never shifts what you were about to click. */
  .reconnect-pill { position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: 50; padding: 4px 12px; border-radius: 999px; background: var(--surface); border: 1px solid var(--border-strong); color: var(--muted, #8b95a8); font-size: 12px; pointer-events: none; }
  .top-err {
    background: color-mix(in srgb, var(--bad) 16%, var(--surface));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    padding: 5px 12px;
    flex-shrink: 0;
  }
  /* Dropping a workspace on the bin arms this rather than deleting outright. */
  .top-confirm {
    display: flex;
    align-items: center;
    gap: 10px;
    background: color-mix(in srgb, var(--bad) 14%, var(--surface));
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    padding: 6px 12px;
    flex-shrink: 0;
  }
  .top-confirm button {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.12em;
    text-transform: uppercase;
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border-strong);
  }
  .top-confirm .confirm-yes {
    color: var(--bad);
    border-color: var(--bad);
    background: color-mix(in srgb, var(--bad) 14%, transparent);
  }
  .top-confirm .confirm-no { color: var(--ink-meta); }
  /* Same strip, good news: a workspace drop confirms itself here, because on the
     ALL tab a moved pane doesn't visibly go anywhere. Clears itself. */
  .top-note {
    background: color-mix(in srgb, var(--sprite) 12%, var(--surface));
    color: var(--sprite);
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    padding: 5px 12px;
    flex-shrink: 0;
  }
  .grid-body {
    flex: 1;
    display: grid;
    /* columns come from the nav picker (inline style); rows always split to fit
       the viewport — the wall never scrolls, you resize the window instead. */
    grid-auto-rows: minmax(0, 1fr);
    gap: 8px;
    /* No side padding: panes run to the window edge so window width IS column
       width until --col-max bites, at which point leftover width becomes even
       side gutters. The 8px stays top/bottom. */
    padding: 8px 0;
    width: 100%;
    margin-inline: auto;
    overflow: hidden;
    background: var(--bg);
  }
  .grid-empty {
    grid-column: 1 / -1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(12px * var(--tscale-ui));
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(0.7); opacity: 0.6; }
  }
</style>
