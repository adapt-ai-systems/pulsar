import test from 'node:test';
import assert from 'node:assert/strict';
import { decodeStat, prefillStat, aggregateStat, speedChart, chartTickAt, chartTicks } from './speedstats.js';
import { stripMetricsHistory } from './metricslatest.js';

test('live matched timing beats a bogus 125 TPS ratio; no instant/busy fallback', () => {
  assert.equal(decodeStat({ decode_tps_live: 22, decode_tps_exp_60s: 125.4 }).main, '22.0');
  assert.equal(decodeStat({ gen_tps: 125.4, gen_tps_per_req: 125.4, slots_busy: 1 }).main, '—');
  assert.equal(decodeStat({ gen_tps: 40, decode_tps_live: 20, slots_busy: 2 }).agg, '40.0');
  assert.equal(decodeStat({ decode_tps_live: 0, decode_tps_exp_60s: 22 }).main, '0.0');
  assert.equal(decodeStat({ decode_tps_exp_60s: 22 }).basis, 'avg');
  assert.equal(decodeStat({ decode_tps_live: 22, metrics_stale: true }).main, '—');
});

test('prefill uses timed requests, never a burst of published prompt tokens', () => {
  assert.equal(prefillStat({ prompt_tps: 49900 }).main, '—');
  assert.equal(prefillStat({ prefill_compute_tps: 60000, prefill_tps_exp_60s: 50, prompt_tps: 49900 }).main, '50.0');
  assert.equal(prefillStat({ prefill_compute_tps: 60000 }).main, '—');
  assert.equal(prefillStat({ prefill_tps_exp_60s: 0 }).main, '0.0');
  assert.equal(prefillStat({ prefill_tps_exp_60s: 50 }).basis, 'avg');
  assert.equal(prefillStat({ prefill_compute_tps: NaN }).main, '—');
});

test('chart time coordinates, missing samples, model switches and replica identity', () => {
  const rows = ['a', 'b'].map(id => ({ id, model: 'glm', points: [
    { t: 0, model: 'glm', dtps: 900 }, // expired
    { t: 90, model: 'other', dtps: 900 }, // old model
    { t: 100, model: 'glm', dtps: 10 },
    { t: 101, model: 'glm', dtps: null },
    { t: 102, model: 'glm', dtps: 20 },
    { t: 110, model: 'glm', dtps: 0 }, // outage breaks the line
    { t: 111, model: 'glm', dtps: 5 }
  ] }));
  const chart = speedChart(rows, 'dtps', 130);
  assert.equal(chart.max, 20);
  assert.equal(chart.rows.length, 2);
  assert.equal(chart.rows[0].path.split('M').length - 1, 3);
  assert.equal(chart.rows[0].path.split('L').length - 1, 1);
  assert.equal(chart.rows[0].value, 5);
  const ys = [...chart.rows[0].path.matchAll(/[ML][\d.]+,([\d.]+)/g)].map(m => +m[1]);
  assert.ok(ys.every(y => y >= 12 && y <= 92), 'paths fit the compact plot');
  assert.doesNotMatch(chart.rows[0].path, /NaN|Infinity/);
});

test('latest proxy keeps bounded speed history only for serving heads', () => {
  const history = Array.from({ length: 900 }, (_, t) => ({ t, model: 'glm', dtps: 22, pfps: 100, gu: 80 }));
  const result = stripMetricsHistory({ hosts: [
    { id: 'head', latest: { llm: { up: true } }, history },
    { id: 'worker', latest: { llm: { up: true, role: 'worker' } }, history }
  ] });
  assert.equal(result.hosts[0].speed_history.length, 125);
  assert.equal(result.hosts[0].speed_history[0].t, 775);
  assert.equal(result.hosts[0].speed_history[0].gu, undefined);
  assert.deepEqual(result.hosts[1].speed_history, []);
});


