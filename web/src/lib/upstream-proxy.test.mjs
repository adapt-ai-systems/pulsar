import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  DEFAULT_DAEMON_PORT,
  DEFAULT_DAEMON_URL,
  METRICS_PROXY_PREFIX,
  HUB_PROXY_PREFIX,
  daemonBaseUrl,
  daemonTargetUrl,
  parseAllowedHosts,
  isHostAllowed,
  isOriginAllowed,
  isCrossSiteFetch,
  isRequestOriginAllowed,
  isWebSocketOriginAllowed,
  normalizeUpstreamUrl,
  resolveTargetUrl,
  settingKeyFromPath,
  createSettingResolver
} from './upstream-proxy.js';

test('daemonBaseUrl: defaults to 127.0.0.1:8726, honors PULSAR_DAEMON_PORT and PULSAR_DAEMON', () => {
  assert.equal(DEFAULT_DAEMON_PORT, 8726);
  assert.equal(daemonBaseUrl({}), DEFAULT_DAEMON_URL);
  assert.equal(daemonBaseUrl({ PULSAR_DAEMON_PORT: '9001' }), 'http://127.0.0.1:9001');
  assert.equal(daemonBaseUrl({ PULSAR_DAEMON_PORT: 'not-a-port' }), DEFAULT_DAEMON_URL);
  assert.equal(daemonBaseUrl({ PULSAR_DAEMON_PORT: '0' }), DEFAULT_DAEMON_URL);
  assert.equal(
    daemonBaseUrl({ PULSAR_DAEMON: 'http://127.0.0.1:8800/', PULSAR_DAEMON_PORT: '9001' }),
    'http://127.0.0.1:8800'
  );
});

test('parseAllowedHosts and isHostAllowed: localhost/IPs pass by default, PULSAR_WEB_ALLOWED_HOSTS extends', () => {
  assert.deepEqual(parseAllowedHosts(''), []);
  assert.deepEqual(parseAllowedHosts(' app.example.com , .internal.example '), [
    'app.example.com',
    '.internal.example'
  ]);

  assert.equal(isHostAllowed('localhost:5292', []), true);
  assert.equal(isHostAllowed('127.0.0.1:5292', []), true);
  assert.equal(isHostAllowed('[::1]:5292', []), true);
  assert.equal(isHostAllowed('192.0.2.10:5292', []), true);
  assert.equal(isHostAllowed('evil.example.com', []), false);
  assert.equal(isHostAllowed('', []), false);

  const extra = parseAllowedHosts('app.example.com, .tail.example');
  assert.equal(isHostAllowed('app.example.com:5292', extra), true);
  assert.equal(isHostAllowed('box.tail.example', extra), true);
  assert.equal(isHostAllowed('other.example.com', extra), false);
  assert.equal(isHostAllowed('anything.example', ['*']), true);
});

test('isOriginAllowed: allows loopback and PULSAR_WEB_ALLOWED_HOSTS only, rejects bare non-loopback IPs', () => {
  assert.equal(isOriginAllowed('http://localhost:5292', []), true);
  assert.equal(isOriginAllowed('http://127.0.0.1:5292', []), true);
  assert.equal(isOriginAllowed('http://127.0.0.1:15296', []), true);
  assert.equal(isOriginAllowed('http://[::1]:5292', []), true);
  assert.equal(isOriginAllowed('https://localhost', []), true);

  assert.equal(isOriginAllowed('http://192.0.2.10:5292', []), false);
  assert.equal(isOriginAllowed('https://example.invalid', []), false);
  assert.equal(isOriginAllowed('null', []), false);
  assert.equal(isOriginAllowed('', []), false);
  assert.equal(isOriginAllowed('file:///etc/passwd', []), false);
  assert.equal(isOriginAllowed('http://localhost:5292/path', []), false);

  const extra = parseAllowedHosts('pulsar.example.test, .tail.example');
  assert.equal(isOriginAllowed('https://pulsar.example.test', extra), true);
  assert.equal(isOriginAllowed('https://box.tail.example:5292', extra), true);
  assert.equal(isOriginAllowed('https://other.example.test', extra), false);
  assert.equal(isOriginAllowed('https://anything.example', ['*']), true);
  assert.equal(isOriginAllowed('null', ['*']), false);
});

