# Pages Hub (Optional)

The Pages Hub lets you browse static HTML pages published by your pulses (reports, dashboards, interactive artifacts) inside Pulsar's main pane and pin favorite pages as tabs in the sidebar.

**Off until configured:** when `hub.url` is unset, the Hub button and sidebar pins are hidden, the web UI makes zero hub requests, and `web/server.js` returns `404` on `/hub-proxy/*`. When `PULSAR_HUB_PAGES_DIR` is unset, daemon page listing (`GET /hub/pages`) is disabled and returns an empty list.

## How it works

Two small pieces work together:

1. **A directory of static pages** on disk, one subdirectory (or symlink to a directory) per slug:
   ```text
   <pages-dir>/
     my-report/
       index.html
     build-dashboard/
       index.html
   ```
   Both the daemon (`pulsar/daemon/hubpages.py`) and pulse context generation (`pulsar/launch/context.py`) read `PULSAR_HUB_PAGES_DIR`. The daemon exposes:
   - `GET /hub/pages` — catalog sorted newest-first, reading each page's `<title>`
   - `GET /hub/pins` — shared sidebar pins (stored in `app_settings` under `hub.pins`)
   - `POST /hub/pins` — `{"slug": "my-report", "title": "..."}` to pin a page
   - `DELETE /hub/pins/{slug}` — unpin a page

2. **A static HTTP server** (for example, `nginx` bound to localhost) serving `<pages-dir>`. Pulsar's web server (`web/server.js`) proxies `/hub-proxy/*` same-origin to your configured `hub.url` setting so the Hub UI can frame pages without cross-origin or extra-port issues.

## Setup

### 1. Create a pages directory and configure `PULSAR_HUB_PAGES_DIR`

```bash
mkdir -p "$HOME/.local/share/pulsar-pages"
echo "PULSAR_HUB_PAGES_DIR=$HOME/.local/share/pulsar-pages" >> "$HOME/.config/pulsar/pulsar.env"
systemctl --user restart pulsar-daemon.service
```

### 2. Serve the pages directory locally

Any static HTTP server works. Example minimal `nginx` server block bound to localhost on port `4444`:

```nginx
server {
    listen 127.0.0.1:4444;
    root /path/to/pulsar-pages;
    index index.html;

    location / {
        try_files $uri $uri/ =404;
    }
}
```

### 3. Enable the Hub in Pulsar settings

Set `hub.url` to the base URL of your pages server (no trailing slash) using either:

- The **web UI settings window** (click the sliders icon at the bottom of the left sidebar, then **settings…**), or
- The daemon HTTP API:

```bash
curl -s -X PUT http://127.0.0.1:8726/settings/hub.url \
  -H 'Content-Type: application/json' \
  -d '"http://127.0.0.1:4444"'
```

`web/server.js` invalidates its cached `hub.url` immediately when `PUT` or `DELETE /settings/hub.url` goes through `/api`, so the Hub takes effect without restarting `pulsar-web.service`.

To turn the Hub back off, clear `hub.url` in the settings window or delete it via the API:

```bash
curl -s -X DELETE http://127.0.0.1:8726/settings/hub.url
```
