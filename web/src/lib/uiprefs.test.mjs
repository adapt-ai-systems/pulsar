import assert from 'node:assert/strict';
import test from 'node:test';
import { loadRuneModule } from './runes-loader.mjs';

function makeStorage(seed = {}) {
  const map = new Map(Object.entries(seed));
  return {
    map,
    getItem: (k) => (map.has(k) ? map.get(k) : null),
    setItem: (k, v) => map.set(k, String(v)),
    removeItem: (k) => map.delete(k)
  };
}

// Legacy page readers at 0b5e40a:
function oldReadColumns(ls, options = [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]) {
  const saved = Number(ls.getItem('pulsar.gridColumns'));
  return options.includes(saved) ? saved : 3;
}
const oldReadHideStopped = (ls) => ls.getItem('pulsar.hideStopped') === '1';
const oldReadListDocked = (ls) => ls.getItem('pulsar.listDocked') === '1';
const oldReadDiceLocked = (ls) => ls.getItem('pulsar.diceLocked') === '1';

test('view prefs defaults and keys match old +page.svelte logic byte-for-byte', async () => {
  const cases = [
    {},
    {
      'pulsar.gridColumns': '5',
      'pulsar.hideStopped': '1',
      'pulsar.listDocked': '1',
      'pulsar.diceLocked': '1'
    },
    {
      'pulsar.gridColumns': '99',
      'pulsar.hideStopped': '0',
      'pulsar.listDocked': '0',
      'pulsar.diceLocked': '0'
    },
    {
      'pulsar.gridColumns': 'garbage',
      'pulsar.hideStopped': 'true',
      'pulsar.listDocked': '',
      'pulsar.diceLocked': 'yes'
    }
  ];

  const mod = await loadRuneModule('uiprefs.svelte.js');
  assert.deepEqual(mod.COLUMN_OPTIONS, [1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);

  for (const seed of cases) {
    const ls = makeStorage(seed);
    globalThis.localStorage = ls;
    mod.hydrateViewPrefs();

    assert.equal(mod.uiPrefs.columns, oldReadColumns(ls));
    assert.equal(mod.uiPrefs.hideStopped, oldReadHideStopped(ls));
    assert.equal(mod.uiPrefs.listDocked, oldReadListDocked(ls));
    assert.equal(mod.uiPrefs.diceLocked, oldReadDiceLocked(ls));
  }
  delete globalThis.localStorage;
});

test('setColumns, setHideStopped, setListDocked, setDiceLocked write exact localStorage keys and values', async () => {
  const ls = makeStorage();
  globalThis.localStorage = ls;
  const mod = await loadRuneModule('uiprefs.svelte.js');
  mod.hydrateViewPrefs();

  mod.setColumns(7);
  assert.equal(mod.uiPrefs.columns, 7);
  assert.equal(ls.getItem('pulsar.gridColumns'), '7');

  // Invalid column count falls back to 3:
  mod.setColumns(42);
  assert.equal(mod.uiPrefs.columns, 3);
  assert.equal(ls.getItem('pulsar.gridColumns'), '3');

  // hideStopped writes '1' when on and removes the key when off (matching +page.svelte):
  mod.setHideStopped(true);
  assert.equal(mod.uiPrefs.hideStopped, true);
  assert.equal(ls.getItem('pulsar.hideStopped'), '1');
  mod.setHideStopped(false);
  assert.equal(mod.uiPrefs.hideStopped, false);
  assert.equal(ls.getItem('pulsar.hideStopped'), null);

  // listDocked and diceLocked write '1' / '0':
  mod.setListDocked(true);
  assert.equal(mod.uiPrefs.listDocked, true);
  assert.equal(ls.getItem('pulsar.listDocked'), '1');
  mod.setListDocked(false);
  assert.equal(mod.uiPrefs.listDocked, false);
  assert.equal(ls.getItem('pulsar.listDocked'), '0');

  mod.setDiceLocked(true);
  assert.equal(mod.uiPrefs.diceLocked, true);
  assert.equal(ls.getItem('pulsar.diceLocked'), '1');
  mod.setDiceLocked(false);
  assert.equal(mod.uiPrefs.diceLocked, false);
  assert.equal(ls.getItem('pulsar.diceLocked'), '0');

  delete globalThis.localStorage;
});
