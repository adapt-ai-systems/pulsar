<script>
  import { fmtTs, fmtTsFull } from './time.js';
  import { getEvents } from './api.js';
  import { decodeToolSnippet } from './render.js';
  // One dim plain-English mono line for a tool call.
  // Tap -> expand raw args/snippet under it. Truncated results fetch the
  // full body from GET /events?event_id= (wire-level trim only).
  let { item, elapsed = null, pulseId = null } = $props();
  let open = $state(false);
  let fullRaw = $state(null);

  async function toggle() {
    open = !open;
    if (!open || !item.truncated || !pulseId || item.id == null || fullRaw != null) return;
    const r = await getEvents(pulseId, 2000, null, { eventId: item.id });
    if (!r.ok) return;
    const tr = (r.value.events ?? []).find((e) => e.type === 'tool_result' && e.id === item.id);
    if (!tr) return;
    const snippet = decodeToolSnippet(tr.snippet ?? tr.result ?? tr.output ?? '');
    const raw = item.raw || '';
    const old = item.resultSnippet || '';
    fullRaw = old && raw.endsWith(old) ? raw.slice(0, raw.length - old.length) + snippet : snippet;
  }
</script>

<div
  class="tline"
  class:err={item.err}
  class:live={item.live}
  class:open
  onclick={toggle}
  role="button"
  tabindex="0"
  onkeydown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); toggle(); } }}
>
  <span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>{item.line}{#if elapsed !== null}<span class="progress"> · running {elapsed}s</span>{/if}{#if item.res}<span class="res"> {item.res}</span>{/if}
  {#if item.raw}<div class="raw">{fullRaw ?? item.raw}</div>{/if}
</div>

<style>
  .tline {
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-tool));
    color: var(--ink-meta);
    padding: 2.5px 14px 2.5px 24px;
    position: relative;
    cursor: pointer;
    line-height: 1.5;
    word-break: break-word;
  }
  .tline::before {
    content: "";
    position: absolute;
    left: 13px;
    top: 9px;
    width: 4px;
    height: 4px;
    border-radius: 50%;
    background: var(--ink-meta);
    opacity: 0.55;
  }
  .tline:hover { color: var(--ink-dim); }
  .tline .progress { color: var(--dreamer); }
  .tline .ts {
    color: var(--ink-meta);
    opacity: 0.75;
    margin-right: 6px;
    white-space: nowrap;
    font-size: 90%;
  }
  .tline .res { color: color-mix(in srgb, var(--ink-dim) 70%, var(--ink-meta)); }
  .tline.err { color: var(--bad); }
  .tline.err::before { background: var(--bad); opacity: 1; }
  .tline.live { color: var(--dreamer); }
  .tline.live::before {
    background: var(--dreamer);
    opacity: 1;
    animation: pulse 1.2s infinite;
  }
  .tline .raw {
    display: none;
    font-size: calc(10px * var(--tscale-tool));
    background: color-mix(in srgb, var(--ink-meta) 7%, var(--surface));
    border: 1px solid var(--border);
    border-radius: 6px;
    padding: 6px 8px;
    margin: 5px 0 6px;
    white-space: pre-wrap;
    word-break: break-word;
    color: var(--ink-dim);
    cursor: auto;
  }
  .tline.open .raw { display: block; }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(0.7); opacity: 0.6; }
  }
</style>
