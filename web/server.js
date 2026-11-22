/**
 * Production server for the Pulsar web UI (default 127.0.0.1:5292).
 *
 * Serves the adapter-node build in ./live (scripts/build-live.sh) and proxies:
 *   /api/*            -> the Pulsar daemon (PULSAR_DAEMON or 127.0.0.1:PULSAR_DAEMON_PORT,
 *                        default 8726), prefix stripped, WebSockets included
 *   /metrics-proxy/*  -> the configured `metrics.url` setting (404 when unset),
 *                        plus the latest-only data view
 *   /hub-proxy/*      -> the configured `hub.url` setting (404 when unset)
 *
 * Daemon JSON is gzipped on the way out to keep mobile page loads small.
 */
import http from 'node:http';
import https from 'node:https';
import net from 'node:net';
import zlib from 'node:zlib';
import { pipeline } from 'node:stream';
import { handler } from './live/handler.js';
import { stripMetricsHistory, wantsMetricsLatest, metricsWantsSpeed } from './src/lib/metricslatest.js';
import {
  daemonBaseUrl,
  daemonTargetUrl,
  parseAllowedHosts,
  isHostAllowed,
  isRequestOriginAllowed,
  isWebSocketOriginAllowed,
  resolveTargetUrl,
  settingKeyFromPath,
  createSettingResolver,
  requestUpstream,
  METRICS_PROXY_PREFIX,
  HUB_PROXY_PREFIX
} from './src/lib/upstream-proxy.js';

const PORT = Number(process.env.PORT || 5292);
const DAEMON_URL = new URL(daemonBaseUrl());
const EXTRA_ALLOWED_HOSTS = parseAllowedHosts();
const COMPRESSIBLE = /json|text\/|javascript|svg/;
const MIN_GZIP_BYTES = 1024;

// Idle sockets drop after 4 s, under uvicorn's 5 s keep-alive, so a reused
// socket is never one the daemon is closing.
const httpAgent = new http.Agent({ keepAlive: true, maxSockets: 128, timeout: 4000 });
const httpsAgent = new https.Agent({ keepAlive: true, maxSockets: 128, timeout: 4000 });

const upstreamSettings = createSettingResolver({ daemonUrl: DAEMON_URL.toString(), ttlMs: 30_000 });

function wantsGzip(req, type, encoding, length) {
  return (
    !encoding &&
    req.method !== 'HEAD' &&
    /\bgzip\b/.test(req.headers['accept-encoding'] || '') &&
    COMPRESSIBLE.test(type) &&
    !/event-stream|ndjson/.test(type) &&
    !(length !== undefined && Number(length) < MIN_GZIP_BYTES)
  );
}

function addVary(headers) {
  headers.vary = headers.vary ? `${headers.vary}, Accept-Encoding` : 'Accept-Encoding';
}

function sendJson(req, res, status, body) {
  const headers = { 'content-type': 'application/json', 'cache-control': 'no-store' };
  if (wantsGzip(req, headers['content-type'], undefined, Buffer.byteLength(body))) {
    headers['content-encoding'] = 'gzip';
    addVary(headers);
    res.writeHead(status, headers);
    res.end(zlib.gzipSync(body));
  } else {
    res.writeHead(status, headers);
    res.end(body);
  }
}

/** Forward one HTTP request to a target URL and stream the reply back. */
function proxyUrl(req, res, targetUrl) {
  const agent = targetUrl.protocol === 'https:' ? httpsAgent : httpAgent;
  const up = requestUpstream(
    targetUrl,
    {
      method: req.method,
      headers: { ...req.headers, host: targetUrl.host },
      agent
    },
    (r) => {
      const headers = { ...r.headers };
      const type = String(headers['content-type'] || '');
      if (wantsGzip(req, type, headers['content-encoding'], headers['content-length'])) {
        delete headers['content-length'];
        headers['content-encoding'] = 'gzip';
        addVary(headers);
        res.writeHead(r.statusCode, headers);
        pipeline(r, zlib.createGzip(), res, () => {});
      } else {
        res.writeHead(r.statusCode, r.statusMessage, headers);
        pipeline(r, res, () => {});
      }
    }
  );
  up.on('error', () => {
    if (!res.headersSent) sendJson(req, res, 502, '{"error":"upstream unreachable"}');
    else res.destroy();
  });
  // A client that goes away (closed tab, aborted fetch) must not leave the
  // upstream request open — long polls and streams would pile up.
  res.on('close', () => {
    if (!res.writableFinished) up.destroy();
  });
  pipeline(req, up, () => {});
}

// Parse metrics data.json once per upstream ETag rather than once per client poll.
let metricsMemo = null; // { target, etag, full, noSpeed }

