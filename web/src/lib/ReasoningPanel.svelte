<script>
  /**
   * The reasoning panel: click a pulse's "working" status strip in a pane, or
   * the working label on its wall card, and this shows the model's reasoning trace.
   *
   * The trace is the runner's ephemeral reasoning.json — display-only, never a
   * durable event. It survives the turn boundary (the last turn stays readable)
   * until the next turn starts. Live pushes arrive over /live; a 500ms HTTP
   * poll covers a down socket.
   *
   * One instance is mounted by the page; the open state lives in
   * reasoningstore.svelte.js.
   */
  import { live } from './live.svelte.js';
  import { readableThinking } from './render.js';
  import { getReasoning, reasoningOn } from './api.js';
  import { closeReasoning, reasoningPanel } from './reasoningstore.svelte.js';

  const open = $derived(reasoningPanel.pulseId !== null);
  let text = $state(null);
  let loaded = $state(false);
  let bodyEl = $state(null);

  const pulse = $derived(
    (live.roster ?? []).find((p) => p.id === reasoningPanel.pulseId) ?? null
  );
  const busy = $derived(Boolean(pulse?.live?.busy_since));
  const running = $derived(pulse?.state === 'running');
  const phase = $derived(
    !pulse ? '' : busy ? 'live' : running ? 'idle' : 'stopped'
  );

  // Turned off in settings while the panel is open: close it rather than keep
  // showing a trace the pulse's own setting now hides. Only acts once the
  // roster has actually delivered the pulse (null = not loaded, not "off").
  $effect(() => {
    if (open && pulse && !reasoningOn(pulse)) closeReasoning();
  });

  function apply(t) {
    text = typeof t === 'string' && t.length ? t : null;
    loaded = true;
  }

  function nearBottom() {
    if (!bodyEl) return true;
    return bodyEl.scrollHeight - bodyEl.scrollTop - bodyEl.clientHeight < 40;
  }

  // Subscribe while open: /live pushes when the socket is up, else poll.
  // Reading live.connected here makes the effect re-run when the socket
  // state changes, so a reconnect swaps the fallback poll for pushes.
  $effect(() => {
    const id = reasoningPanel.pulseId;
    const connected = live.connected;
    if (!id) return;
    text = null;
    loaded = false;
    if (connected) {
      const off = live.watch(id, 100, () => {}, undefined, (msg) =>
        apply(msg.text)
      );
      return off;
    }
    let alive = true;
    const poll = async () => {
      const r = await getReasoning(id);
      if (alive && r.ok) apply(r.value.reasoning?.text);
    };
    poll();
    const timer = setInterval(poll, 500);
    return () => {
      alive = false;
      clearInterval(timer);
    };
  });

  // Follow the growing trace only when already parked at the bottom — the
  // same rule the chat pane uses, so scrolling back to read is not yanked.
  $effect(() => {
    if (!text || !bodyEl || !nearBottom()) return;
    queueMicrotask(() => {
      if (bodyEl) bodyEl.scrollTop = bodyEl.scrollHeight;
    });
  });
</script>

<svelte:window
  onkeydown={(e) => {
    if (e.key === 'Escape' && open) closeReasoning();
  }}
/>

{#if open}
  <div class="trace" role="dialog" aria-label="reasoning trace" data-reasoning-panel>
    <div class="head-row">
      <span class="ttl" title={reasoningPanel.title}>
        reasoning · {reasoningPanel.title || 'pulse'}
      </span>
      {#if phase}
        <span class="state" class:live={busy}>{phase}</span>
      {/if}
      <button class="close" onclick={closeReasoning} title="close" aria-label="close">✕</button>
    </div>
    <div class="body" bind:this={bodyEl}>
      {#if !loaded}
        <p class="empty">reading…</p>
      {:else if text}
        <pre>{readableThinking(text)}</pre>
      {:else if busy}
        <p class="empty">
          working, but no reasoning has arrived yet — this model may not expose
          a reasoning channel.
        </p>
      {:else}
        <p class="empty">
          no reasoning trace right now. It appears here while the model thinks,
          and the last turn's trace stays until the next turn starts.
        </p>
      {/if}
    </div>
  </div>
{/if}

<style>
  /* A floating window, not a modal: the panes behind stay live and un-dimmed.
     Bottom-right keeps it clear of the composer and the top-right menus. */
  .trace {
    position: fixed;
    right: 14px;
    bottom: 14px;
    width: min(560px, calc(100% - 24px));
    max-height: min(62vh, 560px);
    display: flex;
    flex-direction: column;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.6);
    z-index: 70;
    overflow: hidden;
  }
  .head-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 10px 12px;
    border-bottom: 1px solid var(--border, var(--border-strong));
    flex: 0 0 auto;
  }
  .ttl {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.18em;
    color: var(--ink-dim);
    text-transform: uppercase;
  }
  .state {
    flex: 0 0 auto;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 2px 8px;
  }
  .state.live {
    color: var(--dreamer);
    border-color: color-mix(in srgb, var(--dreamer) 55%, transparent);
    animation: tracePulse 1.6s ease-in-out infinite;
  }
  @keyframes tracePulse {
    0%, 100% { opacity: 1; }
    50% { opacity: 0.45; }
  }
  @media (prefers-reduced-motion: reduce) {
    .state.live { animation: none; }
  }
  .close {
    flex: 0 0 auto;
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
    padding: 2px 6px;
    border-radius: 5px;
  }
  .close:hover {
    background: var(--surface-warm);
    color: var(--ink-dim);
  }
  .body {
    flex: 1 1 auto;
    overflow: auto;
    padding: 12px;
  }
  .body pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    line-height: 1.55;
    color: var(--ink-dim, var(--ink));
  }
  .empty {
    margin: 0;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    line-height: 1.6;
    color: var(--ink-meta);
  }
</style>