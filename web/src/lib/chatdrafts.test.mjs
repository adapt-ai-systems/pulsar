import { test } from 'node:test';
import assert from 'node:assert/strict';
import { createDraftStore, DRAFT_PREFIX } from './chatdrafts.js';
function storage() {
  const data = new Map();
  return { getItem: k => data.get(k) ?? null, setItem: (k,v) => data.set(k,v), removeItem: k => data.delete(k) };
}
test('each keystroke is durable before write returns, including whitespace and Unicode', () => {
  const disk = storage(), drafts = createDraftStore(() => disk);
  for (const text of ['h', 'hi', '  hi\n🙂  ']) {
    assert.equal(drafts.write('a', text), true);
    assert.equal(disk.getItem(DRAFT_PREFIX + 'a'), text);
    assert.equal(createDraftStore(() => disk).read('a'), text, 'fresh frontend restores it');
  }
});
test('pulse drafts are isolated and deleting text survives reload', () => {
  const disk = storage(), drafts = createDraftStore(() => disk);
  drafts.write('a', 'alpha'); drafts.write('b', 'beta'); drafts.write('a', '');
  const restored = createDraftStore(() => disk);
  assert.equal(restored.read('a'), ''); assert.equal(restored.read('b'), 'beta');
});
test('expanded and grid copies receive edits and successful send clearing', () => {
  const drafts = createDraftStore(storage), a = [], b = [];
  const off = drafts.subscribe('pulse', text => a.push(text));
  drafts.subscribe('pulse', text => b.push(text));
  drafts.write('pulse', 'hello'); drafts.clearSent('pulse', 'hello');
  assert.deepEqual(a, ['', 'hello', '']); assert.deepEqual(b, a);
  off(); drafts.write('pulse', 'next'); assert.equal(a.length, 3);
});
test('send acknowledgement cannot erase a newer draft', () => {
  const disk = storage(), drafts = createDraftStore(() => disk);
  drafts.write('a', 'first'); drafts.write('a', 'next'); drafts.clearSent('a', 'first');
  assert.equal(drafts.read('a'), 'next'); assert.equal(createDraftStore(() => disk).read('a'), 'next');
});
test('successful send removes the stored draft', () => {
  const disk = storage(), drafts = createDraftStore(() => disk);
  drafts.write('a', '  send me  '); drafts.clearSent('a', '  send me  ');
  assert.equal(disk.getItem(DRAFT_PREFIX + 'a'), null);
  assert.equal(createDraftStore(() => disk).read('a'), '');
});
test('storage failures preserve in-memory text and notify the composer', () => {
  const drafts = createDraftStore(() => { throw Error('storage unavailable'); });
  const updates = []; drafts.subscribe('a', (text, saved) => updates.push({text, saved}));
  assert.equal(drafts.write('a', 'keep this'), false);
  assert.equal(drafts.read('a'), 'keep this');
  assert.deepEqual(updates.at(-1), {text:'keep this', saved:false});
  drafts.subscribe('a', (text, saved) => updates.push({text, saved}));
  assert.deepEqual(updates.at(-1), {text:'keep this', saved:false}, 'remount must retain the warning');
});
function fakeRemote(initial = {}) {
  const data = new Map(Object.entries(initial)), puts = [];
  return { data, puts, get: async k => data.get(k) ?? null, put: async (k, v) => { puts.push([k, v]); data.set(k, v); } };
}
test('an empty composer adopts the daemon copy (iOS lost localStorage)', async () => {
  const remote = fakeRemote({ 'chatDraft.a': 'from daemon' });
  const drafts = createDraftStore(storage, remote), seen = [];
  drafts.subscribe('a', text => seen.push(text));
  await new Promise(r => setTimeout(r, 0));
  assert.equal(drafts.read('a'), 'from daemon'); assert.equal(seen.at(-1), 'from daemon');
});
test('local text wins over the daemon copy', async () => {
  const disk = storage(); disk.setItem(DRAFT_PREFIX + 'a', 'local');
  const drafts = createDraftStore(() => disk, fakeRemote({ 'chatDraft.a': 'old' }));
  drafts.subscribe('a', () => {});
  await new Promise(r => setTimeout(r, 0));
  assert.equal(drafts.read('a'), 'local');
});
test('typing reaches the daemon on flush, and a send clears it there', async () => {
  const remote = fakeRemote(), drafts = createDraftStore(storage, remote);
  drafts.write('a', 'hel'); drafts.write('a', 'hello'); drafts.flush();
  assert.deepEqual(remote.puts, [['chatDraft.a', 'hello']]);
  drafts.clearSent('a', 'hello');
  assert.equal(remote.data.get('chatDraft.a'), '');
});
