<script>
  // The wall — the mobile overview. Opens on a double tap of a pane's
  // title bar or of the app nav; the page owns it, because the nav opens it on
  // surfaces where no deck exists.
  // Shows every pulse in the active grid as a small card: title, status dot,
  // last-message snippet. Tap a card -> that pulse becomes the focused
  // full-screen one. Close via the ✕, tap-outside, or swipe-down.
  import { getEvents, reasoningOn } from './api.js';
  import { clock, subscribeClock } from './clock.svelte.js';
  import { buildItems, pulseStatus } from './render.js';
  import { openReasoning } from './reasoningstore.svelte.js';
  import { topGrids, childGrids, topOf } from './wscolor.js';
  import { moveId } from './pulsetree.js';
  import { createLongPressDrag } from './longpress.svelte.js';
  // Sub-workspaces: top-level chips; the open one unfolds its sub-workspace
  // chips right after it, like the desktop tab strip.
  const tops = $derived(topGrids(grids));
  const openTop = $derived(selectedGrid === null ? null : topOf(selectedGrid, grids));

  /**
   * @type {{
   *   pulses: any[], activeIndex: number,
   *   onpick: (id:string)=>void, onclose: ()=>void, onnew?: ()=>void,
   *   grids?: Array<{id:string,name:string}>,
   *   selectedGrid?: string|null,
   *   onselectgrid?: (id: string|null) => void,
   *   hideStopped?: boolean,
   *   ontogglehidestopped?: () => void,
   *   onmovepulse?: (pulseId: string, gridId: string|null) => void,
   *   onreorderpulses?: (fromId: string, toId: string) => void,
   *   onreordergrids?: (ids: string[]) => void,
   *   ondeletegrid?: (gridId: string) => void
   * }}
   */
  let {
    pulses,
    activeIndex,
    onpick,
    onclose,
    onnew,
    grids = [],
    selectedGrid = null,
    onselectgrid,
    hideStopped = false,
    ontogglehidestopped,
    onmovepulse,
    onreorderpulses,
    onreordergrids,
    ondeletegrid
  } = $props();

  // Last-message snippets, keyed by pulse id. Fetched lazily when the wall opens;
  // a missing snippet is not an error, it's "no messages yet".
  let snippets = $state({});
  const now = $derived(clock.now);

  function snippetFor(items) {
    // Walk backwards for the newest human-readable line (you / reply).
    for (let i = items.length - 1; i >= 0; i--) {
      const it = items[i];
      if (it.kind === 'you') return `you: ${it.text}`;
      if (it.kind === 'reply') return it.text;
    }
    return '';
  }

  /** One batched sweep. `alive()` goes false when the wall closes or the card
   *  set changes, and a dead sweep must not write over the newer one. */
  async function loadSnippets(ids, alive) {
    const fetched = await Promise.all(
      ids.map((id) => getEvents(id, 40).then((r) => [id, r]))
    );
    if (!alive()) return;
    const next = { ...snippets };
    for (const [id, r] of fetched) {
      // A failed fetch is non-fatal: that card just keeps showing no snippet.
      if (r.ok) next[id] = snippetFor(buildItems(r.value.events ?? []));
      else console.debug('[wall] snippet fetch failed', id, r.error);
    }
    snippets = next;
  }

  // Which cards are on the wall, as a value rather than an array identity. The
  // page rebuilds `pulses` every 3s roster poll, so an effect that reads the
  // prop directly re-ran the whole N-request sweep three times a minute; a
  // $derived only fires when the membership itself actually changes.
  const memberKey = $derived(
    pulses.filter((p) => p.state !== 'archived').map((p) => p.id).sort().join(',')
  );

  $effect(() => {
    const ids = memberKey ? memberKey.split(',') : [];
    console.debug('[wall] snippet sweep · pulses=%d', ids.length);
    let alive = true;
    loadSnippets(ids, () => alive);
    return () => {
      alive = false;
    };
  });

  $effect(() => subscribeClock({ focused: true }));

  function statusOf(pulse) {
    if (pulse.state === 'stopped' || pulse.state === 'archived') {
      return { phase: 'offline', label: 'stopped' };
    }
    const status = pulseStatus(pulse?.live ?? null, now);
    // A running roster row without a live block is still not finished.
    return status.phase === 'offline' ? { ...status, phase: 'waking', label: 'waking…' } : status;
  }

  // Pulses needing attention flash: a waiting/asking pulse. We treat a
  // running pulse that is NOT thinking and whose last line is a reply asking a
  // question as "attention" is out of scope here; use the live socket hint when
  // present, else no flash. Kept honest — no invented state.
  function needsAttention(pulse) {
    return pulse?.live?.socket_state === 'waiting';
  }

  // --- swipe down to close ------------------------------------------------ //
  // This has to live here, not in the deck: the wall covers the deck and is not
  // inside it, so the deck's gesture layer never sees a pointer event landing on
  // the wall — the swipe-down it claimed to handle could not fire (found
  // 2026-07-29, verified in Chromium).
  //
  // The gesture is anchored to the wall's chrome — the OVERVIEW bar and the
  // workspace row — and NOT to the card list. Over a scroller the browser
  // claims a downward drag as a native pan after ~20px and fires pointercancel,
  // so a swipe that starts there can never complete; `touch-action: none` on
  // the chrome is what stops the same thing happening at the top of the wall.
  // Pull down from the header, the way any sheet dismisses.
  const SWIPE_CLOSE = 60; // px down
  let scroller = $state(null);
  let gesture = null;

  function onPointerDown(e) {
    if (gesture || scroller?.contains(e.target)) return; // multi-touch / card list
    // A press on a workspace chip may become a drag; a pull-down that closed the
    // wall mid-drag would throw the workspace away with it.
    if (lp.pressing || drag || e.target?.closest?.('[data-ws-drop]')) return;
    gesture = { id: e.pointerId, x: e.clientX, y: e.clientY };
  }

  // Decided on the MOVE, not on release: a release can arrive as pointercancel,
  // which carries no useful position.
  function onPointerMove(e) {
    if (gesture?.id !== e.pointerId) return;
    const dy = e.clientY - gesture.y;
    if (dy < SWIPE_CLOSE || Math.abs(e.clientX - gesture.x) >= dy) return;
    gesture = null;
    console.debug('[wall] CLOSE — swipe-down (dy=%d)', Math.round(dy));
    onclose();
  }

  // --- long-press drag (mobile) ------------------------------------------- //
  // HTML5 drag-and-drop does not exist on touch, so this is hand-rolled on
  // pointer events. Two things drag, with the same gesture:
  //   · a CARD onto a workspace chip → that pulse moves there (ALL = out of any)
  //   · a workspace CHIP onto another chip → reorder, or onto the bin → delete
  // (matching the desktop drags).
  //
  // Long-press rather than drag-on-contact because the card list SCROLLS — an
  // immediate drag would eat every flick. The hold also lets us stop the scroll
  // honestly: nothing has begun panning while the finger is still, so a
  // preventDefault on the first touchmove after the hold does hold (once the
  // browser has claimed the pan, preventDefault is ignored — see the $effect).
  // The machine itself is longpress.svelte.js (LONG_PRESS_MS 320, PRESS_SLOP 9).
  // Without pointer capture the drag would die the moment the finger leaves the
  // card — the release over a workspace chip never reached the handler and the
  // wall just closed instead (found driving the real UI, 2026-07-30).
  // `dropTarget`: null = over nothing; { kind:'ws', id } where id null is the ALL
  // chip; { kind:'bin' } is the delete bar. Note "over nothing" and "over ALL"
  // are different outcomes for a pulse.
  let wallEl = $state(null);

  const canDrag = $derived(!!onmovepulse || !!onreorderpulses);
  const canDragGrid = $derived(!!onreordergrids);

  const lp = createLongPressDrag({
    noTarget: null,
    canStart: (e, info) => (info.kind === 'pulse' ? canDrag : canDragGrid),
    onStart: (d) => {
      gesture = null; // a drag is not a swipe-down-to-close
      console.debug('[wall] drag START %s %s', d.kind, d.id);
    },
    hitTest: (x, y, d) => dropUnder(x, y, d),
    onCancel: (d, why) => {
      if (why === 'pointercancel') console.debug('[wall] drag CANCELLED — pointercancel');
      else console.debug('[wall] drag CANCELLED — released on nothing (%s %s)', d.kind, d.id);
    },
    onDrop: (target, d) => dropOn(target, d),
    touchBlockTarget: () => wallEl
  });
  const drag = $derived(lp.drag);
  const dropTarget = $derived(lp.target);

  /**
   * Arm a long press. `kind` is 'pulse' (a card) or 'grid' (a workspace chip);
   * `item` is that pulse or grid.
   */
  function pressStart(e, kind, item) {
    lp.start(e, { kind, id: item.id, label: kind === 'pulse' ? item.title : item.name });
  }

  /** What is under the finger — a workspace chip, another card, the bin, or
   *  nothing usable. */
  function dropUnder(x, y, d) {
    const el = document.elementFromPoint(x, y);
    if (!el?.closest) return null;
    if (el.closest('[data-ws-bin]')) return d.kind === 'grid' ? { kind: 'bin' } : null;
    // A card dropped on another card → reorder (a card is not a workspace slot,
    // and a card cannot swap with itself).
    if (d.kind === 'pulse' && onreorderpulses) {
      const card = el.closest('[data-card-drop]');
      const cid = card?.getAttribute('data-card-drop');
      if (cid && cid !== d.id) return { kind: 'card', id: cid };
    }
    const chip = el.closest('[data-ws-drop]');
    if (!chip) return null;
    const v = chip.getAttribute('data-ws-drop');
    const id = v === '' ? null : v; // '' is the ALL chip
    if (d.kind === 'grid') {
      // Reordering: ALL is not a slot, and a chip cannot swap with itself.
      return id && id !== d.id ? { kind: 'ws', id } : null;
    }
    return onmovepulse ? { kind: 'ws', id } : null;
  }

  function dropOn(target, { kind, id, label }) {
    if (kind === 'pulse') {
      if (target.kind === 'card') {
        console.debug('[wall] reorder %s (%s) → slot of %s', id, label, target.id);
        onreorderpulses?.(id, target.id);
      } else {
        console.debug('[wall] drop %s (%s) → %s', id, label, target.id ?? 'no workspace');
        onmovepulse?.(id, target.id);
      }
      return;
    }
    if (target.kind === 'bin') {
      console.debug('[wall] workspace %s (%s) → bin', id, label);
      ondeletegrid?.(id);
      return;
    }
    const order = moveId(grids.map((g) => g.id), id, target.id);
    if (!order) return;
    console.debug('[wall] workspace %s (%s) → slot %d', id, label, order.indexOf(id));
    onreordergrids?.(order);
  }

  // The ghost is centred on the finger, so near an edge it would hang off the
  // screen — the title got cut in half at the left margin. Keep it inside.
  let ghostW = $state(0);
  function ghostX(x) {
    const half = (ghostW || 130) / 2 + 6;
    const w = typeof window === 'undefined' ? 390 : window.innerWidth;
    return Math.min(Math.max(x, half), Math.max(half, w - half));
  }
