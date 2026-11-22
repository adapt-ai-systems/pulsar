import { test } from 'node:test';
import assert from 'node:assert/strict';
import { stripMetricsHistory, wantsMetricsLatest, metricsWantsSpeed } from './metricslatest.js';

test('wantsMetricsLatest matches query param and latest.json alias', () => {
  assert.equal(wantsMetricsLatest('/metrics-proxy/data.json'), false);
  assert.equal(wantsMetricsLatest('/metrics-proxy/data.json?latest=1'), true);
  assert.equal(wantsMetricsLatest('/metrics-proxy/data.json?foo=1&latest'), true);
  assert.equal(wantsMetricsLatest('/metrics-proxy/latest.json'), true);
  assert.equal(wantsMetricsLatest('/metrics-proxy/'), false);
});

test('stripMetricsHistory drops history arrays and keeps latest', () => {
  const src = {
    hosts: [
      { id: 'a', name: 'host-a', latest: { gpu: { util: 1 } }, history: [{ t: 1 }, { t: 2 }] },
      { id: 'b', name: 'host-b', latest: { online: true }, history: new Array(900).fill({ t: 0 }) }
    ]
  };
  const out = stripMetricsHistory(src);
  assert.equal(out.hosts.length, 2);
  assert.deepEqual(out.hosts[0].latest, { gpu: { util: 1 } });
  assert.equal('history' in out.hosts[0], false);
  assert.equal('history' in out.hosts[1], false);
  assert.ok(src.hosts[0].history.length === 2, 'input not mutated');
});

test('stripMetricsHistory shrinks large multi-host history snapshots to a few KB', () => {
  const bigHistory = Array.from({ length: 1800 }, (_, i) => ({
    t: 1700000000 + i,
    cpu: 42.5,
    mem: 64.1,
    gpu: 88.2,
    model: 'sample-model',
    dtps: 34.2,
    pfps: 520.0,
    tps: 30.1
  }));
  const src = {
    hosts: Array.from({ length: 4 }, (_, i) => ({
      id: `node-${i}`,
      name: `node-${i}`,
      latest: { online: true, ts: 1700001800, llm: { up: true, role: 'head' } },
      history: bigHistory
    }))
  };
  const rawBytes = Buffer.byteLength(JSON.stringify(src));
  const out = stripMetricsHistory(src);
  const outBytes = Buffer.byteLength(JSON.stringify(out));
  assert.ok(out.hosts.every((h) => h.latest && !('history' in h)));
  assert.ok(rawBytes > 500_000, 'fixture should carry heavy history');
  assert.ok(outBytes < 50_000, `latest-only was ${outBytes} bytes`);
});

test('speed: false drops speed_history too (the collapsed bar never reads it)', () => {
  const host = {
    id: 'a',
    latest: { llm: { up: true, role: 'leader' } },
    history: Array.from({ length: 300 }, (_, i) => ({ t: i, model: 'm', dtps: 10, pfps: 100, tps: 9, gpu: 50 }))
  };
  const full = stripMetricsHistory({ hosts: [host] });
  assert.equal(full.hosts[0].speed_history.length, 125);
  const slim = stripMetricsHistory({ hosts: [host] }, { speed: false });
  assert.deepEqual(slim.hosts[0].speed_history, []);
  assert.deepEqual(slim.hosts[0].latest, host.latest);
});

test('metricsWantsSpeed: only ?speed=0 opts out', () => {
  assert.equal(metricsWantsSpeed('/metrics-proxy/data.json?latest=1'), true);
  assert.equal(metricsWantsSpeed('/metrics-proxy/data.json?latest=1&speed=0'), false);
  assert.equal(metricsWantsSpeed('/metrics-proxy/data.json?speed=1&latest=1'), true);
  assert.equal(metricsWantsSpeed('/metrics-proxy/latest.json'), true);
});
