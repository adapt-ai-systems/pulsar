<script>
  import { untrack } from 'svelte';
  import { createChatScroll } from './chatscroll.js';
  import { getEvents, getTaskEvents, getPartial, getReasoning, getCode, setStreamPartials, streamPartialsOn, setStreamReasoning, reasoningOn, setStreamCode, codeOn, deletePulseStopping, pulseWatchers } from './api.js';
  import { live } from './live.svelte.js';
  import { createChatFeed } from './chatfeed.svelte.js';
  import { openReasoning } from './reasoningstore.svelte.js';
  import ThinkingCard from './ThinkingCard.svelte';
  import { clock, subscribeClock } from './clock.svelte.js';
  import { scheduleStore, subscribeSchedules, refreshSchedules, schedulesFor } from './schedulestore.svelte.js';
  import { pulseStatus, fmtClock, classifyUserMessage, stoppedReasonText } from './render.js';
  import { lastYouFromWindow, lastYouFromWide, WIDE_TAIL } from './lastyou.js';
  import { renderMarkdown, copyCodeClick } from './markdown.js';
  import { htmlCards, loadHtmlCards, cardify } from './htmlcards.svelte.js';
  import { pulseToggle, runPulseAction } from './pulsetoggle.svelte.js';
  import { PULSE_MIME } from './dnd.js';
  import { paneDragOver, paneDrop, fmtSize } from './composerdrop.js';
  import { fmtTs, fmtTsFull, agoLabel, parseTs } from './time.js';
  import ToolLine from './ToolLine.svelte';
  import FoldRow from './FoldRow.svelte';
  import PeerMessage from './PeerMessage.svelte';
  import ReplyMessage from './ReplyMessage.svelte';
  import ToolCard from './ToolCard.svelte';
  import TodoBar from './TodoBar.svelte';
  import { turnStats, lastTurnItems, shortPath } from './toolcards.js';
  import { liveCodeView } from './livecode.js';
  import { uiPrefs } from './uiprefs.svelte.js';
  import SettingsModal from './SettingsModal.svelte';
  import Composer from './Composer.svelte';
  import LastYouStrip from './LastYouStrip.svelte';
  import PaneHead from './PaneHead.svelte';
  import DeleteWatchersModal from './DeleteWatchersModal.svelte';
  import Icon from './Icon.svelte';
  import WatchBar from './WatchBar.svelte';
  import QuestionBar from './QuestionBar.svelte';
  import { questionStore, questionsFor } from './questionstore.svelte.js';

  /** @type {{ pulse: any, onchanged: () => void, onreorder?: (fromId: string, toId: string) => void, onoverview?: () => void, onexpand?: (pulse: any) => void, onfullscreen?: (pulse: any) => void, fullscreen?: boolean, fsStage?: boolean }} */
  let { pulse, onchanged, onreorder, onoverview, onexpand, onfullscreen, onopenpulse, ws = null, fullscreen = false, fsStage = false } = $props();

  // --- wall drag-reorder (desktop only) ----------------------------------- //
  // The header is the drag HANDLE, the whole pane is the drop TARGET. Drag is
  // opt-in via the onreorder prop: the mobile deck shows one pane at a time and
  // passes nothing, so there's nothing to reorder there.
  //
  // The same drag also drops onto a workspace chip in the nav to move the pulse
  // there (GridTabs owns that end) — hence the custom PULSE_MIME below, which is
  // how a drop target tells "a pulse is being dragged" from any other drag. The
  // chips only ever see the mime type during dragover (the browser hides the
  // DATA until drop), so the id also rides in text/plain for the reorder path.
  const canReorder = $derived(!!onreorder);
  let dragging = $state(false);
  let dragOver = $state(false);
  function onDragOver(e) {
    dragOver = paneDragOver(e, { canReorder, dragging }) ?? dragOver;
  }

  function onDrop(e) {
    dragOver = paneDrop(e, { canReorder, pulseId: pulse.id }, { onreorder }) ?? dragOver;
  }

  // Visual tools: the changes strip for the newest turn —
  // files touched, +/− lines, commands run/failed. Only computed in that mode.
  const lastStats = $derived(uiPrefs.toolCards ? turnStats(lastTurnItems(feed.items)) : null);
  let error = $state(''); // visible error strip for this pane
  let notice = $state(''); // visible local-feedback strip (slash commands) — never sent to the pulse
  let draftSaveFailed = $state(false); // set by the Composer; the strip below the head shows it
  let sending = $state(false); // a send is in flight in the Composer
  // Shared 1s/5s ticker — one module timer for every pane's elapsed chip.
  const now = $derived(clock.now);
  // Minutes since the last transcript event while a turn runs with no live
  // tool: a hung model call sat on "working" for 10+ min with no hint.
  // A hint, not "stalled" — the process is alive and may finish.
  const QUIET_HINT_MIN = 10;
  const quietMin = $derived.by(() => {
    for (let i = feed.items.length - 1; i >= 0; i--) {
      const ts = feed.items[i]?.ts;
      if (!ts) continue;
      const ms = parseTs(ts);
      return Number.isFinite(ms) ? Math.floor((now - ms) / 60000) : 0;
    }
    return 0;
  });
  let paneEl = $state(null);
  let paneInView = $state(true);

  // Schedules drive the ⟳ loop-armed header indicator. One shared
  // store polls GET /schedules for every pane (watchstore pattern) — the
  // per-pane 10s GET /pulses/{id}/schedules storm was ~217 req/min.
  const schedules = $derived(schedulesFor(scheduleStore.schedules, pulse.id));
  let showSettings = $state(false);
  const armedLoop = $derived(
    schedules.find((s) => s.kind === 'loop' && s.enabled) ?? null
  );

  let chatEl; // scroll container

  // Derived, not read straight off the prop, because the page hands every pane
  // a NEW pulse object on each 3s roster poll: a $derived only notifies when
  // its VALUE changes, so the poll effect below can depend on the id/state
  // without being torn down and restarted three times a minute.
  const pulseId = $derived(pulse.id);
  const stopped = $derived(pulse.state === 'stopped');
  const archived = $derived(pulse.state === 'archived');
  const running = $derived(pulse.state === 'running');

  function elapsedSeconds(ts) {
    return Math.max(0, Math.round((now - ts) / 1000));
  }

  // Honest live status. Elapsed seconds tick off the daemon's
  // busy_since — no client-invented timer. Shared `clock.now` drives re-render.
  const status = $derived(pulseStatus(pulse?.live ?? null, now));
  const runnerDead = $derived(running && status.phase === 'stopped');
  let seenStartErrTs = null;

  // The status strip opens the reasoning panel, but only
  // when this pulse's Reasoning-traces setting is on. Same handler for the
  // pane strip and the keyboard path; the panel itself lives at the page
  // level and reads the runner's ephemeral reasoning.json.
  const reasoningEnabled = $derived(reasoningOn(pulse));
  function showReasoning() {
    if (!reasoningEnabled) return;
    openReasoning(pulse.id, pulse.title);
  }
  function onTraceKey(e) {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      showReasoning();
    }
  }
  // "Fresh": this pulse finished a turn within the last five minutes and is
  // not busy again — highlights recently completed turns.
  // heat runs 1 → 0 across the window so a 20-second-old reply reads hotter
  // than a 4-minute one; the ring and the idle strip both ride it.
  const FRESH_MS = 5 * 60 * 1000;
  // Clicking into the pane acknowledges it: remember WHICH finish was seen,
  // per pulse, in localStorage, so a reload does not relight it and the next
  // finish does.
  const SEEN_KEY = 'pulsar.freshSeen';
  function readSeen() {
    try { return JSON.parse(localStorage.getItem(SEEN_KEY) || '{}'); } catch { return {}; }
  }
  let seenDoneTs = $state(readSeen()[pulse?.id] ?? null);
  function dismissFresh() {
    if (feed.meta.lastDoneTs === null || seenDoneTs === feed.meta.lastDoneTs) return;
    seenDoneTs = feed.meta.lastDoneTs;
    try {
      const m = readSeen();
      m[pulse.id] = seenDoneTs;
      // Keep the map small: drop entries older than a day.
      const cutoff = Date.now() - 86_400_000;
      for (const k of Object.keys(m)) if (!(m[k] > cutoff)) delete m[k];
      localStorage.setItem(SEEN_KEY, JSON.stringify(m));
    } catch { /* storage may be unavailable; non-fatal */ }
  }
  const freshAge = $derived(feed.meta.lastDoneTs !== null && !status.thinking ? now - feed.meta.lastDoneTs : null);
  const fresh = $derived(freshAge !== null && freshAge >= 0 && freshAge < FRESH_MS && seenDoneTs !== feed.meta.lastDoneTs);
  const freshHeat = $derived(fresh ? Math.max(0.25, 1 - freshAge / FRESH_MS) : 0);

  let chatScroll;
  function trackChat(node) {
    const controller = createChatScroll(node, node.querySelector('.chat-content'), {
      follows: () => stuck,
      setFollowing: (value) => { stuck = value; },
      catchUp: () => feed.catchUp(),
    });
    chatScroll = controller;
    return { destroy() { controller.destroy(); chatScroll = null; } };
  }
  function anchorChat(keep, toBottom) {
    chatScroll?.anchor(keep, toBottom);
  }


  // --- live streaming (per-pulse, settings.stream_partials) --------------- //
  // When on, the ghost bubble reads the runner's ephemeral partial.json.
  // /live pushes {type:"partial"} on mtime/size change; the 500ms HTTP poll
  // is only the fallback when the socket is down. Unset defaults ON for
  // local pi/omp vessels so the ghost appears ~300ms after the model emits
  // instead of waiting for a finished block (median 7s).
  const stream = pulseToggle({
    getPulse: () => pulse,
    read: streamPartialsOn,
    write: setStreamPartials,
    label: 'streaming',
    hold: false,
    onchanged: () => onchanged?.(),
    onerror: (m) => (error = m)
  });
  const streamOn = $derived(stream.value);

  // The ⋯-menu toggle (next to the tool call checkbox).
  // Unlike tool detail this is PULSE state, not a browser pref — it writes
  // through to settings.stream_partials and comes back on the roster poll.
  const toggleStream = (v) => stream.set(v);

  // Reasoning traces: the pane-level twin of the settings
  // checkbox. Same UI gate — write straight through, no respawn.
  const reasoning = pulseToggle({
    getPulse: () => pulse,
    read: reasoningOn,
    write: setStreamReasoning,
    label: 'reasoning traces',
    hold: false,
    onchanged: () => onchanged?.(),
    onerror: (m) => (error = m)
  });
  const toggleReasoning = (v) => reasoning.set(v);

  // Live code: the tool call being written streams into
  // the chat. Same UI-gate toggle as reasoning traces — no respawn.
  const code = pulseToggle({
    getPulse: () => pulse,
    read: codeOn,
    write: setStreamCode,
    label: 'live code',
    hold: false,
    onchanged: () => onchanged?.(),
    onerror: (m) => (error = m)
  });
  const codeEnabled = $derived(code.value);
  // The live code box shows the tail of a long file.
  const CODE_INLINE_CHARS = 20000;
  const liveCode = $derived(liveCodeView(feed.ghostCode));

  // The inline reasoning ghost lives in chatfeed.svelte.js; the box that shows it clamps here.
  const REASONING_INLINE_CHARS = 12000;

  // `stuck` = the view is parked at the newest message, so new events should
  // scroll it. The moment the user scrolls up it goes false: the pane then
  // holds its place through every poll and offers a button back down instead
  // of yanking them to the bottom mid-paragraph.
  let stuck = $state(true);

  // The transcript feed (chatfeed.svelte.js): event window, /live + HTTP poll,
  // held events, scrollback, task list and the streaming ghosts. Everything it
  // reads from the pane comes in as a getter so the $derived values above stay
  // its effect dependencies.
  const feed = createChatFeed({
    live,
    getEvents,
    getPartial,
    getReasoning,
    getCode,
    getTaskEvents,
    getPulse: () => pulse,
    getPulseId: () => pulseId,
    isStopped: () => stopped,
    isStreamOn: () => streamOn,
    isReasoningEnabled: () => reasoningEnabled,
    isCodeEnabled: () => codeEnabled,
    getFoldNoise: () => uiPrefs.foldNoise,
    getChatEl: () => chatEl,
    getStuck: () => stuck,
    setStuck: (value) => { stuck = value; },
    anchor: anchorChat,
    setError: (msg) => { error = msg; },
    onEvents: (evs) => {
      updateLastYou(evs);
      const last = evs[evs.length - 1];
      const errKey = last?.ts ?? '__seen__';
      if (running && last?.type === 'runner_error' && last?.where === 'vessel.start' && errKey !== seenStartErrTs) {
        seenStartErrTs = errKey;
        onchanged?.();
      }
    },
    onReset: () => {
      seenStartErrTs = null;
      lastYou = null;
    }
  });

  // --- "last thing I said" strip ------------------------------------------ //
  // A pinned strip above the log always shows the user's newest sent message —
  // clamped to three lines, tap/click to expand to the full text. Lives
  // outside .chat so it never scrolls away; derived from `items` so it tracks
  // history restores and new sends without extra state.
  // {text, ts, attachments, doneTs|null}. Set from the event window on every
  // apply rather than derived from `items`: the pane only loads ~100 events,
  // and one tool-heavy turn pushes the user's message out of that window. When
  // the window doesn't reach back to a real message, fall back to one wide fetch.
  let lastYou = $state(null);
  // The window scan, the wide fallback and its per-pulse memory live in
  // lastyou.js (2026-11-02: a pulse with no real message re-fetched 2,000
  // events on every live push).
  function updateLastYou(evs) {
    const pid = pulse.id;
    const r = lastYouFromWindow(pid, evs, feed.tailLimit);
    if (r.show !== undefined) lastYou = r.show;
    if (!r.fetch) return;
    getEvents(pid, WIDE_TAIL).then((res) => {
      const show = lastYouFromWide(pid, res.ok ? (res.value?.events ?? []) : null);
      if (pid !== pulse.id || show === undefined) return;
      lastYou = show;
    });
  }
  // The strip mirrors a row that may itself be on screen — rendering it while
  // the real .msg.you bubble is visible reads as a duplicated message (the
  // second "shows up twice" report 2026-09-21; a live trace verified the daemon
  // stored that user_message exactly once). An IntersectionObserver on the
  // newest real YOU row hides the strip while that row is in view and brings it
  // back when it scrolls out. lastYou and its turn clock are untouched — only
  // the strip's visibility reacts.
  //
  // Lifecycle: an action runs when the node is CREATED and destroys when it is
  // removed — it does not re-run per render. There is exactly one real YOU row
  // per user_message, so each sent message gets one arm/destroy pair; destroy
  // clears the flag, so the strip can flash visible for a frame while the next
  // row's observer (created right after, self-correcting) awaits its first
  // async callback. Cosmetic, accepted.
  //
  // The arm condition is by DOM POSITION and realness: trackYouRow attaches in
  // markup after updateLastYou has run for this window, so a row arms only
  // when it is BOTH the last .msg.you element AND a real message by the strip
  // criteria (!isAutomated — the same filter updateLastYou uses: an automated
  // bracketed injection can be the last row while the strip mirrors an older
  // real message, and must not hide it). The wide-fetch
  // fallback for an out-of-window message keeps the strip shown, correct since
  // the row is not rendered. chatEl is bound on the ancestor scroll container
  // before child actions run (Svelte applies parent bindings first); the
  // root:null fallback would key off the viewport, which for a pane-fills-
  // viewport pane agrees with row visibility anyway.
  //
  // destroy() is OWNERSHIP-GUARDED (tail-window slides remove older armed rows
  // AFTER a newer row re-armed, and an unguarded destroy killed the live
  // observer and un-hid the strip): a row only tears
  // down the observer while it is still the owner node.
  let lastYouInView = $state(false);
  let youRowObs = null;
  let youRowOwner = null;
  function trackYouRow(node) {
    const rows = chatEl ? [...chatEl.querySelectorAll('.msg.you')] : [];
    const isLast = !chatEl || !node.parentElement ? true : rows.at(-1) === node;
    const ev = isLast && lastYou ? feed.items.find((it) => it.kind === 'you' && it.ts === lastYou.ts) : null;
    if (!isLast || !lastYou || !ev || classifyUserMessage(ev).automated) return {};
    youRowObs?.disconnect();
    youRowObs = new IntersectionObserver(
      (entries) => { lastYouInView = entries.some((e) => e.isIntersecting); },
      { root: chatEl }
    );
    youRowObs.observe(node);
    youRowOwner = node;
    return {
      destroy() {
        if (youRowOwner !== node) return; // superseded by a newer row — leave it alone
        youRowObs?.disconnect();
        youRowObs = null;
        youRowOwner = null;
        lastYouInView = false;
      },
    };
  }
  // Tap a history row → close the overlay and scroll the log to that bubble.
  // Bubbles carry data-ts; if the one wanted isn't in the loaded window,
  // widen the window to the daemon cap first (same fetch the history used).
  let jumpFlashTs = $state(null);
  async function jumpToYou(row) {
    if (!row.ts || !chatEl) return;
    const find = () => chatEl.querySelector(`.msg.you[data-ts="${CSS.escape(row.ts)}"]`);
    let el = find();
    if (!el) {
      await feed.widenTo(2000);
      await new Promise((r) => requestAnimationFrame(r));
      el = find();
    }
    if (!el) return;
    chatScroll?.release(); // cancel queued tail pins before navigating history
    chatEl.scrollTo({ top: Math.max(0, el.offsetTop - chatEl.offsetTop - 6), behavior: 'smooth' });
    jumpFlashTs = row.ts;
    setTimeout(() => { if (jumpFlashTs === row.ts) jumpFlashTs = null; }, 1800);
  }

  // --- jump between sent messages ----------------------------------------- //
  // While scrolled up, two semi-opaque arrows step the view through the
  // INBOUND messages — the user's own and other pulses' — the natural landmarks
  // in a long back-and-forth. Peer rows must count: in helper pulses every
  // prompt arrives from another pulse, so looking only for .msg.you left those
  // panes with zero jump targets.
  // They read the rendered DOM rather than `items` because what's on screen is
  // exactly these elements; offsets are measured against the shared
  // offsetParent (.chat-wrap), so they're scroll-independent.
  function jumpYou(dir) {
    if (!chatEl) return;
    const els = Array.from(chatEl.querySelectorAll('.msg.you, .msg.peer'));
    const pos = (el) => el.offsetTop - chatEl.offsetTop;
    const top = chatEl.scrollTop;
    let target = null;
    if (dir < 0) {
      // Nearest sent message strictly above the current view top.
      for (const el of els) {
        if (pos(el) < top - 8) target = el;
        else break;
      }
    } else {
      target = els.find((el) => pos(el) > top + 8) ?? null;
    }
    if (target) {
      chatScroll?.release();
      chatEl.scrollTo({ top: Math.max(0, pos(target) - 6), behavior: 'smooth' });
    } else if (dir > 0) {
      // Nothing sent below here — that means the newest exchange.
      feed.catchUp();
    } else {
      // Nothing above in the loaded window: go to its top, which puts the
      // scrollback loader in charge of fetching older history. A visible move
      // beats a silently dead button.
      chatScroll?.release();
      chatEl.scrollTo({ top: 0, behavior: 'smooth' });
    }
  }

  // Reply button: the pulse message the user's next send answers.
  // Carries pulseId so a pane reused for another pulse never sends it there.
  // The Composer shows the reply bar and sends it; the button below sets it.
  let replyTo = $state(null); // {pulseId, text, ts} | null
  const activeReply = $derived(replyTo && replyTo.pulseId === pulse.id ? replyTo : null);
  /** @type {{ focus: () => void } | undefined} */
  let composer = $state();

  function startReply(item) {
    replyTo = { pulseId: pulse.id, text: item.text ?? '', ts: item.ts ?? null };
    composer?.focus();
  }


  // One call for the pane's pulse actions: refresh the roster on ok, show
  // "<label>: <error>" in the error strip otherwise.
  const act = (label, fn) => runPulseAction(label, fn, { onok: () => onchanged?.(), onerr: (m) => (error = m) });

  // Delete: if the pulse owns watchers — condition watches, delegation watches
  // on helpers it spawned — a popup lists what will be closed and asks again;
  // deleting with none is one confirm as
  // before.
  let deleteWatchers = $state(null); // {condition:[], delegation:[]} while the popup is up
  let deleting = $state(false);
  async function doDelete() {
    const w = await pulseWatchers(pulse.id);
    if (w.ok && (w.value.condition.length || w.value.delegation.length)) {
      deleteWatchers = w.value;
      return;
    }
    await deleteNow(false);
  }
  async function deleteNow(closeWatchers) {
    deleting = true;
    await act('delete', () => deletePulseStopping(pulse.id, { closeWatchers }));
    deleting = false;
    deleteWatchers = null;
  }

  $effect(() => {
    const el = paneEl;
    if (!el || typeof IntersectionObserver === 'undefined') return;
    const io = new IntersectionObserver(
      (entries) => { paneInView = entries.some((e) => e.isIntersecting); },
      { threshold: 0 }
    );
    io.observe(el);
    return () => io.disconnect();
  });

  $effect(() => {
    if (stopped) return;
    return subscribeClock({ focused: paneInView });
  });

  $effect(() => subscribeSchedules());
  $effect(() => { loadHtmlCards(); });

  // Chat timestamps live in ./time.js, shared with ToolLine so tool calls
  // carry stamps too.

