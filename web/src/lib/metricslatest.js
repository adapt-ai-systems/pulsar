/**
 * Latest-only view of the metrics collector data.json: drop each host's
 * 900-sample hardware history; retain two minutes of measured speed points.
 * `speed: false` drops those too: only the expanded bar's charts read them.
 */

/**
 * @param {any} data
 * @param {{ speed?: boolean }} [opts]
 */
export function stripMetricsHistory(data, { speed = true } = {}) {
  if (!data || !Array.isArray(data.hosts)) return data;
  return {
    ...data,
    hosts: data.hosts.map((h) => {
      if (!h || typeof h !== 'object') return h;
      const { history, ...rest } = h;
      const ll = h.latest?.llm ?? h.latest?.llama;
      const speed_history = speed && ll?.up && ll.role !== 'worker' && Array.isArray(history)
        ? history.slice(-125).map(p => ({ t: p.t, model: p.model, dtps: p.dtps ?? null, pfps: p.pfps ?? null, tps: p.tps ?? null })) : [];
      return { ...rest, speed_history };
    })
  };
}

/** True for /metrics-proxy/data.json?latest=… or /metrics-proxy/latest.json */
export function wantsMetricsLatest(url) {
  const u = url || '';
  const q = u.indexOf('?');
  const path = q < 0 ? u : u.slice(0, q);
  const qs = q < 0 ? '' : u.slice(q + 1);
  if (path === '/metrics-proxy/latest.json') return true;
  if (path === '/metrics-proxy/data.json') {
    return new URLSearchParams(qs).has('latest');
  }
  return false;
}

/** False for ?speed=0: the caller does not want speed_history. */
export function metricsWantsSpeed(url) {
  const u = url || '';
  const q = u.indexOf('?');
  return q < 0 || new URLSearchParams(u.slice(q + 1)).get('speed') !== '0';
}
