<script>
  import { todoGroups } from './toolcards.js';
  import { fmtTs, fmtTsFull } from './time.js';
  // The pulse's task list: one expandable card sitting right above the
  // idle/working strip. Collapsed it is one line — "TODO 2/7 ·
  // now: <in-progress item>"; expanded it is the tree with a checkbox per
  // item and a done/total count per group. State comes from foldTodos()
  // over the pane's events, so every vessel's task tool lands here.
  let { todo } = $props();
  let open = $state(false);
  const groups = $derived(todoGroups(todo));
  const now = $derived(todo?.active ? (todo.active.activeForm || todo.active.content) : '');
  const allDone = $derived(todo && todo.total > 0 && todo.done === todo.total);
  function toggle() { open = !open; }
</script>

{#if todo && todo.total}
  <div class="todo" class:open class:done={allDone}>
    <div class="row" role="button" tabindex="0" onclick={toggle} onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } }}>
      <span class="chev">{open ? '▾' : '▸'}</span>
      <span class="lbl">TODO</span>
      <span class="count" class:done={allDone}>{todo.done}/{todo.total}</span>
      {#if !open && now}<span class="now" title={now}>· {now}</span>{/if}
      {#if !open && !now && allDone}<span class="now">· all done</span>{/if}
      {#if todo.ts}<span class="upd" title={fmtTsFull(todo.ts)}>updated {fmtTs(todo.ts)}</span>{/if}
    </div>
    {#if open}
      <div class="tree">
        {#each groups as g}
          {#if g.name}
            <div class="grp">{g.name} <span class="gcount">· {g.done}/{g.items.length}</span></div>
          {/if}
          {#each g.items as t (t.id)}
            <div class="item {t.status}" class:sub={!!g.name} title={t.activeForm || t.content}>
              <span class="box">{t.status === 'completed' ? '☑' : t.status === 'in_progress' ? '▸' : '☐'}</span>
              <span class="txt">{t.content}</span>
              {#if t.status === 'completed' && t.doneTs}<span class="when" title={`done ${fmtTsFull(t.doneTs)}`}>{fmtTs(t.doneTs)}</span>
              {:else if t.status === 'in_progress' && t.startedTs}<span class="when" title={`started ${fmtTsFull(t.startedTs)}`}>since {fmtTs(t.startedTs)}</span>
              {:else if t.createdTs}<span class="when dim" title={`added ${fmtTsFull(t.createdTs)}`}>{fmtTs(t.createdTs)}</span>{/if}
            </div>
          {/each}
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .todo {
    flex: 0 0 auto;
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-tool));
    color: var(--ink-meta);
    border-top: 1px solid var(--border);
    background: color-mix(in srgb, var(--ink-meta) 4%, var(--surface));
    line-height: 1.5;
  }
  .row { display: flex; align-items: baseline; gap: 6px; padding: 3px 12px; cursor: pointer; min-width: 0; }
  .row:hover { background: color-mix(in srgb, var(--ink-meta) 6%, transparent); }
  .chev { opacity: 0.6; }
  .lbl { color: var(--dreamer); letter-spacing: 0.08em; font-size: 90%; }
  .count { color: var(--ink-dim); }
  .count.done { color: #4fbf7a; }
  .now { color: var(--ink-dim); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; min-width: 0; flex: 1 1 auto; }
  .upd { margin-left: auto; opacity: 0.7; font-size: 90%; white-space: nowrap; flex: 0 0 auto; }
  .tree { padding: 0 12px 6px 18px; max-height: 40vh; overflow: auto; }
  .grp { color: var(--ink-dim); margin-top: 3px; }
  .gcount { color: var(--ink-meta); }
  .item { display: flex; gap: 7px; align-items: baseline; padding-left: 4px; }
  .item.sub { padding-left: 16px; }
  .item .box { width: 1.2em; text-align: center; flex: 0 0 auto; }
  .item.completed { color: #4fbf7a; }
  .item.completed .txt { opacity: 0.8; }
  .item.in_progress { color: var(--dreamer); }
  .item.pending .txt { color: var(--ink-dim); }
  .txt { overflow-wrap: anywhere; flex: 1 1 auto; }
  .when { margin-left: auto; padding-left: 8px; opacity: 0.75; font-size: 90%; white-space: nowrap; flex: 0 0 auto; }
  .when.dim { opacity: 0.45; }
</style>
