<script>
  import { getEvents } from './api.js';
  import { fmtClock, youHistoryRows } from './render.js';
  import { fmtTs, fmtTsFull, agoLabel, parseTs } from './time.js';
  import Icon from './Icon.svelte';

  /**
   * The pinned "YOU SAID" strip and the sent-message history overlay it opens.
   * The strip is a fragment sibling of the pane's flex column and the overlay is
   * absolutely positioned against the pane, so this renders no wrapper element.
   * The pane owns which message is "last" (it scans the event window) and
   * whether its own row is on screen; this owns the strip's turn clock, the tap
   * rules, and the history fetch.
   *
   * @type {{
   *   lastYou: ({text: string, ts: string|null, attachments: any[], doneTs: string|null})|null,
   *   inView: boolean, pulseId: string, now: number,
   *   onjump: (row: {ts: string|null}) => void
   * }}
   */
  let { lastYou, inView, pulseId, now, onjump } = $props();

  // Turn clock for the strip: send → done, ticking on `now` while unfinished.
  const lastYouTook = $derived.by(() => {
    if (!lastYou?.ts) return NaN;
    const end = lastYou.doneTs ? parseTs(lastYou.doneTs) : now;
    return end - parseTs(lastYou.ts);
  });
  // Single tap scrolls the log to that message; a double tap opens the full
  // sent-message history instead. The jump is deferred past the double-tap
  // window so the first tap of a double doesn't fire it.
  let lastYouTapTimer = null;
  function onLastYouClick() {
    clearTimeout(lastYouTapTimer);
    lastYouTapTimer = setTimeout(() => jumpTo(lastYou), 260);
  }
  function onLastYouDbl() {
    clearTimeout(lastYouTapTimer);
    openYouHistory();
  }

  // The history overlay: every message the user sent to this pulse, newest first,
  // each with when, how long ago, attachments, and what followed it (tool-call
  // count and the first reply's opening). Fetched fresh at the daemon's tail
  // cap rather than from the pane's loaded window, so it reaches well past
  // what's currently scrolled in.
  let youHistory = $state(null); // null = closed; {rows, truncated, loading}
  function closeHistory() { youHistory = null; }
  $effect(() => {
    if (!youHistory) return;
    const onKey = (e) => {
      if (e.key === 'Escape') {
        e.preventDefault();
        closeHistory();
      }
    };
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  });
  // Tap a row (or the strip) → close the overlay and let the pane scroll the
  // log to that bubble.
  function jumpTo(row) {
    youHistory = null;
    onjump(row);
  }
  // "how long the turn took" — send to idle. NaN = never finished (still
  // running, or killed without a done event).
  const fmtTook = fmtClock;
  // agoLabel tops out at hours (built for the status rail); history rows can
  // be days old.
  function histAgo(ts) {
    const ms = now - parseTs(ts);
    if (!(ms >= 0)) return '';
    const days = Math.floor(ms / 86_400_000);
    return days >= 1 ? `${days}d ago` : `${agoLabel(parseTs(ts), now)} ago`;
  }
  async function openYouHistory() {
    youHistory = { rows: [], truncated: false, loading: true };
    const r = await getEvents(pulseId, 2000);
    if (!youHistory) return; // closed while loading
    if (!r.ok) {
      youHistory = { rows: [], truncated: false, loading: false, error: r.error || 'load failed' };
      return;
    }
    const evs = r.value?.events ?? [];
    youHistory = { rows: youHistoryRows(evs), truncated: evs.length >= 2000, loading: false };
  }
