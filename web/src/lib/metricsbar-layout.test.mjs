import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

test('mobile collapsed metrics grow with wrapped rows instead of clipping at 22px', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  const mobile = source.slice(source.indexOf('@media (max-width: 699px)'));
  assert.match(mobile, /\.metrics-bar:not\(\.expanded\)\s*\{[^}]*height: auto;[^}]*min-height: 22px;/);
  assert.match(mobile, /\.llm-band \.stat\.llm\s*\{[^}]*flex-wrap: wrap;[^}]*white-space: normal;/);
});

test('same-model serving heads have distinct identities', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  // Exercise the component's actual derived expression with two replicas.
  const expression = source.match(/const llms = \$derived\(([\s\S]*?)\n  \);/)[1];
  const derive = new Function('hosts', 'shortModel', `return (${expression});`);
  const hosts = ['node1', 'node6'].map(id => ({
    id, short: id, online: true, color: 'green',
    s: { llm: { up: true, model: 'same-model', role: 'head' } },
  }));
  hosts.push({ id: 'node2', short: 'node2', online: true,
    s: { llm: { up: true, model: 'same-model', role: 'worker', paired_with: 'node6' } } });
  const rows = derive(hosts, name => name);
  assert.equal(rows.length, 2);
  assert.equal(rows[0].model, rows[1].model);
  assert.deepEqual(rows.map(row => row.id), ['node1', 'node6']);
  assert.equal(rows[1].boxes, 'node6+node2');
  assert.equal([...source.matchAll(/\{#each shownLlms as l \(l\.id\)\}/g)].length, 3);
  assert.match(source, /\{#each llms as l \(l\.id\)\}/);
  assert.doesNotMatch(source, /\{#each shownLlms as l \(modelKey\(l\)\)\}/);
});


test('collapsed chips show aggregate output and chart view replaces the normal bar', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  assert.match(source, /snippet llmChip\(l, details = false\)/);
  assert.match(source, /\{@render llmChip\(l\)\}/);
  assert.doesNotMatch(source, /class="xrow"/);
  assert.match(source, /<div class="metrics-wrap">\s*\{#if !expanded\}\s*<button/);
  assert.match(source, /<SpeedCharts[^\n]*onclose=\{\(\) => expanded = false\}/);
  const snippet = source.slice(source.indexOf('{#snippet llmChip'), source.indexOf('{#snippet llmTile'));
  const extraStart = snippet.indexOf('{#if details}');
  const kvStart = snippet.indexOf('{#if kvStat');
  const aggregateAt = snippet.indexOf('class="lagg"');
  assert.ok(aggregateAt > 0 && aggregateAt < extraStart, 'aggregate output is also visible when collapsed');
  assert.match(snippet, /aggregateStat\(l\.ll\)\.main/);
  for (const cls of ['lreq', 'lttft', 'lpre', 'lcache']) {
    const at = snippet.indexOf('class="' + cls + '"');
    assert.ok(at > extraStart && at < kvStart, cls + ' is expanded-only');
  }
  assert.match(snippet, /details && kvPeak/);
  assert.match(snippet, /details && waiting/);
  const charts = readFileSync(new URL('./SpeedCharts.svelte', import.meta.url), 'utf8');
  assert.match(charts, /aria-label="Close metrics chart"[^>]*onclick=\{onclose\}/);
});

test('a box serving two models publishes two chips with distinct ids', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  const expression = source.match(/const llms = \$derived\(([\s\S]*?)\n  \);/)[1];
  const derive = new Function('hosts', 'shortModel', `return (${expression});`);
  const hosts = [{ id: 'gpu-a', short: 'gpu-a', online: true, color: 'purple',
    s: { llm: { up: true, model: 'qwen3.8-27b' },
         llms: [{ up: true, model: 'qwen3.8-27b' }, { up: true, model: 'sample-small-27B.gguf' }] } }];
  const rows = derive(hosts, name => name);
  assert.deepEqual(rows.map(r => r.id), ['gpu-a', 'gpu-a:1']);
  assert.deepEqual(rows.map(r => r.hostId), ['gpu-a', 'gpu-a']);
  assert.deepEqual(rows.map(r => r.model), ['qwen3.8-27b', 'sample-small-27B.gguf']);
});

test('folded ranks name the chip by TP size; every serve gets a full-stat chip when expanded', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  const expression = source.match(/const llms = \$derived\(([\s\S]*?)\n  \);/)[1];
  const derive = new Function('hosts', 'shortModel', `return (${expression});`);
  const head = (id, model) => ({ id, short: id, online: true, s: { llm: { up: true, model } } });
  const rank = (id, of) => ({ id, short: id, online: true, s: { llm: { up: true, role: 'worker', paired_with: of } } });
  const hosts = [head('node1', 'model-a'), rank('node3', 'node1'), rank('node5', 'node1'), rank('node6', 'node1'), rank('node4', 'node1'),
    head('worker-a', 'model-b'), rank('worker-b', 'worker-a'), head('gpu-a', 'model-c')];
  const names = { 'model-a': 'ModelA', 'model-b': 'ModelB', 'model-c': 'ModelC' };
  const rows = derive(hosts, n => names[n]);
  assert.deepEqual(rows.map(r => r.name), ['ModelA TP5', 'ModelB TP2', 'ModelC']);
  assert.deepEqual(rows.map(r => r.boxes), ['node1+node3+node5+node6+node4', 'worker-a+worker-b', 'gpu-a']);
  assert.match(source, /class="xchips">\s*\{#each shownLlms as l \(l\.id\)\}\s*\{@render llmChip\(l, true\)\}/);
});

test('shownLlms filters chips by both host toggle and model toggle', () => {
  const source = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');
  const expression = source.match(/const shownLlms = \$derived\(([\s\S]*?)\n  \);/)[1];
  const derive = new Function('llms', 'isMetricsHostHidden', 'modelKey', `return (${expression});`);
  const modelKey = (l) => 'model:' + l.model;
  const llms = [
    { id: 'node-a', hostId: 'node-a', model: 'strata' },
    { id: 'node-b', hostId: 'node-b', model: 'nova' },
  ];
  assert.deepEqual(derive(llms, (k) => k === 'node-a', modelKey).map((l) => l.id), ['node-b']);
  assert.deepEqual(derive(llms, (k) => k === 'model:nova', modelKey).map((l) => l.id), ['node-a']);
  assert.deepEqual(derive(llms, () => false, modelKey).map((l) => l.id), ['node-a', 'node-b']);
});
