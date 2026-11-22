/** Measured rates only: never label aggregate counter bursts as per-request TPS. */
export const speedNumber = (v) => typeof v === 'number' && Number.isFinite(v) && v >= 0 ? v : null;
export const speedLabel = (v) => v == null ? '—' : v >= 1000 ? (v / 1000).toFixed(1) + 'k' : v.toFixed(1);

export function decodeStat(ll) {
  const stale = !!ll.metrics_stale;
  const live = stale ? null : speedNumber(ll.decode_tps_live);
  const avg = stale ? null : speedNumber(ll.decode_tps_exp_60s);
  const value = live ?? avg;
  const busy = speedNumber(ll.slots_busy) ?? 0;
  return { main: speedLabel(value), n: busy > 1 ? busy : null,
    agg: busy > 1 && !stale && speedNumber(ll.gen_tps) != null ? speedLabel(ll.gen_tps) : null,
    totalTitle: 'All requests combined: generated tokens per wall-clock second, latest sample; backend counters may update in batches',
    est: live == null, basis: live == null && avg != null ? 'avg' : '',
    title: stale ? 'Metrics delayed — waiting for a fresh sample' : live != null
      ? 'Measured decode tokens per decode-second per request, trailing ' + (ll.decode_tps_live_window_s || 10) + 's; includes in-progress work. Zero when idle.'
      : avg != null ? 'Decode t/s per request, last 60s of completed requests (not live)'
      : 'Decode speed unavailable — no matched token and timing measurements' };
}

/** Aggregate is a wall-clock output rate, including every concurrent request. */
export function aggregateStat(ll) {
  const value = ll.metrics_stale || ll.up === false || ll.role === 'worker' ? null : speedNumber(ll.gen_tps);
  return { main: speedLabel(value), title: ll.metrics_stale
    ? 'Metrics delayed — waiting for a fresh sample'
    : 'Aggregate output t/s across all requests for this model: generated tokens per wall-clock second, latest sample; backend counters may update in batches' };
}

export function prefillStat(ll) {
  const avg = ll.metrics_stale ? null : speedNumber(ll.prefill_tps_exp_60s);
  // No completed request in 60s: the server-lifetime measured rate, dimmed.
  const life = avg == null && !ll.metrics_stale ? speedNumber(ll.prefill_tps_exp_life) : null;
  return { main: speedLabel(avg ?? life), est: true, basis: avg != null ? 'avg' : life != null ? 'life' : '',
    title: avg != null ? 'Fresh prompt tokens per measured prefill-second, last 60s of completed requests. Cache excluded. This server releases timing at request completion.'
      : life != null ? 'Fresh prompt tokens per measured prefill-second, lifetime of this server (no request completed in the last 60s). Cache excluded.'
      : 'Waiting for measured prefill timing from a completed request — prompt counter bursts are not prefill speed' };
}

/** Time-based paths with gaps for missing/delayed samples, not fake zeroes. */
export function speedChart(series, field, now, seconds = 120, selectedTime = null, hiddenIds = []) {
  const start = now - seconds;
  const rows = series.map(s => ({ ...s, points: (s.points ?? []).filter(p =>
    Number.isFinite(p.t) && p.t >= start && p.t <= now && p.model === s.model) }));
  const max = Math.max(1, ...rows.filter(s => !hiddenIds.includes(s.id)).flatMap(s => s.points.map(p => speedNumber(p[field]) ?? 0)));
  const selectedX = selectedTime != null && selectedTime >= start && selectedTime <= now
    ? 38 + 524 * (selectedTime - start) / seconds : null;
  return { max, selectedX, rows: rows.map(s => {
    let path = '', pen = false, last = null;
    for (const p of s.points) {
      const v = speedNumber(p[field]);
      if (v == null) { pen = false; continue; }
      if (last != null && p.t - last > 5) pen = false;
      const x = 38 + 524 * (p.t - start) / seconds;
      const y = 92 - 80 * v / max;
      path += `${pen ? 'L' : 'M'}${x.toFixed(2)},${y.toFixed(2)} `;
      last = p.t; pen = true;
    }
    // Exact recorded tick only. Missing observations stay unavailable rather
    // than interpolating a value or borrowing a neighboring host's sample.
    const point = selectedTime == null ? s.points.at(-1) : s.points.find(p => p.t === selectedTime);
    const value = speedNumber(point?.[field]);
    return { ...s, path, value, hidden: hiddenIds.includes(s.id), selectedY: selectedX != null && value != null ? 92 - 80 * value / max : null };
  }) };
}


export function chartTicks(series, now, seconds = 120) {
  return [...new Set(series.flatMap(s => (s.points ?? []).filter(p =>
    p.model === s.model && Number.isFinite(p.t) && p.t >= now - seconds && p.t <= now
  ).map(p => p.t)))].sort((a, b) => a - b);
}

/** Snap a click's horizontal position to a real timestamp, never an invented tick. */
export function chartTickAt(series, fraction, now, seconds = 120) {
  if (!Number.isFinite(fraction)) return null;
  const target = now - seconds + Math.max(0, Math.min(1, fraction)) * seconds;
  const ticks = chartTicks(series, now, seconds);
  return ticks.reduce((best, t) => best == null || Math.abs(t - target) < Math.abs(best - target) ? t : best, null);
}
