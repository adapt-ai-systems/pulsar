<script>
  import { speedChart, speedLabel, chartTickAt, chartTicks } from './speedstats.js';
  let { series = [], now = 0, delayed = false, onclose = () => {} } = $props();
  let selectedTime = $state(null);
  let hiddenLines = $state(new Set());
  function toggleLine(id, field) {
    const next = new Set(hiddenLines);
    const key = `${id}:${field}`;
    if (next.has(key)) next.delete(key); else next.add(key);
    hiddenLines = next;
  }
  $effect(() => {
    if (selectedTime != null && (selectedTime < now - 120 || selectedTime > now)) selectedTime = null;
  });
  function inspect(event) {
    const rect = event.currentTarget.getBoundingClientRect();
    const svgX = (event.clientX - rect.left) / rect.width * 600;
    const tick = event.detail === 0 ? chartTicks(series, now).at(-1) ?? null : chartTickAt(series, (svgX - 38) / 524, now);
    selectedTime = tick === selectedTime ? null : tick;
  }
  function inspectKey(event) {
    if (event.key === 'Escape') { selectedTime = null; event.preventDefault(); event.stopPropagation(); return; }
    if (event.key !== 'ArrowLeft' && event.key !== 'ArrowRight') return;
    event.preventDefault();
    const ticks = chartTicks(series, now);
    const i = ticks.indexOf(selectedTime);
    selectedTime = ticks[i < 0 ? ticks.length - 1 : Math.max(0, Math.min(ticks.length - 1, i + (event.key === 'ArrowLeft' ? -1 : 1)))] ?? null;
  }
  const selectionLabel = $derived(selectedTime == null ? '' : new Date(selectedTime * 1000).toLocaleTimeString());
  const charts = $derived([
    { field: 'dtps', title: 'Decode', detail: 'Decode t/s per request, trailing 10 seconds. Solid line, left scale.' },
    { field: 'pfps', title: 'Prefill', detail: 'Fresh tokens per prefill-second, completed requests over the last 60 seconds; cache excluded. Dotted line, right scale.' }
  ].map(chart => ({ ...chart, data: speedChart(series, chart.field, now, 120, selectedTime,
    series.filter(row => hiddenLines.has(`${row.id}:${chart.field}`)).map(row => row.id)) })));
  const aggregate = $derived(speedChart(series, 'tps', now, 120, selectedTime,
    series.filter(row => hiddenLines.has(`${row.id}:tps`)).map(row => row.id)));
  const anyVisible = $derived(charts.some(chart => chart.data.rows.some(row => !row.hidden)));
  const anyPath = $derived(charts.some(chart => chart.data.rows.some(row => !row.hidden && row.path)));
  function valueLabel(value) {
    if (delayed && selectedTime == null) return '—';
    return selectedTime != null && value != null
      ? value.toLocaleString(undefined, {minimumFractionDigits: 1, maximumFractionDigits: 1}) : speedLabel(value);
  }

</script>

