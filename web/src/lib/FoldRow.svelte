<script>
  /**
   * FoldRow — one dim line standing in for a run of daemon watch chatter
   * (render.js foldNoise, keeping replies from getting buried in chat).
   * Collapsed it is a single line saying
   * WHAT happened and how many times; tapped it expands and renders each folded
   * row with the SAME components ChatPane uses inline (PeerMessage,
   * ReplyMessage), so folded rows look and act exactly like inline rows.
   * It drops nothing — it only stops three byte-identical `[delegation watch]` notices from occupying the
   * same vertical space as the reply you came to read.
   */
  import { fmtTs, fmtTsFull } from './time.js';
  import PeerMessage from './PeerMessage.svelte';
  import ReplyMessage from './ReplyMessage.svelte';

  // archived .. onreply are only passed through to ReplyMessage.
  let { item, archived = false, activeReply = null, onreply } = $props();

  let open = $state(false);

  // "watcher ×4" — every row class inside, so a mixed run can't masquerade as
  // something it isn't.
  const head = $derived(`${(item.labels ?? []).join(' + ')} ×${item.count}`);
</script>

<div class="fold-group">
  <div class="fold" class:open role="button" tabindex="0" onclick={() => (open = !open)}
    onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); open = !open; } }}
    aria-expanded={open}
    title={`tap to ${open ? 'collapse' : 'expand ' + item.count + ' folded line' + (item.count === 1 ? '' : 's')}`}>
    <span class="caret">{open ? '▾' : '▸'}</span>
    <span class="cnt">{head}</span>
    <span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>
    {#if !open}<span class="head">{item.head}</span>{/if}
  </div>
  <!-- The rows sit OUTSIDE the clickable header, so a tap on a reply button,
       a card or a note inside a folded row does not collapse the fold. -->
  {#if open}
    <div class="rows">
      {#each item.items as it, j (j)}
        {#if it.kind === 'peer'}
          <PeerMessage item={it} />
        {:else if it.kind === 'reply'}
          <ReplyMessage item={it} {archived} {activeReply} {onreply} />
        {:else if it.kind === 'tool'}
          <div class="tline">{it.line}</div>
        {/if}
      {/each}
    </div>
  {/if}
</div>

<style>
  /* One line, styled like the tool line it sits among: quieter than a message,
     never competing with a reply for attention. */
  .fold {
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-tool));
    color: var(--ink-meta);
    padding: 3px 14px 3px 24px;
    position: relative;
    cursor: pointer;
    line-height: 1.5;
    display: flex;
    flex-wrap: nowrap;
    align-items: baseline;
    gap: 6px;
  }
  .fold::before {
    content: '';
    position: absolute;
    left: 13px;
    top: 9px;
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--ink-meta);
    opacity: 0.45;
  }
  .fold:hover { color: var(--ink-dim); }
  .fold .caret { color: var(--ink-meta); }
  .fold .cnt { letter-spacing: 0.06em; white-space: nowrap; }
  .fold .ts { color: var(--ink-meta); opacity: 0.75; font-size: 90%; white-space: nowrap; }
  /* The first folded notice's own words, clipped to the row so a collapsed fold
     can never grow taller than one line. */
  .fold .head {
    flex: 1 1 auto;
    min-width: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    opacity: 0.8;
  }
  .rows {
    margin: 4px 0 2px 24px;
    border-left: 2px solid var(--border-strong);
    padding-left: 6px;
  }
  .rows .tline {
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-tool));
    color: var(--ink-meta);
    padding: 2px 0;
  }
</style>