</script>

  <!-- Hidden while the row it mirrors is on screen — see trackYouRow. -->
  {#if lastYou && !inView}
    <button
      type="button"
      class="last-you"
      onclick={onLastYouClick}
      ondblclick={onLastYouDbl}
      title="tap: scroll to this message · double-tap: history"
    >
      <span class="last-you-lbl">YOU SAID{#if lastYou.ts} · {fmtTs(lastYou.ts)}{/if}</span>
      {#if fmtTook(lastYouTook)}<span class="last-you-took" class:running={!lastYou.doneTs} title={lastYou.doneTs ? 'turn took (send → idle)' : 'turn running (send → now)'}>⏱ {fmtTook(lastYouTook)}{#if !lastYou.doneTs}…{/if}</span>{/if}
      <span class="last-you-text">{lastYou.text}</span>
    </button>
  {/if}
  {#if youHistory}
    <div class="you-hist">
      <div class="you-hist-head">
        <span>YOUR MESSAGES{#if !youHistory.loading} · {youHistory.rows.length}{/if}</span>
        <button type="button" class="you-hist-close" aria-label="close history" onclick={closeHistory}>✕</button>
      </div>
      <div class="you-hist-list">
        {#if youHistory.loading}
          <div class="you-hist-note">loading…</div>
        {:else if youHistory.error}
          <div class="you-hist-note">⚠ {youHistory.error}</div>
        {:else if !youHistory.rows.length}
          <div class="you-hist-note">nothing sent yet</div>
        {/if}
        {#each youHistory.rows as row (row.n)}
          <div
            class="you-hist-row"
            role="button"
            tabindex="0"
            onclick={(e) => { if (!e.target.closest('a')) jumpTo(row); }}
            onkeydown={(e) => {
              if ((e.key === 'Enter' || e.key === ' ') && !e.target.closest('a')) {
                e.preventDefault();
                jumpTo(row);
              }
            }}
            title="tap to jump to this message"
          >
            <div class="you-hist-meta">
              <span class="you-hist-n">#{row.n}</span>
              <span title={fmtTsFull(row.ts)}>{fmtTsFull(row.ts)}</span>
              {#if row.ts}<span class="you-hist-ago">{histAgo(row.ts)}{#if fmtTook(row.took)} · <span class="you-hist-took" title="send → idle">⏱ {fmtTook(row.took)}</span>{/if}</span>{/if}
            </div>
            {#if row.text}<div class="you-hist-text">{row.text}</div>{/if}
            {#if row.attachments.length}
              <div class="you-hist-att">
                {#each row.attachments as a (a.url ?? a.filename)}
                  <a class="att-chip" href={a.url ? '/api' + a.url : undefined} target="_blank" rel="noreferrer"><Icon name="clip" size={11} /> {a.filename}</a>
                {/each}
              </div>
            {/if}
            <div class="you-hist-after">
              {row.tools} tool call{row.tools === 1 ? '' : 's'}{#if row.reply} · <span class="you-hist-reply">{row.reply}</span>{:else} · no reply yet{/if}
            </div>
          </div>
        {/each}
        {#if youHistory.truncated}
          <div class="you-hist-note">older messages beyond the last 2000 events aren't listed</div>
        {/if}
      </div>
    </div>
  {/if}

<style>
  /* Pinned "last thing I said" strip. Clamped to three lines; tap jumps to the
     message in the log, double-tap opens sent-message history. */
  .last-you {
    all: unset;
    box-sizing: border-box;
    display: block;
    width: 100%;
    flex-shrink: 0;
    cursor: pointer;
    position: relative;
    z-index: 2;
    padding: 7px 12px 8px 14px;
    /* A clearly tinted band, not a bubble: solid accent wash, thick accent
       edge, and a drop shadow onto the log below so it reads as chrome pinned
       over the chat instead of blending in. */
    background: color-mix(in srgb, var(--user) 18%, var(--surface));
    border-top: 1px solid color-mix(in srgb, var(--user) 45%, var(--border));
    border-bottom: 1px solid color-mix(in srgb, var(--user) 70%, var(--border));
    border-left: 5px solid var(--user);
    box-shadow: 0 4px 12px rgb(0 0 0 / 0.35);
  }
  .last-you:hover { background: color-mix(in srgb, var(--user) 26%, var(--surface)); }
  .last-you:focus-visible { outline: 1px solid var(--user); outline-offset: -1px; }
  .last-you-lbl {
    display: inline-block;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    font-weight: 700;
    letter-spacing: 0.12em;
    color: var(--bg);
    background: var(--user);
    padding: 1px 6px;
    border-radius: 999px;
    margin-bottom: 4px;
  }
  .last-you-took {
    display: inline-block;
    margin-left: 6px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    font-weight: 700;
    letter-spacing: 0.06em;
    color: var(--user);
    vertical-align: top;
    margin-top: 1px;
  }
  .last-you-took.running { color: var(--ink-dim); }
  .last-you-text {
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 3;
    overflow: hidden;
    font-family: var(--font-sans);
    font-size: calc(12px * var(--tscale-chat));
    font-weight: 500;
    line-height: 1.35;
    color: var(--ink);
    white-space: pre-wrap;
    word-break: break-word;
  }
  /* Sent-message history — covers the pane (which is position:relative and
     clips overflow), so it never has to fight the grid's z-order. */
  .you-hist {
    position: absolute;
    inset: 0;
    z-index: 40;
    display: flex;
    flex-direction: column;
    background: var(--surface);
  }
  .you-hist-head {
    display: flex;
    align-items: center;
    justify-content: space-between;
    padding: 8px 12px;
    border-bottom: 1px solid var(--border-strong);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.15em;
    color: var(--user);
    flex-shrink: 0;
  }
  .you-hist-close {
    all: unset;
    cursor: pointer;
    padding: 2px 8px;
    border-radius: 5px;
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
  }
  .you-hist-close:hover { background: var(--surface-warm); color: var(--ink); }
  .you-hist-list { flex: 1; min-height: 0; overflow-y: auto; overflow-x: hidden; }
  .you-hist-note {
    padding: 10px 12px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-meta);
  }
  .you-hist-row {
    padding: 8px 12px 9px;
    border-bottom: 1px solid var(--border);
    border-left: 3px solid color-mix(in srgb, var(--user) 60%, transparent);
  }
  .you-hist-row { cursor: pointer; }
  .you-hist-row:hover { background: color-mix(in srgb, var(--user) 8%, transparent); }
  .you-hist-row:focus-visible { outline: 1px solid var(--user); outline-offset: -1px; }
  .you-hist-meta {
    display: flex;
    gap: 8px;
    align-items: baseline;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    margin-bottom: 3px;
  }
  .you-hist-n { color: var(--user); }
  .you-hist-took { color: var(--ink-dim); }
  .you-hist-ago { margin-left: auto; }
  .you-hist-text {
    font-family: var(--font-sans);
    font-size: calc(12px * var(--tscale-chat));
    font-weight: 500;
    line-height: 1.35;
    color: var(--ink);
    white-space: pre-wrap;
    word-break: break-word;
  }
  .you-hist-att { display: flex; flex-wrap: wrap; gap: 4px; margin-top: 4px; }
  .you-hist-after {
    margin-top: 4px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .you-hist-reply { color: var(--ink-dim); }
  .att-chip {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    padding: 2px 7px;
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    background: var(--surface-warm);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-dim);
    text-decoration: none;
    max-width: 220px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .att-chip:hover { color: var(--ink); }
</style>
