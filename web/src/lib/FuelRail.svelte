<script>
  /**
   * FUEL rail. A thin rail under the nav that taps to expand a
   * usage-limits panel.
   *
   * DATA HONESTY: this renders NO invented numbers. Until a real usage source is
   * wired (see usage.js), every vendor shows `no data` and the rail carries an
   * honest empty state — structure now, data later. When usage.js returns real
   * windows, the populated branch lights up automatically.
   *
   * Standing logging rule: a fetch failure logs with context and shows an
   * unobtrusive state; it never blanks the rail or throws.
   */
  import { fetchUsage, clampPercent, formatResetIn, usageColor } from './usage.js';
  import { pollWhileVisible } from './pollvisible.js';

  let { pollIntervalMs = 60_000 } = $props();

  let data = $state(null); // {vendors:{claude,codex,grok}} or null before first load
  let expanded = $state(false);

  // The panel is position:FIXED and pinned to the viewport's right edge, not
  // to this button's. Once the rail moved into the nav row the button sits
  // ~90px from the right, and a 374px panel anchored to it ran off the left
  // of the screen. Only `top` needs measuring.
  let wrapEl;
  let panelStyle = $state('');

  function placePanel() {
    if (!wrapEl) return;
    const nav = wrapEl.closest('.nav');
    const vw = window.innerWidth;
    const vh = window.innerHeight;
    if (nav) {
      // In the top nav: hang off the NAV's bottom, not the pill's — the pill
      // is vertically centred inside the nav, so its own bottom edge would
      // leave the panel tucked under the nav's lower border.
      const top = Math.round(nav.getBoundingClientRect().bottom + 6);
      panelStyle = `top:${top}px; max-height:calc(100dvh - ${top + 8}px)`;
      return;
    }
    // In the SideRail's bottom cluster: the pill sits near the bottom of the screen,
    // so open UPWARD from its top edge — hanging below ran the panel off the
    // bottom of the phone. Sit beside the rail rather than the viewport's
    // far right, clamped so the panel stays on screen at phone widths.
    const r = wrapEl.getBoundingClientRect();
    const bottom = Math.round(vh - r.top + 6);
    const width = Math.min(430, vw - 16);
    const left = Math.max(8, Math.min(Math.round(r.right + 8), vw - width - 8));
    panelStyle = `bottom:${bottom}px; left:${left}px; right:auto; max-height:calc(100dvh - ${bottom + 8}px)`;
  }

  function toggle() {
    expanded = !expanded;
    if (expanded) placePanel();
  }

  $effect(() => {
    if (!expanded) return;
    // Rotating the phone or resizing moves the nav; follow it rather than
    // leaving the panel stranded mid-screen.
    window.addEventListener('resize', placePanel);
    return () => window.removeEventListener('resize', placePanel);
  });
  // 30s tick so the reset countdowns advance between usage polls.
  let now = $state(Date.now());
  $effect(() => {
    const id = setInterval(() => (now = Date.now()), 30_000);
    return () => clearInterval(id);
  });

  async function refresh() {
    const r = await fetchUsage();
    if (r.ok) {
      data = r.value;
    } else {
      console.log('[fuel] fetch usage failed', r.error);
      // Keep prior data if any; otherwise present an explicit empty snapshot so
      // the rail still renders `no data` rather than vanishing.
      if (!data) data = { vendors: { claude: null, codex: null, grok: null, gemini: null } };
    }
  }

  // Usage poll: paused while the tab is hidden, refreshes at once on return.
  $effect(() => pollWhileVisible(refresh, pollIntervalMs));

  const vendors = $derived([
    { key: 'claude', name: 'CLAUDE', short: 'CL', snapshot: data?.vendors?.claude ?? null },
    { key: 'codex', name: 'CODEX', short: 'CX', snapshot: data?.vendors?.codex ?? null },
    { key: 'grok', name: 'GROK', short: 'GX', snapshot: data?.vendors?.grok ?? null },
    { key: 'gemini', name: 'GEMINI', short: 'GM', snapshot: data?.vendors?.gemini ?? null }
  ]);

  // Rail readings: first 3 windows for Claude, 2 for Gemini (5h + weekly), first 1 for Codex/Grok.
  function railWindows(key, snapshot) {
    if (!snapshot?.windows?.length) return [];
    return snapshot.windows.slice(0, key === 'claude' ? 3 : key === 'gemini' ? 2 : 1);
  }
</script>

