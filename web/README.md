# Pulsar web

The SvelteKit/Svelte 5 interface for Pulsar. It is the single-user
surface for pulse grids, chat, schedules, usage, settings, and appearance.

## Deployed service

`pulsar-web.service` owns `127.0.0.1:5292` (configurable via `PORT`). It serves
a production build: `server.js` runs the adapter-node output in `live/`.

There is no hot reload. A UI change is live only after a rebuild/restart:

```bash
systemctl --user restart pulsar-web.service
curl -s http://127.0.0.1:5292/
```

- `scripts/build-live.sh` builds into `live-next/` and swaps it in only when
  the build succeeds. A failed build keeps the old copy live; the error is in
  `journalctl --user -u pulsar-web.service`. `live-prev/` is the copy before.
- Open tabs check for a new build every 60 s and reload when nobody is typing
  (`src/routes/+layout.svelte`).
- `pnpm build`, the test gate, writes `build/` and never touches `live/`.
- The unit runs `scripts/build-live.sh` as ExecStartPre, `node server.js` as
  ExecStart, and an ExecStartPost that waits until the server answers.

For an isolated local development environment, run `pnpm dev` on a different
port.

## Proxies

`server.js` keeps browser requests same-origin (`vite.config.js` does the same
for `pnpm dev`):

| Browser prefix | Backend |
|---|---|
| `/api` | Pulsar daemon (`PULSAR_DAEMON` or `127.0.0.1:PULSAR_DAEMON_PORT`, default `8726`), prefix stripped, gzipped |
| `/hub-proxy` | Optional pages hub configured via the `hub.url` setting (404 when unset) |
| `/metrics-proxy` | Optional metrics dashboard configured via the `metrics.url` setting (404 when unset) |

The `/api` proxy also carries the `/api/live` WebSocket.
`PULSAR_WEB_ALLOWED_HOSTS` (comma-separated hostnames, empty by default) extends
the allowed `Host` header list beyond `localhost` and bare IP addresses.

## Current UI

- Desktop multi-pane grid with named grid tabs and pane reordering
- Mobile swipe deck and overview/wall
- Global pulse drawer with search, archive, unarchive and delete
- Pulse creation, pause, restore, reset and model/effort settings
- Interval, once and daily schedules, including pulse-to-pulse targets
- Auto-handoff settings and manual handoff
- Skills and clean-memory diet controls
- Activity view and usage rail
- Appearance colors, background image, layout and text size
- Markdown chat rendering with expandable tool calls

## Data flow

The client is poll-based:

- grids/pulses refresh every few seconds;
- each visible pane requests its event tail;
- activity, schedules and usage refresh on their own intervals;
- messages and mutations use ordinary JSON or multipart requests.

`live.socket_state` and server-provided `busy_since` drive the
“thinking · Ns” state. Failed requests render in the affected surface rather
than becoming a silent blank.

`src/lib/markdown.js` is the rendering trust boundary. Keep its HTML escaping,
URL-scheme filtering and regression tests intact.

## Important files

```text
src/routes/+page.svelte       application shell and responsive surface selection
src/lib/ChatPane.svelte       pulse chat pane and composer
src/lib/MobileDeck.svelte     phone navigation
src/lib/Wall.svelte           overview/reordering surface
src/lib/SessionDrawer.svelte  global searchable pulse list
src/lib/SettingsModal.svelte  pulse settings and schedules
src/lib/api.js                daemon client and result envelopes
src/lib/models.js             model picker catalog and handoff defaults
src/lib/markdown.js           safe Markdown rendering
src/lib/render.js             normalized events to chat items
src/lib/theme.js              appearance palettes, tokens and background framing
server.js                     production server and backend proxies
vite.config.js                dev-server port and proxies
```

## Verification

```bash
cd web
node --test src/lib/*.test.mjs
npx svelte-check --threshold error
pnpm build
```

The build uses `adapter-node`; the deployed copy is `live/` (see above).
