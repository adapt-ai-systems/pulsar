<script>
  /**
   * WatchersFleet — the Watchers shelf. Every pulse that is keeping an eye on
   * something, grouped by owner: its condition watches (with met / delivered /
   * failed state) and its schedules (next fire, on/off), plus pulses shelved
   * as watching or paused. Watching and paused pulses leave the workspace grid
   * and live here; tapping an owner opens it. Replaces the separate Watches
   * and Schedules panels.
   */
  import {
    listConditionWatches, deleteConditionWatch, listAllSchedules,
    setScheduleEnabled, deleteSchedule, listPulses, setPulseMode
  } from './api.js';
  import { watchLabel, watchDetail } from './watches.js';
  import { scheduleLabel } from './schedule.js';
  import { groupMonitoring, fmtUntil, watchDelivery, nextFireAt } from './fleet.js';
  import { pollWhileVisible } from './pollvisible.js';

  let { pulses = [], onopenpulse = () => {}, onchanged = () => {} } = $props();

  let watches = $state([]);
  let schedules = $state([]);
  let loaded = $state(false);
  let error = $state('');
  let confirmId = $state(null);
  let now = $state(new Date());
  // Owners that are not on the live roster (archived pulses keep their
  // watches and loops) — resolved from the archived set so the group head
  // shows a title, not a bare id.
  let archived = $state({}); // id -> {title, state, mode}

  const pulseInfo = $derived({
    ...archived,
    ...Object.fromEntries(
      pulses.map((p) => [p.id, { title: p.title, state: p.state, mode: p.settings?.mode ?? null }])
    )
  });
  // "hide off": spent once-wakes and disabled loops pile up as OFF rows, and
  // archived owners with nothing live are noise. Default on, remembered across
  // reloads.
  const LS_HIDE_OFF = 'pulsar.watchers.hideOff';
  let hideOff = $state(
    typeof localStorage === 'undefined' ? true : localStorage.getItem(LS_HIDE_OFF) !== '0'
  );
  function setHideOff(v) {
    hideOff = v;
    try { localStorage.setItem(LS_HIDE_OFF, v ? '1' : '0'); } catch {}
  }
  const allGroups = $derived(groupMonitoring(watches, schedules, pulseInfo, now));
  const groups = $derived(
    hideOff
      ? allGroups
          .map((g) => ({
            ...g,
            schedules: g.state === 'archived' ? [] : g.schedules.filter((s) => s.enabled)
          }))
          .filter((g) => (g.mode && g.state !== 'archived') || g.met || g.failed || g.watches.length || g.schedules.length)
      : allGroups
  );
  const hiddenCount = $derived(allGroups.length - groups.length);

  async function refreshArchived() {
    const r = await listPulses({ state: 'archived', slim: true });
    if (!r.ok) {
      console.log('[watchers] archived lookup failed', r.error);
      return;
    }
    archived = Object.fromEntries(
      (r.value ?? [])
        .filter((p) => p.state === 'archived')
        .map((p) => [p.id, { title: p.title, state: 'archived', mode: p.settings?.mode ?? null }])
    );
  }

  async function refresh() {
    const [rw, rs] = await Promise.all([listConditionWatches(null, 'watching'), listAllSchedules()]);
    loaded = true;
    now = new Date();
    if (rw.ok) watches = rw.value ?? [];
    if (rs.ok) schedules = rs.value ?? [];
    if (rw.ok && rs.ok) error = '';
    else {
      error = rw.error ?? rs.error;
      console.log('[watchers] list failed', rw.error, rs.error);
    }
  }

  async function removeWatch(id) {
    const r = await deleteConditionWatch(id);
    confirmId = null;
    if (!r.ok) {
      error = r.error;
      console.log('[watchers] delete watch failed', id, r.error);
      return;
    }
    watches = watches.filter((w) => w.id !== id);
  }

  async function toggleSchedule(s) {
    const r = await setScheduleEnabled(s.id, !s.enabled);
    if (!r.ok) {
      error = r.error;
      console.log('[watchers] toggle failed', s.id, r.error);
      return;
    }
    schedules = schedules.map((x) => (x.id === s.id ? r.value : x));
  }

  async function removeSchedule(id) {
    const r = await deleteSchedule(id);
    confirmId = null;
    if (!r.ok) {
      error = r.error;
      console.log('[watchers] delete schedule failed', id, r.error);
      return;
    }
    schedules = schedules.filter((s) => s.id !== id);
  }

  // "back to workspace": clear the mode so the pulse returns to the grid.
  async function unshelve(g) {
    const r = await setPulseMode(g.pulseId, null);
    if (!r.ok) {
      error = r.error;
      console.log('[watchers] unshelve failed', g.pulseId, r.error);
      return;
    }
    onchanged();
  }

  $effect(() => {
    refreshArchived();
    return pollWhileVisible(refresh, 10000);
  });

  // Floating tooltip: rows truncate, so hovering (or tapping, on a phone) a
  // row floats the WHOLE note/prompt under it. Opens to the right of the panel
  // after a 1.5s hover pause; a tap toggles it at once. Falls back to below
  // the row when there is no room on the right (phone).
  let tipId = $state(null);
  let tipPos = $state(null); // {left, top, width} or null (below-row fallback)
  const TIP_DELAY_MS = 1500;
  let tipTimer = null;
  function placeTip(el) {
    const r = el.getBoundingClientRect();
    const vw = window.innerWidth;
    const room = vw - r.right - 20;
    if (room < 200) { tipPos = null; return; }
    tipPos = { left: r.right + 10, top: r.top, width: Math.min(420, room) };
  }
  function tipEnter(id, el) {
    clearTimeout(tipTimer);
    tipTimer = setTimeout(() => { placeTip(el); tipId = id; }, TIP_DELAY_MS);
  }
  function tipLeave() {
    clearTimeout(tipTimer);
    tipId = null;
  }
  function tipToggle(id, el) {
    clearTimeout(tipTimer);
    if (tipId === id) { tipId = null; return; }
    placeTip(el);
    tipId = id;
  }
  function tipStyle() {
    return tipPos ? `position:fixed;left:${tipPos.left}px;top:${tipPos.top}px;width:${tipPos.width}px;right:auto` : '';
  }
  function tipText(kind, x) {
    if (kind === 'watch') return x.note ? `${x.note}\n\n${watchLabel({ ...x, note: null })}` : watchLabel(x);
    return `${scheduleLabel(x)}${x.prompt ? `\n\n${x.prompt}` : ''}`;
  }

  const GLYPH = { pulse_state: '◉', file: '▤', log: '≡', http: '⇄', cmd: '$' };
  const SGLYPH = { loop: '⟳', daily: '◷', once: '☾' };

  function restLabel(g) {
    if (g.state === 'archived') return 'archived';
    if (g.state === 'running') return 'awake';
    if (g.mode === 'watching' || g.watches.length || g.schedules.length) return 'sleeping';
    return 'stopped';
  }
  function fmtAgo(d) {
    if (!d) return null;
    const mins = Math.round((now.getTime() - d.getTime()) / 60000);
    if (mins < 1) return 'just now';
    if (mins < 60) return `${mins}m ago`;
    if (mins < 1440) return `${Math.floor(mins / 60)}h ago`;
    return `${Math.floor(mins / 1440)}d ago`;
  }
