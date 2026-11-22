import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import vm from 'node:vm';
import { setDefaultTimeZone, getDefaultTimeZone, fmtBookmarkTime } from './time.js';

function loadSvelteModule(relPath, mocks) {
  let code = readFileSync(new URL(relPath, import.meta.url), 'utf8');
  // Strip ESM imports and collect exports into a returned object so we can run
  // runes-based .svelte.js modules in node:test without a Svelte compiler step.
  code = code.replace(/^import\s+[\s\S]*?from\s+['"][^'"]+['"];\s*$/gm, '');
  const exported = [];
  code = code.replace(/^export\s+(?:async\s+)?function\s+(\w+)/gm, (m, name) => {
    exported.push(name);
    return m.replace(/^export\s+/, '');
  });
  code = code.replace(/^export\s+const\s+(\w+)/gm, (m, name) => {
    exported.push(name);
    return m.replace(/^export\s+/, '');
  });
  const ctx = vm.createContext({
    $state: (x) => x,
    Intl,
    URL,
    Promise,
    console,
    ...mocks
  });
  vm.runInContext(`${code}\nglobalThis.__exports = { ${exported.join(', ')} };`, ctx);
  return ctx.__exports;
}

test('appsettings defaults keep hub & metrics off, owner="the user", guard on, browser zone', () => {
  const mod = loadSvelteModule('./appsettings.svelte.js', {
    getSetting: async () => ({ ok: true, value: null }),
    putSetting: async () => ({ ok: true }),
    deleteSetting: async () => ({ ok: true }),
    setDefaultTimeZone: () => {}
  });
  assert.equal(mod.ownerDisplayName(), 'the user');
  assert.equal(mod.activeTimeZone(), undefined);
  assert.equal(mod.isHubEnabled(), false);
  assert.equal(mod.isMetricsEnabled(), false);
  assert.equal(mod.appSettings.guardEnabled, true);
  assert.deepEqual(Array.from(mod.appSettings.guardExtraPaths), []);
  assert.equal(mod.appSettings.defaultBackup, null);
});

test('loadAppSettings reads /settings/{key} and saveAppSettings validates, puts, and deletes', async () => {
  const store = new Map([
    ['owner.name', 'Alex'],
    ['owner.timezone', 'America/New_York'],
    ['hub.url', 'http://127.0.0.1:8080/hub/'],
    ['metrics.url', ''],
    ['guard.enabled', false],
    ['guard.extra_paths', ['/srv/prod', '/opt/data']],
    ['default_backup', { model: 'gpt-6-sol', effort: 'high' }]
  ]);
  const calls = [];
  let syncedTz = null;
  const mod = loadSvelteModule('./appsettings.svelte.js', {
    getSetting: async (k) => ({ ok: true, value: store.has(k) ? store.get(k) : null }),
    putSetting: async (k, v) => { calls.push(['PUT', k, v]); store.set(k, v); return { ok: true }; },
    deleteSetting: async (k) => { calls.push(['DELETE', k]); store.delete(k); return { ok: true }; },
    setDefaultTimeZone: (tz) => { syncedTz = tz; }
  });

  await mod.loadAppSettings();
  assert.equal(mod.ownerDisplayName(), 'Alex');
  assert.equal(mod.activeTimeZone(), 'America/New_York');
  assert.equal(syncedTz, 'America/New_York');
  assert.equal(mod.isHubEnabled(), true);
  assert.equal(mod.appSettings.hubUrl, 'http://127.0.0.1:8080/hub');
  assert.equal(mod.isMetricsEnabled(), false);
  assert.equal(mod.appSettings.guardEnabled, false);
  assert.deepEqual(mod.appSettings.guardExtraPaths, ['/srv/prod', '/opt/data']);
  assert.deepEqual({ ...mod.appSettings.defaultBackup }, { model: 'gpt-6-sol', effort: 'high' });

  // Rejects invalid timezone and non-http URLs without writing.
  const badTz = await mod.saveAppSettings({
    ownerName: 'Alex', ownerTimezone: 'Mars/Olympus', hubUrl: '', metricsUrl: '',
    guardEnabled: true, guardExtraPaths: [], defaultBackup: null
  });
  assert.equal(badTz.ok, false);
  assert.equal(calls.length, 0);

  const badUrl = await mod.saveAppSettings({
    ownerName: '', ownerTimezone: '', hubUrl: 'ftp://example.com', metricsUrl: '',
    guardEnabled: true, guardExtraPaths: [], defaultBackup: null
  });
  assert.equal(badUrl.ok, false);
  assert.equal(calls.length, 0);

  // Clearing optional settings deletes those keys (including default_backup).
  const ok = await mod.saveAppSettings({
    ownerName: '',
    ownerTimezone: '',
    hubUrl: '',
    metricsUrl: 'https://metrics.example.com/dash/',
    guardEnabled: true,
    guardExtraPaths: ['/srv/a', '/srv/b'],
    defaultBackup: null
  });
  assert.equal(ok.ok, true);
  assert.equal(mod.ownerDisplayName(), 'the user');
  assert.equal(mod.isHubEnabled(), false);
  assert.equal(mod.isMetricsEnabled(), true);
  assert.equal(mod.appSettings.metricsUrl, 'https://metrics.example.com/dash');
  assert.equal(mod.appSettings.defaultBackup, null);
  assert.equal(syncedTz, null);
  assert.ok(calls.some(([m, k]) => m === 'DELETE' && k === 'hub.url'));
  assert.ok(calls.some(([m, k]) => m === 'DELETE' && k === 'owner.name'));
  assert.ok(calls.some(([m, k]) => m === 'DELETE' && k === 'owner.timezone'));
  assert.ok(calls.some(([m, k]) => m === 'DELETE' && k === 'default_backup'));
  assert.ok(calls.some(([m, k, v]) => m === 'PUT' && k === 'metrics.url' && v === 'https://metrics.example.com/dash'));

  // Saving defaultBackup with model only omits effort; with effort includes it.
  calls.length = 0;
  const setModelOnly = await mod.saveAppSettings({
    ownerName: '', ownerTimezone: '', hubUrl: '', metricsUrl: '',
    guardEnabled: true, guardExtraPaths: [],
    defaultBackup: { model: 'claude-opus-5-5', effort: '' }
  });
  assert.equal(setModelOnly.ok, true);
  assert.deepEqual({ ...mod.appSettings.defaultBackup }, { model: 'claude-opus-5-5' });
  assert.ok(calls.some(([m, k, v]) => m === 'PUT' && k === 'default_backup' && v.model === 'claude-opus-5-5' && !('effort' in v)));

  calls.length = 0;
  const setWithEffort = await mod.saveAppSettings({
    ownerName: '', ownerTimezone: '', hubUrl: '', metricsUrl: '',
    guardEnabled: true, guardExtraPaths: [],
    defaultBackup: { model: 'gpt-6-sol', effort: 'high' }
  });
  assert.equal(setWithEffort.ok, true);
  assert.deepEqual({ ...mod.appSettings.defaultBackup }, { model: 'gpt-6-sol', effort: 'high' });
  assert.ok(calls.some(([m, k, v]) => m === 'PUT' && k === 'default_backup' && v.model === 'gpt-6-sol' && v.effort === 'high'));
});

