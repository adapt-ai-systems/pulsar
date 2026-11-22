<script>
  /**
   * One finished thinking block in the chat: the durable `reasoning` event the runner writes when the model moves from thinking
   * to a reply or a tool call. Collapsed to one line until tapped, so the
   * chat reads as replies and tool calls with the thinking available in
   * place — the floating ReasoningPanel keeps the whole turn's trace.
   */
  import { fmtTs, fmtTsFull } from './time.js';
  import { readableThinking } from './render.js';

  let { item } = $props();
  let open = $state(false);

  const head = $derived.by(() => {
    const s = readableThinking(item.text).split('\n').find((l) => l.trim()) ?? '';
    const t = s.trim();
    return t.length > 110 ? `${t.slice(0, 110)}…` : t;
  });

  function toggle() {
    open = !open;
  }
</script>

<div
  class="think"
  class:open
  onclick={toggle}
  role="button"
  tabindex="0"
  aria-expanded={open}
  title={open ? 'tap to collapse' : 'tap to read the thinking'}
  onkeydown={(e) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggle();
    }
  }}
>
  <div class="lbl">
    <span class="tri">{open ? '▾' : '▸'}</span>THINKING{#if item.ts}<span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>{/if}
    {#if !open}<span class="head">{head}</span>{/if}
  </div>
  {#if open}
    <pre>{readableThinking(item.text)}</pre>
  {/if}
</div>

<style>
  .think {
    padding: 4px 14px 4px 14px;
    cursor: pointer;
    border-radius: 6px;
  }
  .think:hover { background: var(--surface-warm); }
  .lbl {
    display: flex;
    align-items: baseline;
    gap: 6px;
    min-width: 0;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.18em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .tri {
    flex: 0 0 auto;
    letter-spacing: 0;
    width: 0.9em;
  }
  .ts {
    flex: 0 0 auto;
    letter-spacing: 0.06em;
    opacity: 0.8;
  }
  .head {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    letter-spacing: 0;
    text-transform: none;
    font-size: calc(10.5px * var(--tscale-tool));
    color: var(--ink-meta);
  }
  pre {
    margin: 6px 0 2px;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-chat));
    line-height: 1.5;
    color: var(--ink-dim, var(--ink));
  }
</style>