</script>

<!-- Dim layer under the wall (visual only — the wall covers it, so clicks
     land on .wall below, which is the real tap-outside close target). -->
<div class="wall-scrim" role="presentation"></div>

<!-- Tap anywhere that isn't a card -> close. The wall spans the whole screen
     ABOVE the scrim (z 81 > 80), so the close handler must live here: a
     scrim-only handler is unreachable and the deck's swipe layer can't see
     pointer events over the wall either (wall mounts outside .deck). -->
<div
  class="wall"
  class:dragging={!!drag}
  bind:this={wallEl}
  role="dialog"
  aria-label="pulse overview"
  onclick={onclose}
  onpointerdown={onPointerDown}
  onpointermove={onPointerMove}
  onpointerup={() => (gesture = null)}
  onpointercancel={() => (gesture = null)}
>
  <div class="wall-head">
    {#if drag}
      <!-- Short on purpose: a longer line wrapped at 390px and pushed the
           workspace chips down mid-drag, away from the finger aiming at them. -->
      <span class="head-drag">MOVING</span>
    {:else}
      <span>OVERVIEW · {pulses.length} {pulses.length === 1 ? 'pulse' : 'pulses'}</span>
    {/if}
    <!-- Visible way out — tap-outside works too, but an explicit button makes
         closing obvious on mobile. -->
    <div class="wall-head-actions">
      {#if onnew}
        <button class="wall-new" onclick={(e) => { e.stopPropagation(); onnew(); }} aria-label="new pulse">＋</button>
      {/if}
      <button class="wall-close" onclick={(e) => { e.stopPropagation(); onclose(); }} aria-label="close overview">✕</button>
    </div>
  </div>
  <!-- Workspaces first. This overlay is the one place with a full row to spend
       on them, so they get their real names here instead of the 16px the nav
       can afford. Switching here reloads the deck beneath. -->
  {#if onselectgrid}
    <div class="wall-ws" role="group" aria-label="workspaces">
      <!-- data-ws-drop is what a dragged card hit-tests against; "" = ALL, i.e.
           out of every workspace. -->
      <button
        class="ws-chip"
        class:on={selectedGrid === null}
        class:droppable={drag?.kind === 'pulse'}
        class:drop-on={dropTarget?.kind === 'ws' && dropTarget.id === null}
        data-ws-drop=""
        onclick={(e) => { e.stopPropagation(); onselectgrid(null); }}
      >ALL</button>
      {#snippet wsChip(g, sub)}
        <!-- A chip is both a target (a pulse dropped here) and a source (hold it
             to reorder the row or bin it). -->
        <button
          class="ws-chip"
          class:sub
          class:family={!sub && selectedGrid !== g.id && openTop === g.id}
          class:on={selectedGrid === g.id}
          class:droppable={!!drag}
          class:drop-on={dropTarget?.kind === 'ws' && dropTarget.id === g.id}
          class:carrying={drag?.kind === 'grid' && drag.id === g.id}
          data-ws-drop={g.id}
          onclick={(e) => { e.stopPropagation(); onselectgrid(g.id); }}
          onpointerdown={(e) => pressStart(e, 'grid', g)}
          onpointermove={lp.move}
          onpointerup={lp.up}
          onpointercancel={lp.cancel}
        >{g.name}</button>
      {/snippet}
      {#each tops as g (g.id)}
        {@render wsChip(g, false)}
        {#if openTop === g.id && childGrids(g.id, grids).length}
          <span class="sub-lead" aria-hidden="true">›</span>
          {#each childGrids(g.id, grids) as c (c.id)}
            {@render wsChip(c, true)}
          {/each}
        {/if}
      {/each}
    </div>
  {/if}

  {#if ontogglehidestopped}
    <!-- Filters the wall AND the deck beneath it (the page filters the list the
         deck swipes through), the ALL tab included. -->
    <label class="wall-hide" onclick={(e) => e.stopPropagation()}>
      <input type="checkbox" checked={hideStopped} onchange={ontogglehidestopped} />
      <span>hide pulses that aren't running</span>
    </label>
  {/if}

  {#if canDrag && (pulses.length > 1 || grids.length > 0)}
    <!-- The gesture is invisible otherwise: nothing about a card says it can be
         held and dragged. Rendered mid-drag too, with different words — hiding
         it moved the chip row up under the finger you were aiming with. -->
    <div class="wall-hint" class:live={!!drag}>
      {#if drag?.kind === 'grid'}
        drop on another workspace to reorder, or on the bin below to delete
      {:else if drag}
        release on another pulse to reorder, or on a workspace to move it there
      {:else}
        hold a pulse or a workspace to drag it
      {/if}
    </div>
  {/if}

  <div class="wall-grid" bind:this={scroller}>
    {#each pulses as pulse, i (pulse.id)}
      {@const cardStatus = statusOf(pulse)}
      <button
        class="wall-card"
        class:active={i === activeIndex}
        class:flash={needsAttention(pulse)}
        class:working={cardStatus.phase === 'thinking'}
        class:work-glow={cardStatus.phase === 'thinking' || needsAttention(pulse)}
        class:lifted={drag?.kind === 'pulse' && drag.id === pulse.id}
        class:drop-on={dropTarget?.kind === 'card' && dropTarget.id === pulse.id}
        data-card-drop={pulse.id}
        onclick={(e) => {
          e.stopPropagation();
          // The working status label is its own target: clicking it opens the
          // reasoning panel, clicking anywhere else on the card picks the
          // pulse. Avoids nesting a button inside the card's button.
          if (cardStatus.phase === 'thinking' && reasoningOn(pulse)
              && e.target.closest?.('.wc-status')) {
            openReasoning(pulse.id, pulse.title);
            return;
          }
          onpick(pulse.id);
        }}
        onpointerdown={(e) => pressStart(e, 'pulse', pulse)}
        onpointermove={lp.move}
        onpointerup={lp.up}
        onpointercancel={lp.cancel}
      >
        <div class="wc-head">
          <span
            class="dot"
            class:working={cardStatus.phase === 'thinking'}
            class:idle={cardStatus.phase === 'idle'}
            class:stalled={cardStatus.phase === 'stalled'}
            class:stopped={cardStatus.phase === 'offline' || cardStatus.phase === 'stopping'}
          ></span>
          <span class="wc-status" class:working={cardStatus.phase === 'thinking'} class:stalled={cardStatus.phase === 'stalled'} title={cardStatus.phase === 'thinking' && reasoningOn(pulse) ? 'show reasoning trace' : undefined}>{cardStatus.label}</span>
          <span class="wc-title" title={pulse.title}>{pulse.title}</span>
        </div>
        <div class="wc-snip">{snippets[pulse.id] ?? '…'}</div>
      </button>
    {/each}
    {#if pulses.length === 0}
      <div class="wall-empty">
        {hideStopped ? 'No running pulses here — untick the filter to see the rest.' : 'No pulses in this workspace.'}
      </div>
    {/if}
  </div>

  <!-- The bin lives at the BOTTOM on a phone, not beside the chips like the
       desktop nav: the chips are what you drag between when reordering, and a
       delete target among them is one slip away from the wrong outcome. -->
  {#if drag?.kind === 'grid' && ondeletegrid}
    <div class="wall-bin" class:hot={dropTarget?.kind === 'bin'} data-ws-bin role="presentation">
      🗑 drop here to delete “{drag.label}”
    </div>
  {/if}

  <!-- The thing under the finger. pointer-events:none so the hit test sees the
       chip underneath it, not the ghost. -->
  {#if drag}
    <div
      class="drag-ghost"
      bind:clientWidth={ghostW}
      style="left: {ghostX(drag.x)}px; top: {Math.max(drag.y, 44)}px;"
    >{drag.label}</div>
  {/if}
</div>

<style>
  /* Workspace row — wraps rather than scrolling sideways, because a name you
     cannot see is the whole problem this row exists to fix. */
  .wall-ws {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 0 14px 12px;
    flex-shrink: 0;
    /* Part of the pull-down-to-close surface: see onPointerDown. */
    touch-action: none;
  }
  .ws-chip {
    padding: 6px 11px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 7px;
    white-space: nowrap;
  }
  .ws-chip.on {
    color: var(--dreamer);
    border-color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 12%, transparent);
  }
  /* Mid-drag: every chip reads as a target, the one under the finger fills in. */
  .ws-chip.sub { border-style: dotted; font-size: calc(9px * var(--tscale-ui)); padding: 4px 9px; }
  .ws-chip.sub.on { border-style: solid; }
  .ws-chip.family { border-color: var(--dreamer); color: var(--dreamer); }
  .sub-lead { color: var(--dreamer); opacity: 0.7; align-self: center; }
  .ws-chip.droppable {
    border-style: dashed;
    border-color: var(--sprite);
  }
  .ws-chip.drop-on {
    border-style: solid;
    color: var(--sprite);
    border-color: var(--sprite);
    background: color-mix(in srgb, var(--sprite) 20%, transparent);
  }
  .wall-hide {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 0 14px 10px;
    flex-shrink: 0;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .wall-hide input { accent-color: var(--dreamer); width: 15px; height: 15px; }
  .wall-hide:has(input:checked) { color: var(--dreamer); }
  .wall-hint {
    padding: 0 14px 8px;
    font-family: var(--font-mono);
    font-size: calc(9.5px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-meta);
    flex-shrink: 0;
  }
  .wall-hint.live { color: var(--sprite); }
  .ws-chip.carrying { opacity: 0.35; }
  /* Bottom bar, thumb-height, only while a workspace is in flight. */
  .wall-bin {
    position: absolute;
    left: 10px;
    right: 10px;
    bottom: 10px;
    z-index: 40;
    min-height: 62px;
    display: flex;
    align-items: center;
    justify-content: center;
    gap: 8px;
    padding: 0 14px;
    text-align: center;
    border-radius: 12px;
    border: 1px dashed var(--bad);
    background: color-mix(in srgb, var(--bad) 16%, var(--bg));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    letter-spacing: 0.1em;
  }
  .wall-bin.hot {
    border-style: solid;
    background: color-mix(in srgb, var(--bad) 34%, var(--bg));
    box-shadow: 0 0 16px color-mix(in srgb, var(--bad) 60%, transparent);
  }
  .head-drag { color: var(--sprite); white-space: nowrap; }
  /* The card being carried, and the ghost that follows the finger. */
  .wall-card.lifted { opacity: 0.35; }
  /* While carrying a card, nothing in the wall may be selected or panned. */
  .wall.dragging {
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
  }

  /* Below every fixed overlay (drawers/modals/activity live at z 50-70):
     at z 81 the wall covered the activity log when both were open. */
  .wall-scrim {
    position: absolute;
    inset: 0;
    background: rgba(2, 3, 5, 0.6);
    z-index: 30;
  }
  .wall {
    position: absolute;
    inset: 0;
    z-index: 31;
    display: flex;
    flex-direction: column;
    background: color-mix(in srgb, var(--bg) 96%, transparent);
    backdrop-filter: blur(2px);
    /* slide up from the composer */
    animation: wall-in 160ms ease-out;
  }
  @keyframes wall-in {
    from { transform: translateY(16px); opacity: 0; }
    to { transform: translateY(0); opacity: 1; }
  }
  .wall-head {
    flex-shrink: 0;
    padding: 12px 14px 8px;
    /* Pull down from here to dismiss — the browser must not read it as a pan. */
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.2em;
    color: var(--ink-meta);
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .wall-close {
    color: var(--ink-dim);
    font-size: calc(14px * var(--tscale-ui));
    padding: 4px 10px;
    border-radius: 6px;
    border: 1px solid var(--border-strong);
    background: var(--surface);
  }
  .wall-head-actions { display: flex; align-items: center; gap: 8px; }
  .wall-new {
    width: 30px;
    height: 30px;
    border-radius: 999px;
    color: var(--bg);
    background: var(--sprite);
    font-family: var(--font-sans);
    font-size: calc(20px * var(--tscale-ui));
    line-height: 1;
  }
  .wall-grid {
    flex: 1;
    overflow-y: auto;
    display: grid;
    /* A plain 1fr track keeps its min-content floor. Long pulse names/status
       strings then force each column wider than the phone and the right card
       becomes unreachable because the wall only scrolls vertically. */
    grid-template-columns: repeat(2, minmax(0, 1fr));
    gap: 8px;
    padding: 8px 10px 16px;
    align-content: start;
  }
  .wall-card {
    display: flex;
    flex-direction: column;
    gap: 5px;
    text-align: left;
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    background: var(--surface);
    padding: 9px 10px;
    min-height: 74px;
    min-width: 0;
    overflow: hidden;
  }
  .wall-card.active {
    border-color: var(--dreamer);
    box-shadow: 0 0 0 1px var(--dreamer);
  }
  /* The card the carried one would land on — same voice as the chips' drop-on. */
  .wall-card.drop-on {
    border-color: var(--sprite);
    box-shadow: 0 0 0 1px var(--sprite);
    background: color-mix(in srgb, var(--sprite) 14%, var(--surface));
  }
  /* Working pulse: same flashing dreamer ring the grid panes carry, driven by
     the same appearance sliders (ring width / glow / speed), so the overview
     reads busy-vs-quiet exactly like the grid does. Listed before .flash so a
     waiting pulse's matron flash wins if both ever apply. */
  .wall-card.working {
    border-color: var(--dreamer);
    --work-halo: 14px;
    --work-halo-color: var(--dreamer);
    overflow: visible;
  }
  .wall-card.flash {
    border-color: var(--matron);
    --work-halo: 10px;
    --work-halo-color: var(--matron);
    overflow: visible;
    animation: none;
  }
  .wall-card.flash::after {
    animation-duration: 1.2s;
  }
  .wc-head { display: flex; align-items: center; gap: 6px; min-width: 0; }
  .wc-status {
    flex: 0 1 auto;
    max-width: 48%;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
  }
  .wc-status.working { color: var(--dreamer); cursor: pointer; text-decoration: underline dotted; }
  .wc-status.stalled { color: var(--bad); }
  .wc-title {
    flex: 1 1 auto;
    min-width: 0;
    font-size: calc(12.5px * var(--tscale-ui));
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; background: var(--ink-meta); }
  .dot.working { background: var(--sprite); }
  .wall-card.work-glow .dot.working { animation: pulse 1.6s infinite; }
  .dot.idle { background: var(--ink-meta); }
  .dot.stalled { background: var(--bad); }
  .dot.stopped { background: var(--border-strong); }
  .wc-snip {
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-dim);
    line-height: 1.4;
    display: -webkit-box;
    -webkit-line-clamp: 3;
    -webkit-box-orient: vertical;
    overflow: hidden;
  }
  .wall-empty {
    grid-column: 1 / -1;
    padding: 24px;
    text-align: center;
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(12px * var(--tscale-ui));
  }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(0.7); opacity: 0.6; }
  }
  @media (prefers-reduced-motion: reduce) {
    .wall-card.work-glow .dot.working { animation: none; }
  }
</style>
