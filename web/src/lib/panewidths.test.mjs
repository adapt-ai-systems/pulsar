import { test } from 'node:test';
import assert from 'node:assert/strict';
import { isWatcher } from './pulsetree.js';
import { loadRuneModule } from './runes-loader.mjs';

test('isWatcher: Watchers-shelf mode or a Watcher title', () => {
  assert.equal(isWatcher({ title: 'Status check', settings: { mode: 'watching' } }), true);
  assert.equal(isWatcher({ title: 'Uptime Watcher (Opus low)', settings: {} }), true);
  assert.equal(isWatcher({ title: 'Watcher — Build Fleet' }), true);
  assert.equal(isWatcher({ title: 'GLM 5.5 watch', settings: { mode: null } }), false);
  assert.equal(isWatcher({ title: 'Paused thing', settings: { mode: 'paused' } }), false);
});

test('pane widths: defaults and saved values', async () => {
  const store = new Map();
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
  };
  store.set('pulsar.paneWidths', JSON.stringify({ list: 410, inbox: 'junk' }));
  const m = await loadRuneModule('uiprefs.svelte.js');
  m.hydrateViewPrefs();
  assert.deepEqual(m.uiPrefs.paneWidths, { list: 410, inbox: 1000 });
  m.setPaneWidth('list', 333.6);
  assert.equal(JSON.parse(store.get('pulsar.paneWidths')).list, 334);
  m.resetPaneWidth('list');
  assert.equal(m.uiPrefs.paneWidths.list, 320);
});
