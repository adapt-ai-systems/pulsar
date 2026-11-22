import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEVICE_PREFIX,
  getDeviceSetting,
  putDeviceSetting
} from './device.js';

function fakeStorage(initial = {}) {
  const m = new Map(Object.entries(initial));
  return {
    getItem: (k) => (m.has(k) ? m.get(k) : null),
    setItem: (k, v) => m.set(k, String(v))
  };
}

test('getDeviceSetting and putDeviceSetting read/write JSON with DEVICE_PREFIX and handle throwing storage', () => {
  const prev = globalThis.localStorage;
  try {
    const s = fakeStorage();
    globalThis.localStorage = s;
    assert.equal(DEVICE_PREFIX, 'pulsar-device.');
    assert.equal(getDeviceSetting('theme'), undefined);
    assert.equal(putDeviceSetting('theme', { dark: true }), true);
    assert.equal(s.getItem('pulsar-device.theme'), '{"dark":true}');
    assert.deepEqual(getDeviceSetting('theme'), { dark: true });

    globalThis.localStorage = {
      getItem() { throw new Error('denied'); },
      setItem() { throw new Error('denied'); }
    };
    assert.equal(getDeviceSetting('theme'), undefined);
    assert.equal(putDeviceSetting('theme', 'x'), false);
  } finally {
    if (prev === undefined) delete globalThis.localStorage;
    else globalThis.localStorage = prev;
  }
});