<div class="fuel-wrap" bind:this={wrapEl}>
  <button
    class="fuel-rail"
    type="button"
    aria-expanded={expanded}
    aria-label={expanded ? 'collapse usage' : 'expand usage'}
    onclick={toggle}
  >
    <!-- One pip per limit window: the wedge is how full it is, the colour is
         how worried to be (green→yellow→orange→red, usageColor). Compact
         enough to sit in the nav row, so the rail costs no vertical space —
         the numbers live in the panel this button opens. -->
    {#each vendors as v (v.key)}
      <span class="fuel-vendor">
        <span class="vname">{v.short}</span>
        {#if railWindows(v.key, v.snapshot).length}
          {#each railWindows(v.key, v.snapshot) as w (w.id)}
            <span
              class="pip"
              style="--fill:{clampPercent(w.used_percent)}%; --tone:{usageColor(w.used_percent)}"
              title="{v.name} · {w.label} · {Math.round(clampPercent(w.used_percent))}%"
            ></span>
          {/each}
        {:else}
          <span class="fuel-empty" title="{v.name}: no usage data">–</span>
        {/if}
      </span>
    {/each}
  </button>

  {#if expanded}
    <div class="usage-panel" id="usage-detail" style={panelStyle}>
      <div class="usage-title"><h2>USAGE LIMITS</h2><span>60s cache · Claude + Codex CLI + Grok + Gemini (agy)</span></div>
      <div class="vendors">
        {#each vendors as v (v.key)}
          <div class="vendor">
            <h3>{v.name}{#if v.snapshot?.account}<span class="plan">{v.snapshot.account}</span>{/if}{#if v.snapshot?.plan}<span class="plan">{v.snapshot.plan}</span>{/if}</h3>
            {#if v.snapshot?.windows?.length}
              {#each v.snapshot.windows as w (w.id)}
                {@const resetIn = formatResetIn(w.resets_at, now)}
                <div class="window">
                  <div class="meta">
                    <span class="wlabel">{w.label}</span>
                    {#if resetIn}<span class="reset" title={w.resets_at}>↺ {resetIn}</span>{/if}
                    <b>{Math.round(clampPercent(w.used_percent))}%</b>
                  </div>
                  <div class="bar"><span style="width:{clampPercent(w.used_percent)}%; background:{usageColor(w.used_percent)}"></span></div>
                </div>
              {/each}
            {:else}
              <p class="no-data">no data</p>
            {/if}
          </div>
        {/each}
      </div>
    </div>
  {/if}
</div>

<style>
  .fuel-wrap { position: relative; flex-shrink: 0; }
  /* Pip cluster, sized to live inline in the nav row rather than on a line of
     its own to save vertical space on phones. */
  .fuel-rail {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 3px 8px;
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    white-space: nowrap;
  }
  .fuel-rail:hover { border-color: var(--ink-meta); }
  .fuel-vendor { display: flex; align-items: center; gap: 4px; flex: none; }
  .fuel-vendor .vname { color: var(--ink-meta); letter-spacing: 0.08em; }
  .fuel-empty { color: var(--ink-meta); }
  /* The wedge is the fill, the colour is the severity. conic-gradient with a
     hard stop reads as a pie at 9px where a ring or a bar would not. */
  .pip {
    width: 10px;
    height: 10px;
    border-radius: 50%;
    flex: none;
    background: conic-gradient(var(--tone) var(--fill), var(--surface-warm) 0);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--tone) 45%, transparent);
  }

  /* Phone: drop the CL/CX/GX text. With it the nav overflowed by a few pixels
     and scrolled sideways. The 8px gap between vendor groups (vs 4px within
     one) still shows where one vendor ends and the next begins, and every pip
     keeps its tooltip while the panel carries the full labels. */
  @media (max-width: 700px) {
    .fuel-vendor .vname { display: none; }
  }

  .usage-panel {
    /* fixed, not absolute: anchored to the viewport so it cannot be pushed off
       screen by wherever the button happens to sit. placePanel() sets top (nav)
       or bottom+left (rail) inline; right/width keep it on screen at any width. */
    position: fixed;
    right: 8px;
    left: auto;
    width: min(430px, calc(100vw - 16px));
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    box-shadow: 0 14px 40px rgba(0, 0, 0, 0.55);
    padding: 14px;
    z-index: 60;
    font-family: var(--font-mono);
    /* Cap to the viewport (max-height set inline) and scroll inside if needed. */
    overflow-y: auto;
    overscroll-behavior: contain;
  }
  /* Phone: the two vendor columns get too narrow to fit "resets in …" — stack. */
  @media (max-width: 480px) {
    .vendors { grid-template-columns: 1fr; }
  }
  .usage-title { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 12px; }
  .usage-title h2 { font-size: calc(10px * var(--tscale-ui)); letter-spacing: 0.2em; color: var(--ink); }
  .usage-title span { font-size: calc(9px * var(--tscale-ui)); color: var(--ink-meta); }
  .vendors { display: grid; grid-template-columns: 1fr 1fr; gap: 14px; }
  .vendor h3 { font-size: calc(10px * var(--tscale-ui)); letter-spacing: 0.14em; color: var(--ink); display: flex; gap: 6px; align-items: center; }
  .vendor .plan { font-size: calc(9px * var(--tscale-ui)); color: var(--ink-meta); border: 1px solid var(--border); border-radius: 999px; padding: 1px 7px; }
  .no-data { font-size: calc(10px * var(--tscale-ui)); color: var(--ink-meta); margin-top: 7px; }
  .window { margin-top: 8px; }
  .window .meta { display: flex; align-items: baseline; gap: 6px; font-size: calc(10px * var(--tscale-ui)); color: var(--ink-dim); margin-bottom: 3px; }
  .window .meta .wlabel { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .window .meta .reset { margin-left: auto; color: var(--ink-meta); font-size: calc(9px * var(--tscale-ui)); white-space: nowrap; }
  .window .meta b { color: var(--ink); }
  .bar { height: 4px; border-radius: 3px; background: color-mix(in srgb, var(--ink-meta) 18%, transparent); overflow: hidden; }
  /* Colour is set inline from usageColor() — same ramp as the pips. */
  .bar span { display: block; height: 100%; border-radius: 3px; }
</style>
