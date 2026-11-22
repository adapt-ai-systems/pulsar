# Metrics Bar (Optional)

The metrics bar is a one-line strip below Pulsar's top navigation that displays live host status and local-LLM inference throughput (decode tokens/sec, aggregate throughput, prefill rate, TTFT, prefix-cache hit rate, KV-cache usage, and active/queued request slots). Tap the strip to expand inline charts (`SpeedCharts.svelte`), or double-tap to open the full dashboard page in an overlay.

**Off until configured:** when `metrics.url` is unset, `MetricsBar.svelte` stays hidden and makes zero network requests, and `web/server.js` returns `404` on `/metrics-proxy/*`.

## Enable the metrics bar

`metrics.url` is the **base URL of your metrics dashboard** (not the `data.json` file URL). Pulsar's web server (`web/server.js`) proxies `/metrics-proxy/*` to `metrics.url`:

- The strip fetches `/metrics-proxy/data.json?latest=1` (proxied to `<metrics.url>/data.json`, with history arrays stripped server-side and cached by ETag).
- Double-tapping the strip frames `/metrics-proxy/` (proxied to `<metrics.url>/`) in an overlay.

Set `metrics.url` using either:

- The **web UI settings window** (click the sliders icon at the bottom of the left sidebar, then **settings…**), or
- The daemon HTTP API:

```bash
curl -s -X PUT http://127.0.0.1:8726/settings/metrics.url \
  -H 'Content-Type: application/json' \
  -d '"http://127.0.0.1:4444/metrics"'
```

`web/server.js` invalidates its cached `metrics.url` immediately on `PUT`/`DELETE /settings/metrics.url` through `/api`.

To turn the metrics bar off again, clear `metrics.url` in the settings window or delete it via the API:

```bash
curl -s -X DELETE http://127.0.0.1:8726/settings/metrics.url
```

## Optional live file-change push (`PULSAR_METRICS_DATA_PATH`)

By default, the metrics bar refreshes on a 15-second poll (or every 1 second while expanded). If your collector writes `data.json` atomically to local disk on the Pulsar host, you can point the daemon at that file so `/live` WebSocket clients receive immediate invalidation pings on every write:

```bash
echo "PULSAR_METRICS_DATA_PATH=/path/to/metrics/data.json" >> "$HOME/.config/pulsar/pulsar.env"
systemctl --user restart pulsar-daemon.service
```

When `PULSAR_METRICS_DATA_PATH` is unset (`pulsar/daemon/live.py`), no file is watched.

## Expected JSON payload (`<metrics.url>/data.json`)

`web/src/lib/MetricsBar.svelte` and `web/src/lib/metricslatest.js` read a JSON object of the shape `{hosts: [{id, name, latest: {online, ts, llm: {...}}, history: [...]}]}`:

```json
{
  "hosts": [
    {
      "id": "gpu-1",
      "name": "gpu-1",
      "latest": {
        "online": true,
        "ts": 1760000000,
        "llm": {
          "up": true,
          "role": "head",
          "model": "my-local-model",
          "backend": "vllm",
          "tp_size": 1,
          "decode_tps_live": 42.5,
          "gen_tps": 42.5,
          "prefill_tps_exp_60s": 1800,
          "ttft_s_60s": 0.4,
          "cache_hit_pct_60s": 78,
          "cache_tps": 3200,
          "kv_used_tokens": 24000,
          "kv_total_tokens": 262144,
          "slots_busy": 1,
          "slots_total": 8,
          "requests_deferred": 0,
          "metrics_stale": false
        }
      },
      "history": [
        {
          "t": 1760000000,
          "model": "my-local-model",
          "dtps": 42.5,
          "pfps": 1800,
          "tps": 42.5
        }
      ]
    }
  ]
}
```

Field notes from the frontend (`MetricsBar.svelte`, `speedstats.js`, `metricslatest.js`):

- A host is shown online when `latest.online` is `true` and `latest.ts` (Unix seconds) is less than 30 seconds old.
- A host serving multiple models on one box can publish `latest.llms` (array of LLM objects) instead of a single `latest.llm`.
- Multi-GPU tensor-parallel workers set `"role": "worker"` and `"paired_with": "<head-name>"`; the head chip aggregates their count (`tp_size`).
- Optional numeric metrics are omitted cleanly from the chip when absent or `null`.
- Collapsed mode requests `?latest=1&speed=0` so `web/server.js` drops `history` and `speed_history` on every poll; expanded mode requests `?latest=1` to include the last 2 minutes of `speed_history` points for the chart.
