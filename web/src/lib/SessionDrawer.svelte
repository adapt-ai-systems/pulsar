<script>
  import PulseTree from './PulseTree.svelte';

  /**
   * Global session drawer: the shell around the pulse list — scrim,
   * the docked/overlay panel and the title row. The list itself, with its
   * search, sort, workspaces panel and drag machines, is PulseTree.svelte.
   *
   * @type {{ grids: Array<{id:string,name:string}>, onclose: () => void, onchanged: () => void, onopen?: (p: any) => void }}
   */
  // side: 'left' docks the drawer against the desktop SideRail (every panel
  // opens beside the rail); 'right' is the phone, which has no rail and keeps
  // the slide-in from the right.
  let { grids = [], rosterPulses = null, onclose, onchanged, ongridschanged, onopen, onopengrid, onmovepulse, onreorderworkspaces, side = 'right', docked = false, width = 320 } = $props();
</script>

{#if !docked}
  <div class="scrim" onclick={onclose} role="presentation"></div>
{/if}
<!-- Desktop: the list DOCKS beside the rail as a navigation column that stays
     open while you work — no scrim, nothing closes it but its own ✕ or the
     rail button. The phone keeps the overlay. -->
<aside
  class="drawer on"
  class:left={side === 'left'}
  class:docked
  style={docked ? `width: ${width}px;` : undefined}
  role={docked ? 'navigation' : 'dialog'}
  aria-modal={docked ? undefined : 'true'}
  aria-label="all pulses"
>
  <PulseTree {grids} {rosterPulses} {onchanged} {ongridschanged} {onopen} {onopengrid} {onmovepulse} {onreorderworkspaces} touchBlock="self">
    {#snippet head()}
      <div class="head-row">
        <h2>ALL PULSES</h2>
        <button class="close" onclick={onclose} title="close" aria-label="close">✕</button>
      </div>
    {/snippet}
  </PulseTree>
</aside>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: rgba(2, 3, 5, 0.5);
    z-index: 60;
  }
  /* Task spec: drawer slides from the LEFT. */
  .drawer {
    position: fixed;
    top: 0;
    right: 0;
    bottom: 0;
    width: min(360px, 92%);
    background: var(--surface);
    border-left: 1px solid var(--border-strong);
    z-index: 70;
    display: flex;
    flex-direction: column;
    box-shadow: 12px 0 40px rgba(0, 0, 0, 0.5);
  }
  .drawer.left {
    right: auto;
    left: var(--rail-w, 44px);
    width: min(420px, calc(100vw - var(--rail-w, 44px)));
    border-left: none;
    border-right: 1px solid var(--border-strong);
    animation: drawer-in 140ms ease-out;
  }
  @keyframes drawer-in {
    from { transform: translateX(-12px); opacity: 0; }
    to { transform: none; opacity: 1; }
  }
  @media (prefers-reduced-motion: reduce) { .drawer.left { animation: none; } }
  /* Docked: an in-flow column between the rail and the workspace. */
  .drawer.docked {
    position: relative;
    left: auto;
    right: auto;
    top: auto;
    bottom: auto;
    width: 320px;
    flex: none;
    height: 100%;
    min-height: 0;
    z-index: auto;
    box-shadow: none;
    animation: none;
    border-left: none;
    border-right: 1px solid var(--border-strong);
  }
  .head-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
  }
  .head-row h2 {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.2em;
    color: var(--ink-dim);
  }
  .close {
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
    padding: 2px 6px;
    border-radius: 5px;
  }
  .close:hover { background: var(--surface-warm); color: var(--ink-dim); }

</style>
