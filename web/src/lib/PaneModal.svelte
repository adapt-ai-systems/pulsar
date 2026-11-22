<script>
  import ChatPane from './ChatPane.svelte';
  import Icon from './Icon.svelte';

  // A pane expanded off the grid into a floating window. No scrim on
  // purpose: several can be open at once, and the grid behind stays visible and
  // clickable — this is the "see every pane, bounce between them" surface. The
  // drag mechanics use pointer capture on
  // the head bar, clamped so the bar can never leave the screen).
  /** @type {{ pulse: any, x: number, y: number, z: number, full?: boolean, onchanged: () => void, onclose: () => void, onfront: () => void, onmove: (pos: { x: number, y: number }) => void, onopenpulse?: (id: string) => void }} */
  let { pulse, x, y, z, full = false, onchanged, onclose, onfront, onmove, onopenpulse } = $props();

  /** @type {HTMLElement | null} */
  let el = null;
  let pos = $state({ x, y });

  // Maximize: the toggle in the head fills the PANE AREA — the space the grid
  // has — not the viewport, so the tabs, metrics bar, rail, pulse list and the
  // Inbox column stay visible and usable. The windowed position/size is
  // remembered so restore puts it back. `full` seeds it. Where there is no
  // pane area it fills the viewport right of the rail.
  function measureArea() {
    if (typeof document === 'undefined') return null;
    const a = document.querySelector('.pane-area');
    if (a) {
      const r = a.getBoundingClientRect();
      return { l: Math.round(r.left), t: Math.round(r.top), w: Math.round(r.width), h: Math.round(r.height) };
    }
    const l = Math.max(0, Math.round(document.querySelector('nav.rail')?.getBoundingClientRect().right ?? 0));
    return { l, t: 0, w: window.innerWidth - l, h: window.innerHeight };
  }
  let isFull = $state(full);
  let area = $state(full ? measureArea() : null);
  let prev = null; // { pos, size } snapshot taken when entering full screen
  $effect(() => {
    if (!isFull) return;
    const remeasure = () => (area = measureArea());
    remeasure();
    // A list or column that opens, closes or is dragged wider resizes the
    // pane area with no window resize event.
    const a = document.querySelector('.pane-area');
    const ro = a ? new ResizeObserver(remeasure) : null;
    ro?.observe(a);
    window.addEventListener('resize', remeasure);
    return () => { ro?.disconnect(); window.removeEventListener('resize', remeasure); };
  });

  function toggleFull() {
    if (isFull) {
      isFull = false;
      if (prev) {
        pos = prev.pos;
        size = prev.size;
      }
    } else {
      prev = { pos, size };
      area = measureArea(); // before the flip: no first-frame jump
      isFull = true;
      onfront?.();
    }
  }
  // null until the first resize: the window keeps its CSS default size
  // (min(760px, 92vw) × min(80vh, 860px)) until you grab an edge.
  let size = $state(null); // { w, h }
  let drag = null;
  let resize = null;

  const MIN_W = 340;
  const MIN_H = 240;

  // The HEAD must stay reachable — it is the only handle. Left/right allow the
  // body to hang mostly off-screen as long as ~160px of the bar remains.
  function clampPos(nx, ny) {
    const w = el?.offsetWidth ?? 720;
    return {
      x: Math.max(160 - w, Math.min(nx, window.innerWidth - 160)),
      y: Math.max(4, Math.min(ny, window.innerHeight - 44))
    };
  }

  function onPointerDown(e) {
    // A press that starts on the close button is a click, never a drag.
    // Full screen: the head is a toggle/restore bar, not a drag handle.
    if (isFull || e.target.closest('button, input, textarea, select, a')) return;
    drag = { id: e.pointerId, dx: e.clientX - pos.x, dy: e.clientY - pos.y };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function onPointerMove(e) {
    if (!drag || e.pointerId !== drag.id) return;
    pos = clampPos(e.clientX - drag.dx, e.clientY - drag.dy);
  }
  function onPointerEnd(e) {
    if (!drag || e.pointerId !== drag.id) return;
    drag = null;
    onmove?.({ x: pos.x, y: pos.y });
  }

  // --- edge/corner resize, like a normal window --------------------------- //
  // Each handle names the edges it moves ('n', 'se', …). Dragging a left/top
  // edge moves the window as well as sizing it, so the opposite edge stays put.
  function onResizeDown(e, edges) {
    if (isFull) return; // maximized: the pane area is the size — no resize
    resize = {
      id: e.pointerId,
      edges,
      startX: e.clientX,
      startY: e.clientY,
      x: pos.x,
      y: pos.y,
      w: el?.offsetWidth ?? 720,
      h: el?.offsetHeight ?? 520
    };
    e.currentTarget.setPointerCapture(e.pointerId);
  }
  function onResizeMove(e) {
    if (!resize || e.pointerId !== resize.id) return;
    const dx = e.clientX - resize.startX;
    const dy = e.clientY - resize.startY;
    let { x: nx, y: ny, w, h } = resize;
    if (resize.edges.includes('e')) w = resize.w + dx;
    if (resize.edges.includes('s')) h = resize.h + dy;
    if (resize.edges.includes('w')) {
      w = resize.w - dx;
      nx = resize.x + dx;
    }
    if (resize.edges.includes('n')) {
      h = resize.h - dy;
      ny = resize.y + dy;
    }
    w = Math.max(MIN_W, Math.min(w, window.innerWidth));
    h = Math.max(MIN_H, Math.min(h, window.innerHeight));
    // Re-anchor after clamping so a clamped west/north drag doesn't drift.
    if (resize.edges.includes('w')) nx = resize.x + resize.w - w;
    if (resize.edges.includes('n')) ny = resize.y + resize.h - h;
    size = { w, h };
    pos = { x: nx, y: Math.max(0, ny) };
  }
  function onResizeEnd(e) {
    if (!resize || e.pointerId !== resize.id) return;
    resize = null;
    onmove?.({ x: pos.x, y: pos.y });
  }
</script>

<!-- Capture-phase so any interaction anywhere in the window raises it above
     its siblings before the target handles the event. -->
<div
  class="float-pane"
  bind:this={el}
  class:full={isFull}
  style={isFull && area
    ? `left: ${area.l}px; top: ${area.t}px; width: ${area.w}px; height: ${area.h}px; z-index: ${z};`
    : `left: ${pos.x}px; top: ${pos.y}px; z-index: ${z};${size ? ` width: ${size.w}px; height: ${size.h}px;` : ''}`}
  onpointerdowncapture={onfront}
  role="dialog"
  aria-label={pulse.title}
>
  <div
    class="float-head"
    onpointerdown={onPointerDown}
    onpointermove={onPointerMove}
    onpointerup={onPointerEnd}
    onpointercancel={onPointerEnd}
    role="presentation"
  >
    <!-- The head owns the maximize/restore toggle in BOTH states, same
         position — fullscreen only swaps the icon, the button never moves. -->
    <button
      class="float-max"
      onclick={toggleFull}
      title={isFull ? 'restore window' : 'full screen'}
      aria-label={isFull ? 'restore window' : 'full screen'}
    ><Icon name={isFull ? 'shrink' : 'expand'} size={11} stroke={2} /></button>
    <span class="float-t" title={pulse.title}>{pulse.title}</span>
    <button class="float-x" onclick={onclose} title="close" aria-label="close expanded pane">×</button>
  </div>
  <div class="float-body">
    {#key pulse.id}
      <ChatPane {pulse} {onchanged} {onopenpulse} fullscreen={isFull} />
    {/key}
  </div>
  <!-- Invisible resize handles ringing the window: 4 edges + 4 corners.
       Gone while maximized — the pane area is the size. -->
  {#if !isFull}
  {#each [['n', 'rh-n'], ['s', 'rh-s'], ['e', 'rh-e'], ['w', 'rh-w'], ['ne', 'rh-ne'], ['nw', 'rh-nw'], ['se', 'rh-se'], ['sw', 'rh-sw']] as [edges, cls] (cls)}
    <div
      class="rh {cls}"
      onpointerdown={(e) => onResizeDown(e, edges)}
      onpointermove={onResizeMove}
      onpointerup={onResizeEnd}
      onpointercancel={onResizeEnd}
      role="presentation"
    ></div>
  {/each}
  {/if}
</div>

<style>
  .float-pane {
    position: fixed;
    width: min(760px, 92vw);
    height: min(80vh, 860px);
    display: flex;
    flex-direction: column;
    background: var(--bg);
    border: 10px solid var(--border-strong);
    border-radius: 10px;
    overflow: hidden;
    /* No scrim, so the shadow alone has to separate it from the grid. */
    box-shadow: 0 18px 60px rgb(0 0 0 / 0.5), 0 2px 10px rgb(0 0 0 / 0.35);
  }
  .float-head {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 5px 10px;
    background: var(--surface);
    border-bottom: 1px solid var(--border-strong);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.08em;
    text-transform: uppercase;
    color: var(--ink-meta);
    cursor: grab;
    flex-shrink: 0;
    user-select: none;
    -webkit-user-select: none;
    /* Pointer-capture drag on touch needs the browser's pan gesture off. */
    touch-action: none;
  }
  .float-head:active { cursor: grabbing; }
  .float-pane.full { border: none; border-radius: 0; box-shadow: none; }
  .float-pane.full .float-head { cursor: default; }
  .float-head button.float-max {
    display: flex;
    align-items: center;
    color: var(--ink-meta);
    padding: 2px 5px;
    border-radius: 6px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .float-head button.float-max:hover { color: var(--sprite); background: var(--surface-warm); }
  .float-t {
    flex: 1;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .float-x {
    color: var(--ink-meta);
    font-size: calc(15px * var(--tscale-ui));
    line-height: 1;
    padding: 1px 7px;
    border-radius: 6px;
    cursor: pointer;
  }
  .float-x:hover { color: var(--bad); background: var(--surface-warm); }
  .float-body {
    flex: 1;
    min-height: 0;
    display: flex;
  }
  /* Resize handles: invisible hit strips over the edges, corners on top so
     their diagonal cursors win. touch-action off — pointer capture drives. */
  .rh { position: absolute; touch-action: none; }
  .rh-n { top: 0; left: 8px; right: 8px; height: 5px; cursor: ns-resize; }
  .rh-s { bottom: 0; left: 8px; right: 8px; height: 5px; cursor: ns-resize; }
  .rh-e { right: 0; top: 8px; bottom: 8px; width: 5px; cursor: ew-resize; }
  .rh-w { left: 0; top: 8px; bottom: 8px; width: 5px; cursor: ew-resize; }
  .rh-ne { top: 0; right: 0; width: 11px; height: 11px; cursor: nesw-resize; }
  .rh-nw { top: 0; left: 0; width: 11px; height: 11px; cursor: nwse-resize; }
  .rh-se { bottom: 0; right: 0; width: 11px; height: 11px; cursor: nwse-resize; }
  .rh-sw { bottom: 0; left: 0; width: 11px; height: 11px; cursor: nesw-resize; }

  /* The embedded pane fills the window; the window owns border and radius. */
  .float-body :global(.pane) {
    flex: 1;
    min-width: 0;
    border: none;
    border-radius: 0;
  }
  /* Phones: the head's controls are the only way out of a full-screen
     window, so give them a finger-sized target (previously 17px). */
  @media (max-width: 699px) {
    .float-x, .float-max, .float-open {
      min-height: 40px;
      min-width: 40px;
      display: inline-flex;
      align-items: center;
      justify-content: center;
    }
  }
</style>
