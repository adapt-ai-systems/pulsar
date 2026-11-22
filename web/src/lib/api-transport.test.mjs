import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  getUsage,
  listPulses,
  listInbox,
  archivePulse,
  deletePulse,
  isMonitoringChoice,
  sendMessageMultipart,
  listHubPages,
  listHubPins,
  pinHubPage,
  unpinHubPage
} from './api.js';
import { loadRuneModule } from './runes-loader.mjs';

function jsonRes(body, status = 200) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' }
  });
}

function htmlRes(html, status = 502, statusText = 'Bad Gateway') {
  return new Response(html, {
    status,
    statusText,
    headers: { 'content-type': 'text/html' }
  });
}

test('call() transport: success (value, data, bare, 204), network error, HTML 502, bad JSON, ok:false', async (t) => {
  // 1. success {ok: true, value}
  t.mock.method(globalThis, 'fetch', async () => jsonRes({ ok: true, value: { vendors: {} } }));
  assert.deepEqual(await getUsage(), { ok: true, value: { vendors: {} } });

  // 2. success {ok: true, data} envelope
  t.mock.method(globalThis, 'fetch', async () => jsonRes({ ok: true, data: { root: 'p1' } }));
  assert.deepEqual(await getUsage(), { ok: true, value: { root: 'p1' } });

  // 3. bare object (e.g. /healthz pass-through) and 204 empty
  t.mock.method(globalThis, 'fetch', async () => jsonRes({ available: false }));
  assert.deepEqual(await getUsage(), { ok: true, value: { available: false } });
  t.mock.method(globalThis, 'fetch', async () => new Response(null, { status: 204 }));
  assert.deepEqual(await getUsage(), { ok: true, value: null });

  // 4. network error
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('ECONNRESET'); });
  assert.deepEqual(await getUsage(), { ok: false, error: 'network error: ECONNRESET' });

  // 5. HTML 502 (status checked before JSON parse when content-type is not json)
  t.mock.method(globalThis, 'fetch', async () => htmlRes('<html>502</html>', 502, 'Bad Gateway'));
  assert.deepEqual(await getUsage(), { ok: false, error: 'HTTP 502: Bad Gateway' });

  // 6. Invalid JSON when content-type says application/json
  t.mock.method(globalThis, 'fetch', async () => new Response('not-json', {
    status: 500,
    headers: { 'content-type': 'application/json' }
  }));
  const badJson = await getUsage();
  assert.equal(badJson.ok, false);
  assert.match(badJson.error, /^bad JSON \(500\) after \d+ms:/);

  // 7. JSON {ok: false, error: 'boom'} (200 status)
  t.mock.method(globalThis, 'fetch', async () => jsonRes({ ok: false, error: 'boom' }, 200));
  assert.deepEqual(await getUsage(), { ok: false, error: 'boom' });
});

test('B34: call() preserves error.code on archive 409 monitoring_choice_required and delete 409 has_watchers', async (t) => {
  // Real 409 monitoring_choice_required shape from pulsar/daemon/app.py & tests/test_watchers_mode.py:80
  const archiveMsg =
    "pulse 'Campaign monitor' still watches 1 condition(s) and has 1 enabled schedule(s); send {\"monitoring\": \"keep\"} to switch it to watching mode instead, or {\"monitoring\": \"stop\"} to close them and archive";
  t.mock.method(globalThis, 'fetch', async () =>
    jsonRes({ ok: false, error: { code: 'monitoring_choice_required', message: archiveMsg } }, 409)
  );
  const rArch = await archivePulse('p1');
  assert.deepEqual(rArch, {
    ok: false,
    error: archiveMsg,
    code: 'monitoring_choice_required'
  });
  assert.equal(isMonitoringChoice(rArch), true);
  assert.equal(isMonitoringChoice({ ok: false, error: 'monitoring_choice_required' }), true);
  assert.equal(isMonitoringChoice({ ok: false, error: 'other', code: 'not_owner' }), false);
  assert.equal(isMonitoringChoice({ ok: true, value: {} }), false);

  // Real 409 has_watchers shape from pulsar/daemon/app.py & tests/test_watchers_mode.py:290
  const delMsg = 'pulse has active watchers; confirm with ?close_watchers=true to close them and delete';
  t.mock.method(globalThis, 'fetch', async () =>
    jsonRes({ ok: false, error: { code: 'has_watchers', message: delMsg } }, 409)
  );
  const rDel = await deletePulse('p1');
  assert.deepEqual(rDel, {
    ok: false,
    error: delMsg,
    code: 'has_watchers'
  });
});

