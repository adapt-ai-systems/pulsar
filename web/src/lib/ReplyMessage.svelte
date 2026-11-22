<script>
  /**
   * One finished reply from the pulse. The ONE copy: ChatPane renders it inline
   * and FoldRow renders it inside an expanded fold, so a folded reply gets
   * HTML cards, hub preview and the reply button too. The shared `.msg` base,
   * `.dreamer` and `.md` CSS lives in ChatPane, which also draws the streaming
   * ghost with it.
   */
  import { fmtTs, fmtTsFull } from './time.js';
  import { renderMarkdown } from './markdown.js';
  import { htmlCards, cardify } from './htmlcards.svelte.js';
  import Icon from './Icon.svelte';

  /**
   * @type {{
   *   item: any,
   *   archived?: boolean,
   *   activeReply?: any,
   *   onreply?: (item: any) => void,
   * }}
   */
  let { item, archived = false, activeReply = null, onreply } = $props();
</script>

<div class="msg dreamer">
  <div class="lbl">REPLY{#if item.ts}<span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>{/if}</div>
  <!-- Markdown only on the model's side. Your own text stays literal:
       you type globs and paths (**/*.js, some_var) far more often than
       you type emphasis. See markdown.js for the escaping guarantees. -->
  {#if item.notes}
    <details class="body sys-note">
      <summary>Handoff notes for the next session</summary>
      <!-- eslint-disable-next-line svelte/no-at-html-tags -->
      <div class="md">{@html renderMarkdown(item.text)}</div>
    </details>
  {:else}
    <!-- eslint-disable-next-line svelte/no-at-html-tags -->
    <div class="body md" use:cardify={{ on: htmlCards.on, text: item.text }}>{@html renderMarkdown(item.text)}</div>
  {/if}
  {#if !archived}
    <!-- Reply button: quote this message into the next send so the pulse knows
         which earlier message the user means. -->
    <div class="reply-row">
      <button
        class="reply-btn"
        class:on={activeReply !== null && activeReply.ts === (item.ts ?? null) && activeReply.text === item.text}
        onclick={() => onreply?.(item)}
        title="reply to this message"
        aria-label="reply to this message"
      ><Icon name="reply" size={11} stroke={2} /> reply</button>
    </div>
  {/if}
</div>

<style>
  /* Reply button: bottom-right of each reply bubble; quiet until hovered. */
  .reply-row { display: flex; justify-content: flex-end; margin-top: 2px; }
  .reply-btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 1px 6px;
    border: 1px solid transparent;
    border-radius: 6px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-meta);
    opacity: 0.75;
  }
  .reply-btn:hover, .reply-btn.on {
    opacity: 1;
    color: var(--dreamer);
    border-color: color-mix(in srgb, var(--dreamer) 40%, transparent);
  }
  .sys-note summary { cursor: pointer; color: var(--muted, #8b95a8); }
</style>
