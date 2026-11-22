<script>
  /**
   * ColResize — a vertical drag divider that resizes the column to its LEFT.
   * The page owns the width; this only reports it. Double-click resets.
   */
  let { width, onresize, onreset, min = 240, label = 'resize column' } = $props();

  let drag = $state(null); // {x, w} at pointerdown

  const clamp = (w) =>
    Math.round(Math.max(min, Math.min(w, (typeof window === 'undefined' ? 4000 : window.innerWidth) - 240)));

  function start(e) {
    e.preventDefault();
    e.currentTarget.setPointerCapture(e.pointerId);
    drag = { x: e.clientX, w: width };
  }
  function move(e) {
    if (drag) onresize?.(clamp(drag.w + e.clientX - drag.x), false);
  }
  function end() {
    if (!drag) return;
    drag = null;
    onresize?.(clamp(width), true);
  }
</script>

<div
  class="col-resize"
  class:dragging={!!drag}
  role="separator"
  aria-orientation="vertical"
  aria-label={label}
  title="drag to resize · double-click to reset"
  onpointerdown={start}
  onpointermove={move}
  onpointerup={end}
  onpointercancel={end}
  ondblclick={() => onreset?.()}
></div>

<style>
  .col-resize {
    flex: 0 0 6px;
    margin-left: -3px;
    margin-right: -3px;
    cursor: col-resize;
    touch-action: none;
    position: relative;
    z-index: 5;
    align-self: stretch;
  }
  .col-resize:hover,
  .col-resize.dragging { background: var(--border-strong); }
</style>