test('isRequestOriginAllowed and isWebSocketOriginAllowed: block foreign Origin and cross-site Sec-Fetch-Site', () => {
  assert.equal(isCrossSiteFetch('cross-site'), true);
  assert.equal(isCrossSiteFetch('Cross-Site'), true);
  assert.equal(isCrossSiteFetch('same-origin'), false);
  assert.equal(isCrossSiteFetch(undefined), false);

  // Safe methods always pass origin check
  assert.equal(isRequestOriginAllowed('GET', { origin: 'https://example.invalid' }, []), true);
  assert.equal(isRequestOriginAllowed('HEAD', { 'sec-fetch-site': 'cross-site' }, []), true);
  assert.equal(isRequestOriginAllowed('OPTIONS', { origin: 'https://example.invalid' }, []), true);

  // No Origin and no Sec-Fetch-Site (CLI tools) pass on mutating methods and WS
  assert.equal(isRequestOriginAllowed('POST', {}, []), true);
  assert.equal(isWebSocketOriginAllowed({}, []), true);

  // Mutating methods with allowed Origin pass
  assert.equal(isRequestOriginAllowed('POST', { origin: 'http://127.0.0.1:5292' }, []), true);
  assert.equal(isRequestOriginAllowed('PUT', { origin: 'http://localhost:5292', 'sec-fetch-site': 'same-origin' }, []), true);

  // Mutating methods with disallowed Origin or cross-site Sec-Fetch-Site fail
  for (const method of ['POST', 'PUT', 'PATCH', 'DELETE']) {
    assert.equal(isRequestOriginAllowed(method, { origin: 'https://example.invalid' }, []), false);
    assert.equal(isRequestOriginAllowed(method, { origin: 'null' }, []), false);
    assert.equal(isRequestOriginAllowed(method, { origin: 'http://192.0.2.10:5292' }, []), false);
    assert.equal(isRequestOriginAllowed(method, { 'sec-fetch-site': 'cross-site' }, []), false);
  }

  // WebSocket upgrades
  assert.equal(isWebSocketOriginAllowed({ origin: 'http://127.0.0.1:5292' }, []), true);
  assert.equal(isWebSocketOriginAllowed({ origin: 'https://example.invalid' }, []), false);
  assert.equal(isWebSocketOriginAllowed({ origin: 'null' }, []), false);
  assert.equal(isWebSocketOriginAllowed({ origin: 'http://192.0.2.10:5292' }, []), false);
  assert.equal(isWebSocketOriginAllowed({ 'sec-fetch-site': 'cross-site' }, []), false);

  const extra = parseAllowedHosts('pulsar.example.test');
  assert.equal(isRequestOriginAllowed('POST', { origin: 'https://pulsar.example.test' }, extra), true);
  assert.equal(isWebSocketOriginAllowed({ origin: 'https://pulsar.example.test' }, extra), true);
});

test('normalizeUpstreamUrl accepts http/https and rejects empty or non-http schemes', () => {
  assert.equal(normalizeUpstreamUrl('http://127.0.0.1:8080/'), 'http://127.0.0.1:8080');
  assert.equal(normalizeUpstreamUrl('https://metrics.example.com/sub///'), 'https://metrics.example.com/sub');
  assert.equal(normalizeUpstreamUrl(''), null);
  assert.equal(normalizeUpstreamUrl('   '), null);
  assert.equal(normalizeUpstreamUrl(null), null);
  assert.equal(normalizeUpstreamUrl('file:///etc/passwd'), null);
  assert.equal(normalizeUpstreamUrl('not a url'), null);
});

