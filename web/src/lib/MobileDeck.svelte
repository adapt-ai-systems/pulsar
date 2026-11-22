<script>
  // Mobile mode (viewport < 700px).
  //
  // ONE pulse full-screen; horizontal swipe left/right moves between the pulses
  // of the active grid (touch + trackpad), ported from the mockup PHONE deck:
  // a transform track that snaps, with a swipe threshold.
  //
  // The overview (wall) opens on a DOUBLE TAP of a pane's title bar. A long
  // press did it until 2026-07-29, but holding the bar set the browser
  // selecting the title text, so the gesture came with a smear of highlight.
  // Before that it was a swipe-up starting in the composer zone, which fought
  // scrolling:
  // with a tall message the composer is where your thumb naturally lands to
  // flick the chat up, so reading a long reply kept throwing you into the
  // overview. There is now no vertical gesture that opens it, so nothing
  // competes with the scroll.
  //
  // The wall itself is the PAGE's, not the deck's: double-tapping the app nav
  // opens it too, and that has to work on a surface with no deck at all (the
  // Inbox). The deck only asks for it and reports which pulse it is on.
  // Closing it (tap-outside, ✕, swipe-down) is the wall's own business — it
  // covers the deck, so none of those gestures reach this file.
  //
  // Threshold: > 60px horizontal for a deck swipe. Every gesture decision logs
  // to console.debug.
  import { onDestroy } from 'svelte';
  import ChatPane from './ChatPane.svelte';
  import NoToolsNotice from './NoToolsNotice.svelte';
  import { putSetting } from './api.js';
  import { MOBILE_LOCATION_KEY } from './surface-handler.js';
  import { createDeckSwipe, H_THRESHOLD } from './deckswipe.js';

  /**
   * @type {{
   *   pulses: any[],
   *   ready?: boolean,
   *   noTools?: boolean,
   *   onchanged: () => void,
   *   focusId?: string|null,
   *   onfocused?: () => void,
   *   gridKey?: string,
   *   onoverview?: () => void,
   *   onactive?: (id: string) => void,
   *   onopenpulse?: (id: string) => void
   * }}
   */
  let {
    pulses, ready = true, noTools = false, onchanged, focusId = null, onfocused, gridKey = 'all',
    onoverview, onactive, onopenpulse
  } = $props();

  // Axis-lock thresholds live in deckswipe.js (H_LOCK < V_LOCK on purpose:
  // the horizontal axis is contested. `touch-action: pan-y` lets the browser
  // claim a drag with any vertical component as a native pan, and when it
  // does it fires pointercancel and the swipe silently evaporates. Claiming
  // a decisive sideways drag early wins that race.

  // WHICH PULSE is on screen — the source of truth. The slot number is not:
  // a reorder from the wall or another client, or a pulse removed above the
  // active card, left the same number pointing at a different conversation,
  // silently swapping the user onto another chat and persisting that id.
  let activeId = $state(null);
  // Where to land when the active pulse is gone entirely — its old slot, so a
  // deleted pulse leaves you on its neighbour rather than back at the start.
  let lastSlot = $state(0);

  const index = $derived.by(() => {
    const i = pulses.findIndex((p) => p.id === activeId);
    if (i >= 0) return i;
    return Math.min(lastSlot, Math.max(0, pulses.length - 1));
  });

  /** Move the deck to a slot — always by naming the pulse that sits there. */
  function focusSlot(i) {
    const p = pulses[i];
    if (!p) return;
    activeId = p.id;
    lastSlot = i;
  }

  // Remember which pulse is focused, per grid tab, so backgrounding the app
  // (iOS evicts the page) or switching tabs comes back to the pane you were
  // on instead of the first one. Keyed by pulse ID, not index — the list
  // order/membership can change between visits.
  const deckKey = () => `pulsar.deckPulse.${gridKey}`;
  let restoredFor = $state(null);
  let savedRemote = null;
  let remoteTimer;
  $effect(() => {
    if (!ready || restoredFor === gridKey || pulses.length === 0) return;
    restoredFor = gridKey;
    let saved;
    try { saved = localStorage.getItem(deckKey()); } catch { return; }
    if (!saved) return;
    const i = pulses.findIndex((p) => p.id === saved);
    if (i >= 0) {
      focusSlot(i);
      console.debug('[deck] restored focus → pulse %d (%s) for grid %s', i, saved, gridKey);
    }
  });
  $effect(() => {
    // A partial roster (especially before child workspaces load) must never
    // replace the saved location with its temporary first pane.
    if (!ready || restoredFor !== gridKey || focusId) return;
    const p = pulses[index];
    if (!p) return;
    // Adopt whatever the deck actually landed on — first paint, or the active
    // pulse having disappeared — so the id stays the thing being tracked.
    if (p.id !== activeId) {
      activeId = p.id;
      lastSlot = index;
    }
    try {
      localStorage.setItem(deckKey(), p.id);
    } catch { /* storage unavailable */ }
    // Daemon copy, for when iOS loses localStorage. Sent once the deck rests
    // on a pane for 1.5s, so a run of swipes is one request, not one per move.
    if (savedRemote !== p.id) {
      const loc = { grid: gridKey, pulse: p.id };
      clearTimeout(remoteTimer);
      remoteTimer = setTimeout(() => {
        savedRemote = loc.pulse;
        putSetting(MOBILE_LOCATION_KEY, loc);
      }, 1500);
    }
    // The page needs this to mark the current card in the wall it now owns.
    onactive?.(p.id);
  });

  // Live drag state. Gesture math is in createDeckSwipe; these two are the
  // visual outputs the slides read.
  let track = $state(null); // the transform track element
  let dragging = $state(false);
  let dragDX = $state(0); // current horizontal drag offset in px (visual)
  const swipe = createDeckSwipe();
  let winBound = false;

  // External focus request (activity sidebar tap → focus this pulse in the
  // deck). Reuses the deck's own focus mechanism — focusSlot, same as a swipe
  // or a wall pick — rather than a parallel path.
  $effect(() => {
    if (!ready || !focusId) return;
    const i = pulses.findIndex((p) => p.id === focusId);
    if (i >= 0) {
      focusSlot(i);
      console.debug('[deck] external focus → pulse %d (%s)', i, focusId);
      onfocused?.();
    } else {
      console.debug('[deck] external focus MISS — pulse %s not in deck', focusId);
    }
    // Keep a missed request pending until the live roster includes its pulse.
  });

  // Move/up/cancel bind on window, not the deck node. pointerdown is on the
  // stable .deck (not a keyed slide/pane). But if the original target is
  // replaced mid-gesture — stream-partial {@html} rebuilds a copy button,
  // schedules/now-tick re-render a tool row — subsequent events retarget to
  // document and would miss a listener on .deck. Window sees them either way.
  // Capture still waits for H_LOCK so a tap on a control is the control's.
  function bindWin() {
    if (winBound || typeof window === 'undefined') return;
    winBound = true;
    window.addEventListener('pointermove', onPointerMove, { passive: false });
    window.addEventListener('pointerup', endGesture);
    window.addEventListener('pointercancel', endGesture);
  }
  function unbindWin() {
    if (!winBound || typeof window === 'undefined') return;
    winBound = false;
    window.removeEventListener('pointermove', onPointerMove);
    window.removeEventListener('pointerup', endGesture);
    window.removeEventListener('pointercancel', endGesture);
  }
  onDestroy(() => {
    unbindWin();
    swipe.reset();
  });

  function onPointerDown(e) {
    // Only the first pointer; ignore multi-touch (let the browser pinch/scroll).
    // Interactive targets (copy buttons, links, tool rows) still ARM tracking
    // — a swipe that begins on one of them has to navigate. We do not capture
    // or preventDefault here: a tap under H_LOCK stays the element's click
    // (the bug 0f651cc was actually fixing).
    if (!swipe.down(e)) return;
    dragging = false;
    dragDX = 0;
    bindWin();
  }

  function onPointerMove(e) {
    const r = swipe.move(e, { index, count: pulses.length });
    if (r.ignored) return;
    dragging = r.dragging;
    dragDX = r.dragDX;
    if (r.capture) track?.setPointerCapture?.(e.pointerId);
    if (r.preventDefault) e.preventDefault();
  }

  function endGesture(e) {
    const r = swipe.up(e, { index, count: pulses.length });
    if (r.ignored) return;
    unbindWin();
    dragging = false;
    dragDX = 0;
    try { track?.releasePointerCapture?.(e.pointerId); } catch { /* not capturing */ }

    if (r.axis === 'x') {
      if (r.navigated) {
        focusSlot(r.to);
        console.debug(
          '[deck] swipe %s → pulse %d (dx=%d)',
          r.settledDX < 0 ? 'left' : 'right', r.to, Math.round(r.dx)
        );
      } else {
        console.debug('[deck] swipe below threshold, snap back (dx=%d)', Math.round(r.dx));
      }
      return;
    }

    // A vertical gesture here is always a scroll: it never opens the wall (that
    // is a double tap) and never closes it (the wall is on top of this deck and
    // handles its own swipe-down).
    if (r.axis === null) {
      // Never silent: a gesture that died before the axis lock is exactly the
      // failure mode that ate sideways swipes (browser claims the pan, fires
      // pointercancel). If this shows up on 'pointercancel' with a real dx,
      // the browser is still winning the race and H_LOCK needs to come down.
      console.debug(
        '[deck] gesture ended before axis lock via %s (dx=%d dy=%d)',
        e.type, Math.round(r.dx), Math.round(r.dy)
      );
    }
  }

  // Trackpad horizontal swipe (wheel with deltaX) — port of "trackpad" from task.
  let wheelAccum = 0;
  let wheelTimer;
  function onWheel(e) {
    if (Math.abs(e.deltaX) <= Math.abs(e.deltaY)) return; // vertical wheel = scroll
    e.preventDefault();
    wheelAccum += e.deltaX;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => (wheelAccum = 0), 180);
    if (wheelAccum <= -H_THRESHOLD && index > 0) {
      focusSlot(index - 1);
      wheelAccum = 0;
      console.debug('[deck] trackpad swipe right → pulse %d', index);
    } else if (wheelAccum >= H_THRESHOLD && index < pulses.length - 1) {
      focusSlot(index + 1);
      wheelAccum = 0;
      console.debug('[deck] trackpad swipe left → pulse %d', index);
    }
  }

  // Position the three mounted panes relative to the viewport instead of
  // translating one enormous track by index × 100%. With dozens of pulses the
  // old track became a 10,000px+ composited surface; mobile WebKit sometimes
  // stopped painting that surface entirely after viewport/keyboard changes.
  function slideStyle(i) {
    const offset = (i - index) * 100;
    const transform = !dragging && dragDX === 0 && offset === 0
      ? 'none'
      : `translateX(calc(${offset}% + ${dragDX}px))`;
    return `transform: ${transform}; transition: ${dragging ? 'none' : 'transform 220ms cubic-bezier(.22,.61,.36,1)'};`;
  }
