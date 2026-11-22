<script>
  import { fmtTs, fmtTsFull } from './time.js';
  import { shortPath } from './toolcards.js';
  import ToolLine from './ToolLine.svelte';
  // Visual-tools rendering of one tool call: an edit is a diff card, a write
  // is a file-created card with a preview, a shell call is a command block
  // with its exit code,
  // reads/searches are quiet one-liners. Falls back to ToolLine when the
  // item has no card. Everything is derived from item.card (toolcards.js) —
  // no vessel-specific logic lives here.
  let { item, elapsed = null, pulseId = null } = $props();
  const c = $derived(item.card);
  // Diffs and previews start open for edits/writes (that's the point of the
  // mode); shell output starts open only when it failed.
  let open = $state(null);
  const isOpen = $derived(open ?? (c?.kind === 'edit' || c?.kind === 'write' || (c?.kind === 'shell' && item.err)));
  const hunks = $derived(c?.hunks ?? []);
  const lineCount = $derived(hunks.reduce((n, h) => n + h.rows.length, 0));
  const DIFF_MAX = 60;
  let showAll = $state(false);
  // Clip the combined diff at DIFF_MAX rows across hunks until "show all".
  const shownHunks = $derived.by(() => {
    if (showAll) return hunks;
    let left = DIFF_MAX;
    const out = [];
    for (const h of hunks) {
      if (left <= 0) break;
      out.push(h.rows.length <= left ? h : { ...h, rows: h.rows.slice(0, left) });
      left -= h.rows.length;
    }
    return out;
  });
  const resultText = $derived((item.resultSnippet || '').trimEnd());
  function toggle() { open = !isOpen; }
</script>

