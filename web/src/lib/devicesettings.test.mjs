// Per-device settings: two devices keep different values, an unset device
// falls back to the shared KV. Run: node src/lib/devicesettings.test.mjs
import assert from 'node:assert/strict';

function fakeStorage() {
  const m = new Map();
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)) };
}
const shared = { 'ui.compact': true };
globalThis.fetch = async (url) => {
  const key = decodeURIComponent(String(url).split('/settings/')[1]);
  return { ok: true, status: 200, headers: { get: () => 'application/json' }, json: async () => ({ ok: true, value: shared[key] ?? null }) };
};
const api = await import('./api.js');

globalThis.localStorage = fakeStorage(); // device A
assert.equal(api.getDeviceSetting('ui.compact'), undefined);
assert.equal((await api.getSettingForDevice('ui.compact')).value, true); // shared fallback
api.putDeviceSetting('ui.compact', false);
assert.equal((await api.getSettingForDevice('ui.compact')).value, false);
const a = globalThis.localStorage;

globalThis.localStorage = fakeStorage(); // device B, untouched by A
assert.equal((await api.getSettingForDevice('ui.compact')).value, true);
api.putDeviceSetting('theme.pick', 'nightfall');
assert.equal(api.getDeviceSetting('theme.pick'), 'nightfall');

globalThis.localStorage = a; // back on A: keeps its own values
assert.equal(api.getDeviceSetting('theme.pick'), undefined);
assert.equal(api.getDeviceSetting('ui.compact'), false);
console.log('devicesettings ok');