test('chart inspection snaps to real ticks, preserving exact values and gaps', () => {
  const series = [{ id: 'a', model: 'glm', points: [
    { t: 110, model: 'old', dtps: 999 },
    { t: 100, model: 'glm', dtps: 20, pfps: 600 },
    { t: 102, model: 'glm', dtps: null, pfps: 700 },
    { t: 104, model: 'glm', dtps: 30, pfps: 800 }
  ] }, { id: 'b', model: 'glm', points: [{ t: 104, model: 'glm', dtps: 40 }] }];
  assert.deepEqual(chartTicks(series, 220), [100, 102, 104]);
  assert.equal(chartTickAt(series, 2 / 120, 220), 102);
  assert.equal(chartTickAt(series, -1, 220), 100);
  assert.equal(chartTickAt(series, 1, 220), 104);
  assert.equal(chartTickAt([], .5, 220), null);
  assert.equal(chartTickAt(series, NaN, 220), null);
  const decode = speedChart(series, 'dtps', 220, 120, 102);
  assert.equal(decode.rows[0].value, null, 'missing data is not interpolated');
  assert.equal(decode.rows[1].value, null, 'other host cannot borrow a different tick');
  const prefill = speedChart(series, 'pfps', 220, 120, 102);
  assert.equal(prefill.rows[0].value, 700);
  assert.equal(prefill.selectedX, decode.selectedX);
  assert.equal(speedChart(series, 'dtps', 220, 120, 104).rows[0].value, 30);
  assert.equal(speedChart(series, 'dtps', 221, 120, 104).rows[0].value, 30, 'live updates do not move selection');
  assert.equal(speedChart(series, 'dtps', 230, 120, 104).selectedX, null, 'expired tick is outside the plot');
});


test('combined chart has independent scales and independently hidden model lines', () => {
  const series = [
    { id: 'a', model: 'glm', points: [{ t: 100, model: 'glm', dtps: 20, pfps: 600 }] },
    { id: 'b', model: 'glm', points: [{ t: 100, model: 'glm', dtps: 40, pfps: 1200 }] }
  ];
  const decode = speedChart(series, 'dtps', 100, 120, 100, ['b']);
  const prefill = speedChart(series, 'pfps', 100, 120, 100);
  assert.equal(decode.max, 20);
  assert.equal(prefill.max, 1200);
  assert.equal(decode.selectedX, prefill.selectedX);
  assert.equal(decode.selectedX, 562, 'right axis has its own gutter');
  assert.equal(decode.rows[1].hidden, true);
  assert.equal(decode.rows[1].value, 40, 'hidden line keeps its clickable value');
  assert.equal(prefill.rows[1].hidden, false, 'decode toggle does not hide prefill');
  const none = speedChart(series, 'dtps', 100, 120, null, ['a', 'b']);
  assert.equal(none.max, 1);
  assert.ok(none.rows.every(row => row.hidden));
});


test('aggregate is displayed for zero, one, or many active requests without guessing', () => {
  for (const slots_busy of [0, 1, 4]) assert.equal(aggregateStat({gen_tps: 63.2, slots_busy}).main, '63.2');
  assert.equal(aggregateStat({gen_tps: 0}).main, '0.0');
  for (const ll of [{}, {gen_tps: NaN}, {gen_tps: Infinity}, {gen_tps: -1},
    {gen_tps: 60, metrics_stale: true}, {gen_tps: 60, up: false}, {gen_tps: 60, role: 'worker'}]) {
    assert.equal(aggregateStat(ll).main, '—');
  }
});

test('aggregate history reaches charts once per head and keeps exact rates', () => {
  const history = [{t: 100, model: 'glm', dtps: 20, tps: 60}, {t: 101, model: 'glm', dtps: 20, tps: null}];
  const data = stripMetricsHistory({hosts: [
    {id:'head', latest:{llm:{up:true}}, history},
    {id:'rank', latest:{llm:{up:true, role:'worker'}}, history}
  ]});
  assert.equal(data.hosts[0].speed_history[0].tps, 60);
  assert.equal(data.hosts[0].speed_history[1].tps, null);
  assert.deepEqual(data.hosts[1].speed_history, []);
  const chart = speedChart([{id:'head', model:'glm', points:data.hosts[0].speed_history}], 'tps', 101, 120, 100);
  assert.equal(chart.max, 60);
  assert.equal(chart.rows[0].value, 60);
});

test('prefill falls back to the lifetime measured rate when no request completed in 60s', () => {
  const st = prefillStat({ prefill_tps_exp_60s: null, prefill_tps_exp_life: 753.7 });
  assert.equal(st.main, '753.7');
  assert.equal(st.basis, 'life');
  assert.equal(prefillStat({ metrics_stale: true, prefill_tps_exp_life: 753.7 }).main, '—');
});