</script>

<label class="filter" title="hide disabled and spent schedules, and archived owners with nothing live">
  <input type="checkbox" checked={hideOff} onchange={(e) => setHideOff(e.currentTarget.checked)} />
  hide switched-off{#if hideOff && hiddenCount > 0} · {hiddenCount} hidden{/if}
</label>
{#if !loaded}
  <p class="note">loading…</p>
{:else if groups.length === 0 && hideOff && hiddenCount > 0}
  <!-- Switching a schedule off made it vanish behind the filter with
       the "nobody" text — say where it went. -->
  <p class="note">{hiddenCount} switched-off {hiddenCount === 1 ? 'item is' : 'items are'} hidden. Untick “hide switched-off” to see {hiddenCount === 1 ? 'it' : 'them'} and switch back on.</p>
{:else if groups.length === 0}
  <p class="note">Nobody is keeping an eye on anything. A pulse lands here when it registers a watch or a schedule, or when you set it to watching / paused from its ⋯ menu.</p>
{:else}
  {#each groups as g (g.pulseId)}
    <section class="owner" class:met={g.met > 0} class:failed={g.met === 0 && g.failed > 0} class:paused={g.mode === 'paused'}>
      <div class="owner-head">
        <button class="owner-open" onclick={() => onopenpulse(g.pulseId)} title="open pulse">
          <span class="owner-title">{g.title}</span>
        </button>
        {#if g.mode}<span class="chip mode">{g.mode}</span>{/if}
        <!-- "paused · awake" read as a contradiction: a set-aside
             pulse shows its mode alone. -->
        {#if g.mode !== 'paused'}<span class="chip rest">{restLabel(g)}</span>{/if}
        {#if g.mode}
          <button class="ico" title="back to the workspace grid (clears {g.mode})" onclick={() => unshelve(g)}>↩</button>
        {/if}
      </div>
      <div class="owner-meta">
        {#if g.met}<span class="hot">{g.met} met</span> · {/if}
        {#if g.failed}<span class="bad">{g.failed} wake failed</span> · {/if}
        {#if g.nextAt}next check {fmtUntil(g.nextAt, now)}{:else if g.watches.length + g.schedules.length}no next check{:else}no watches or schedules{/if}
        {#if g.lastFiredAt} · last fired {fmtAgo(g.lastFiredAt)}{/if}
      </div>
      {#each g.watches as w (w.id)}
        {@const dv = watchDelivery(w)}
        <div
          class="row"
          class:hot={w.bad}
          role="group"
          onmouseenter={(e) => tipEnter(w.id, e.currentTarget)}
          onmouseleave={tipLeave}
        >
          <span class="kind" title={w.kind}>{GLYPH[w.kind] ?? '?'}</span>
          <div class="info">
            <button class="label" onclick={(e) => tipToggle(w.id, e.currentTarget.closest('.row'))}>{watchLabel(w)}</button>
            {#if tipId === w.id}<div class="tip" style={tipStyle()}>{tipText('watch', w)}</div>{/if}
            <div class="sub">
              {watchDetail(w)}
              {#if dv === 'failed'} · <span class="bad" title={w.last_delivery}>wake failed</span>{:else if dv === 'delivered'} · woken{/if}
            </div>
          </div>
          {#if confirmId === w.id}
            <button class="ico del armed" title="confirm delete" onclick={() => removeWatch(w.id)}>confirm?</button>
            <button class="ico" title="cancel" onclick={() => (confirmId = null)}>✕</button>
          {:else}
            <button class="ico del" title="delete watch" onclick={() => (confirmId = w.id)}>×</button>
          {/if}
        </div>
      {/each}
      {#each g.schedules as s (s.id)}
        <div
          class="row"
          class:off={!s.enabled || g.state === 'archived'}
          role="group"
          onmouseenter={(e) => tipEnter(s.id, e.currentTarget)}
          onmouseleave={tipLeave}
        >
          <span class="kind sched" title={s.kind}>{SGLYPH[s.kind] ?? '?'}</span>
          <div class="info">
            <button class="label" onclick={(e) => tipToggle(s.id, e.currentTarget.closest('.row'))}>{scheduleLabel(s)}{s.prompt ? ` — ${s.prompt}` : ''}</button>
            {#if tipId === s.id}<div class="tip" style={tipStyle()}>{tipText('schedule', s)}</div>{/if}
            <div class="sub">{g.state === 'archived' ? 'archived — never fires' : s.enabled ? `fires ${fmtUntil(nextFireAt(s, now), now)}` : 'off'}{s.last_fired_at ? ` · last ${fmtAgo(new Date(s.last_fired_at))}` : ''}</div>
          </div>
          <button class="ico" title={s.enabled ? 'disable' : 'enable'} onclick={() => toggleSchedule(s)}>{s.enabled ? 'ON' : 'OFF'}</button>
          {#if confirmId === s.id}
            <button class="ico del armed" onclick={() => removeSchedule(s.id)}>confirm?</button>
            <button class="ico" onclick={() => (confirmId = null)} aria-label="cancel">✕</button>
          {:else}
            <button class="ico del" title="delete schedule" onclick={() => (confirmId = s.id)}>×</button>
          {/if}
        </div>
      {/each}
    </section>
  {/each}
{/if}
{#if error}<p class="note err">{error}</p>{/if}

<style>
  .filter {
    display: flex;
    align-items: center;
    gap: 6px;
    margin: 2px 2px 6px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--ink-meta);
    cursor: pointer;
  }
  .note { font-size: calc(12px * var(--tscale-ui)); color: var(--ink-meta); margin: 8px 2px; line-height: 1.45; }
  .note.err { color: var(--bad); }
  .owner { margin: 4px 0 12px; }
  .owner-head {
    display: flex;
    align-items: center;
    gap: 6px;
    padding: 6px 2px 2px;
    border-bottom: 1px solid var(--border-strong);
  }
  .owner-open { flex: 1; min-width: 0; text-align: left; padding: 0; }
  .owner-open:hover .owner-title { color: var(--dreamer); }
  .owner-title {
    font-weight: 600;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    display: block;
  }
  .owner.met .owner-title { color: var(--warden); }
  .owner.failed .owner-title { color: var(--bad); }
  .owner.paused .owner-title { color: var(--ink-dim); }
  .chip {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0 4px;
    color: var(--ink-meta);
    flex-shrink: 0;
  }
  .chip.mode { color: var(--dreamer); border-color: var(--dreamer); opacity: 0.85; }
  .owner.paused .chip.mode { color: var(--ink-meta); border-color: var(--border); }
  .owner-meta {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.04em;
    color: var(--ink-meta);
    padding: 4px 2px 2px;
  }
  .hot { color: var(--warden); }
  .bad { color: var(--bad); }
  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 6px;
    border-radius: 6px;
    font-size: calc(12px * var(--tscale-ui));
    position: relative;
  }
  .tip {
    position: absolute;
    left: 6px;
    right: 6px;
    top: calc(100% - 2px);
    z-index: 80; /* above the SidePanel (70) when floated to its right */
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    box-shadow: 0 10px 30px rgb(0 0 0 / 0.5);
    padding: 8px 10px;
    font-size: calc(12px * var(--tscale-ui));
    line-height: 1.45;
    color: var(--ink);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    pointer-events: none;
  }
  .row:hover { background: var(--surface-warm); }
  .row.hot .label { color: var(--warden); }
  .row.off { opacity: 0.55; }
  .kind { width: 14px; text-align: center; color: var(--ink-meta); flex-shrink: 0; }
  .kind.sched { color: var(--dreamer); }
  .info { flex: 1; min-width: 0; }
  .label {
    display: block;
    width: 100%;
    text-align: left;
    padding: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
    color: var(--ink);
    font: inherit;
    cursor: default;
  }
  .sub {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-meta);
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .ico {
    flex-shrink: 0;
    color: var(--ink-meta);
    border: 1px solid var(--border);
    border-radius: 4px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 1px 6px;
  }
  .ico:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .ico.del.armed { border-color: var(--bad); color: var(--bad); }
</style>