<div class="speed-charts" aria-label="Live model speed chart">
  <figure>
    <figcaption><strong>Decode / prefill</strong><div class="chart-actions">{#if selectedTime != null}<button class="selection-time" title="Clear selected sample and return to live values" onclick={() => selectedTime = null}>{selectionLabel} ×</button>{:else}<span>{delayed ? 'DATA DELAYED' : 'LIVE · 1s'}</span>{/if}<button class="close-chart" type="button" aria-label="Close metrics chart" title="Close chart" onclick={onclose}>×</button></div></figcaption>
    <small title="Decode uses a 10-second window. Prefill uses completed requests over 60 seconds, excluding cached tokens; prefill timing arrives at request completion.">Solid: decode (left) · dotted: prefill (right)</small>
    <div class="legend">
      {#each series as model, i (model.id)}
        <div class="legend-row" style:color={model.color}>
          <span>{model.name} · {model.short}</span>
          {#each charts as chart (chart.field)}
            {@const row = chart.data.rows[i]}
            <button class="value-toggle" class:off={row.hidden} data-field={chart.field} data-host={row.id}
              aria-pressed={!row.hidden} aria-label={`${row.hidden ? 'Show' : 'Hide'} ${chart.title} for ${model.name} on ${model.short}`}
              title={`${chart.detail} Click the value to ${row.hidden ? 'show' : 'hide'} its line.`}
              onclick={() => toggleLine(row.id, chart.field)}>
              <span class="swatch" class:dotted={chart.field === 'pfps'} aria-hidden="true"></span>{chart.field === 'dtps' ? 't/s' : 'prefill'} <b>{valueLabel(row.value)}</b>
            </button>
          {/each}
        </div>
      {/each}
    </div>
    <button class="plot-hit" onclick={inspect} onkeydown={inspectKey} aria-label="Inspect decode and prefill: tap to pin a sample; arrow keys step through samples; Escape clears">
      <svg data-selected-time={selectedTime} viewBox="0 0 600 116" role="img" aria-label="Decode and prefill over the last two minutes; decode on the left scale, prefill on the right">
        <line x1="38" y1="12" x2="562" y2="12" />
        <line x1="38" y1="52" x2="562" y2="52" />
        <line x1="38" y1="92" x2="562" y2="92" />
        <text x="34" y="16" text-anchor="end">{charts[0].data.rows.some(r => !r.hidden) ? speedLabel(charts[0].data.max) : '—'}</text>
        <text x="34" y="96" text-anchor="end">0</text>
        <text x="566" y="16">{charts[1].data.rows.some(r => !r.hidden) ? speedLabel(charts[1].data.max) : '—'}</text>
        <text x="566" y="96">0</text>
        <text x="38" y="114">−2 min</text><text x="300" y="114" text-anchor="middle">−1 min</text><text x="562" y="114" text-anchor="end">now</text>
        {#each charts as chart (chart.field)}
          {#each chart.data.rows as row (row.id)}
            {#if !row.hidden}<path data-field={chart.field} data-host={row.id} class:dotted={chart.field === 'pfps'} d={row.path} stroke={row.color}><title>{chart.title}: {row.name} on {row.short}</title></path>{/if}
          {/each}
        {/each}
        {#if charts[0].data.selectedX != null}
          <line class="crosshair" x1={charts[0].data.selectedX} y1="12" x2={charts[0].data.selectedX} y2="92" />
          {#each charts as chart (chart.field)}
            {#each chart.data.rows as row (row.id)}
              {#if !row.hidden && row.selectedY != null}<circle cx={chart.data.selectedX} cy={row.selectedY} r="3" fill={row.color} />{/if}
            {/each}
          {/each}
        {/if}
        {#if !anyPath}<text x="300" y="56" text-anchor="middle">{anyVisible ? 'Waiting for measured samples' : 'Tap a value above to show a line'}</text>{/if}
      </svg>
    </button>
  </figure>
  <figure class="aggregate-chart">
    <figcaption><strong>Aggregate output t/s</strong><span>{selectedTime != null ? selectionLabel : delayed ? 'DATA DELAYED' : 'LIVE · 1s'}</span></figcaption>
    <small>All concurrent requests per model · tokens / wall-clock second · latest counter rate</small>
    <div class="legend">
      {#each aggregate.rows as row (row.id)}
        <button class="value-toggle" class:off={row.hidden} style:color={row.color} data-field="tps" data-host={row.id}
          aria-pressed={!row.hidden} aria-label={`${row.hidden ? 'Show' : 'Hide'} aggregate output for ${row.name} on ${row.short}`}
          onclick={() => toggleLine(row.id, 'tps')}>
          <span class="swatch" aria-hidden="true"></span>{row.name} · {row.short} Σ <b>{valueLabel(row.value)}</b> t/s
        </button>
      {/each}
    </div>
    <button class="plot-hit" onclick={inspect} onkeydown={inspectKey} aria-label="Inspect aggregate output: tap to pin a sample; arrow keys step through samples; Escape clears">
      <svg data-selected-time={selectedTime} viewBox="0 0 600 116" role="img" aria-label="Aggregate output TPS over the last two minutes">
        <line x1="38" y1="12" x2="562" y2="12" />
        <line x1="38" y1="52" x2="562" y2="52" />
        <line x1="38" y1="92" x2="562" y2="92" />
        <text x="34" y="16" text-anchor="end">{aggregate.rows.some(r => !r.hidden) ? speedLabel(aggregate.max) : '—'}</text>
        <text x="34" y="96" text-anchor="end">0</text>
        <text x="38" y="114">−2 min</text><text x="300" y="114" text-anchor="middle">−1 min</text><text x="562" y="114" text-anchor="end">now</text>
        {#each aggregate.rows as row (row.id)}
          {#if !row.hidden}<path data-field="tps" data-host={row.id} d={row.path} stroke={row.color}><title>Aggregate output: {row.name} on {row.short}</title></path>{/if}
        {/each}
        {#if aggregate.selectedX != null}
          <line class="crosshair" x1={aggregate.selectedX} y1="12" x2={aggregate.selectedX} y2="92" />
          {#each aggregate.rows as row (row.id)}
            {#if !row.hidden && row.selectedY != null}<circle cx={aggregate.selectedX} cy={row.selectedY} r="3" fill={row.color} />{/if}
          {/each}
        {/if}
        {#if !aggregate.rows.some(r => !r.hidden && r.path)}<text x="300" y="56" text-anchor="middle">{aggregate.rows.some(r => !r.hidden) ? 'Waiting for output samples' : 'Tap a value above to show a line'}</text>{/if}
      </svg>
    </button>
  </figure>
</div>

<style>
  .speed-charts { display: block; width: 100%; padding: 3px; pointer-events: auto; }
  figure { min-width: 0; margin: 0; padding: 6px; border: 1px solid var(--edge, #263443); background: var(--bg, #0b0f14); }
  .aggregate-chart { margin-top: 6px; }
  figcaption { display: flex; justify-content: space-between; gap: 8px; font-size: 12px; }
  .chart-actions { display: flex; align-items: center; gap: 6px; }
  .close-chart { display: inline-flex; align-items: center; justify-content: center; width: 20px; height: 20px; padding: 0; border: 0; background: transparent; color: inherit; font: inherit; font-size: 16px; line-height: 1; cursor: pointer; touch-action: manipulation; }
  .close-chart:hover { color: var(--ink, #fff); }
  figcaption span, small { opacity: .7; font-size: 10px; }
  .legend { display: flex; flex-wrap: wrap; gap: 3px 14px; margin: 3px 0; font-size: 10px; }
  .legend-row { display: flex; flex-wrap: wrap; align-items: center; gap: 3px 8px; }
  .value-toggle { display: inline-flex; align-items: center; gap: 3px; color: inherit; background: transparent; border: 0; padding: 3px 0; min-height: 22px; font: inherit; cursor: pointer; touch-action: manipulation; }
  .value-toggle.off { opacity: .4; }
  .value-toggle.off b { text-decoration: line-through; }
  .swatch { display: inline-block; width: 12px; border-top: 2px solid currentColor; }
  .swatch.dotted { border-top-style: dotted; }
  path.dotted { stroke-dasharray: 1 5; stroke-linecap: round; }
  .plot-hit { display: block; width: 100%; border: 0; padding: 0; margin: 0; background: transparent; color: inherit; cursor: crosshair; touch-action: manipulation; }
  .plot-hit:focus-visible { outline: 1px solid currentColor; }
  .selection-time { border: 0; padding: 0; background: transparent; color: inherit; font: inherit; font-size: 10px; cursor: pointer; }
  .crosshair { opacity: .85; stroke-dasharray: 3 3; vector-effect: non-scaling-stroke; }
  svg { display: block; width: 100%; }
  line { stroke: currentColor; opacity: .15; }
  text { fill: currentColor; opacity: .65; font-size: 11px; }
  path { fill: none; stroke-width: 2; vector-effect: non-scaling-stroke; }
  @media (min-width: 700px) { .plot-hit { max-width: 840px; margin: 0 auto; } }
</style>
