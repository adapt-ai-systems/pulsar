<script>
  import SpeedCharts from './SpeedCharts.svelte';
  import { decodeStat, prefillStat, aggregateStat } from './speedstats.js';
  import { groupModelPulses } from './modelpulses.js';
  import { modelLabel } from './models.js';
  let { pulses = [], onopenpulse } = $props();
  const modelPulses = $derived(groupModelPulses(pulses));

  function openModelPulse(id) {
    open = false;
    onopenpulse?.(id);
  }

  import { live } from './live.svelte.js';
  import { untrack } from 'svelte';
  import { isMetricsHostHidden, setMetricsHostHidden } from './uiprefs.svelte.js';
  import { isMetricsEnabled } from './appsettings.svelte.js';

  /**
   * Metrics stat bar: a one-line strip under the nav fed by the configured
   * `metrics.url` dashboard (proxied same-origin at /metrics-proxy/), so there
   * is no CORS issue and the full dashboard can be iframed. Tap/click the strip
   * to expand; double-tap for the full dashboard overlay. Hidden and silent
   * when `metrics.url` is unset. Visibility is also toggled from Appearance
   * (uiPrefs.showMetricsBar).
   */
  let data = $state(null);
  let fetchFailed = $state(false);
  let open = $state(false);
  // Ticks each poll so staleness re-evaluates even when data.json stops moving.
  let now = $state(Date.now() / 1000);

  let fetching = false;
  let fetchQueued = false;
  async function tick() {
    if (!isMetricsEnabled()) return;
    // A hidden tab still gets every push; the bar catches up when shown.
    if (typeof document !== 'undefined' && document.visibilityState === 'hidden') return;
    if (fetching) { fetchQueued = true; return; }
    fetching = true;
    now = Date.now() / 1000;
    try {
      // Collapsed, the bar skips speed_history to keep polls small.
      const r = await fetch(`/metrics-proxy/data.json?latest=1${expanded ? '' : '&speed=0'}`, { cache: 'no-store' });
      if (!r.ok) throw new Error(String(r.status));
      data = await r.json();
      fetchFailed = false;
    } catch (e) {
      // Collector unreachable — keep the last numbers, dim the bar.
      fetchFailed = true;
      console.debug('[metrics] data.json fetch failed', e);
    } finally {
      fetching = false;
      if (fetchQueued) { fetchQueued = false; tick(); }
    }
  }

  // New collector samples arrive over /live; polling only heals missed pushes.
  // The overlay iframe keeps its own independent loop.
  $effect(() => {
    if (!isMetricsEnabled()) return;
    const off = live.onFeed('metrics_sample', tick);
    untrack(tick); // tick reads `expanded`; this effect must not re-run on it
    const id = setInterval(tick, 15000);
    const onVis = () => { if (document.visibilityState === 'visible') tick(); };
    document.addEventListener('visibilitychange', onVis);
    return () => { off(); clearInterval(id); document.removeEventListener('visibilitychange', onVis); };
  });

  // Host identity colors — order-based palette matching the dashboard.
  const HOST_COLORS = ['#3ddc84', '#4da3ff', '#39d0d8', '#ff6fae', '#ff9f43', '#f5e663', '#8ecf3f', '#ff7a6b', '#b48bff', '#ffb454'];

  // Per-host: collector says online, its sample is fresh, and the fetch works.
  const hosts = $derived(
    (data?.hosts ?? []).map((h, i) => ({
      id: h.id,
      short: h.name ?? h.id,
      color: HOST_COLORS[i % HOST_COLORS.length],
      s: h.latest ?? null,
      online: !!h.latest?.online && !fetchFailed && now - (h.latest?.ts ?? 0) < 30
    }))
  );
  const anyOnline = $derived(hosts.some((h) => h.online));
  // One chip per serving head: the loaded model, the hosts in its TP group,
  // its t/s and busy/total slots. A host can serve multiple models at once via
  // `llms`; one chip each, the first keeps the host id.
  const llms = $derived(
    hosts
      .filter((h) => h.online)
      .flatMap((h) => (h.s?.llms?.length ? h.s.llms : [h.s?.llm ?? h.s?.llama]).map((ll, i) => ({ h, ll, i })))
      .filter(({ ll }) => ll?.up && ll?.role !== 'worker')
      .map(({ h, ll, i }) => {
        const ranks = hosts.filter((w) => w.online && (w.s?.llm ?? w.s?.llama)?.role === 'worker' && (w.s?.llm ?? w.s?.llama)?.paired_with === h.short);
        // TP width: trust a reported tp_size (head or any rank) over reachable rank count.
        const tp = Math.max(ranks.length ? ranks.length + 1 : 0, +ll.tp_size || 0, ...ranks.map((w) => +(w.s?.llm ?? w.s?.llama)?.tp_size || 0));
        return {
          id: i ? h.id + ':' + i : h.id,
          hostId: h.id,
          short: h.short,
          color: h.color,
          ll,
          model: ll.model ?? ll.backend ?? 'LLM',
          name: shortModel(ll.label ?? ll.model ?? ll.backend ?? 'LLM') + (tp > 1 ? ' TP' + tp : ''),
          boxes: [h.short, ...ranks.map((w) => w.short)].join('+')
        };
      })
  );

  // Model chips share the hosts' hidden-set, keyed "model:<served id>" so a
  // toggle in the dashboard head hides a model's chip from the bar the same
  // way it hides a host. Visibility remains a model preference; DOM identity
  // is the host id so two heads serving the same model stay distinct.
  const modelKey = (l) => 'model:' + l.model;
  const shownLlms = $derived(
    llms.filter((l) => !isMetricsHostHidden(l.hostId) && !isMetricsHostHidden(modelKey(l)))
  );

  // Chip-sized model label derived purely from the collector data: drop org
  // prefix, weights file extension, and common quantization suffixes. Full name
  // stays in the hover title.
  function shortModel(name) {
    const clean = String(name).replace(/^[^/]*\//, '');
    const t = clean
      .replace(/\.(gguf|safetensors)$/i, '')
      .replace(/[-_](nvfp4|fp4|fp8|awq|gptq|int4|int8|bf16)$/i, '')
      .split(/[-_]/)
      .filter(Boolean);
    if (t.length > 1 && /^\d+(\.\d+)?[a-z]?$/i.test(t[1]) && !/\d/.test(t[0])) t.splice(0, 2, t[0] + t[1]);
    return t.slice(0, 2).join(' ') || clean;
  }
  const n0 = (v) => (v == null ? '—' : String(Math.round(+v)));
  // Prefill rates run into the thousands — "6.7k" beside decode's "47.4".
  const kTps = (v) => (!v ? '0' : +v >= 1000 ? (+v / 1000).toFixed(1) + 'k' : (+v).toFixed(0));
  // KV-cache used/total in tokens — e.g. "26k/1.0M": 0 decimals for <1M,
  // 1 decimal + M above, bare zero reads "0" with no unit.
  const fin = (v) => typeof v === 'number' && Number.isFinite(v) ? v : null;
  // Null-safe numeric read: `+null` is 0, which would make an empty window
  // render as "0.0 t/s" instead of falling back.
  const num = (v) => (v == null || v === '' ? null : fin(+v));
  const kTok = (t) => {
    const v = fin(+t);
    if (v == null || v < 0) return null;
    if (v === 0) return '0';
    return v >= 1e6 ? (v / 1e6).toFixed(1) + 'M' : String(Math.round(v / 1000)) + 'k';
  };
  // Every published number passes a finite + sanity-range gate so malformed
  // backend data takes the omission path instead of rendering NaN/Infinity.
  const kvStat = (ll) => {
    const used = num(ll.kv_used_tokens);
    const total = num(ll.kv_total_tokens);
    if (used == null || used < 0 || total == null || !(total > 0)) return null;
    const u = kTok(used), t = kTok(total);
    // 1e308-class overflow renders "1e+302M" — omit rather than glyph it.
    return u != null && t != null && u.indexOf('e+') < 0 && t.indexOf('e+') < 0 ? u + '/' + t : null;
  };
  // Current cached-vs-fresh prompt share over the collector's short window
  // (cache_hit_pct_window_s, default 10s). Mid-decode the window empties;
  // the collector holds the last share ~30s and flags cache_hit_stale (dimmed).
  // Lifetime number + raw split live in the hover title.
  const cacheStat = (ll) => {
    const share = num(ll.cache_hit_pct_10s);
    // Range gate: collector already clamps, but a hand-edited data.json or a
    // proxy mangling numbers must still hit the omission path.
    if (share == null || share < 0 || share > 100) return null;
    const life = num(ll.cache_hit_pct_lifetime);
    const split = ll.prompt_cached_total != null && ll.prompt_fresh_total != null
      ? (() => { const c = kTok(+ll.prompt_cached_total), f = kTok(+ll.prompt_fresh_total);
                return c != null && f != null ? ' — ' + c + ' cached / ' + f + ' fresh' : ''; })()
      : '';
    const win = ll.cache_hit_pct_window_s != null ? ll.cache_hit_pct_window_s + 's' : '10s';
    return { pct: Math.round(share) + '%',
             stale: !!ll.cache_hit_stale,
             title: 'last ' + win + (ll.cache_hit_stale ? ' (held — last prompt traffic aged out)' : '')
                    + (life != null && life >= 0 && life <= 100 ? ' — lifetime ' + life + '%' : '') + split };
  };
  // Decode uses matched live token/time observations; prefill uses measured
  // completed requests because vLLM publishes its prompt counters in bursts.
  const secs = (v) => (v == null ? null : +v >= 10 ? Math.round(+v) + 's' : (+v).toFixed(1) + 's');
  // Time to first token: mean over the last 60s of requests, dimmed when only
  // the lifetime mean exists, omitted when the backend exports neither.
  const ttftStat = (ll) => {
    const w = num(ll.ttft_s_60s), life = num(ll.ttft_s_life);
    const v = w ?? life;
    if (v == null || v < 0) return null;
    return { main: secs(v), est: w == null,
             title: 'time to first token, mean' + (w != null ? ' over last 60s' : ' (lifetime — no request started in 60s)')
                    + (life != null && w != null ? ' — lifetime ' + secs(life) : '') };
  };
  const cacheStat2 = (ll) => {
    const share60 = num(ll.cache_hit_pct_60s);
    const s10 = cacheStat(ll);
    const life = num(ll.cache_hit_pct_lifetime);
    const lifeOk = life != null && life >= 0 && life <= 100;
    const pct = share60 != null && share60 >= 0 && share60 <= 100 ? Math.round(share60) + '%' : (s10 ? s10.pct : null);
    const speed = num(ll.cache_tps) || num(ll.cache_tps_avg_60s) || 0;
    // No prompt traffic in the window: show the lifetime share, dimmed.
    if (pct == null && s10 == null) {
      return lifeOk ? { pct: Math.round(life) + '%', speed: kTps(speed), stale: true,
                        title: 'prompt tokens served from prefix cache, lifetime (no prompt traffic in the last 60s) — ⚡ cache-hit ingest t/s' } : null;
    }
    return { pct: pct ?? '—', speed: kTps(speed), stale: pct == null || (share60 == null && !!s10?.stale),
             title: 'prompt tokens served from prefix cache, last 60s' + (lifeOk ? ' — lifetime ' + life + '%' : '') + ' — ⚡ cache-hit ingest t/s' };
  };
  const kvPeak = (ll) => {
    const pk = num(ll.kv_peak_tokens_60s), used = num(ll.kv_used_tokens);
    if (pk == null || used == null || pk <= used) return null;
    const t = kTok(pk);
    return t != null && t.indexOf('e+') < 0 ? t : null;
  };
  const waiting = (ll) => { const w = num(ll.requests_deferred); return w != null && w > 0 ? Math.round(w) : null; };

  // One tap expands the bar down (every shown model on its own wrapping row);
  // a double tap opens the full dashboard. A short timer tells the two apart;
  // the second click of a double tap lands while the timer is live and is ignored.
  let expanded = $state(false);
  // Push is the primary transport; while charts are visible, heal lost pushes
  // every second rather than leaving a live chart frozen for 15 seconds.
  $effect(() => {
    if (!expanded || !isMetricsEnabled()) return;
    untrack(tick); // the collapsed fetches carried no speed history
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  });
  const speedSeries = $derived(shownLlms.map(l => ({ ...l,
    points: data?.hosts?.find(h => h.id === (l.hostId ?? l.id))?.speed_history ?? []
  })));

  let tapTimer = null;
  function onTap() {
    if (tapTimer) return;
    tapTimer = setTimeout(() => { tapTimer = null; expanded = !expanded; }, 260);
  }
  function onDbl() {
    if (tapTimer) { clearTimeout(tapTimer); tapTimer = null; }
    open = true;
  }
  function onKey(e) {
    if (e.key === 'Escape') { if (open) open = false; else if (expanded) expanded = false; }
  }
</script>

<svelte:window onkeydown={onKey} />

{#snippet llmChip(l, details = false)}
    <span class="stat llm" style="--hc:{l.color}" title="{l.model} on {l.boxes}">
      <span class="lmodel">{l.name}</span>
      <b title="{decodeStat(l.ll).title}" class:est={decodeStat(l.ll).est}>{decodeStat(l.ll).main}</b><i> t/s{decodeStat(l.ll).basis ? ' avg' : ''}</i>
      <i class="lagg" title="{aggregateStat(l.ll).title}">Σ<b>{aggregateStat(l.ll).main}</b> t/s</i>
      {#if details}
        {#if decodeStat(l.ll).n}<i class="lreq" title="{decodeStat(l.ll).title}">×{decodeStat(l.ll).n}</i>{/if}
        {#if ttftStat(l.ll)}<i class="lttft" title="{ttftStat(l.ll).title}">ttft <b class:est={ttftStat(l.ll).est}>{ttftStat(l.ll).main}</b></i>{/if}
        <i class="lpre" title="{prefillStat(l.ll).title}">↑<b class:est={prefillStat(l.ll).est}>{prefillStat(l.ll).main}</b>{prefillStat(l.ll).basis ? ' ' + prefillStat(l.ll).basis : ''}</i>
        {#if cacheStat2(l.ll)}<i class="lcache" class:stale={cacheStat2(l.ll).stale} title="{cacheStat2(l.ll).title}">cache <b>{cacheStat2(l.ll).pct}</b> ⚡<b>{cacheStat2(l.ll).speed}</b></i>{/if}
      {/if}
      {#if kvStat(l.ll)}
        <i class="lkv" title={details ? 'KV cache: live pinned tokens ▴ 60s peak / capacity' : 'KV cache: live pinned tokens / capacity'}>kv <b>{kvStat(l.ll).split('/')[0]}</b>{#if details && kvPeak(l.ll)}<s class="lpeak">▴{kvPeak(l.ll)}</s>{/if}/<b>{kvStat(l.ll).split('/')[1]}</b></i>
      {/if}
      {#if l.ll.slots_total != null}
        <i class="lslots" title="running / max concurrent requests{waiting(l.ll) ? ' — ' + waiting(l.ll) + ' waiting in queue' : ''}">{n0(l.ll.slots_busy ?? 0)}/{n0(l.ll.slots_total)}{#if details && waiting(l.ll)}<s class="lwait">+{waiting(l.ll)}</s>{/if}</i>
      {/if}
    </span>
{/snippet}

{#snippet llmTile(l)}
    <!-- Phone compact tile: t/s, agents running, KV used. -->
    <span class="ltile" style="--hc:{l.color}" title="{l.name} — {l.model} on {l.boxes} · t/s · running · kv">
      <b>{decodeStat(l.ll).main}</b><i>{n0(l.ll.slots_busy ?? 0)}</i>{#if kvStat(l.ll)}<i>{kvStat(l.ll).split('/')[0]}</i>{/if}
    </span>
{/snippet}

{#if isMetricsEnabled()}
<div class="metrics-wrap">
{#if !expanded}
<button
  class="metrics-bar"
  class:offline={!anyOnline}
  class:expanded
  onclick={onTap}
  ondblclick={onDbl}
  title="Metrics — tap to expand, double-tap for the full dashboard"
  aria-label="Metrics stats — tap to expand, double-tap for the full dashboard"
  aria-expanded={expanded}
>
  <span class="tag">METRICS</span>
  {#if hosts.length === 0}
    <span class="stat off-note">offline</span>
  {/if}
  {#if !expanded}
    <span class="llm-band">
    {#each shownLlms as l (l.id)}
      {@render llmChip(l)}
    {/each}
    </span>
    <span class="tile-band">
    {#each shownLlms as l (l.id)}
      {@render llmTile(l)}
    {/each}
    </span>
  {/if}
</button>
{/if}
{#if expanded}
  <div class="metrics-expand" role="region" aria-label="Metrics models, expanded">
    <!-- Full stat chip per serve: decode, Σ, ttft, prefill, cache, kv, slots. -->
    <div class="xchips">
    {#each shownLlms as l (l.id)}
      {@render llmChip(l, true)}
    {/each}
    </div>
    <SpeedCharts series={speedSeries} {now} delayed={fetchFailed || shownLlms.some(l => l.ll.metrics_stale)} onclose={() => expanded = false} />
  </div>
{/if}
</div>

{#if open}
  <!-- Full dashboard, same-origin through the /metrics-proxy proxy. Overlay,
       not a navigation, so closing lands back where the user was. -->
  <div class="metrics-overlay" role="dialog" aria-label="Metrics dashboard">
    <div class="overlay-head">
      <span class="ov-title"><span class="dot" class:on={anyOnline}></span>METRICS</span>
      <!-- Which hosts/models the top bar shows: one toggle per entry. -->
      <div class="ov-hosts">
        {#each hosts as h (h.id)}
          <button
              class="ov-host"
              class:off={isMetricsHostHidden(h.id)}
              style="--hc:{h.color}"
              onclick={() => setMetricsHostHidden(h.id, !isMetricsHostHidden(h.id))}
              title={isMetricsHostHidden(h.id) ? `show ${h.short} in the bar` : `hide ${h.short} from the bar`}
              aria-label={isMetricsHostHidden(h.id) ? `show ${h.short} in the bar` : `hide ${h.short} from the bar`}
              aria-pressed={!isMetricsHostHidden(h.id)}
            >
            <span class="ov-eye" aria-hidden="true">{isMetricsHostHidden(h.id) ? '◡' : '●'}</span>
            <span class="ov-hostname">{h.short}</span>
          </button>
        {/each}
        {#if llms.length}<span class="ov-sep" aria-hidden="true"></span>{/if}
        {#each llms as l (l.id)}
          <button
              class="ov-host ov-model"
              class:off={isMetricsHostHidden(modelKey(l))}
              style="--hc:{l.color}"
              onclick={() => setMetricsHostHidden(modelKey(l), !isMetricsHostHidden(modelKey(l)))}
              title={isMetricsHostHidden(modelKey(l)) ? `show ${l.name} in the bar` : `hide ${l.name} from the bar`}
              aria-label={isMetricsHostHidden(modelKey(l)) ? `show ${l.name} in the bar` : `hide ${l.name} from the bar`}
              aria-pressed={!isMetricsHostHidden(modelKey(l))}
            >
            <span class="ov-eye" aria-hidden="true">{isMetricsHostHidden(modelKey(l)) ? '◡' : '●'}</span>
            <span class="ov-hostname">{l.name}</span>
          </button>
        {/each}
      </div>
      <button class="ov-close" onclick={() => (open = false)} title="close" aria-label="close dashboard">✕</button>
    </div>
    <section class="model-pulses" aria-label="Active pulses by model">
      <h2>Active pulses by model</h2>
      {#each modelPulses as group (group.model)}
        <div class="model-pulse-row">
          <h3 title={group.model}>{modelLabel(group.model)} <span>({group.pulses.length})</span></h3>
          <div class="pulse-links">
            {#each group.pulses as pulse (pulse.id)}
              <button
                class="pulse-link"
                onclick={() => openModelPulse(pulse.id)}
                title={`Open ${pulse.title || pulse.id} · working`}
              >
                <span>{pulse.title || pulse.id}</span>
                <small>working</small>
                <span aria-hidden="true">↗</span>
              </button>
            {/each}
          </div>
        </div>
      {:else}
        <p>No pulses are working right now.</p>
      {/each}
    </section>
    <iframe src="/metrics-proxy/" title="Metrics dashboard"></iframe>
  </div>
{/if}
{/if}

<style>
  .model-pulses {
    flex: 0 1 auto; min-height: 0; max-height: 35dvh; overflow-y: auto;
    padding: 10px 12px; background: var(--bg); color: var(--ink);
    border-bottom: 1px solid var(--border);
  }
  .model-pulses h2 { margin: 0 0 8px; font-size: 13px; }
  .model-pulse-row + .model-pulse-row { margin-top: 10px; }
  .model-pulse-row h3 { margin: 0 0 4px; font-size: 12px; overflow-wrap: anywhere; }
  .model-pulse-row h3 span, .pulse-link small { color: var(--ink-meta); font-weight: normal; }
  .pulse-links { display: flex; flex-wrap: wrap; gap: 5px; }
  .pulse-link {
    display: inline-flex; align-items: center; gap: 7px; min-height: 44px;
    max-width: 100%; padding: 6px 10px; border: 1px solid var(--border);
    border-radius: 8px; background: var(--surface); color: var(--ink);
    text-align: left; cursor: pointer; font-size: 12px;
  }
  .pulse-link > span:first-child { overflow-wrap: anywhere; min-width: 0; }
  .pulse-link:hover { background: var(--surface-warm); }
  .pulse-link:focus-visible { outline: 2px solid var(--ink); outline-offset: 2px; }
  .metrics-overlay iframe { min-height: 0; }

  .metrics-bar {
    display: flex;
    align-items: center;
    gap: 14px;
    width: 100%;
    /* Grows to more rows when many models are up. */
    min-height: 22px;
    padding: 2px 12px;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.06em;
    cursor: pointer;
    white-space: nowrap;
    text-align: left;
  }
  .metrics-bar:hover { background: var(--surface-warm); color: var(--ink-dim); }
  .metrics-wrap { position: relative; display: flex; flex-direction: column; width: 100%; flex-shrink: 0; }
  /* No 300ms double-tap zoom delay on phones: dblclick must arrive promptly. */
  .metrics-bar { touch-action: manipulation; }
  .metrics-bar.expanded { border-bottom-color: transparent; }
  /* Expanded panel: one row per shown model, stats wrap instead of scroll. */
  .metrics-expand {
    display: flex;
    flex-direction: column;
    gap: 5px;
    padding: 4px 12px 7px;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.06em;
  }
  /* Expanded rows sit centered. */
  .metrics-expand .xchips { display: flex; flex-wrap: wrap; justify-content: center; gap: 4px 14px; }
  .metrics-expand .stat.llm { justify-content: center; text-align: center; }
  .metrics-expand .stat.llm { flex-wrap: wrap; white-space: normal; row-gap: 3px; flex-shrink: 1; min-width: 0; }
  .metrics-expand .stat.llm + .stat.llm { margin-left: 0; }
  .stat.llm .lagg { margin-left: 4px; }
  .stat.llm .lagg b { color: var(--ink); }
  .metrics-bar.offline { opacity: 0.55; }
  .hname { font-weight: 700; letter-spacing: 0.1em; color: var(--ink); }
  .dot {
    width: 7px;
    height: 7px;
    border-radius: 50%;
    background: var(--bad);
    flex-shrink: 0;
  }
  .dot.on {
    background: var(--sprite);
    box-shadow: 0 0 6px color-mix(in srgb, var(--sprite) 70%, transparent);
  }
  .tag {
    font-weight: 700;
    letter-spacing: 0.18em;
    color: var(--ink-dim);
  }
  .stat { flex-shrink: 0; }
  .stat b {
    color: var(--ink);
    font-weight: 600;
    font-variant-numeric: tabular-nums;
  }
  .stat i { font-style: normal; color: var(--ink-meta); font-variant-numeric: tabular-nums; }
  .stat b.gpu { color: var(--ink); }
  /* Model chips sit apart from the host chips and from each other: their
     own tinted box (head host's color), a wider gap, a divider before the
     first one. */
  .stat.llm {
    display: inline-flex;
    align-items: center;
    gap: 7px;
    padding: 1px 8px;
    border-radius: 9px;
    background: color-mix(in srgb, var(--hc) 9%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--hc) 25%, transparent);
  }
  .stat.llm + .stat.llm { margin-left: 6px; }
  /* The model chips wrap inside this band, not the bar itself — a wrap at the
     .metrics-bar level reserves a full row height for the band's overflow and
     reads as a blank line above the metrics. */
  .llm-band {
    display: inline-flex;
    align-items: center;
    gap: 6px;
    flex: 1 1 auto;
    min-width: 0;
    flex-wrap: wrap;
    justify-content: center;
    row-gap: 2px;
  }
  .llm-band .stat.llm { min-width: 0; }
  .tile-band { display: none; }
  @media (max-width: 699px) {
    .llm-band, .metrics-bar .tag { display: none; }
    /* Centered on the bar. */
    .tile-band {
      display: flex;
      flex-wrap: wrap;
      justify-content: center;
      gap: 2px;
      flex: 1 1 auto;
      min-width: 0;
    }
    .ltile {
      display: flex;
      min-width: 72px;
      gap: 4px;
      justify-content: center;
      align-items: baseline;
      padding: 0 3px;
      border-radius: 4px;
      font-size: 9px;
      letter-spacing: 0;
      line-height: 14px;
      background: color-mix(in srgb, var(--hc) 12%, transparent);
      box-shadow: inset 2px 0 0 var(--hc);
    }
    .ltile b { color: var(--ink); font-weight: 600; font-variant-numeric: tabular-nums; }
    .ltile i { font-style: normal; color: var(--ink-meta); font-variant-numeric: tabular-nums; }
  }
  .llm-sep { flex-shrink: 0; width: 1px; height: 12px; background: var(--border); margin: 0 2px; }
  /* Only the model name carries the host color; the t/s number reads in
     plain ink like every other stat. */
  .stat.llm b { color: var(--ink); }
  .stat.llm .lmodel { color: var(--hc, var(--dreamer)); font-weight: 700; letter-spacing: 0.08em; }
  .stat.llm .lslots { margin-left: 4px; }
  .stat.llm .lreq { margin-left: 2px; opacity: 0.7; }
  .stat.llm .lttft { margin-left: 5px; }
  .stat.llm .lttft b { color: var(--ink); }
  /* Estimated (fallback) numbers dim like the bar's other not-quite-live ones. */
  .stat.llm b.est { opacity: 0.55; }
  .stat.llm .lpeak { text-decoration: none; margin-left: 2px; opacity: 0.6; }
  .stat.llm .lwait { text-decoration: none; margin-left: 1px; color: var(--warn, #e0a030); font-weight: 700; }
  .stat.llm .lpre { margin-left: 5px; }
  .stat.llm .lpre b { color: var(--ink); }
  .stat.llm .lkv { margin-left: 5px; }
  .stat.llm .lkv b { color: var(--ink); }
  .stat.llm .lcache { margin-left: 5px; }
  .stat.llm .lcache b { color: var(--ink); }
  /* Held (stale) share: prompt traffic aged out of the window but within the
     30s hold — dim it like the bar's other not-quite-live numbers. */
  .stat.llm .lcache.stale { opacity: 0.45; }
  .off-note { text-transform: uppercase; letter-spacing: 0.14em; }
  /* Phone: drop the tag; hosts and model chips stay (dashboard head toggles trim them). */
  @media (max-width: 699px) {
    .metrics-bar { gap: 10px; }
    .metrics-bar:not(.expanded) { height: auto; min-height: 22px; padding-top: 4px; padding-bottom: 4px; }
    /* The band must never shrink to zero: when host chips alone overflow the
       row, a 0px width stacks wrapped chips into a tall invisible column. A
       floor keeps the chips wrapping inside a real width. */
    .llm-band { min-width: 100%; }
    .llm-band .stat.llm { flex-wrap: wrap; white-space: normal; flex-shrink: 1; max-width: 100%; row-gap: 3px; }
    .llm-band .stat.llm + .stat.llm { margin-left: 0; }
    .tag { display: none; }
  }

  .metrics-overlay {
    position: fixed;
    inset: 0;
    z-index: 90;
    display: flex;
    flex-direction: column;
    background: #0b0f14; /* the dashboard page's own background */
  }
  .overlay-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    height: 40px;
    padding: 0 12px;
    flex-shrink: 0;
    border-bottom: 1px solid var(--border);
    background: var(--bg);
  }
  .ov-title {
    display: flex;
    align-items: center;
    gap: 8px;
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    letter-spacing: 0.16em;
    color: var(--ink-dim);
  }
  .ov-close {
    flex-shrink: 0;
    color: var(--ink-meta);
    font-size: calc(14px * var(--tscale-ui));
    padding: 4px 10px;
    border-radius: 6px;
  }
  .ov-close:hover { background: var(--surface-warm); color: var(--ink); }
  .ov-hosts {
    display: flex;
    align-items: center;
    gap: 6px;
    margin-right: 10px;
    flex: 1 1 auto;
    min-width: 0;
    overflow-x: auto;
    scrollbar-width: none;
  }
  .ov-hosts::-webkit-scrollbar { display: none; }
  .ov-host {
    display: inline-flex;
    align-items: center;
    gap: 5px;
    padding: 2px 8px;
    border: 0;
    border-radius: 8px;
    background: color-mix(in srgb, var(--hc) 10%, transparent);
    box-shadow: inset 0 0 0 1px color-mix(in srgb, var(--hc) 28%, transparent);
    color: var(--ink-dim);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.08em;
    cursor: pointer;
  }
  .ov-host:hover { background: color-mix(in srgb, var(--hc) 20%, transparent); color: var(--ink); }
  .ov-eye {
    color: var(--hc);
    font-size: calc(11px * var(--tscale-ui));
    line-height: 1;
  }
  .ov-host.off { opacity: 0.45; }
  .ov-model .ov-hostname { letter-spacing: 0.08em; }
  .ov-sep { flex-shrink: 0; width: 1px; height: 14px; background: var(--border); margin: 0 4px; }
  .ov-host.off .ov-eye { color: var(--ink-meta); }
  iframe {
    flex: 1;
    width: 100%;
    border: 0;
  }
</style>
