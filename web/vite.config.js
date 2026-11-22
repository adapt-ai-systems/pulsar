import { sveltekit } from '@sveltejs/kit/vite';
import { defineConfig } from 'vite';
import { pipeline } from 'node:stream';
import { stripMetricsHistory, wantsMetricsLatest, metricsWantsSpeed } from './src/lib/metricslatest.js';
import {
  daemonBaseUrl,
  parseAllowedHosts,
  resolveTargetUrl,
  settingKeyFromPath,
  createSettingResolver,
  requestUpstream,
  METRICS_PROXY_PREFIX,
  HUB_PROXY_PREFIX
} from './src/lib/upstream-proxy.js';

const DAEMON_TARGET = daemonBaseUrl();
const EXTRA_ALLOWED_HOSTS = parseAllowedHosts();

/**
 * Dev-server middleware for optional /metrics-proxy/* and /hub-proxy/* routes,
 * resolving upstream URLs from the daemon's `metrics.url` and `hub.url` settings.
 * Unset = 404. Mirrors web/server.js.
 */
function optionalProxiesPlugin() {
  const resolver = createSettingResolver({ daemonUrl: DAEMON_TARGET, ttlMs: 30_000 });
  return {
    name: 'pulsar-optional-proxies',
    configureServer(server) {
      server.middlewares.use(async (req, res, next) => {
        const url = req.url || '/';
        if ((req.method === 'PUT' || req.method === 'DELETE') && url.startsWith('/api/settings/')) {
          const key = settingKeyFromPath(url.slice(4));
          if (key === 'metrics.url' || key === 'hub.url') resolver.invalidate(key);
          return next();
        }
        if (wantsMetricsLatest(url)) {
          const baseUrl = await resolver.get('metrics.url');
          const target = baseUrl ? resolveTargetUrl(baseUrl, METRICS_PROXY_PREFIX, `${METRICS_PROXY_PREFIX}/data.json`) : null;
          if (!target) {
            res.statusCode = 404;
            res.setHeader('Content-Type', 'application/json');
            res.end('{"error":"metrics not configured"}');
            return;
          }
          const speed = metricsWantsSpeed(url);
          const up = requestUpstream(target, { method: 'GET' }, (r) => {
            const chunks = [];
            r.on('data', (c) => chunks.push(c));
            r.on('end', () => {
              try {
                const data = JSON.parse(Buffer.concat(chunks).toString('utf8'));
                const body = JSON.stringify(stripMetricsHistory(data, { speed }));
                res.statusCode = 200;
                res.setHeader('Content-Type', 'application/json');
                res.setHeader('Cache-Control', 'no-store');
                res.end(body);
              } catch {
                res.statusCode = 502;
                res.setHeader('Content-Type', 'application/json');
                res.end('{"error":"metrics latest strip failed"}');
              }
            });
          });
          up.on('error', () => {
            res.statusCode = 502;
            res.setHeader('Content-Type', 'application/json');
            res.end('{"error":"metrics collector unreachable"}');
          });
          up.end();
          return;
        }
        const match =
          url === METRICS_PROXY_PREFIX || url.startsWith(`${METRICS_PROXY_PREFIX}/`) || url.startsWith(`${METRICS_PROXY_PREFIX}?`)
            ? { key: 'metrics.url', prefix: METRICS_PROXY_PREFIX }
            : url === HUB_PROXY_PREFIX || url.startsWith(`${HUB_PROXY_PREFIX}/`) || url.startsWith(`${HUB_PROXY_PREFIX}?`)
              ? { key: 'hub.url', prefix: HUB_PROXY_PREFIX }
              : null;
        if (!match) return next();
        const baseUrl = await resolver.get(match.key);
        const target = baseUrl ? resolveTargetUrl(baseUrl, match.prefix, url) : null;
        if (!target) {
          res.statusCode = 404;
          res.setHeader('Content-Type', 'application/json');
          res.end(`{"error":"${match.key} not configured"}`);
          return;
        }
        const up = requestUpstream(
          target,
          { method: req.method, headers: { ...req.headers, host: target.host } },
          (r) => {
            res.writeHead(r.statusCode || 502, r.statusMessage, r.headers);
            pipeline(r, res, () => {});
          }
        );
        up.on('error', () => {
          if (!res.headersSent) {
            res.statusCode = 502;
            res.setHeader('Content-Type', 'application/json');
            res.end('{"error":"upstream unreachable"}');
          } else {
            res.destroy();
          }
        });
        pipeline(req, up, () => {});
      });
    }
  };
}

export default defineConfig({
  plugins: [optionalProxiesPlugin(), sveltekit()],
  server: {
    host: '127.0.0.1',
    port: Number(process.env.PORT || 5292),
    // Fail loudly if the port is already taken instead of silently moving to another port.
    strictPort: true,
    ...(EXTRA_ALLOWED_HOSTS.length
      ? { allowedHosts: EXTRA_ALLOWED_HOSTS.includes('*') ? true : EXTRA_ALLOWED_HOSTS }
      : {}),
    // Proxy daemon API through the dev server to avoid CORS and keep the
    // client module talking to a same-origin /api prefix.
    proxy: {
      '/api': {
        target: DAEMON_TARGET,
        changeOrigin: true,
        // ws: /api/live is a WebSocket.
        ws: true,
        rewrite: (p) => p.replace(/^\/api/, '')
      }
    }
  }
});
