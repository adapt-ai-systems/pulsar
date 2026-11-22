<script>
  /**
   * WatchBar — the strip above a pane's status rail: "N watches". Click to
   * expand upward into the list (what this pulse is waiting on), click a
   * row's × to drop a watch. Hidden entirely when the pulse has none to keep
   * the wall uncluttered.
   */
  import { watchStore, subscribeWatches, removeWatch, watchesFor } from './watchstore.svelte.js';
  import { watchLabel, watchDetail } from './watches.js';

  let { pulseId } = $props();

  let open = $state(false);
  let confirmId = $state(null);

  const mine = $derived(watchesFor(watchStore.watches, pulseId));
  const met = $derived(mine.filter((w) => !!w.bad).length);
  // Collapsed head carries the watcher info itself, not just a count: one
  // watch → its label and detail; several → their labels joined, ellipsized by CSS.
  const headInfo = $derived(
    mine.length === 1
      ? `${watchLabel(mine[0])} · ${watchDetail(mine[0])}`
      : mine.map((w) => watchLabel(w)).join(' · ')
  );

  $effect(() => subscribeWatches());

  const GLYPH = { pulse_state: '◉', file: '▤', log: '≡', http: '⇄', cmd: '$' };
</script>

{#if mine.length > 0}
  <div class="watchbar" class:open class:met={met > 0}>
    {#if open}
      <div class="list">
        {#each mine as w (w.id)}
          <div class="row" class:bad={!!w.bad}>
            <span class="kind">{GLYPH[w.kind] ?? '?'}</span>
            <div class="info">
              <div class="lbl" title={watchLabel(w)}>{watchLabel(w)}</div>
              <div class="sub" title={JSON.stringify(w.spec)}>{watchDetail(w)}</div>
            </div>
            {#if confirmId === w.id}
              <button class="x armed" onclick={() => { removeWatch(w.id); confirmId = null; }}>confirm?</button>
              <button class="x" onclick={() => (confirmId = null)} aria-label="cancel">✕</button>
            {:else}
              <button class="x" title="delete watch" onclick={() => (confirmId = w.id)}>×</button>
            {/if}
          </div>
        {/each}
      </div>
    {/if}
    <button
      class="head"
      onclick={() => (open = !open)}
      aria-expanded={open}
      title={open ? 'collapse' : 'show what this pulse is waiting on'}
    >
      <span class="eye" aria-hidden="true">◉</span>
      <span class="count">{mine.length} {mine.length === 1 ? 'watch' : 'watches'}{#if met > 0} · {met} met{/if}</span>
      <span class="hinfo" title={headInfo}>{headInfo}</span>
      <span class="chev" aria-hidden="true">{open ? '▾' : '▴'}</span>
    </button>
  </div>
{/if}

<style>
  .watchbar {
    display: flex;
    flex-direction: column;
    flex-shrink: 0;
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--warden) 4%, var(--surface));
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: color-mix(in srgb, var(--warden) 70%, var(--ink-meta));
  }
  .watchbar.met { color: var(--warden); }
  .head {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 3px 12px;
    text-align: left;
    color: inherit;
    letter-spacing: 0.04em;
  }
  .head:hover { background: color-mix(in srgb, var(--warden) 8%, var(--surface)); }
  .eye { font-size: 9px; }
  .chev { margin-left: auto; opacity: 0.6; }
  .count { flex-shrink: 0; }
  .hinfo {
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    opacity: 0.8;
  }
  .list {
    display: flex;
    flex-direction: column;
    max-height: 40%;
    max-height: min(220px, 40vh);
    overflow-y: auto;
    padding: 4px 8px 2px;
    border-bottom: 1px dashed var(--border);
  }
  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 4px;
    border-radius: 4px;
    color: var(--ink-dim);
  }
  .row:hover { background: var(--surface-warm); }
  .row.bad { box-shadow: inset 2px 0 0 var(--warden); }
  .kind { width: 12px; text-align: center; color: var(--ink-meta); flex-shrink: 0; }
  .info { flex: 1; min-width: 0; }
  .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; font-family: var(--font-sans); font-size: calc(11.5px * var(--tscale-ui)); }
  .sub { color: var(--ink-meta); white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .x {
    flex-shrink: 0;
    color: var(--ink-meta);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0 5px;
    line-height: 1.5;
  }
  .x:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .x.armed { border-color: var(--bad); color: var(--bad); }
</style>
