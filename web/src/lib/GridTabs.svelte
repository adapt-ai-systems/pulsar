<script>
  import { createGrid } from './api.js';
  import { thinkingState } from './render.js';
  import { isPulseDrag, isGridDrag, PULSE_MIME, GRID_MIME } from './dnd.js';
  import { wsColor, topGrids, childGrids, topOf, gridFamily } from './wscolor.js';
  import { isWsHidden, toggleWsHidden } from './wshidden.svelte.js';
  import { moveId } from './pulsetree.js';

  /**
   * Named grid tabs. An implicit "ALL" tab (selected = null) plus one
   * tab per grid, and a + button that creates a grid via a small inline prompt.
   *
   * The chips are DROP TARGETS: drag a pane by its title bar onto a chip and
   * the pulse moves into that workspace.
   *
   * The chips are also DRAG SOURCES: drag one onto another to reorder the strip,
   * or onto the bin that appears just past the chips while a chip is in flight —
   * dropping there asks the page to confirm the delete.
   *
   * @type {{
   *   grids: Array<{id:string,name:string}>,
   *   selected: string|null,
   *   onselect: (id: string|null) => void,
   *   oncreated: (id?: string) => unknown,
   *   onmovepulse?: (pulseId: string, gridId: string) => void,
   *   onreorder?: (ids: string[]) => void,
   *   ondelete?: (gridId: string) => void
   * }}
   */
  let {
    grids = [],
    pulses = [],
    unseen = {},
    selected = null,
    onselect,
    oncreated,
    onmovepulse,
    onreorder,
    ondelete
  } = $props();

  // Workspace attention: the chip flashes (dreamer, same as the pulse cards)
  // while any pulse inside is working; once a pulse finishes a turn and the
  // workspace is still unseen, its mark in `unseen` turns the chip solid
  // (filled matron, no animation) until the user opens that workspace.
  // A parent chip speaks for its sub-workspaces too.
  function anyGridWorking(gridId) {
    const fam = gridFamily(gridId, grids);
    return pulses.some((x) => fam.has(x.grid_id) && thinkingState(x.live, Date.now()).thinking);
  }
  function gridNeedsAttention(gridId) {
    return [...gridFamily(gridId, grids)].some((id) => unseen[id]) && !anyGridWorking(gridId);
  }

  // Sub-workspaces: the strip shows top-level chips; open one and its
  // sub-workspace chips unfold right after it, smaller, with a + to add
  // another. A sub tab selected keeps its parent chip lit (class family) so
  // the strip still reads as one row.
  const tops = $derived(topGrids(grids));
  const openTop = $derived(selected === null ? null : topOf(selected, grids));

  // --- pulse drops --------------------------------------------------------- //
  // `dragActive` lights every chip the moment a pane drag starts, so the targets
  // announce themselves instead of having to be guessed at. Window-level because
  // the drag begins in a pane, nowhere near this component.
  let dragActive = $state(false);
  let dropId = $state(null); // chip currently under the pointer
  // --- workspace reorder --------------------------------------------------- //
  let gridDragId = $state(null); // the chip being carried
  let gridDropId = $state(null); // the chip it would take the place of
  let binHot = $state(false); // the carried chip is over the bin

  $effect(() => {
    if (!onmovepulse) return;
    const start = (e) => (dragActive = isPulseDrag(e));
    const end = () => {
      dragActive = false;
      dropId = null;
    };
    window.addEventListener('dragstart', start);
    window.addEventListener('dragend', end);
    window.addEventListener('drop', end);
    return () => {
      window.removeEventListener('dragstart', start);
      window.removeEventListener('dragend', end);
      window.removeEventListener('drop', end);
    };
  });

  function onChipDragStart(e, g) {
    if (!onreorder) {
      e.preventDefault();
      return;
    }
    gridDragId = g.id;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData(GRID_MIME, g.id);
    // text/plain is what a chip dropped OUTSIDE the app would paste as; the name
    // is the only useful thing there. Drops inside always read the mime type.
    e.dataTransfer.setData('text/plain', g.name);
  }

  function onChipDragEnd() {
    gridDragId = null;
    gridDropId = null;
    binHot = false;
  }

  function onBinDragOver(e) {
    if (!ondelete || !isGridDrag(e)) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    binHot = true;
  }

  function onBinDrop(e) {
    binHot = false;
    if (!ondelete || !isGridDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    const id = e.dataTransfer.getData(GRID_MIME);
    gridDragId = null;
    gridDropId = null;
    if (id) ondelete(id);
  }

  function onChipDragOver(e, id) {
    if (onmovepulse && isPulseDrag(e)) {
      e.preventDefault(); // required, else the browser refuses the drop
      e.dataTransfer.dropEffect = 'move';
      dropId = id;
      return;
    }
    if (onreorder && isGridDrag(e) && id !== gridDragId) {
      e.preventDefault();
      e.dataTransfer.dropEffect = 'move';
      gridDropId = id;
    }
  }

  function onChipDrop(e, id) {
    if (onmovepulse && isPulseDrag(e)) {
      dropId = null;
      e.preventDefault();
      e.stopPropagation(); // a drop must not also switch tabs
      const pulseId = e.dataTransfer.getData(PULSE_MIME) || e.dataTransfer.getData('text/plain');
      if (pulseId) onmovepulse(pulseId, id);
      return;
    }
    if (!onreorder || !isGridDrag(e)) return;
    e.preventDefault();
    e.stopPropagation();
    const fromId = e.dataTransfer.getData(GRID_MIME);
    gridDropId = null;
    gridDragId = null;
    const order = moveId(grids.map((g) => g.id), fromId, id);
    if (order) onreorder(order);
  }

  let adding = $state(false);
  let addParent = $state(null); // grid id when adding a sub-workspace
  let name = $state('');
  let creating = $state(false);
  let error = $state('');

  function startAdd(parentId = null) {
    adding = true;
    addParent = parentId;
    name = '';
    error = '';
  }
  function cancelAdd() {
    adding = false;
    name = '';
    error = '';
  }

  async function submit() {
    const n = name.trim();
    if (!n || creating) return;
    creating = true;
    error = '';
    const r = await createGrid(n, addParent);
    creating = false;
    if (r.ok) {
      adding = false;
      name = '';
      await oncreated?.(r.value?.id);
      if (r.value?.id) onselect?.(r.value.id);
    } else {
      error = r.error;
      console.log('[grid-tabs] create grid failed', r.error);
    }
  }

  function onKey(e) {
    if (e.key === 'Enter') submit();
    else if (e.key === 'Escape') cancelAdd();
  }

  function scrollAddFormIntoView(node) {
    const strip = node.closest('.grid-tabs');
    if (!strip) return;
    const sr = strip.getBoundingClientRect();
    const nr = node.getBoundingClientRect();
    if (nr.right > sr.right) {
      strip.scrollLeft += nr.right - sr.right + 6;
    } else if (nr.left < sr.left) {
      strip.scrollLeft -= sr.left - nr.left + 6;
    }
  }

  function revealAddForm(node) {
    const reveal = () => scrollAddFormIntoView(node);
    reveal();
    const raf = typeof requestAnimationFrame === 'function' ? requestAnimationFrame(reveal) : 0;
    return {
      destroy() {
        if (raf && typeof cancelAnimationFrame === 'function') cancelAnimationFrame(raf);
      }
    };
  }

  // NOTE: this component is DESKTOP-ONLY as of 2026-07-29. The narrow-nav
  // dropdown that used to live here is gone with it — on a phone the overview
  // lists the workspaces at full width, and the nav no longer shows them at all.
</script>

<div class="grid-tabs">
  <!-- No ALL tab: the app icon in the nav is the all-pulses button. -->
  {#snippet addForm()}
    <span class="add-inline" use:revealAddForm>
      <!-- svelte-ignore a11y_autofocus -->
      <input
        type="text"
        placeholder={addParent ? 'sub-workspace…' : 'workspace name…'}
        bind:value={name}
        onkeydown={onKey}
        onfocus={(e) => {
          const wrap = e.currentTarget.closest('.add-inline');
          if (wrap) scrollAddFormIntoView(wrap);
        }}
        autofocus
        aria-label={addParent ? 'new sub-workspace name' : 'new workspace name'}
      />
      <button class="mini go" onclick={submit} disabled={creating || !name.trim()} title="create">✓</button>
      <button class="mini" onclick={cancelAdd} title="cancel">✕</button>
    </span>
  {/snippet}

  {#snippet chip(g, sub)}
    <button
      class="chip"
      class:sub
      class:on={selected === g.id}
      class:family={!sub && selected !== g.id && openTop === g.id}
      class:working={anyGridWorking(g.id)}
      class:work-glow={anyGridWorking(g.id)}
      class:solid={gridNeedsAttention(g.id)}
      class:droppable={dragActive}
      class:drop-on={dropId === g.id}
      class:carrying={gridDragId === g.id}
      class:swap-with={gridDropId === g.id}
      draggable={!!onreorder}
      onclick={() => onselect?.(g.id)}
      ondragstart={(e) => onChipDragStart(e, g)}
      ondragend={onChipDragEnd}
      ondragover={(e) => onChipDragOver(e, g.id)}
      ondragleave={() => {
        if (dropId === g.id) dropId = null;
        if (gridDropId === g.id) gridDropId = null;
      }}
      ondrop={(e) => onChipDrop(e, g.id)}
      title={dragActive ? `move pulse to ${g.name}` : null}
      style={`--ws-color:${wsColor(g.id, grids)}`}
      data-ws-drop={g.id}
      class:all-hidden={isWsHidden(g.id)}
    ><span
        class="ws-dot"
        class:off={isWsHidden(g.id)}
        role="button"
        tabindex="0"
        title={isWsHidden(g.id) ? `show ${g.name} in the ALL view` : `hide ${g.name} from the ALL view`}
        aria-label={isWsHidden(g.id) ? `show ${g.name} in the ALL view` : `hide ${g.name} from the ALL view`}
        onclick={(e) => { e.stopPropagation(); toggleWsHidden(g.id); }}
        onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); e.stopPropagation(); toggleWsHidden(g.id); } }}
      ></span>{g.name}</button>
  {/snippet}

  {#each tops as g (g.id)}
    {@render chip(g, false)}
    {#if openTop === g.id}
      <span class="sub-strip" role="group" aria-label={`sub-workspaces of ${g.name}`}>
        <span class="sub-lead" aria-hidden="true">›</span>
        {#each childGrids(g.id, grids) as c (c.id)}
          {@render chip(c, true)}
        {/each}
        {#if adding && addParent === g.id}
          {@render addForm()}
        {:else}
          <button class="add-grid add-sub" onclick={() => startAdd(g.id)} title={`new sub-workspace in ${g.name}`} aria-label={`new sub-workspace in ${g.name}`}>+ sub</button>
        {/if}
      </span>
    {/if}
  {/each}

  <!-- The bin, right where the chips end: it takes the + button's place while a
       workspace is in flight. A swap, not an insert — the strip keeps its width,
       so nothing slides out from under the cursor mid-drag. -->
  {#if gridDragId}
    <div
      class="ws-bin"
      class:hot={binHot}
      ondragover={onBinDragOver}
      ondragleave={() => (binHot = false)}
      ondrop={onBinDrop}
      role="presentation"
    >🗑 delete</div>
  {:else if adding && addParent === null}
    {@render addForm()}
  {:else}
    <button class="add-grid" onclick={() => startAdd(null)} title="new workspace" aria-label="new workspace">+</button>
  {/if}

  {#if error}
    <span class="tab-err" title={error}>⚠ {error}</span>
  {/if}
</div>

<style>
  .grid-tabs {
    display: flex;
    align-items: center;
    gap: 5px;
    margin-left: 6px;
    /* ONE scrollable row — wrapping inside the fixed-height nav clipped tabs
       off the top on narrow screens. */
    flex-wrap: nowrap;
    overflow-x: auto;
    min-width: 0;
    scrollbar-width: none;
  }
  .grid-tabs::-webkit-scrollbar { display: none; }
  /* Each workspace is a bordered chip so section boundaries read clearly. */
  .grid-tabs button {
    position: relative;
    padding: 5px 10px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.14em;
    color: var(--ink-meta);
    text-transform: uppercase;
    white-space: nowrap;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
  }
  .grid-tabs button:hover { color: var(--ink-dim); border-color: var(--ink-meta); }
  /* Workspace color dot — the same color outlines that workspace's panes in
     the ALL view (wscolor.js). */
  /* The square is a toggle: click it to hide/show that workspace's pulses in
     the ALL view (wshidden.svelte.js). Hollow = hidden there. Padding widens
     the hit target without moving the square. */
  .grid-tabs button .ws-dot {
    display: inline-block;
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--ws-color);
    box-shadow: inset 0 0 0 1.5px var(--ws-color);
    margin-right: 6px;
    vertical-align: middle;
    position: relative;
    top: -1px;
    cursor: pointer;
    outline: 4px solid transparent; /* hit area */
  }
  .grid-tabs button .ws-dot:hover { transform: scale(1.4); }
  .grid-tabs button .ws-dot:focus-visible { outline: 2px solid var(--dreamer); outline-offset: 2px; }
  .grid-tabs button .ws-dot.off { background: transparent; }
  .grid-tabs button.all-hidden { opacity: 0.55; }
  /* Active workspace fills in rather than adding an underline — with a border
     around every chip, an extra rule underneath was just noise. */
  .grid-tabs button.on {
    color: var(--dreamer);
    border-color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 12%, transparent);
  }
  /* A pulse inside this workspace is working — flash the chip the same dreamer
     ring the pulse cards carry (same appearance sliders, so busy reads the
     same everywhere). Listed before .solid so an active flash wins. */
  .grid-tabs button.working {
    color: var(--dreamer);
    border-color: var(--dreamer);
    --work-halo: 14px;
    --work-halo-color: var(--dreamer);
    border-radius: 6px;
  }
  /* A pulse finished a turn here and the workspace isn't open — SOLID filled
     badge (no flash) so it reads as "look in here", distinct from the live
     animation, until the workspace (or ALL) is opened. */
  .grid-tabs button.solid {
    color: var(--bg);
    border-color: var(--matron);
    background: var(--matron);
    box-shadow: 0 0 8px color-mix(in srgb, var(--matron) 55%, transparent);
  }
  /* While a pane is being dragged every chip reads as a target, and the one
     under the pointer fills in — without this the drop was invisible until it
     had already happened. */
  .grid-tabs button.droppable {
    border-style: dashed;
    border-color: var(--sprite);
    color: var(--ink-dim);
  }
  /* [data-drop-hot]: the pulse list's hold-to-drag hovering this chip. */
  .grid-tabs button.drop-on,
  .grid-tabs :global(button[data-drop-hot]) {
    border-style: solid;
    border-color: var(--sprite);
    color: var(--sprite);
    background: color-mix(in srgb, var(--sprite) 18%, transparent);
  }
  /* A workspace being carried, and the chip whose slot it would take. Dreamer
     rather than sprite, so a workspace drag never looks like a pulse drop. */
  .grid-tabs button.carrying { opacity: 0.35; }
  .grid-tabs button.swap-with {
    border-color: var(--dreamer);
    color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 16%, transparent);
  }
  .ws-bin {
    display: flex;
    align-items: center;
    gap: 5px;
    flex-shrink: 0;
    /* Pinned to the right edge of the strip: this row SCROLLS sideways, and with
       seven workspaces the bin sat mostly past the edge — a target you can only
       half see (caught in the browser, 2026-07-30). Opaque, so chips scrolling
       beneath it don't read through. */
    position: sticky;
    right: 0;
    z-index: 2;
    padding: 5px 11px;
    border-radius: 6px;
    border: 1px dashed var(--bad);
    background: color-mix(in srgb, var(--bad) 14%, var(--bg));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.14em;
    text-transform: uppercase;
    white-space: nowrap;
  }
  .ws-bin.hot {
    border-style: solid;
    background: color-mix(in srgb, var(--bad) 34%, var(--bg));
    box-shadow: 0 0 10px color-mix(in srgb, var(--bad) 55%, transparent);
  }
  /* Sub-workspace chips unfold after their open parent: smaller, lighter, in
     one bracketed run so they read as "inside" that workspace. */
  .sub-strip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 4px 2px 2px;
    border-radius: 6px;
    background: color-mix(in srgb, var(--dreamer) 6%, transparent);
  }
  .sub-lead { color: var(--dreamer); font-size: calc(12px * var(--tscale-ui)); opacity: 0.7; }
  .grid-tabs button.sub {
    padding: 3px 8px;
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.1em;
    border-style: dotted;
  }
  .grid-tabs button.sub.on { border-style: solid; }
  /* The parent of the selected sub-workspace stays lit (it is "open"). */
  .grid-tabs button.family {
    color: var(--dreamer);
    border-color: color-mix(in srgb, var(--dreamer) 60%, var(--border-strong));
  }
  .grid-tabs .add-sub { padding: 1px 6px; font-size: calc(10px * var(--tscale-ui)); }
  .grid-tabs .add-grid {
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
    padding: 3px 9px;
    border-style: dashed;
  }
  .grid-tabs .add-grid:hover { color: var(--ink-dim); }

  .add-inline {
    display: inline-flex;
    align-items: center;
    gap: 3px;
    margin-left: 2px;
    flex-shrink: 0;
  }
  .add-inline input {
    width: 110px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 7px;
    padding: 4px 7px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink);
  }
  .add-inline input::placeholder { color: var(--ink-meta); }
  .mini {
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-meta);
    padding: 2px 5px;
    border-radius: 5px;
  }
  .mini:hover { background: var(--surface-warm); color: var(--ink-dim); }
  .mini.go { color: var(--sprite); }
  .mini:disabled { opacity: 0.4; cursor: default; }

  .tab-err {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--bad);
    margin-left: 4px;
    max-width: 160px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }

/* No narrow-nav rules: below 700px the nav does not render this component at
   all, so there is nothing here to collapse. */
</style>