test('resolveTargetUrl builds safe upstream URLs and blocks path traversal', () => {
  const m = resolveTargetUrl('http://127.0.0.1:9000/dash', METRICS_PROXY_PREFIX, '/metrics-proxy/data.json?latest=1');
  assert.equal(m?.href, 'http://127.0.0.1:9000/dash/data.json?latest=1');

  const h = resolveTargetUrl('https://hub.example.com/pages', HUB_PROXY_PREFIX, '/hub-proxy/my-page/');
  assert.equal(h?.href, 'https://hub.example.com/pages/my-page/');

  const root = resolveTargetUrl('http://127.0.0.1:9000/dash', METRICS_PROXY_PREFIX, '/metrics-proxy');
  assert.equal(root?.href, 'http://127.0.0.1:9000/dash/');

  assert.equal(resolveTargetUrl('', METRICS_PROXY_PREFIX, '/metrics-proxy/data.json'), null);
  assert.equal(resolveTargetUrl('http://127.0.0.1:9000/dash', METRICS_PROXY_PREFIX, '/metrics-proxy/../secret'), null);
  assert.equal(resolveTargetUrl('http://127.0.0.1:9000/dash', METRICS_PROXY_PREFIX, '/metrics-proxy//evil.example/x'), null);
  assert.equal(resolveTargetUrl('http://127.0.0.1:9000/dash', METRICS_PROXY_PREFIX, '/other/path'), null);
});

test('daemonTargetUrl keeps requests on the daemon origin and rejects host-escape forms', () => {
  const daemon = new URL('http://127.0.0.1:8726');

  const ok = daemonTargetUrl(daemon, '/pulses?state=running&limit=10');
  assert.equal(ok?.href, 'http://127.0.0.1:8726/pulses?state=running&limit=10');
  assert.equal(daemonTargetUrl(daemon, '/')?.href, 'http://127.0.0.1:8726/');
  assert.equal(daemonTargetUrl(daemon, '?filter=active')?.href, 'http://127.0.0.1:8726/?filter=active');

  // Escape forms that `new URL(path, daemon)` would resolve to another host:
  assert.equal(daemonTargetUrl(daemon, '//other.host/x'), null);
  assert.equal(daemonTargetUrl(daemon, '/\\other.host/x'), null);
  assert.equal(daemonTargetUrl(daemon, 'http://other.host/x'), null);
  assert.equal(daemonTargetUrl(daemon, 'https://other.host/x'), null);
  assert.equal(daemonTargetUrl(daemon, '\\\\other.host/x'), null);
  assert.equal(daemonTargetUrl(daemon, '/\t/other.host/x'), null);
});

test('settingKeyFromPath extracts decoded setting keys', () => {
  assert.equal(settingKeyFromPath('/settings/metrics.url'), 'metrics.url');
  assert.equal(settingKeyFromPath('/settings/hub.url?x=1'), 'hub.url');
  assert.equal(settingKeyFromPath('/pulses'), null);
});

test('createSettingResolver caches for TTL, returns null when unset, and honors invalidate()', async () => {
  let clock = 1000;
  let calls = 0;
  let value = null;
  const resolver = createSettingResolver({
    ttlMs: 30_000,
    now: () => clock,
    fetchSetting: async () => {
      calls++;
      return value;
    }
  });

  // Unset -> null, cached within TTL
  assert.equal(await resolver.get('hub.url'), null);
  assert.equal(calls, 1);
  clock += 10_000;
  assert.equal(await resolver.get('hub.url'), null);
  assert.equal(calls, 1);

  // Explicit invalidate refreshes immediately
  value = 'http://127.0.0.1:8080/hub/';
  resolver.invalidate('hub.url');
  assert.equal(await resolver.get('hub.url'), 'http://127.0.0.1:8080/hub');
  assert.equal(calls, 2);

  // After TTL expires, re-fetches
  value = '';
  clock += 30_001;
  assert.equal(await resolver.get('hub.url'), null);
  assert.equal(calls, 3);
});
