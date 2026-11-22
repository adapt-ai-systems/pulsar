# Recommended Setup: Host on Tailscale & Install as an App

The recommended way to run Pulsar is on **one Linux host** published privately on your [Tailscale](https://tailscale.com/) tailnet with `tailscale serve`. Because Tailscale provides automatic HTTPS certificates inside your private tailnet, Pulsar becomes reachable from every machine on Tailscale and can be installed as a standalone **Chrome / home-screen app (PWA)** on **Mac, Windows, iPhone, and Android** — with zero open public ports.

Pulsar has **no built-in login or authentication**. Both `pulsar-daemon.service` (`127.0.0.1:8726`) and `pulsar-web.service` (`127.0.0.1:5292`) bind to loopback (`127.0.0.1`) only.

> **Security warning:** Never bind Pulsar to `0.0.0.0`, forward its ports on a router, or expose it to the public internet. Anyone who can reach Pulsar can run agent sessions and shell commands as your Linux user.

## 1. Install Tailscale

Install and sign in to Tailscale on the Linux host running Pulsar and on each laptop or phone you want to use:

```bash
# On the Linux host (see https://tailscale.com/download/linux for distro packages):
sudo tailscale up
```

Find your machine's MagicDNS hostname:

```bash
tailscale status
```

## 2. Enable HTTPS certificates on your tailnet

In the Tailscale admin console, enable **MagicDNS** and **HTTPS Certificates** for your tailnet. `tailscale serve` uses these to terminate TLS automatically.

## 3. Allow your Tailscale hostname in `pulsar-daemon.service` and `pulsar-web.service`

To block DNS-rebinding and cross-site browser requests, both the daemon and `web/server.js` allow only loopback hosts by default. Add your Tailscale MagicDNS hostname to `PULSAR_WEB_ALLOWED_HOSTS` in `~/.config/pulsar/pulsar.env` and restart both services:

```bash
echo "PULSAR_WEB_ALLOWED_HOSTS=<your-machine>.<your-tailnet>" >> "$HOME/.config/pulsar/pulsar.env"
systemctl --user restart pulsar-daemon.service pulsar-web.service
```

`PULSAR_WEB_ALLOWED_HOSTS` accepts a comma-separated list of exact hostnames, `.suffix` wildcards, or `*` (using `*` turns off the `Host` and `Origin` checks).

## 4. Publish Pulsar's web UI with `tailscale serve`

Keep `pulsar-web.service` bound to `127.0.0.1:5292` and let Tailscale Serve proxy HTTPS traffic to it:

```bash
tailscale serve --bg --https=5292 http://127.0.0.1:5292
```

(You can omit `--https=5292` to serve on the default HTTPS port 443 instead.)

Check the active mapping:

```bash
tailscale serve status
```

Open the printed HTTPS URL (`https://<your-machine>.<your-tailnet>:5292/`) from any device signed in to the same tailnet. WebSockets (`/api/live`), gzipped API responses, and configured `/hub-proxy/*` and `/metrics-proxy/*` routes work through `tailscale serve` automatically.

## 5. Install as an app on Mac, Windows, iPhone, and Android

Because `tailscale serve` serves Pulsar over valid HTTPS and the web UI includes a web app manifest, every major device can install it as a dedicated app window:

- **Mac & Windows (Chrome, Edge, or Brave):**
  1. Open your Tailscale HTTPS URL (`https://<your-machine>.<your-tailnet>:5292/`).
  2. Click the **Install** icon on the right side of the address bar (or open `⋮ → Cast, save, and share → Install page as app`).
  3. Launch it like a native desktop app from your Dock, Spotlight, or Taskbar.
- **iPhone & iPad (Safari):**
  1. Make sure Tailscale is connected on your iPhone/iPad.
  2. Open your Tailscale HTTPS URL in Safari.
  3. Tap **Share → Add to Home Screen → Add**.
  4. Launch from your Home Screen for a full-screen mobile swipe-deck app.
- **Android (Chrome):**
  1. Make sure Tailscale is connected on your Android device.
  2. Open your Tailscale HTTPS URL in Chrome.
  3. Tap `⋮ → Install app` (or **Add to Home screen**).

## 6. Keep services running on a headless machine

If you run Pulsar on a headless Linux box or VM without an active desktop login, enable systemd user lingering so `pulsar-daemon.service`, `pulsar-web.service`, and your pulses keep running after SSH logout:

```bash
loginctl enable-linger "$USER"
```

## 7. Stop remote serving

To turn off the Tailscale mapping while keeping Pulsar running on localhost:

```bash
tailscale serve --https=5292 off
```

## Web server environment variables

Configured in `web/server.js` and `web/src/lib/upstream-proxy.js` (set in `~/.config/pulsar/pulsar.env`):

- `PORT` — localhost listen port for `pulsar-web.service` (default `5292`)
- `PULSAR_DAEMON` — full daemon base URL (takes precedence over `PULSAR_DAEMON_PORT`)
- `PULSAR_DAEMON_PORT` — localhost daemon port when `PULSAR_DAEMON` is unset (default `8726`)
- `PULSAR_WEB_ALLOWED_HOSTS` — comma-separated extra hostnames allowed by both the daemon and `web/server.js` for `Host` and `Origin` checks (`*` turns these checks off)
- `PULSAR_UI_URL` — target URL for `web/probe/ui-probe.py` and `web/probe/inbox-probe.py` (default `http://127.0.0.1:5292/`)