test('hubstore makes zero API calls when hub.url is unset and uses /hub-proxy/ when set', async () => {
  let enabled = false;
  let apiCalls = 0;
  const mod = loadSvelteModule('./hubstore.svelte.js', {
    isHubEnabled: () => enabled,
    listHubPages: async () => { apiCalls++; return { ok: true, value: [{ slug: 'demo', title: 'Demo' }] }; },
    listHubPins: async () => { apiCalls++; return { ok: true, value: [{ slug: 'demo', title: 'Demo' }] }; },
    pinHubPage: async () => { apiCalls++; return { ok: true, value: [] }; },
    unpinHubPage: async () => { apiCalls++; return { ok: true, value: [] }; }
  });

  await mod.loadPages();
  await mod.loadPins();
  await mod.pin('demo');
  assert.equal(apiCalls, 0, 'hubstore must not hit daemon hub endpoints when hub.url is unset');
  assert.deepEqual(Array.from(mod.hub.pages), []);
  assert.deepEqual(Array.from(mod.hub.pins), []);

  enabled = true;
  await mod.loadPages();
  await mod.loadPins();
  assert.equal(apiCalls, 2);
  assert.equal(mod.hub.pages.length, 1);
  assert.equal(mod.hub.pins.length, 1);
  assert.equal(mod.pageUrl('demo'), '/hub-proxy/demo/');
});

test('UI gates hub button/pins and metrics bar on hub.url / metrics.url', () => {
  const page = readFileSync(new URL('../routes/+page.svelte', import.meta.url), 'utf8');
  const rail = readFileSync(new URL('./SideRail.svelte', import.meta.url), 'utf8');
  const modal = readFileSync(new URL('./AppSettingsModal.svelte', import.meta.url), 'utf8');
  const bar = readFileSync(new URL('./MetricsBar.svelte', import.meta.url), 'utf8');

  assert.match(page, /\{#if uiPrefs\.showMetricsBar && isMetricsEnabled\(\)\}/);
  assert.match(page, /hubEnabled=\{isHubEnabled\(\)\}/);
  assert.match(rail, /\{#if hubEnabled\}/);
  assert.match(bar, /\{#if isMetricsEnabled\(\)\}/);
  for (const label of ['Owner name', 'Time zone', 'Pages hub URL', 'Metrics bar URL', 'Delete guard', 'Extra protected paths', 'Default backup model']) {
    assert.ok(modal.includes(label), `AppSettingsModal missing field: ${label}`);
  }
});

test('fmtBookmarkTime honors configured owner.timezone', () => {
  try {
    setDefaultTimeZone('UTC');
    assert.equal(getDefaultTimeZone(), 'UTC');
    assert.equal(fmtBookmarkTime('2026-11-08T20:15:00+00:00', undefined, { withZone: true }), '8:15 PM UTC');

    setDefaultTimeZone('America/New_York');
    const ny = fmtBookmarkTime('2026-11-08T20:15:00+00:00', undefined, { withZone: true });
    assert.match(ny, /^3:15 PM (EST|GMT-5)$/);
  } finally {
    setDefaultTimeZone(null);
  }
});
