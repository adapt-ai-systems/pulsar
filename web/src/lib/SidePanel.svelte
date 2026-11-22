<script>
  /**
   * SidePanel — the sheet that slides out beside the SideRail (desktop only).
   * Owns nothing but the frame: title, close, scrim. Content is a snippet.
   */
  // flush: no body padding/scroll (the child fills the body). width: CSS width.
  // docked: a page column (no scrim, no overlay); the page sets its width.
  let { title, onclose, children, flush = false, width = null, head = null, docked = false } = $props();

  function onKey(e) {
    if (e.key === 'Escape') onclose?.();
  }
</script>

<svelte:window onkeydown={onKey} />

{#if !docked}<div class="scrim" onclick={onclose} role="presentation"></div>{/if}
<div class="panel" class:docked role={docked ? 'region' : 'dialog'} aria-modal={docked ? undefined : 'true'} aria-label={title} style={width && !docked ? `width: min(${width}, calc(100vw - var(--rail-w, 44px)))` : undefined}>
  <header class="head">
    <h2>{title}</h2>
    {@render head?.()}
    <button class="close" onclick={onclose} aria-label="close">✕</button>
  </header>
  <div class="body" class:flush>
    {@render children?.()}
  </div>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: rgba(2, 3, 5, 0.35);
    z-index: 60;
  }
  .panel {
    position: fixed;
    top: 0;
    bottom: 0;
    /* Sits flush against the rail (--rail-w set by the page). */
    left: var(--rail-w, 44px);
    width: min(420px, calc(100vw - var(--rail-w, 44px)));
    background: var(--surface);
    border-right: 1px solid var(--border-strong);
    z-index: 70;
    display: flex;
    flex-direction: column;
    box-shadow: 12px 0 40px rgba(0, 0, 0, 0.45);
    animation: panel-in 140ms ease-out;
  }
  @keyframes panel-in {
    from { transform: translateX(-12px); opacity: 0; }
    to { transform: none; opacity: 1; }
  }
  @media (prefers-reduced-motion: reduce) { .panel { animation: none; } }
  .panel.docked { position: relative; inset: auto; left: auto; width: 100%; height: 100%; z-index: auto; box-shadow: none; animation: none; }
  .head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 10px;
    padding: 12px 14px;
    border-bottom: 1px solid var(--border);
    flex-shrink: 0;
  }
  .head h2 {
    margin: 0;
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-dim);
  }
  .close {
    color: var(--ink-meta);
    padding: 4px 8px;
    border-radius: 6px;
    font-size: calc(13px * var(--tscale-ui));
  }
  .close:hover { background: var(--surface-warm); color: var(--ink-dim); }
  .body.flush { padding: 0; overflow: hidden; position: relative; }
  .body { flex: 1; min-height: 0; overflow-y: auto; padding: 10px 12px 20px; }
</style>
