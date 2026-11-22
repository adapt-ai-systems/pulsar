<script>
  /**
   * One message injected by ANOTHER pulse, a watcher or Pulsar itself
   * (render.js strips the daemon's inbox prefix). Credited to the sender in
   * --peer so it never reads as something the user typed. The ONE copy:
   * ChatPane renders it inline and FoldRow renders it inside an expanded fold,
   * so a folded row looks identical to an inline row.
   * The shared `.msg` base, label and timestamp CSS lives in ChatPane.
   */
  import { fmtTs, fmtTsFull } from './time.js';

  let { item } = $props();
</script>

<div class="msg peer">
  <div class="lbl">
    <span class="peer-name" title={item.fromId ? `pulse ${item.fromId}` : item.from === 'pulsar' ? 'sent by Pulsar, not you' : 'daemon watch'}>{item.from}</span>
    {#if item.ts}<span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>{/if}
  </div>
  {#if item.summary}
    <details class="body sys-note">
      <summary>{item.summary}</summary>
      <div class="sys-text">{item.text}</div>
    </details>
  {:else if item.text}
    <div class="body">{item.text}</div>
  {/if}
</div>

<style>
  /* A message from another pulse — sender's title as the label, washed in the
     dedicated --peer token (Appearance → "Other pulses") so it is visually
     distinct from messages sent by the user. */
  .msg.peer { background: color-mix(in srgb, var(--bub-peer) var(--bub-peer-fill), transparent); }
  .msg.peer .lbl { color: var(--peer); }
  .msg.peer .peer-name { text-transform: uppercase; }
  .msg.peer .body { font-family: var(--font-sans); font-size: calc(12px * var(--tscale-chat)); color: var(--ink); white-space: pre-wrap; }
  .sys-note summary { cursor: pointer; color: var(--muted, #8b95a8); }
  .sys-note .sys-text { white-space: pre-wrap; margin-top: 6px; font-size: 0.9em; max-height: 20em; overflow: auto; }
</style>
