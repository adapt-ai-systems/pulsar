/**
 * Shared upstream proxy helpers for web/server.js (production) and
 * web/vite.config.js (dev).
 *
 * Resolves optional upstream base URLs (`hub.url`, `metrics.url`) from the
 * Pulsar daemon's /settings/{key} HTTP API with a short TTL cache. Unset or
 * invalid values resolve to null so proxy paths answer 404 without making
 * upstream calls.
 */
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';

export const DEFAULT_DAEMON_PORT = 8726;
export const DEFAULT_DAEMON_URL = `http://127.0.0.1:${DEFAULT_DAEMON_PORT}`;
export const METRICS_PROXY_PREFIX = '/metrics-proxy';
export const HUB_PROXY_PREFIX = '/hub-proxy';

/**
 * Base URL of the Pulsar daemon.
 * Precedence: PULSAR_DAEMON (full URL) > PULSAR_DAEMON_PORT (default 8726 on 127.0.0.1).
 */
export function daemonBaseUrl(env = process.env) {
  const raw = String(env.PULSAR_DAEMON || '').trim().replace(/\/+$/, '');
  if (raw) return raw;
  const portNum = Number(env.PULSAR_DAEMON_PORT);
  const port = Number.isInteger(portNum) && portNum > 0 && portNum <= 65535 ? portNum : DEFAULT_DAEMON_PORT;
  return `http://127.0.0.1:${port}`;
}

/**
 * Parse comma-separated hostnames from PULSAR_WEB_ALLOWED_HOSTS.
 * Empty = only localhost and bare IP addresses are allowed.
 */
export function parseAllowedHosts(raw = process.env.PULSAR_WEB_ALLOWED_HOSTS) {
  return String(raw || '')
    .split(',')
    .map((h) => h.trim().toLowerCase())
    .filter(Boolean);
}

/**
 * True if the HTTP Host header is allowed (localhost, bare IP, or listed in
 * PULSAR_WEB_ALLOWED_HOSTS). Supports '*' (any) and '.suffix' domain entries.
 */
export function isHostAllowed(host, extraHosts = []) {
  const name = String(host || '')
    .replace(/:\d+$/, '')
    .replace(/^\[|\]$/g, '')
    .toLowerCase();
  if (!name) return false;
  if (name === 'localhost' || net.isIP(name) !== 0) return true;
  for (const allowed of extraHosts) {
    if (allowed === '*') return true;
    if (allowed.startsWith('.') && name.endsWith(allowed)) return true;
    if (name === allowed) return true;
  }
  return false;
}

const LOOPBACK_ORIGIN_HOSTS = new Set(['localhost', '127.0.0.1', '::1']);
const SAFE_HTTP_METHODS = new Set(['GET', 'HEAD', 'OPTIONS']);

/**
 * True if an HTTP Origin header value belongs to an allowed host
 * (localhost, 127.0.0.1, [::1], or an entry in PULSAR_WEB_ALLOWED_HOSTS).
 * Arbitrary bare IP addresses are NOT allowed here.
 */
export function isOriginAllowed(origin, extraHosts = []) {
  if (typeof origin !== 'string') return false;
  const trimmed = origin.trim();
  if (!trimmed) return false;
  let u;
  try {
    u = new URL(trimmed);
  } catch {
    return false;
  }
  if (u.protocol !== 'http:' && u.protocol !== 'https:') return false;
  if (u.username || u.password || u.search || u.hash) return false;
  if (u.pathname !== '/' || trimmed.endsWith('/')) return false;
  const name = u.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (!name) return false;
  if (LOOPBACK_ORIGIN_HOSTS.has(name)) return true;
  for (const allowed of extraHosts) {
    const clean = String(allowed || '')
      .trim()
      .toLowerCase()
      .replace(/^\[|\]$/g, '');
    if (!clean) continue;
    if (clean === '*') return true;
    if (clean.startsWith('.') && name.endsWith(clean)) return true;
    if (name === clean) return true;
  }
  return false;
}

/** True if the Sec-Fetch-Site header marks the request as cross-site. */
export function isCrossSiteFetch(secFetchSite) {
  if (Array.isArray(secFetchSite)) {
    return secFetchSite.some((v) => isCrossSiteFetch(v));
  }
  return String(secFetchSite || '').trim().toLowerCase() === 'cross-site';
}

/**
 * Validate Origin and Sec-Fetch-Site headers for an HTTP request.
 * Safe methods (GET, HEAD, OPTIONS) pass. Requests without Origin and
 * without Sec-Fetch-Site pass.
 */
export function isRequestOriginAllowed(method, headers = {}, extraHosts = []) {
  if (SAFE_HTTP_METHODS.has(String(method || 'GET').toUpperCase())) return true;
  if (isCrossSiteFetch(headers['sec-fetch-site'])) return false;
  if (headers.origin !== undefined) {
    return isOriginAllowed(headers.origin, extraHosts);
  }
  return true;
}

/**
 * Validate Origin and Sec-Fetch-Site headers for a WebSocket upgrade.
 * Requests without Origin and without Sec-Fetch-Site pass.
 */
export function isWebSocketOriginAllowed(headers = {}, extraHosts = []) {
  if (isCrossSiteFetch(headers['sec-fetch-site'])) return false;
  if (headers.origin !== undefined) {
    return isOriginAllowed(headers.origin, extraHosts);
  }
  return true;
}

/** Normalize an http/https base URL string, or return null if unset/invalid. */
export function normalizeUpstreamUrl(raw) {
  if (typeof raw !== 'string') return null;
  const trimmed = raw.trim().replace(/\/+$/, '');
  if (!trimmed) return null;
  try {
    const u = new URL(trimmed);
    if (u.protocol !== 'http:' && u.protocol !== 'https:') return null;
    return u.toString().replace(/\/+$/, '');
  } catch {
    return null;
  }
}

