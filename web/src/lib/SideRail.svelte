<script>
  /**
   * SideRail — the left sidebar carrying destinations and the brand icon: the
   * Pulsar icon sits at the top via brandSlot, tabs below it.
   *
   * Layout zones: destinations contiguous under the brand; spacer (or pinned
   * Hub tabs when hub is configured); the pulse LIST button and + NEW PULSE
   * adjacent; the usage meter as its own bordered block; then a dimmed UTILITY
   * footer always last (view settings, dice last).
   *
   * Optional snippets: brandSlot (top), fuelSlot, viewMenu.
   */
  import Icon from './Icon.svelte';
  import HubGlyph from './HubGlyph.svelte';
  import { hub, unpin } from './hubstore.svelte.js';

  let {
    active = null,            // 'pulses'|'watches'|'inbox'|'hub'|null
    inboxUnread = 0,
    watchOpen = 0,
    watchMet = 0,
    diceLocked = false,
    onselect = () => {},
    ondice = () => {},
    onnew = () => {},         // the + new pulse button (bottom cluster)
    fsMode = false,           // app full-screen mode is on
    onfsmode = null,          // toggle app full-screen mode (null = no button)
    hubEnabled = false,       // true only when `hub.url` setting is configured
    hubOpen = null,           // slug of the Hub page on screen (null = none)
    onhubpage = () => {},     // tap a pinned Hub page tab
    viewMenu,                 // snippet: contents of the view popover
    brandSlot,                // snippet: the app icon (top of the rail)
    fuelSlot,                 // snippet: the usage meter
    clockSlot,                // snippet: the stacked clock (meter + clock, one home)
    detached = false          // true → phone mode: no fixed desktop width
  } = $props();

  let viewOpen = $state(false);

  const ALL_TABS = [
    // 'pulses' (the list) lives in the bottom cluster, not here.
    // Watchers: one shelf for pulses monitoring watches/schedules and paused projects.
    { id: 'watches', icon: 'watch', label: 'Watchers — pulses keeping an eye on things, and paused projects', hue: 'warden' },
    { id: 'inbox', icon: 'inbox', label: 'Inbox — async comms rail', hue: 'user' },
    { id: 'hub', icon: 'hub', label: 'Hub — published pages', hue: 'dreamer' }
  ];
  const visibleTabs = $derived(hubEnabled ? ALL_TABS : ALL_TABS.filter((t) => t.id !== 'hub'));

  // Drag a pinned Hub tab onto the remove target to unpin it.
  // Mouse: the drag starts once the pointer moves.
  // Touch: a 450ms hold starts it; moving first is a scroll.
  const LONG_PRESS_MS = 450;
  const MOUSE_SLOP = 6;
  const TOUCH_SLOP = 10;
  let drag = $state(null);   // {slug, title, x, y, hot} while dragging
  let trashEl = $state(null);
  let press = null;          // the pointer that might become a drag
  let swallowClick = false;  // the click that ends a drag is not a tap

  function overTrash(x, y) {
    const r = trashEl?.getBoundingClientRect();
    return !!r && x >= r.left && x <= r.right && y >= r.top && y <= r.bottom;
  }
  function startDrag(x, y) {
    if (!press) return;
    drag = { slug: press.slug, title: press.title, x, y, hot: false };
    swallowClick = true;
    navigator.vibrate?.(15);
  }
  function onPinMove(e) {
    if (!press || e.pointerId !== press.id) return;
    const far = Math.hypot(e.clientX - press.x0, e.clientY - press.y0);
    if (!drag) {
      if (press.touch) {
        if (far > TOUCH_SLOP) endPress(); // the finger is scrolling the strip
        return;
      }
      if (far <= MOUSE_SLOP) return;
      startDrag(e.clientX, e.clientY);
    }
    drag.x = e.clientX;
    drag.y = e.clientY;
    drag.hot = overTrash(e.clientX, e.clientY);
  }
  function onPinUp(e) {
    if (!press || e.pointerId !== press.id) return;
    if (drag && overTrash(e.clientX, e.clientY)) unpin(drag.slug);
    endPress();
  }
  // While dragging, the finger must not scroll the strip or the page.
  function blockScroll(e) {
    if (drag) e.preventDefault();
  }
  function endPress() {
    clearTimeout(press?.timer);
    press = null;
    drag = null;
    window.removeEventListener('pointermove', onPinMove);
    window.removeEventListener('pointerup', onPinUp);
    window.removeEventListener('pointercancel', endPress);
    window.removeEventListener('touchmove', blockScroll);
    // The click (if any) fires right after pointerup; let it be swallowed,
    // then accept taps again.
    setTimeout(() => (swallowClick = false), 60);
  }
  function onPinDown(e, p) {
    if (e.pointerType === 'mouse' && e.button !== 0) return;
    endPress();
    swallowClick = false;
    const pr = {
      slug: p.slug,
      title: p.title,
      id: e.pointerId,
      x0: e.clientX,
      y0: e.clientY,
      touch: e.pointerType !== 'mouse',
      timer: 0
    };
    press = pr;
    if (pr.touch) pr.timer = setTimeout(() => startDrag(pr.x0, pr.y0), LONG_PRESS_MS);
    window.addEventListener('pointermove', onPinMove);
    window.addEventListener('pointerup', onPinUp);
    window.addEventListener('pointercancel', endPress);
    window.addEventListener('touchmove', blockScroll, { passive: false });
  }
  function onPinClick(slug) {
    if (swallowClick) {
      swallowClick = false;
      return;
    }
    onhubpage(slug);
  }

  function onWinClick(e) {
    if (viewOpen && !e.target.closest?.('.view-wrap')) viewOpen = false;
  }