</script>

<div
  class="pane-shell"
  class:ws-first={!!ws?.first}
  class:work-glow={status.thinking && paneInView}
  bind:this={paneEl}
>
<div
  class="pane"
  class:working={status.thinking}
  class:asking={questionsFor(questionStore.questions, pulse.id).length > 0}
  class:ws={!!ws?.color}
  class:fresh
  style={`${ws?.color ? `--ws-color:${ws.color};` : ''}${fresh ? `--fresh-heat:${freshHeat.toFixed(2)};` : ''}` || null}
  class:dragging
  class:drag-over={dragOver}
  data-pulse-id={pulse.id}
  onpointerdowncapture={dismissFresh}
  onfocusincapture={dismissFresh}
  ondragover={onDragOver}
  ondragleave={() => (dragOver = false)}
  ondrop={onDrop}
  role="presentation"
>
  <PaneHead
    {pulse}
    {status}
    {ws}
    {stopped}
    {archived}
    {running}
    {fullscreen}
    {canReorder}
    {armedLoop}
    {stream}
    {reasoning}
    {reasoningEnabled}
    {code}
    {codeEnabled}
    {onchanged}
    onerror={(m) => (error = m)}
    onsettings={() => (showSettings = true)}
    ondelete={doDelete}
    ondragbegin={() => (dragging = true)}
    ondragfinish={() => { dragging = false; dragOver = false; }}
    {onoverview}
    {onexpand}
    {onfullscreen}
    {fsStage}
  />

  {#if error}
    <div class="err-strip">⚠ {error}</div>
  {/if}
  {#if notice}
    <div class="ok-strip" role="status">{notice}</div>
  {/if}
  {#if draftSaveFailed}
    <div class="err-strip" role="status">⚠ Draft could not be saved in this browser. Copy it before reloading.</div>
  {/if}

  <LastYouStrip {lastYou} inView={lastYouInView} pulseId={pulse.id} {now} onjump={jumpToYou} />

  <!-- The wrapper exists purely to anchor the jump-to-bottom button: it can't
       live inside .chat, because absolute children of a scroll container
       scroll away with the content. -->
  <div class="chat-wrap">
    <!-- svelte-ignore a11y_click_events_have_key_events -->
    <!-- svelte-ignore a11y_no_static_element_interactions -->
    <div class="chat" bind:this={chatEl} use:trackChat onscroll={() => feed.growWindow()} onclick={copyCodeClick}>
      <div class="chat-content">
      <div class="chat-pad"></div>
    {#if feed.moreAbove}
      <div class="older-hint">older ↑ — scroll up to load</div>
    {/if}
    {#each feed.items as item (item.key)}
      {#if item.kind === 'you'}
        <div class="msg you" class:flash={jumpFlashTs !== null && item.ts === jumpFlashTs} data-ts={item.ts ?? ''} use:trackYouRow>
          <div class="lbl">YOU{#if item.ts}<span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>{/if}{#if item.injected}<span class="mid-turn" title="sent while the pulse was working — it reads this before its next step, inside the same turn">sent mid-turn</span>{/if}</div>
          {#if item.replyTo}
            <div class="reply-quote" title={item.replyTo.text}>
              <span class="reply-quote-lbl"><Icon name="reply" size={10} stroke={2} /> replying to{#if item.replyTo.ts}{` · ${fmtTs(item.replyTo.ts)}`}{/if}</span>
              <span class="reply-quote-text">{item.replyTo.text}</span>
            </div>
          {/if}
          {#if item.text}
            <div class="body">{item.text}</div>
          {/if}
          {#if item.attachments?.length}
            <div class="att-row">
              {#each item.attachments as a (a.url ?? a.filename)}
                {#if (a.mime_type ?? '').startsWith('image/') && a.url}
                  <a class="att-img" href={'/api' + a.url} target="_blank" rel="noreferrer" title={a.filename}>
                    <img src={'/api' + a.url} alt={a.filename} loading="lazy" />
                  </a>
                {:else if (a.mime_type ?? '').startsWith('video/') && a.url}
                  <!-- User uploads do not have a separate captions track for
                       Pulsar to attach; preserve any captions in the file. -->
                  <!-- svelte-ignore a11y_media_has_caption -->
                  <video class="att-video" src={'/api' + a.url} controls preload="metadata" title={a.filename}></video>
                {:else}
                  <a
                    class="att-chip"
                    href={a.url ? '/api' + a.url : undefined}
                    target="_blank"
                    rel="noreferrer"
                    title={a.filename}
                  ><Icon name="clip" size={11} /> {a.filename} <span class="att-size">{fmtSize(a.size ?? 0)}</span></a>
                {/if}
              {/each}
            </div>
          {/if}
        </div>
      {:else if item.kind === 'peer' && uiPrefs.hidePeer}
        <!-- hidden: pulse or watcher message, "hide pulse messages" is on -->
      {:else if item.kind === 'peer'}
        <PeerMessage {item} />
      {:else if item.kind === 'fold'}
        <!-- A run of daemon watch chatter, collapsed to ONE line (render.js
             foldNoise). Nothing is dropped — tap to expand the rows inside it,
             each rendered exactly as it would have been. -->
        <FoldRow {item} {archived} {activeReply} onreply={startReply} />
      {:else if item.kind === 'reply'}
        <ReplyMessage {item} {archived} {activeReply} onreply={startReply} />
      {:else if item.kind === 'thinking' && reasoningEnabled}
        <!-- One finished thinking block: a collapsed card
             in order with the reply / tool call it led to. Hidden with the
             per-pulse Reasoning-traces toggle, like the panel. -->
        <ThinkingCard {item} />
      {:else if item.kind === 'tool' && uiPrefs.showToolCalls}
        <!-- The live pulse is only honest while the runner is actually busy:
             a tool orphaned by a pause/kill must not keep animating. -->
        {#if uiPrefs.toolCards}
          <ToolCard
            item={item.live && !status.thinking ? { ...item, live: false } : item}
            elapsed={item.live && status.thinking && feed.meta.liveTool ? elapsedSeconds(feed.meta.liveTool.ts) : null}
            pulseId={pulse.id}
          />
        {:else}
          <ToolLine
            item={item.live && !status.thinking ? { ...item, live: false } : item}
            elapsed={item.live && status.thinking && feed.meta.liveTool ? elapsedSeconds(feed.meta.liveTool.ts) : null}
            pulseId={pulse.id}
          />
        {/if}
      {:else if item.kind === 'spawned'}
        <!-- Daemon marker: this pulse spawned another. Always shown (not a
             tool line), and the link opens that pane in place — the document
             click handler catches ?pulse-focus= (notifreveal.svelte.js).
             target="_blank" is required, same as the markdown hot links: a
             link with no target is taken by SvelteKit's router first (it
             calls preventDefault and only rewrites the URL), so the click
             handler bails and the tap does nothing. -->
        <div class="spawned">
          {#if item.ts}<span class="ts" title={fmtTsFull(item.ts)}>{fmtTs(item.ts)}</span>{/if}↗ spawned
          <a href={`/?pulse-focus=${encodeURIComponent(item.pulseId)}`} target="_blank" rel="noreferrer noopener" title="Open this pulse">{item.title}</a>{#if item.model}<span class="spawned-model"> · {item.model}</span>{/if}
        </div>
      {:else if item.kind === 'turnend'}
        <!-- A turn that ended without a reply (tool-only / interrupted) —
             visibly distinct from "still hasn't answered". An interrupted
             turn is marked even after a partial reply. -->
        <div class="turnend">
          — {item.interrupted ? 'turn interrupted' : 'turn ended'}
          ({item.tools === 0 ? '' : `${item.tools} tool call${item.tools === 1 ? '' : 's'}, `}{item.replies ? 'partial reply' : 'no reply'}) —
        </div>
      {/if}
    {/each}
      {#if lastStats && (lastStats.files.length || lastStats.commands)}
        <!-- Changes strip for the newest turn (visual tools mode). -->
        <div class="changes">
          <div class="changes-sum">
            {#if lastStats.files.length}{lastStats.files.length} file{lastStats.files.length === 1 ? '' : 's'} changed · <span class="plus">+{lastStats.added}</span> <span class="minus">−{lastStats.removed}</span>{/if}
            {#if lastStats.files.length && lastStats.commands} · {/if}
            {#if lastStats.commands}{lastStats.commands} command{lastStats.commands === 1 ? '' : 's'}{#if lastStats.failed}, <span class="minus">{lastStats.failed} failed</span>{/if}{/if}
          </div>
          {#each lastStats.files as f (f.path)}
            <div class="changes-file" title={f.path}>
              <span class="changes-kind" class:created={f.created}>{f.created ? 'A' : 'M'}</span>
              <span class="changes-path">{shortPath(f.path)}</span>
              <span class="plus">+{f.added}</span> <span class="minus">−{f.removed}</span>
            </div>
          {/each}
        </div>
      {/if}
      {#if status.thinking && feed.ghostReasoning}
        <!-- Live thinking ghost: the model's reasoning
             trace, inline in the chat while it works. Rendered whether or not
             the view follows the tail — unmounting it on scroll-up clamped
             the scrollTop into shrunken content and threw the reader hundreds
             of px up the transcript (the "randomly scrolls back" report
             2026-09-14). The body is the trace TAIL (long turns clamp); click
             opens the full trace in the panel. -->
        <div class="msg dreamer ghost thinking">
          <div class="lbl">THINKING <span class="live-tag">streaming</span></div>
          <!-- Only the OPEN block streams here; finished blocks are the
               collapsed cards above. -->
          <div
            class="think-body"
            bind:this={feed.reasoningEl}
            role="button"
            tabindex="0"
            title="open the full reasoning trace"
            onclick={showReasoning}
            onkeydown={onTraceKey}
          >
            {#if feed.ghostReasoning.length > REASONING_INLINE_CHARS}
              <div class="think-trim">… earlier thinking trimmed — click for the full trace</div>
            {/if}
            <pre>{feed.ghostReasoning.slice(-REASONING_INLINE_CHARS)}</pre>
          </div>
        </div>
      {/if}
      {#if feed.ghostText}
        <!-- Live streaming ghost: the in-progress reply, replaced seamlessly by
             the real event at the block boundary (see pollPartial/render). -->
        <div class="msg dreamer ghost">
          <div class="lbl">REPLY <span class="live-tag">streaming</span></div>
          <!-- eslint-disable-next-line svelte/no-at-html-tags -->
          <div class="body md" class:card-wait={htmlCards.on}>{@html renderMarkdown(feed.ghostText)}</div>
        </div>
      {/if}
      {#if status.thinking && liveCode}
        <!-- Live code card: the tool call the model is
             writing right now — a Write's file, an Edit's diff, a command —
             replaced by the finished tool card when the call lands. -->
        <div class="msg dreamer ghost livecode">
          <div class="lbl">
            {liveCode.tool.toUpperCase()} <span class="live-tag">writing</span>
            {#if liveCode.path}<span class="code-path" title={liveCode.path}>{shortPath(liveCode.path)}</span>{/if}
          </div>
          <div class="code-body" bind:this={feed.codeEl}>
            {#if liveCode.lines}
              <pre>{#each liveCode.lines.slice(-600) as l, i (i)}<span class="ln" class:del={l.sign === '-'} class:add={l.sign === '+'}>{l.sign} {l.text}
</span>{/each}</pre>
            {:else}
              {#if liveCode.body.length > CODE_INLINE_CHARS}
                <div class="think-trim">… earlier lines trimmed</div>
              {/if}
              <pre>{liveCode.body.slice(-CODE_INLINE_CHARS)}<span class="caret">▌</span></pre>
            {/if}
            {#if feed.ghostCode?.truncated}
              <div class="think-trim">… too long to stream the rest — the tool card shows the call</div>
            {/if}
          </div>
        </div>
      {/if}
      {#if feed.items.length === 0 && !error && !feed.ghostText && !feed.ghostReasoning && !feed.ghostCode}
        <div class="empty">no messages yet</div>
      {/if}
      </div>
    </div>

    {#if !stuck}
      <!-- Sent-message stepper: only while reading back (same trigger as the
           jump-to-newest button). Semi-opaque so it doesn't sit on the text. -->
      <div class="you-nav">
        <button onclick={() => jumpYou(-1)} title="previous sent message (yours or another pulse's)" aria-label="previous sent message">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M6 15l6-6 6 6" />
          </svg>
        </button>
        <button onclick={() => jumpYou(1)} title="next sent message (yours or another pulse's)" aria-label="next sent message">
          <svg viewBox="0 0 24 24" width="14" height="14" fill="none" stroke="currentColor"
               stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
            <path d="M6 9l6 6 6-6" />
          </svg>
        </button>
      </div>
      <button
        class="to-bottom"
        class:has-new={feed.heldNew}
        onclick={() => feed.catchUp()}
        title={feed.heldNew ? 'new messages below — jump to newest' : 'jump to newest'}
        aria-label="jump to newest"
      >
        <svg viewBox="0 0 24 24" width="15" height="15" fill="none" stroke="currentColor"
             stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
          <path d="M12 5v14M6 13l6 6 6-6" />
        </svg>
      </button>
    {/if}
  </div>

  <!-- Condition watches — "N watches" strip above the status rail,
       expands upward into the list on click. Renders nothing when there are
       none. -->
  <!-- Open questions from this pulse — sits ABOVE the
       watch bar, open by default, minimizes to one line. -->
  <QuestionBar pulseId={pulse.id} />
  <WatchBar pulseId={pulse.id} />
  <!-- The pulse's task list: expandable, sits right above the idle/working strip. -->
  <TodoBar todo={feed.todo} />

  <div class="strip-row">
  {#if running}
    {#if status.thinking && feed.meta.liveTool}
      <div
        class="helper-bar"
        class:traceable={reasoningEnabled}
        role="button"
        tabindex="0"
        aria-disabled={!reasoningEnabled}
        title={reasoningEnabled ? 'show reasoning trace' : undefined}
        onclick={showReasoning}
        onkeydown={onTraceKey}
      >
        <!-- One plain line, clipped: a long shell command wrapped to six
             all-caps lines and squeezed the chat to three rows. -->
        <span class="tdot"></span>
        <span class="run-text" title={feed.meta.liveTool.line}>running: {(feed.meta.liveTool.line ?? '').split('\n')[0]}</span>
        <span class="run-secs">· {elapsedSeconds(feed.meta.liveTool.ts)}s</span>
      </div>
    {:else if status.thinking}
      <div
        class="helper-bar"
        class:traceable={reasoningEnabled}
        role="button"
        tabindex="0"
        aria-disabled={!reasoningEnabled}
        title={reasoningEnabled ? 'show reasoning trace' : undefined}
        onclick={showReasoning}
        onkeydown={onTraceKey}
      >
        <span class="tdot"></span> {status.label}{#if quietMin >= QUIET_HINT_MIN}<span class="quiet-hint"> · no new output for {quietMin} min — if it looks stuck, ■ stop and send again</span>{/if}
      </div>
    {:else if status.phase === 'waking' || status.phase === 'stopping'}
      <div class="helper-bar">
        <span class="tdot"></span> {status.label}
      </div>
    {:else if status.phase === 'stopped'}
      {@const reason = stoppedReasonText(status.evidence)}
      <div class="helper-bar stopped-reason" title={reason}>
        <span class="tdot"></span> stopped — {reason}
      </div>
    {:else if status.stalled}
      <!-- Reserved for real trouble only: process dead/unresponsive.
           Evidence rides the title attr per the standing logging/observability rule. -->
      <div class="helper-bar stalled" title={status.evidence}>
        <span class="tdot"></span> stalled — {status.evidence}
      </div>
    {:else if status.phase === 'idle'}
      <div
        class="helper-bar idle"
        class:traceable={reasoningEnabled}
        class:fresh={fresh && !feed.meta.lastFailed}
        class:failed={feed.meta.lastFailed}
        role="button"
        tabindex="0"
        aria-disabled={!reasoningEnabled}
        title={reasoningEnabled ? 'show the last reasoning trace' : undefined}
        onclick={showReasoning}
        onkeydown={onTraceKey}
      >
        <span class="tdot"></span> {#if feed.meta.lastFailed}✕ last turn failed · {agoLabel(feed.meta.lastDoneTs, now)} ago{:else if fresh}✓ just finished · {agoLabel(feed.meta.lastDoneTs, now)} ago{:else}idle{#if feed.meta.lastDoneTs !== null} · finished {agoLabel(feed.meta.lastDoneTs, now)} ago{/if}{/if}{#if feed.meta.lastTurnMs !== null} · took {fmtClock(feed.meta.lastTurnMs)}{/if}{#if feed.meta.lastThinkTok} · thought {feed.meta.lastThinkTok.toLocaleString()} tokens{/if}
      </div>
    {:else}
      <!-- A running roster row without a live block is between daemon polls;
           keep the rail present without inventing elapsed progress. -->
      <div class="helper-bar">
        <span class="tdot"></span> {status.label || 'waking…'}
      </div>
    {/if}
  {:else if stopped && pulse.settings?.stop_reason}
    <!-- A pulse that died says WHY on its tile — the store records
         stop_reason (fence breach, vessel error, dead runner) but until now
         nothing rendered it. -->
    {@const reason = stoppedReasonText(pulse.settings.stop_reason)}
    <div class="helper-bar stopped-reason" title={reason}>
      <span class="tdot"></span> stopped — {reason}
    </div>
  {/if}
  </div>

  <Composer
    bind:this={composer}
    {pulse}
    stopped={stopped || runnerDead}
    {archived}
    running={running && !runnerDead}
    bind:sending
    bind:replyTo
    bind:draftSaveFailed
    onerror={(m) => (error = m)}
    onnotice={(m) => (notice = m)}
    onsent={() => feed.poll(false, true)}
    {onchanged}
  />
</div>
</div>


{#if showSettings}
  <SettingsModal
    {pulse}
    onclose={() => (showSettings = false)}
    onchanged={() => {
      refreshSchedules();
      onchanged?.();
    }}
  />
{/if}

{#if deleteWatchers}
  <DeleteWatchersModal
    {pulse}
    watchers={deleteWatchers}
    {deleting}
    onconfirm={deleteNow}
    oncancel={() => (deleteWatchers = null)}
  />
{/if}

<style>
  .pane-shell {
    display: flex;
    flex-direction: column;
    min-height: 0;
    height: 100%;
    flex: 1;
    border-radius: 7px;
  }
  /* ALL view: the first pane of a workspace block (not at a row start) opens
     a wider gap on its left so the blocks read apart. */
  .pane-shell.ws-first { margin-left: 28px; }
  .pane {
    display: flex;
    flex-direction: column;
    position: relative;
    border: 1px solid var(--border-strong);
    border-radius: 7px;
    background: var(--bg);
    overflow: hidden;
    min-height: 0;
    flex: 1;
    /* Named so the head's @container query measures THIS pane's width. */
    container: pane / inline-size;
  }
  .pane.dragging { opacity: 0.4; }
  /* Working/thinking: the whole pane carries a thick flashing border in the
     same color the thinking strip uses, so a busy pulse reads at a glance. The
     thickness rides in the box-shadow ring, not border-width, so the layout
     never shifts when a pulse starts or stops working. */
  /* Every knob is an appearance setting (theme.js WORK_NUMS): ring width,
     glow, flash speed, header tint. Fallbacks match the setting defaults. */
  .pane.working {
    border-color: var(--work-color, var(--dreamer));
  }
  /* A pulse waiting on an answer: amber edge, beats the working color. */
  .pane.asking { border-color: #f2c57a; }
  /* ALL view: every pane wears its workspace color as a 2px outline plus a
     small name tag in the head, so workspace blocks read at a glance. Inset
     ring, so no layout shift; the working ring sits outside it and still shows. */
  .pane.ws { box-shadow: inset 0 0 0 2px var(--ws-color); border-color: var(--ws-color); }
  .pane.drag-over {
    border-color: var(--work-color, var(--dreamer));
    box-shadow: 0 0 0 1px var(--work-color, var(--dreamer)), 0 0 18px color-mix(in srgb, var(--work-color, var(--dreamer)) 40%, transparent);
  }
  .quiet-hint { color: var(--warden); }
  .mid-turn { margin-left: 8px; font-size: 0.85em; color: var(--ink-meta); letter-spacing: 0.04em; text-transform: none; }

  .ok-strip {
    background: color-mix(in srgb, var(--sprite) 14%, var(--surface));
    color: var(--sprite);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 4px 10px;
    border-bottom: 1px solid color-mix(in srgb, var(--sprite) 40%, var(--border));
    flex-shrink: 0;
    word-break: break-word;
    white-space: pre-line;
  }

  .err-strip {
    background: color-mix(in srgb, var(--bad) 16%, var(--surface));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 4px 10px;
    border-bottom: 1px solid color-mix(in srgb, var(--bad) 40%, var(--border));
    flex-shrink: 0;
    word-break: break-word;
  }

  /* overflow-x hidden: one unbreakable token otherwise makes the whole log
     pannable sideways on iOS, shifting the pane off-screen left. */
  /* --page paints the message log specifically, so the surface you READ on is
     tunable separately from the wall/nav/pane chrome (--bg). Before this it was
     only on <body>, which the full-bleed app shell covers completely, so the
     "Page" swatch in the theme modal changed nothing. */
  .msg.you.flash { animation: youFlash 1.8s ease-out; }
  @keyframes youFlash {
    0%, 40% { box-shadow: inset 0 0 0 2px var(--user); }
    100% { box-shadow: inset 0 0 0 2px transparent; }
  }

  .chat-wrap {
    position: relative;
    flex: 1;
    min-height: 0;
    display: flex;
    flex-direction: column;
  }
  .chat {
    flex: 1;
    overflow-y: auto;
    overflow-x: hidden;
    min-height: 0;
    display: flex;
    flex-direction: column;
    background: var(--page);
  }
  .chat-content {
    display: flex;
    flex-direction: column;
    flex: 0 0 auto;
    min-height: 100%;
  }
  .chat-pad { flex: 1; }
  /* Sent-message stepper — stacked above the jump-to-newest button, same
     right edge. Semi-opaque at rest so the arrows read as chrome, not content;
     full strength under the finger/pointer. */
  .you-nav {
    position: absolute;
    right: 14px;
    bottom: 50px;
    display: flex;
    flex-direction: column;
    gap: 6px;
    opacity: 0.55;
    transition: opacity 0.15s;
  }
  .you-nav:hover,
  .you-nav:active { opacity: 1; }
  .you-nav button {
    width: 30px;
    height: 30px;
    padding: 0;
    display: grid;
    place-items: center;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--user);
    cursor: pointer;
    box-shadow: 0 2px 10px rgb(0 0 0 / 0.35);
  }
  .you-nav button:hover { color: var(--ink); border-color: var(--ink-meta); }

  /* Only rendered while scrolled up (see `stuck`). Sits above the chat rather
     than inside it, so it stays put while the content moves under it. */
  .to-bottom {
    position: absolute;
    right: 14px;
    bottom: 12px;
    width: 30px;
    height: 30px;
    padding: 0;
    display: grid;
    place-items: center;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    background: var(--surface);
    color: var(--ink-dim);
    cursor: pointer;
    box-shadow: 0 2px 10px rgb(0 0 0 / 0.35);
  }
  .to-bottom:hover {
    color: var(--ink);
    border-color: var(--ink-meta);
  }
  /* Something arrived while you were reading up. The pane deliberately did not
     render it, so the button is the only signal that there is anything new. */
  .to-bottom.has-new {
    color: var(--dreamer);
    border-color: var(--dreamer);
  }
  .to-bottom.has-new::after {
    content: '';
    position: absolute;
    top: -2px;
    right: -2px;
    width: 8px;
    height: 8px;
    border-radius: 50%;
    background: var(--dreamer);
    border: 1.5px solid var(--surface);
  }
  .older-hint {
    text-align: center;
    padding: 8px 0 4px;
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.12em;
    flex-shrink: 0;
  }
  .empty {
    padding: 12px 14px;
    color: var(--ink-meta);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
  }
  .chat :global(.msg) {
    border-top: 1px solid color-mix(in srgb, var(--ink) 14%, transparent);
    padding: 5px 14px 7px;
    min-width: 0;
  }
  /* Silent-turn boundary: quieter than a tool line — it marks absence. */
  /* Visual-tools changes strip: git-status-style summary of the newest turn. */
  .changes {
    flex: 0 0 auto;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-tool));
    color: var(--ink-meta);
    margin: 6px 14px 6px 20px;
    padding: 5px 8px;
    border: 1px solid var(--border);
    border-radius: 7px;
    background: color-mix(in srgb, var(--ink-meta) 5%, var(--surface));
  }
  .changes .changes-sum { color: var(--ink-dim); margin-bottom: 2px; }
  .changes .changes-file { display: flex; gap: 8px; align-items: baseline; }
  .changes .changes-path { color: var(--ink-dim); flex: 1 1 auto; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .changes .changes-kind { color: #d9a441; width: 1ch; }
  .changes .changes-kind.created { color: #4fbf7a; }
  .changes .plus { color: #4fbf7a; }
  .changes .minus { color: #e0605c; }
  .turnend {
    font-family: var(--font-mono);
    font-size: calc(9.5px * var(--tscale-tool));
    color: var(--ink-meta);
    opacity: 0.7;
    padding: 4px 14px;
    text-align: center;
  }
  .spawned {
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-tool));
    color: var(--ink-dim);
    padding: 4px 14px 4px 24px;
    line-height: 1.5;
    overflow-wrap: anywhere;
  }
  .spawned .ts {
    color: var(--ink-meta);
    opacity: 0.75;
    margin-right: 6px;
    font-size: 90%;
  }
  .spawned a { color: var(--dreamer); text-decoration: underline; text-underline-offset: 2px; }
  .spawned a:hover { color: var(--sprite); }
  .spawned .spawned-model { color: var(--ink-meta); }
  /* Long unbroken tokens (URLs, hashes, pasted one-liners) wrap instead of
     widening the bubble past the pane. */
  .chat :global(.msg .body) { overflow-wrap: anywhere; word-break: break-word; }
  .chat :global(.msg .lbl) {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    margin-bottom: 3px;
    display: flex;
    align-items: baseline;
  }
  .chat :global(.msg .lbl .ts) {
    margin-left: 8px;
    color: var(--ink-meta);
    letter-spacing: 0.04em;
    font-size: calc(8.5px * var(--tscale-chat));
  }
  /* Bubble washes are tunable tokens (Appearance → Chat messages): base color
     via --bub-*, strength via --bub-*-fill. */
  .chat :global(.msg.dreamer) { background: color-mix(in srgb, var(--bub-reply) var(--bub-reply-fill), transparent); }
  .chat :global(.msg.dreamer .lbl) { color: var(--dreamer); }
  /* Reply-first emphasis. The reply is the main content in the pane, so it is
     the only block with an accent: a rule down its left edge in the AI voice
     color, a stronger wash
     than any other row, and a bold label. The rule rides --bub-reply, which
     theme.js always derives from --dreamer (never pinnable) — pinning it once
     rendered this rule pale gray on gray. Wash strength stays tunable in
     Appearance; the rule strength is the part sliders don't reach, and it is
     the part that must survive a light theme, hence 85%. Layout-neutral: no
     extra height, so long replies cost no more scrolling on the phone. */
  .chat :global(.msg.dreamer) {
    box-shadow: inset 3px 0 0 color-mix(in srgb, var(--bub-reply) 85%, transparent);
    /* indent + left padding, not padding-left alone: a box-shadow spreads to
       the border box, so an inset rule at 0 sits flush with the row's own left
       edge, and the extra padding can then widen the row past the scroll
       container (.chat is overflow-y:auto) and drag a horizontal scrollbar in
       on the phone. .changes already uses this 6px-over-base indent. */
    margin-left: 6px;
    padding-left: 11px;
  }
  .chat :global(.msg.dreamer .lbl) { font-weight: 700; letter-spacing: 0.18em; }
  /* No pre-wrap on the markdown side — block elements own the whitespace now,
     and hard line breaks are explicit <br>s from the renderer. */
  .chat :global(.msg.dreamer .body) { font-family: var(--font-serif); font-size: calc(12.5px * var(--tscale-chat)); line-height: 1.5; color: var(--ink); }
  /* Live-streaming ghost: slightly translucent + a typing caret, so an
     in-progress reply never reads as a finished one. */
  .msg.ghost { opacity: 0.88; }
  .msg.ghost .body::after {
    content: '▍';
    color: var(--dreamer);
    animation: caretBlink 1s steps(1) infinite;
  }
  .msg.ghost .lbl .live-tag {
    margin-left: 6px;
    font-size: calc(8px * var(--tscale-ui));
    letter-spacing: 0.12em;
    text-transform: uppercase;
    color: var(--sprite);
  }
  /* Inline thinking ghost: clamped to the trace tail so a long turn can't
     flood the pane; plain monospace pre (reasoning is raw text, not chat
     markdown). Click anywhere in the body opens the full panel. */
  .msg.ghost.thinking .think-body {
    max-height: min(34vh, 300px);
    overflow-y: auto;
    cursor: pointer;
    border-radius: 6px;
  }
  .msg.ghost.thinking .think-body pre {
    margin: 0;
    white-space: pre-wrap;
    word-break: break-word;
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-chat));
    line-height: 1.5;
    color: var(--ink-dim, var(--ink));
  }
  /* Live code card: monospace, clamped, follows its own tail like the
     thinking box. Edit lines tint by sign. */
  .msg.ghost.livecode .code-path {
    margin-left: 8px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    text-transform: none;
    letter-spacing: 0;
  }
  .msg.ghost.livecode .code-body {
    max-height: min(40vh, 360px);
    overflow: auto;
    border-radius: 6px;
  }
  .msg.ghost.livecode .code-body pre {
    margin: 0;
    white-space: pre;
    font-family: var(--font-mono);
    font-size: calc(10.5px * var(--tscale-chat));
    line-height: 1.5;
    color: var(--ink);
  }
  .msg.ghost.livecode .ln.del { color: #e0605c; }
  .msg.ghost.livecode .ln.add { color: #4fbf7a; }
  .msg.ghost.livecode .caret {
    color: var(--dreamer);
    animation: caretBlink 1s steps(1) infinite;
  }
  .msg.ghost.livecode .think-trim,
  .msg.ghost.thinking .think-trim {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--ink-meta);
    margin-bottom: 4px;
  }
  @keyframes caretBlink {
    0%, 49% { opacity: 1; }
    50%, 100% { opacity: 0; }
  }

  /* --- rendered markdown (see markdown.js) ------------------------------- */
  .chat :global(.md p) { margin: 0 0 0.65em; }
  .chat :global(.md p:last-child) { margin-bottom: 0; }
  .chat :global(.md h1),
  .chat :global(.md h2),
  .chat :global(.md h3),
  .chat :global(.md h4),
  .chat :global(.md h5),
  .chat :global(.md h6) {
    font-family: var(--font-sans);
    font-weight: 600;
    line-height: 1.3;
    margin: 1em 0 0.4em;
    color: var(--ink);
  }
  .chat :global(.md h1:first-child),
  .chat :global(.md h2:first-child),
  .chat :global(.md h3:first-child) { margin-top: 0; }
  /* Chat panes are narrow columns — a 2em h1 would be absurd. Headings step
     down gently and lean on weight/color instead of size. */
  .chat :global(.md h1) { font-size: calc(15px * var(--tscale-chat)); }
  .chat :global(.md h2) { font-size: calc(13.5px * var(--tscale-chat)); }
  .chat :global(.md h3) { font-size: calc(12.5px * var(--tscale-chat)); }
  .chat :global(.md h4),
  .chat :global(.md h5),
  .chat :global(.md h6) { font-size: calc(12px * var(--tscale-chat)); color: var(--ink-dim); }
  .chat :global(.md strong) { font-weight: 700; color: var(--ink); }
  .chat :global(.md em) { font-style: italic; }
  .chat :global(.md del) { text-decoration: line-through; color: var(--ink-meta); }
  .chat :global(.md a) { color: var(--dreamer); text-decoration: underline; text-underline-offset: 2px; }
  .chat :global(.md a:hover) { color: var(--sprite); }
  .chat :global(.md ul),
  .chat :global(.md ol) { margin: 0 0 0.65em; padding-left: 1.4em; }
  /* Two/three-digit markers need more room or the tens digit is clipped at
     the pane edge ("31." rendered as "1."). */
  .chat :global(.md ol:has(> li:nth-child(10))) { padding-left: 2.1em; }
  .chat :global(.md ol:has(> li:nth-child(100))) { padding-left: 2.8em; }
  .chat :global(.md li) { margin: 0.15em 0; }
  .chat :global(.md ul ul),
  .chat :global(.md ol ol),
  .chat :global(.md ul ol),
  .chat :global(.md ol ul) { margin-bottom: 0; }
  .chat :global(.md code) {
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-chat));
    background: var(--surface-warm);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0.5px 4px;
    overflow-wrap: anywhere;
  }
  /* Code block = bar (language + copy) welded onto the top of the <pre>. */
  .chat :global(.md .code-wrap) { margin: 0 0 0.65em; }
  /* HTML cards (htmlcards.svelte.js): the live frame replaces the code until
     "source" is tapped. */
  .chat :global(.md iframe.html-card) { display: block; width: 100%; height: 120px; border: 0; border-radius: 6px; background: var(--bg); }
  .chat :global(.md .code-wrap.carded:not(.show-src) pre) { display: none; }
  /* While a reply streams, an html/svg block shows a placeholder, not its
     code: the card appears once the reply is done. */
  .chat :global(.md.card-wait .code-wrap:has(code.lang-html) pre),
  .chat :global(.md.card-wait .code-wrap:has(code.lang-svg) pre) { display: none; }
  .chat :global(.md.card-wait .code-wrap:has(code.lang-html))::after,
  .chat :global(.md.card-wait .code-wrap:has(code.lang-svg))::after {
    content: 'building card…'; display: block; padding: 14px 10px; border: 1px solid var(--border-strong);
    border-radius: 0 0 6px 6px; color: var(--ink-meta); font-family: var(--font-mono);
  }
  .chat :global(.md .src-btn) { background: none; border: 0; color: inherit; font: inherit; cursor: pointer; opacity: 0.7; margin-left: auto; margin-right: 6px; }
  .chat :global(.md .code-bar) {
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    padding: 3px 6px 3px 10px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-bottom: none;
    border-radius: 6px 6px 0 0;
  }
  .chat :global(.md .code-lang) {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-chat));
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .chat :global(.md .copy-btn) {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-chat));
    letter-spacing: 0.08em;
    color: var(--ink-meta);
    background: none;
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    padding: 2px 7px;
    cursor: pointer;
    flex: none;
  }
  .chat :global(.md .copy-btn:hover) { color: var(--dreamer); border-color: var(--dreamer); }
  .chat :global(.md .copy-btn.ok) { color: var(--sprite); border-color: var(--sprite); }
  .chat :global(.md .copy-btn.bad) { color: var(--bad); border-color: var(--bad); }
  .chat :global(.md pre) {
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 0 0 6px 6px;
    padding: 8px 10px;
    margin: 0;
    overflow-x: auto;
  }
  /* The <code> chip styling must not double up inside a block. */
  .chat :global(.md pre code) {
    background: none;
    border: none;
    padding: 0;
    font-size: calc(11px * var(--tscale-chat));
    line-height: 1.45;
    white-space: pre;
    overflow-wrap: normal;
  }
  .chat :global(.md blockquote) {
    margin: 0 0 0.65em;
    padding: 2px 0 2px 10px;
    border-left: 2px solid var(--border-strong);
    color: var(--ink-dim);
  }
  .chat :global(.md hr) { border: none; border-top: 1px solid var(--border); margin: 1em 0; }
  .chat :global(.md table) {
    border-collapse: collapse;
    margin: 0 0 0.65em;
    font-size: calc(11px * var(--tscale-chat));
    display: block;
    overflow-x: auto;
  }
  .chat :global(.md th),
  .chat :global(.md td) {
    border: 1px solid var(--border-strong);
    padding: 3px 7px;
    text-align: left;
  }
  .chat :global(.md th) { color: var(--ink); font-weight: 600; background: var(--surface-warm); }
  .msg.you { background: color-mix(in srgb, var(--bub-you) var(--bub-you-fill), transparent); }
  .msg.you .lbl { color: var(--user); }
  .msg.you .body { font-family: var(--font-sans); font-size: calc(12px * var(--tscale-chat)); font-weight: 500; color: var(--ink); white-space: pre-wrap; }

  .strip-row { display: flex; align-items: stretch; flex-shrink: 0; }
  .strip-row > .helper-bar { flex: 1; min-width: 0; }
  .helper-bar {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 12px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 4%, var(--surface));
    border-top: 1px solid var(--border);
    flex-shrink: 0;
  }
  .helper-bar .run-text { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; text-transform: none; }
  .helper-bar .run-secs { flex-shrink: 0; }
  .helper-bar .tdot {
    width: 4px; height: 4px; border-radius: 50%;
    background: var(--dreamer);
  }
  /* Clickable status strip: opens the reasoning panel for this pulse. */
  .helper-bar.traceable {
    cursor: pointer;
  }
  .helper-bar.traceable:hover { filter: brightness(1.25); }
  .helper-bar.traceable:focus-visible {
    outline: 2px solid var(--dreamer);
    outline-offset: -2px;
  }
  /* Reasoning traces turned off: the strip is still a button to AT (disabled),
     but it no longer reads or acts as one. */
  .helper-bar[aria-disabled='true'] { cursor: default; }
  .helper-bar[aria-disabled='true']:hover { filter: none; }
  .pane-shell.work-glow .helper-bar .tdot { animation: pulse 1.2s infinite; }
  .helper-bar.idle {
    color: var(--ink-meta);
    background: color-mix(in srgb, var(--ink-meta) 3%, var(--surface));
    border-top-color: var(--border);
  }
  .helper-bar.idle .tdot { background: var(--ink-meta); animation: none; }
  /* Fresh (finished < 5 min ago): the strip lights up in the fresh color and
     the pane wears an outer ring; both cool down with --fresh-heat. */
  .helper-bar.idle.fresh {
    color: var(--fresh-color, #7ff0ff);
    background: color-mix(in srgb, var(--fresh-color, #7ff0ff) calc(var(--fresh-heat, 1) * 22%), var(--surface));
    border-top-color: color-mix(in srgb, var(--fresh-color, #7ff0ff) 50%, var(--border));
    font-weight: 700;
  }
  .helper-bar.idle.fresh .tdot { background: var(--fresh-color, #7ff0ff); box-shadow: 0 0 6px var(--fresh-color, #7ff0ff); }
  .pane.fresh {
    outline: 3px solid var(--fresh-color, #7ff0ff);
    outline-offset: 1px;
    box-shadow:
      inset 0 0 0 2px var(--ws-color, transparent),
      0 0 calc(6px + var(--fresh-heat, 1) * 18px) color-mix(in srgb, var(--fresh-color, #7ff0ff) calc(var(--fresh-heat, 1) * 70%), transparent);
  }
  .helper-bar.stalled {
    color: var(--bad);
    background: color-mix(in srgb, var(--bad) 6%, var(--surface));
    border-top-color: color-mix(in srgb, var(--bad) 30%, var(--border));
  }
  .helper-bar.stalled .tdot { background: var(--bad); animation: none; }
  .helper-bar.idle.failed { color: var(--bad); }
  .helper-bar.idle.failed .tdot { background: var(--bad); box-shadow: none; }
  .helper-bar.stopped-reason {
    color: var(--ink-dim);
    background: color-mix(in srgb, var(--bad) 4%, var(--surface));
    border-top-color: color-mix(in srgb, var(--bad) 20%, var(--border));
    overflow: hidden;
    white-space: nowrap;
    text-overflow: ellipsis;
    display: block;
    line-height: 1.9;
  }
  .helper-bar.stopped-reason .tdot { display: none; }


  /* The quoted message on the sent YOU bubble (the composer's own reply bar
     is in Composer.svelte). Clamped: the pulse gets the full text, this is a
     reminder. */
  .reply-quote {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    border-left: 2px solid color-mix(in srgb, var(--dreamer) 70%, transparent);
    background: color-mix(in srgb, var(--dreamer) 6%, var(--surface));
    border-radius: 0 6px 6px 0;
    margin: 2px 0 4px;
    padding: 3px 8px;
  }
  .reply-quote-lbl {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: var(--font-mono);
    font-size: calc(8.5px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--dreamer);
  }
  .reply-quote-text {
    font-family: var(--font-serif);
    font-size: calc(11px * var(--tscale-chat));
    line-height: 1.35;
    color: var(--ink-dim);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
  }

  /* Sent attachments in the YOU bubble — image thumbnails / file chips. */
  .att-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    margin-top: 5px;
  }
  .att-img {
    display: block;
    max-width: 160px;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    overflow: hidden;
  }
  .att-img img { display: block; max-width: 100%; max-height: 120px; }
  .att-video {
    display: block;
    width: min(260px, 100%);
    max-height: 180px;
    border: 1px solid var(--border-strong);
    border-radius: 6px;
    background: #000;
  }
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
  .att-size { color: var(--ink-meta); }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(0.7); opacity: 0.6; }
  }
  @media (prefers-reduced-motion: reduce) {
    .pane-shell.work-glow .helper-bar .tdot { animation: none; }
  }

</style>