/**
 * Resolve an incoming proxy request URL (e.g. '/metrics-proxy/data.json?a=1')
 * against a configured base URL (e.g. 'http://127.0.0.1:9000/metrics').
 * Returns null if the request would escape the base URL's origin or path.
 */
export function resolveTargetUrl(baseUrl, prefix, reqUrl) {
  const cleanBase = normalizeUpstreamUrl(baseUrl);
  if (!cleanBase) return null;
  const raw = String(reqUrl || '/');
  if (raw !== prefix && !raw.startsWith(`${prefix}/`) && !raw.startsWith(`${prefix}?`)) {
    return null;
  }
  const rest = raw.slice(prefix.length);
  const qIdx = rest.indexOf('?');
  const rawPath = qIdx < 0 ? rest : rest.slice(0, qIdx);
  const query = qIdx < 0 ? '' : rest.slice(qIdx);
  if (rawPath.startsWith('//')) return null;
  const relPath = rawPath.replace(/^\/+/, '');
  const baseDir = new URL(`${cleanBase}/`);
  let target;
  try {
    target = new URL(relPath + query, baseDir);
  } catch {
    return null;
  }
  if (target.origin !== baseDir.origin || !target.pathname.startsWith(baseDir.pathname)) {
    return null;
  }
  return target;
}

/**
 * Build a daemon target URL anchored strictly to `daemonUrl`'s origin so a
 * crafted `/api/...` request path (e.g. `//other.host/x`, `/\other.host/x`,
 * or an absolute URL) can never redirect the proxy off the daemon host.
 * Returns null if the path is invalid or would resolve to a different origin.
 * @param {string | URL} daemonUrl
 * @param {string} path
 * @returns {URL | null}
 */
export function daemonTargetUrl(daemonUrl, path) {
  let base;
  try {
    base = daemonUrl instanceof URL ? daemonUrl : new URL(String(daemonUrl));
  } catch {
    return null;
  }
  if (base.protocol !== 'http:' && base.protocol !== 'https:') return null;
  if (typeof path !== 'string' || !path) return null;
  if (/[\r\n\t]/.test(path)) return null;
  if (!path.startsWith('/') && !path.startsWith('?')) return null;
  if (path.startsWith('//') || path.startsWith('/\\')) return null;
  const normalizedPath = path.startsWith('?') ? `/${path}` : path;
  let target;
  let relativeCheck;
  try {
    target = new URL(`${base.origin}${normalizedPath}`);
    relativeCheck = new URL(normalizedPath, base.origin);
  } catch {
    return null;
  }
  if (target.origin !== base.origin || relativeCheck.origin !== base.origin) {
    return null;
  }
  return target;
}

/**
 * Extract the setting key from a daemon API path like '/settings/metrics.url',
 * or null if the path is not a settings write.
 */
export function settingKeyFromPath(path) {
  const clean = String(path || '').split('?')[0];
  if (!clean.startsWith('/settings/')) return null;
  try {
    return decodeURIComponent(clean.slice('/settings/'.length));
  } catch {
    return null;
  }
}

/**
 * Fetch a single JSON setting value from the daemon over HTTP/HTTPS.
 * @param {string} daemonUrl
 * @param {string} key
 * @param {number} [timeoutMs]
 * @returns {Promise<any>}
 */
export function fetchDaemonSetting(daemonUrl, key, timeoutMs = 2000) {
  return new Promise((resolve) => {
    let url;
    try {
      url = new URL(`${daemonUrl.replace(/\/+$/, '')}/settings/${encodeURIComponent(key)}`);
    } catch {
      resolve(null);
      return;
    }
    const mod = url.protocol === 'https:' ? https : http;
    const req = mod.get(url, { timeout: timeoutMs }, (res) => {
      if (res.statusCode !== 200) {
        res.resume();
        resolve(null);
        return;
      }
      const chunks = [];
      res.on('data', (c) => chunks.push(c));
      res.on('end', () => {
        try {
          const body = JSON.parse(Buffer.concat(chunks).toString('utf8'));
          resolve(body?.ok ? body.value : null);
        } catch {
          resolve(null);
        }
      });
    });
    req.on('timeout', () => {
      req.destroy();
      resolve(null);
    });
    req.on('error', () => resolve(null));
  });
}

/**
 * Cached resolver for daemon-backed upstream URL settings (`hub.url`, `metrics.url`).
 * @param {{ daemonUrl?: string, ttlMs?: number, fetchSetting?: (key: string) => Promise<any>, now?: () => number }} [opts]
 */
export function createSettingResolver({
  daemonUrl = daemonBaseUrl(),
  ttlMs = 30_000,
  fetchSetting = (key) => fetchDaemonSetting(daemonUrl, key),
  now = () => Date.now()
} = {}) {
  /** @type {Map<string, { value: string | null, expiresAt: number }>} */
  const cache = new Map();

  return {
    async get(key) {
      const hit = cache.get(key);
      if (hit && hit.expiresAt > now()) return hit.value;
      const raw = await fetchSetting(key);
      const value = normalizeUpstreamUrl(raw);
      cache.set(key, { value, expiresAt: now() + ttlMs });
      return value;
    },
    invalidate(key) {
      if (key) cache.delete(key);
      else cache.clear();
    }
  };
}

/** Open an http/https request to a URL object. */
export function requestUpstream(targetUrl, options, onRes) {
  const mod = targetUrl.protocol === 'https:' ? https : http;
  return mod.request(targetUrl, options, onRes);
}