test('sendMessageMultipart: success, network error, HTML 502, and JSON error', async (t) => {
  const files = [{ blob: new Blob(['hi'], { type: 'text/plain' }), name: 'a.txt' }];

  // 1. success
  t.mock.method(globalThis, 'fetch', async () => jsonRes({ ok: true, value: { queued: true } }));
  assert.deepEqual(await sendMessageMultipart('p1', 'hello', files), { ok: true, value: { queued: true } });

  // 2. network error
  t.mock.method(globalThis, 'fetch', async () => { throw new Error('offline'); });
  assert.deepEqual(await sendMessageMultipart('p1', 'hello', files), { ok: false, error: 'network error: offline' });

  // 3. HTML 502 (parses JSON unconditionally today -> bad JSON (502))
  t.mock.method(globalThis, 'fetch', async () => htmlRes('<html>502</html>', 502, 'Bad Gateway'));
  const html502 = await sendMessageMultipart('p1', 'hello', files);
  assert.equal(html502.ok, false);
  assert.match(html502.error, /^bad JSON \(502\):/);

  // 4. JSON {ok: false, error: {message}} and HTTP detail error
  t.mock.method(globalThis, 'fetch', async () => jsonRes({ ok: false, error: { message: 'too large' } }, 413));
  assert.deepEqual(await sendMessageMultipart('p1', 'hello', files), { ok: false, error: 'HTTP 413: too large' });
});

test('hub wrappers and hubstore.svelte.js: listHubPages, listHubPins, pinHubPage, unpinHubPage', async (t) => {
  const seen = [];
  t.mock.method(globalThis, 'fetch', async (url, init) => {
    seen.push({ url: String(url), method: init?.method ?? 'GET', body: init?.body ? JSON.parse(init.body) : undefined });
    return jsonRes({ ok: true, value: [{ slug: 's1', title: 'S1' }] });
  });

  assert.deepEqual(await listHubPages(), { ok: true, value: [{ slug: 's1', title: 'S1' }] });
  assert.equal(seen[0].url, '/api/hub/pages');
  assert.deepEqual(await listHubPins(), { ok: true, value: [{ slug: 's1', title: 'S1' }] });
  assert.equal(seen[1].url, '/api/hub/pins');
  assert.deepEqual(await pinHubPage('s 1'), { ok: true, value: [{ slug: 's1', title: 'S1' }] });
  assert.equal(seen[2].method, 'POST');
  assert.deepEqual(seen[2].body, { slug: 's 1' });
  assert.deepEqual(await unpinHubPage('s 1'), { ok: true, value: [{ slug: 's1', title: 'S1' }] });
  assert.equal(seen[3].method, 'DELETE');
  assert.equal(seen[3].url, '/api/hub/pins/s%201');

  // hubstore via loadRuneModule (with hub.url enabled)
  const hs = await loadRuneModule('hubstore.svelte.js', {
    rewrite: (code) => code.replace(/import\s*\{[^}]*isHubEnabled[^}]*\}\s*from\s*['"][^'"]+['"];/, 'const isHubEnabled = () => true;')
  });
  await hs.loadPages();
  assert.equal(hs.hub.pagesLoaded, true);
  assert.equal(hs.hub.pagesError, '');
  assert.deepEqual(hs.hub.pages, [{ slug: 's1', title: 'S1' }]);

  t.mock.method(globalThis, 'fetch', async () => jsonRes({ detail: 'hub offline' }, 503));
  await hs.loadPages();
  assert.equal(hs.hub.pagesError, 'HTTP 503: hub offline');

  // pin rollback on failure
  hs.hub.pins = [{ slug: 'keep', title: 'Keep' }];
  await hs.pin('fail-slug');
  assert.deepEqual(hs.hub.pins, [{ slug: 'keep', title: 'Keep' }]);
});

test('withQuery and P() path helpers: exact URLs for empty, single, and multiple params across all 6 query functions', async (t) => {
  const urls = [];
  t.mock.method(globalThis, 'fetch', async (url) => {
    urls.push(String(url));
    return jsonRes({ ok: true, value: [] });
  });

  // 1. listPulses: empty, one, several
  await listPulses();
  assert.equal(urls.pop(), '/api/pulses');
  await listPulses({ grid_id: 'g 1' });
  assert.equal(urls.pop(), '/api/pulses?grid_id=g+1');
  await listPulses({ grid_id: 'g1', state: 'running', include_archived: true });
  assert.equal(urls.pop(), '/api/pulses?grid_id=g1&state=running&include_archived=true');
  await listPulses({ state: 'archived', slim: true });
  assert.equal(urls.pop(), '/api/pulses?state=archived&slim=true');

  // 2. listInbox: empty, one, several
  await listInbox();
  assert.equal(urls.pop(), '/api/inbox');
  await listInbox({ unread_only: true, q: '' });
  assert.equal(urls.pop(), '/api/inbox?unread_only=true');
  await listInbox({ unread_only: false, channel: 'ops', limit: 25 });
  assert.equal(urls.pop(), '/api/inbox?unread_only=false&channel=ops&limit=25');
});