</script>

<div class="deck-wrap">
  {#if !ready}
    <div class="deck-empty">Loading pulses…</div>
  {:else if pulses.length === 0}
    <div class="deck-empty">
      {#if noTools}
        <NoToolsNotice />
      {:else}
        No pulses. Tap ＋ to start one.
      {/if}
    </div>
  {:else}
    <div
      class="deck"
      bind:this={track}
      onpointerdown={onPointerDown}
      onwheel={onWheel}
    >
      {#each pulses as pulse, i (pulse.id)}
        {#if Math.abs(i - index) <= 1}
          <!-- Virtualized: mount only the active pulse and its immediate
               neighbors. The slides are absolutely positioned, so empty
               shells for every other pulse add no geometry; they only leave
               WebKit managing dozens of viewport-sized transform layers. -->
          <div class="slide" class:dragging style={slideStyle(i)}>
            <ChatPane {pulse} {onchanged} onoverview={() => onoverview?.()} {onopenpulse} />
          </div>
        {/if}
      {/each}
    </div>

    <!-- No always-on peekmap strip: the overview appears only on a double tap
         of a title bar or the app nav. The wall renders one level up, in the page. -->
  {/if}
</div>

<style>
  .deck-wrap {
    flex: 1;
    position: relative;
    display: flex;
    flex-direction: column;
    min-height: 0;
    overflow: hidden;
  }
  .deck {
    flex: 1;
    display: flex;
    position: relative;
    min-height: 0;
    touch-action: pan-y; /* let vertical scroll through to the chat; we own horizontal */
  }
  .slide {
    position: absolute;
    inset: 0;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  /* Promote only the moving pane(s), never the whole multi-pulse deck. */
  .slide.dragging { will-change: transform; }
  /* The ported deck gives each slide's pane full bleed. */
  .slide :global(.pane) {
    border: none;
    border-radius: 0;
    flex: 1;
  }
  .deck-empty {
    flex: 1;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(12px * var(--tscale-ui));
  }
</style>