async function serveMetricsLatest(req, res) {
  const baseUrl = await upstreamSettings.get('metrics.url');
  if (!baseUrl) {
    sendJson(req, res, 404, '{"error":"metrics not configured"}');
    return;
  }
  const target = resolveTargetUrl(baseUrl, METRICS_PROXY_PREFIX, `${METRICS_PROXY_PREFIX}/data.json`);
  if (!target) {
    sendJson(req, res, 404, '{"error":"metrics not configured"}');
    return;
  }
  const speed = metricsWantsSpeed(req.url);
  const memo = metricsMemo && metricsMemo.target === target.href ? metricsMemo : null;
  const headers = memo ? { 'if-none-match': memo.etag } : {};
  const agent = target.protocol === 'https:' ? httpsAgent : httpAgent;
  const up = requestUpstream(target, { method: 'GET', agent, headers }, (r) => {
    if (r.statusCode === 304 && memo) {
      r.resume();
      sendJson(req, res, 200, speed ? memo.full : memo.noSpeed);
      return;
    }
    const chunks = [];
    r.on('data', (c) => chunks.push(c));
    r.on('end', () => {
      try {
        const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
        const full = JSON.stringify(stripMetricsHistory(data));
        const noSpeed = JSON.stringify(stripMetricsHistory(data, { speed: false }));
        const etag = r.statusCode === 200 ? r.headers.etag : undefined;
        metricsMemo = etag ? { target: target.href, etag, full, noSpeed } : null;
        sendJson(req, res, 200, speed ? full : noSpeed);
      } catch {
        sendJson(req, res, 502, '{"error":"metrics latest strip failed"}');
      }
    });
  });
  up.on('error', () => sendJson(req, res, 502, '{"error":"metrics collector unreachable"}'));
  up.end();
}

async function proxyConfiguredUpstream(req, res, settingKey, prefix) {
  const baseUrl = await upstreamSettings.get(settingKey);
  if (!baseUrl) {
    sendJson(req, res, 404, `{"error":"${settingKey} not configured"}`);
    return;
  }
  const target = resolveTargetUrl(baseUrl, prefix, req.url || '/');
  if (!target) {
    sendJson(req, res, 400, '{"error":"invalid proxy path"}');
    return;
  }
  proxyUrl(req, res, target);
}

const server = http.createServer((req, res) => {
  if (!isHostAllowed(req.headers.host, EXTRA_ALLOWED_HOSTS)) {
    res.writeHead(403, { 'content-type': 'text/plain' });
    res.end('Blocked host');
    return;
  }
  if (!isRequestOriginAllowed(req.method, req.headers, EXTRA_ALLOWED_HOSTS)) {
    res.writeHead(403, { 'content-type': 'text/plain' });
    res.end('Blocked origin');
    return;
  }
  const url = req.url || '/';
  if (url === '/api' || url.startsWith('/api/') || url.startsWith('/api?')) {
    const daemonPath = url.slice(4) || '/';
    const target = daemonTargetUrl(DAEMON_URL, daemonPath);
    if (!target) {
      sendJson(req, res, 400, '{"error":"invalid api path"}');
      return;
    }
    if (req.method === 'PUT' || req.method === 'DELETE') {
      const key = settingKeyFromPath(target.pathname);
      if (key === 'metrics.url' || key === 'hub.url') {
        upstreamSettings.invalidate(key);
        if (key === 'metrics.url') metricsMemo = null;
      }
    }
    proxyUrl(req, res, target);
  } else if (wantsMetricsLatest(url)) {
    serveMetricsLatest(req, res);
  } else if (url === METRICS_PROXY_PREFIX || url.startsWith(`${METRICS_PROXY_PREFIX}/`) || url.startsWith(`${METRICS_PROXY_PREFIX}?`)) {
    proxyConfiguredUpstream(req, res, 'metrics.url', METRICS_PROXY_PREFIX);
  } else if (url === HUB_PROXY_PREFIX || url.startsWith(`${HUB_PROXY_PREFIX}/`) || url.startsWith(`${HUB_PROXY_PREFIX}?`)) {
    proxyConfiguredUpstream(req, res, 'hub.url', HUB_PROXY_PREFIX);
  } else {
    // Unhashed files (backgrounds, icons, manifest) revalidate on every load;
    // their ETags survive rebuilds (build-live.sh keeps source mtimes), so a
    // repeat costs a 304. _app/immutable keeps the adapter's year-long cache.
    if (!url.startsWith('/_app/immutable/')) res.setHeader('cache-control', 'no-cache');
    handler(req, res, () => {
      res.writeHead(404, { 'content-type': 'text/plain' });
      res.end('Not found');
    });
  }
});

// WebSocket: /api/live (pane state). Replay the
// upgrade request to the daemon, then splice the two sockets together.
server.on('upgrade', (req, socket, head) => {
  const url = req.url || '/';
  if (
    !isHostAllowed(req.headers.host, EXTRA_ALLOWED_HOSTS) ||
    !isWebSocketOriginAllowed(req.headers, EXTRA_ALLOWED_HOSTS)
  ) {
    socket.end(
      'HTTP/1.1 403 Forbidden\r\nContent-Type: text/plain\r\nConnection: close\r\nContent-Length: 9\r\n\r\nForbidden'
    );
    return;
  }
  if (!url.startsWith('/api/')) {
    socket.destroy();
    return;
  }
  const daemonHost = DAEMON_URL.hostname;
  const daemonPort = Number(DAEMON_URL.port || (DAEMON_URL.protocol === 'https:' ? 443 : 80));
  const up = net.connect(daemonPort, daemonHost, () => {
    const lines = [`${req.method} ${url.slice(4)} HTTP/${req.httpVersion}`];
    for (let i = 0; i < req.rawHeaders.length; i += 2) {
      const name = req.rawHeaders[i];
      lines.push(`${name}: ${name.toLowerCase() === 'host' ? DAEMON_URL.host : req.rawHeaders[i + 1]}`);
    }
    up.write(lines.join('\r\n') + '\r\n\r\n');
    if (head?.length) up.write(head);
    socket.pipe(up).pipe(socket);
  });
  const close = () => {
    socket.destroy();
    up.destroy();
  };
  up.on('error', close);
  up.on('close', close);
  socket.on('error', close);
  socket.on('close', close);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`pulsar-web: serving ./live on http://127.0.0.1:${PORT}`);
});