</script>

<svelte:window onclick={onWinClick} onkeydown={(e) => { if (viewOpen && e.key === 'Escape') viewOpen = false; }} />

<nav class="rail" class:detached aria-label="sidebar">
  {#if brandSlot}
    <div class="brand-wrap">{@render brandSlot()}</div>
  {/if}

  {#each visibleTabs as t (t.id)}
    <button
      class="tab hue-{t.hue}"
      class:on={active === t.id && !(t.id === 'hub' && hubOpen)}
      class:met={t.id === 'watches' && watchMet > 0}
      onclick={() => onselect(t.id)}
      title={t.label}
      aria-label={t.label}
      aria-pressed={active === t.id}
    >
      <Icon name={t.icon} size={17} stroke={1.8} />
      {#if t.id === 'inbox' && inboxUnread > 0}
        <span class="badge">{inboxUnread > 99 ? '99+' : inboxUnread}</span>
      {:else if t.id === 'watches' && watchOpen > 0}
        <span class="badge" class:quiet={watchMet === 0}>{watchMet > 0 ? watchMet : watchOpen}</span>
      {/if}
    </button>
  {/each}

  <!-- Pinned Hub pages: browser-style tabs in the rail's free middle,
       scrolling when there are more than fit. Empty (or hub off), it acts as
       the flex spacer. -->
  <div class="pins" class:has={hubEnabled && hub.pins.length > 0} aria-label="pinned hub pages">
    {#if hubEnabled}
    {#each hub.pins as p (p.slug)}
      <button
        class="tab pin"
        class:on={hubOpen === p.slug}
        class:lifted={drag?.slug === p.slug}
        onclick={() => onPinClick(p.slug)}
        onpointerdown={(e) => onPinDown(e, p)}
        oncontextmenu={(e) => e.preventDefault()}
        ondragstart={(e) => e.preventDefault()}
        title="{p.title} — drag to remove"
        aria-label={p.title}
        aria-pressed={hubOpen === p.slug}
      >
        <HubGlyph slug={p.slug} title={p.title} size={24} />
      </button>
    {/each}
    {/if}
  </div>

  {#if drag}
    <!-- The remove target covers the rail's bottom cluster while a tab is
         being dragged; it reaches past the narrow rail so it is easy to hit. -->
    <div class="trash" class:hot={drag.hot} bind:this={trashEl} aria-hidden="true">
      <Icon name="delete" size={22} stroke={1.8} />
      <span>remove</span>
    </div>
    <div class="ghost" class:hot={drag.hot} style="left: {drag.x}px; top: {drag.y}px;" aria-hidden="true">
      <HubGlyph slug={drag.slug} title={drag.title} size={30} />
    </div>
  {/if}

  <!-- Pulse-list toggle and + new pulse: adjacent pair, no dead slots. -->
  <button
    class="tab hue-ink"
    class:on={active === 'pulses'}
    onclick={() => onselect('pulses')}
    title="all pulses"
    aria-label="all pulses"
    aria-pressed={active === 'pulses'}
  >
    <Icon name="drawer" size={17} stroke={1.8} />
  </button>
  {#if onfsmode}
    <!-- App full-screen mode: one pulse fills the pane area and stays active
         across Hub/Inbox trips. -->
    <button
      class="tab hue-ink"
      class:on={fsMode}
      onclick={onfsmode}
      title={fsMode ? 'leave full-screen mode' : 'full-screen mode'}
      aria-label={fsMode ? 'leave full-screen mode' : 'full-screen mode'}
      aria-pressed={fsMode}
    >
      <Icon name={fsMode ? 'shrink' : 'expand'} size={16} stroke={2} />
    </button>
  {/if}
  <button class="tab new" onclick={onnew} title="new pulse (Ctrl+Enter)" aria-label="new pulse">
    <Icon name="new" size={16} stroke={2.4} />
  </button>

  {#if fuelSlot}
    <div class="fuel-wrap">{@render fuelSlot()}</div>
  {/if}

  {#if clockSlot}
    <!-- Meter and clock share one home in the rail. -->
    <div class="clock-wrap">{@render clockSlot()}</div>
  {/if}

  <!-- Utility footer: chrome, not destinations — dimmed, always last, dice last. -->
  <div class="foot">
    <div class="view-wrap">
      <button
        class="tab"
        class:on={viewOpen}
        onclick={() => (viewOpen = !viewOpen)}
        title="view — running only, columns, appearance"
        aria-label="view settings"
        aria-expanded={viewOpen}
      >
        <Icon name="settings" size={16} stroke={1.8} />
      </button>
      {#if viewOpen}
        <div class="view-pop" role="menu">
          {@render viewMenu?.(() => (viewOpen = false))}
        </div>
      {/if}
    </div>

    <button
      class="tab"
      class:locked={diceLocked}
      onclick={ondice}
      title={diceLocked ? 'dice locked — double-tap to unlock' : 'random theme — double-tap to lock'}
      aria-label={diceLocked ? 'dice locked, double-tap to unlock' : 'random theme'}
      data-testid="theme-dice"
    >
      <Icon name="dice" size={16} stroke={1.8} />
      {#if diceLocked}<span class="lock" aria-hidden="true">🔒</span>{/if}
    </button>
  </div>
</nav>

<style>
  .rail {
    width: var(--rail-w, 44px);
    flex-shrink: 0;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    padding: 8px 0 10px;
    border-right: 1px solid var(--border);
    background: var(--bg);
    position: relative;
    z-index: 5;
    user-select: none;
  }
  /* The rail is a stacking context, so its popovers (usage panel, view menu)
     sat under the full-screen pane. Lift the rail only while one is open. */
  .rail:has(:global(.usage-panel)),
  .rail:has(.view-pop) {
    z-index: 200;
  }
  .pins {
    flex: 1;
    min-height: 0;
    width: 100%;
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 6px;
    overflow-y: auto;
    overflow-x: hidden;
    scrollbar-width: none;
  }
  .pins::-webkit-scrollbar { display: none; }
  .pins.has {
    margin: 4px 0;
    padding: 8px 0;
    border-top: 1px solid var(--border);
    border-bottom: 1px solid var(--border);
  }
  .tab.pin {
    flex-shrink: 0;
    opacity: 0.8;
    color: var(--ink);
    touch-action: pan-y;
    -webkit-touch-callout: none;
    -webkit-user-select: none;
    user-select: none;
  }
  .tab.pin.lifted { opacity: 0.25; }
  .trash {
    position: absolute;
    left: 0;
    bottom: 0;
    width: max(100%, 104px);
    height: 170px;
    display: flex;
    flex-direction: column;
    align-items: center;
    justify-content: center;
    gap: 6px;
    color: #ff6b6b;
    background: color-mix(in srgb, #ff4d4d 14%, var(--bg));
    border: 2px dashed #ff6b6b;
    border-radius: 12px;
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    letter-spacing: 0.06em;
    z-index: 90;
    transition: background 120ms, color 120ms;
  }
  .trash.hot { color: #fff; background: #e5484d; border-style: solid; }
  .ghost {
    position: fixed;
    transform: translate(-50%, -50%) scale(1.15);
    pointer-events: none;
    z-index: 300;
    filter: drop-shadow(0 6px 14px rgba(0, 0, 0, 0.55));
    transition: opacity 120ms;
  }
  .ghost.hot { opacity: 0.55; }
  .tab.pin:hover, .tab.pin.on { opacity: 1; }
  /* Inset: the scrolling list clips anything drawn outside the button. */
  .tab.pin.on { box-shadow: inset 0 0 0 1.5px var(--ink-dim); background: var(--surface-warm); }
  .tab.new { color: var(--sprite); }
  .tab.new:hover { color: var(--bg); background: var(--sprite); }
  .tab {
    position: relative;
    display: flex;
    align-items: center;
    justify-content: center;
    width: 32px;
    height: 32px;
    border-radius: 8px;
    color: var(--ink-meta);
    transition: color 120ms, background 120ms;
  }
  .tab:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .tab.on { color: var(--ink); background: var(--surface-warm); }
  /* The selected tab carries a thin bar on the rail's outer edge — the rail
     equivalent of the old nav's surface-on ring, without the ring. */
  .tab.on::before {
    content: '';
    position: absolute;
    left: -6px;
    top: 7px;
    bottom: 7px;
    width: 2px;
    border-radius: 2px;
    background: currentColor;
  }
  /* Each destination keeps the hue it had in the nav, muted toward ink-meta;
     hover/active lifts to the full palette color. */
  .hue-user { color: color-mix(in srgb, var(--user) 60%, var(--ink-meta)); }
  .hue-user:hover, .hue-user.on { color: var(--user); }
  .hue-dreamer { color: color-mix(in srgb, var(--dreamer) 60%, var(--ink-meta)); }
  .hue-dreamer:hover, .hue-dreamer.on { color: var(--dreamer); }
  .hue-sprite { color: color-mix(in srgb, var(--sprite) 60%, var(--ink-meta)); }
  .hue-sprite:hover, .hue-sprite.on { color: var(--sprite); }
  .hue-warden { color: color-mix(in srgb, var(--warden) 60%, var(--ink-meta)); }
  .hue-warden:hover, .hue-warden.on, .hue-warden.met { color: var(--warden); }
  .badge {
    position: absolute;
    top: -3px;
    right: -4px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    min-width: 16px;
    height: 16px;
    padding: 0 4px;
    border-radius: 8px;
    color: var(--page);
    background: var(--ink-dim);
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    font-weight: 700;
    line-height: 1;
  }
  .hue-user .badge { background: var(--user); }
  .hue-q { color: #f2c57a; cursor: pointer; }
  .hue-q:hover { color: #ffd493; background: var(--surface-warm); }
  .hue-q .badge { background: #f2c57a; }
  .qmark { font-family: var(--font-mono); font-weight: 700; font-size: calc(15px * var(--tscale-ui)); }
  .hue-warden .badge { background: var(--warden); }
  .badge.quiet { background: var(--ink-meta); }
  .tab.locked { color: var(--ink-meta); opacity: 0.55; }
  .lock { position: absolute; right: 0; bottom: 0; font-size: 8px; line-height: 1; }
  .view-wrap { position: relative; }
  .view-pop {
    position: absolute;
    left: calc(100% + 10px);
    bottom: 0;
    min-width: 200px;
    padding: 10px 12px;
    display: flex;
    flex-direction: column;
    gap: 10px;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    box-shadow: 8px 8px 30px rgba(0, 0, 0, 0.45);
    z-index: 80;
  }
  /* Utility footer: grouped under a hairline, secondary weight — same 32px
     squares as the destinations, consistently dimmed (dice last). */
  .foot {
    display: flex;
    flex-direction: column;
    align-items: center;
    gap: 4px;
    width: 100%;
    padding-top: 8px;
    border-top: 1px solid var(--border);
  }
  .foot .tab { color: var(--ink-meta); opacity: 0.62; }
  .foot .tab:hover, .foot .tab.on { opacity: 1; }
  /* Phone mode (sidebar on): the shared-channel rules above assume the rail
     sits against the app edge; against the deck it only needs its own width. */
  .rail.detached { width: max-content; }
  .rail.detached .view-pop {
    /* The full desktop menu is 289px wide. Beside the rail that overhangs a
       320px phone, cutting off the right edge and its controls. */
    width: min(290px, calc(100vw - 52px));
    min-width: 0;
  }
  /* Brand slot: like a tab square, but bigger and breathing with the app's
     working state (styles the page-side snippet's button via :global). */
  .brand-wrap { display: contents; }
  .brand-wrap :global(.rail-brand) {
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 3px;
    border-radius: 8px;
    flex-shrink: 0;
  }
  .brand-wrap :global(.rail-brand .brand-mark) { display: block; width: 22px; height: 22px; }
  .brand-wrap :global(.rail-brand.home-on) {
    box-shadow: 0 0 0 1px var(--dreamer), 0 0 10px color-mix(in srgb, var(--dreamer) 45%, transparent);
  }
  .brand-wrap :global(.rail-brand.working .brand-mark) {
    animation: rail-brand-flash 1.8s ease-in-out infinite;
  }
  .brand-wrap :global(.rail-brand.offline .brand-mark) {
    filter: grayscale(1) brightness(0.55);
    animation: none;
  }
  @media (prefers-reduced-motion: reduce) {
    .brand-wrap :global(.rail-brand.working .brand-mark) {
      animation: none;
      filter: brightness(1.5) drop-shadow(0 0 5px rgb(255 255 255 / 0.4));
    }
  }
  @keyframes rail-brand-flash {
    0%, 100% { filter: none; }
    50% {
      filter: brightness(2.4) saturate(0.35)
              drop-shadow(0 0 6px rgb(255 255 255 / 0.55));
    }
  }
  /* Fuel meter sits in the bottom cluster above the view popover; the pill
     narrows so it fits the rail (the full panel opens from it untouched —
     the FuelRail component's own fixed-position panel handles placement). */
  .fuel-wrap { display: contents; }
  .fuel-wrap :global(.fuel-rail) {
    flex-direction: column;
    gap: 5px;
    padding: 7px 5px;
    border-radius: 10px;
    border-color: var(--border);
  }
  .fuel-wrap :global(.fuel-vendor) { flex-direction: column; gap: 3px; }
  /* Stacked clock under the meter, centered like the tabs. */
  .clock-wrap { display: flex; justify-content: center; }
  .clock-wrap :global(.clock) { margin-left: 0; }
</style>