{#if !c}
  <ToolLine {item} {elapsed} {pulseId} />
{:else if c.kind === 'read' || c.kind === 'search' || c.kind === 'fetch' || c.kind === 'agent'}
  <div class="quiet" class:err={item.err} class:live={item.live}>
    <span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>
    {#if c.kind === 'read'}
      <span class="verb">read</span> <span class="path" title={c.path}>{shortPath(c.path)}</span>{#if c.offset != null || c.limit != null}<span class="dim"> lines {c.offset ?? 1}{c.limit != null ? `–${(c.offset ?? 1) + c.limit - 1}` : '+'}</span>{/if}
    {:else if c.kind === 'search'}
      <span class="verb">{c.name === 'glob' || c.name === 'list_dir' || c.name === 'ls' ? 'listed' : 'searched'}</span> <span class="q">{c.query}</span>{#if c.scope && c.scope !== c.query}<span class="dim"> in {c.scope}</span>{/if}
    {:else if c.kind === 'fetch'}
      <span class="verb">fetched</span> <span class="q">{c.url}</span>
    {:else}
      <span class="verb">agent</span> <span class="q">{c.description}</span>
    {/if}
    {#if elapsed !== null}<span class="progress"> · running {elapsed}s</span>{/if}
    {#if item.res}<span class="res"> {item.res}</span>{/if}
  </div>
{:else}
  <div class="card {c.kind}" class:err={item.err} class:live={item.live} class:open={isOpen}>
    <div class="head" role="button" tabindex="0" onclick={toggle} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } }}>
      <span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>
      {#if c.kind === 'edit'}
        <span class="badge edit">edit</span>
        <span class="path" title={c.path}>{shortPath(c.path) || '(file)'}</span>
        <span class="stat"><span class="plus">+{c.added}</span> <span class="minus">−{c.removed}</span></span>
        {#if c.replaceAll}<span class="dim">all occurrences</span>{/if}
      {:else if c.kind === 'write'}
        <span class="badge write">write</span>
        <span class="path" title={c.path}>{shortPath(c.path) || '(file)'}</span>
        <span class="stat"><span class="plus">+{c.lines}</span> <span class="dim">line{c.lines === 1 ? '' : 's'}</span></span>
      {:else}
        <span class="badge shell" class:bad={item.err}>$</span>
        <span class="cmd-title">{c.description || c.command.split('\n')[0]}</span>
        {#if item.stopped}<span class="exit">stopped</span>{:else if item.exitCode != null}<span class="exit" class:bad={item.exitCode !== 0}>exit {item.exitCode}</span>{:else if item.err}<span class="exit bad">failed</span>{:else if item.done}<span class="exit ok">ok</span>{/if}
      {/if}
      {#if elapsed !== null}<span class="progress">running {elapsed}s</span>{/if}
      <span class="chev">{isOpen ? '▾' : '▸'}</span>
    </div>
    {#if isOpen}
      {#if c.kind === 'edit'}
        {#if hunks.length}
          {#each shownHunks as h, hi}
            <pre class="diff" class:next={hi > 0}>{#each h.rows as r}<span class="ln {r.t === '+' ? 'add' : r.t === '-' ? 'del' : 'ctx'}">{r.t}{r.s}
</span>{/each}</pre>
          {/each}
          {#if lineCount > DIFF_MAX && !showAll}
            <button class="more" onclick={() => (showAll = true)}>show all {lineCount} lines</button>
          {/if}
        {:else}
          <div class="dim pad">no diff text in this call</div>
        {/if}
      {:else if c.kind === 'write'}
        <pre class="preview">{c.preview}</pre>
        {#if c.more > 0}<div class="dim pad">… {c.more} more line{c.more === 1 ? '' : 's'}</div>{/if}
      {:else}
        <pre class="cmd">{c.command}</pre>
        {#if resultText}<pre class="out" class:bad={item.err}>{resultText}</pre>{/if}
      {/if}
      {#if c.kind !== 'shell' && item.err && resultText}
        <pre class="out bad">{resultText}</pre>
      {/if}
    {/if}
  </div>
{/if}

<style>
  .quiet, .card { font-family: var(--font-mono); font-size: calc(10.5px * var(--tscale-tool)); color: var(--ink-meta); line-height: 1.5; word-break: break-word; }
  /* The chat is a flex column: without flex-shrink 0 an overflow:hidden card
     collapses to its 2px of border on mobile. */
  .quiet { padding: 2px 14px 2px 24px; position: relative; flex: 0 0 auto; }
  .quiet::before { content: ""; position: absolute; left: 13px; top: 9px; width: 4px; height: 4px; border-radius: 50%; background: var(--ink-meta); opacity: 0.55; }
  .quiet.live { color: var(--dreamer); }
  .quiet.live::before { background: var(--dreamer); opacity: 1; animation: pulse 1.2s infinite; }
  .quiet.err { color: var(--bad); }
  .ts { color: var(--ink-meta); opacity: 0.75; margin-right: 6px; white-space: nowrap; font-size: 90%; }
  .verb { opacity: 0.8; }
  .path, .q { color: var(--ink-dim); }
  .dim { opacity: 0.7; }
  .res { color: color-mix(in srgb, var(--ink-dim) 70%, var(--ink-meta)); }
  .progress { color: var(--dreamer); }

  .card { flex: 0 0 auto; margin: 4px 14px 4px 20px; border: 1px solid var(--border); border-radius: 7px; background: color-mix(in srgb, var(--ink-meta) 5%, var(--surface)); overflow: hidden; }
  .card.live { border-color: color-mix(in srgb, var(--dreamer) 55%, var(--border)); }
  .card.err { border-color: color-mix(in srgb, var(--bad) 55%, var(--border)); }
  .head { display: flex; align-items: center; gap: 7px; padding: 4px 8px; cursor: pointer; min-width: 0; }
  .head:hover { background: color-mix(in srgb, var(--ink-meta) 6%, transparent); }
  .head .path, .head .cmd-title { color: var(--ink-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 auto; }
  .badge { font-size: 85%; padding: 0 5px; border-radius: 4px; border: 1px solid var(--border); text-transform: uppercase; letter-spacing: 0.04em; flex: 0 0 auto; }
  .badge.edit { color: #d9a441; border-color: color-mix(in srgb, #d9a441 45%, var(--border)); }
  .badge.write { color: #4fbf7a; border-color: color-mix(in srgb, #4fbf7a 45%, var(--border)); }
  .badge.shell { color: var(--ink-dim); }
  .badge.shell.bad { color: var(--bad); }
  .stat { flex: 0 0 auto; }
  .plus { color: #4fbf7a; }
  .minus { color: #e0605c; }
  .exit { flex: 0 0 auto; font-size: 85%; opacity: 0.85; }
  .exit.ok { color: #4fbf7a; }
  .exit.bad { color: var(--bad); }
  .chev { flex: 0 0 auto; opacity: 0.6; }
  pre { margin: 0; font: inherit; white-space: pre-wrap; word-break: break-word; font-size: calc(10px * var(--tscale-tool)); }
  .diff { border-top: 1px solid var(--border); padding: 4px 0; }
  .diff.next { border-top: 1px dashed var(--border); }
  .diff .ln { display: block; padding: 0 8px; }
  .diff .add { background: rgba(79, 191, 122, 0.14); color: color-mix(in srgb, #4fbf7a 70%, var(--ink-dim)); }
  .diff .del { background: rgba(224, 96, 92, 0.14); color: color-mix(in srgb, #e0605c 70%, var(--ink-dim)); }
  .diff .ctx { color: var(--ink-meta); }
  .preview, .cmd { border-top: 1px solid var(--border); padding: 5px 8px; color: var(--ink-dim); }
  .out { border-top: 1px dashed var(--border); padding: 5px 8px; color: var(--ink-meta); max-height: 240px; overflow: auto; }
  .out.bad { color: var(--bad); }
  .pad { padding: 4px 8px; }
  .more { font: inherit; color: var(--dreamer); background: none; border: 0; padding: 3px 8px; cursor: pointer; }
  @keyframes pulse { 0%, 100% { transform: scale(1); opacity: 1; } 50% { transform: scale(0.7); opacity: 0.6; } }
</style>
