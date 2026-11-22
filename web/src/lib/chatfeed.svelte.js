import { untrack } from 'svelte';
import { mergeDelta } from './evmerge.js';
import { buildItems, foldNoise, keyItems, turnMeta } from './render.js';
import { ghostSupersededBy } from './ghostnet.js';
import { foldTodos, mergeTaskEvents, isTaskEvent } from './toolcards.js';

/**
 * The transcript feed of one chat pane (moved out of ChatPane.svelte).
 *
 * Owns the event window and everything derived from it: the tail window and its
 * growth (`tailLimit`, `moreAbove`), the byte `cursor`, events held while the
 * reader is scrolled back (`held`, `heldNew`), the rendered `items`, the task
 * list (`todo`), the status-rail `meta`, and the streaming ghosts (reply
 * text, reasoning, live code) with their handover flags. It also registers the three
 * effects that keep it fed: the 2 s poll + `/live` subscription, the 500 ms HTTP
 * partial fallback, and the fold-mode re-render.
 *
 * Call it during component init: it creates `$effect`s. Nothing reactive from
 * the component is imported — everything the feed reads from outside arrives as
 * a getter, so the component's `$derived`s stay the dependency sources and a
 * test can drive it with plain functions.
 *
 * @param {Object} deps
 * @param {{connected: boolean, watch: Function, setTail: Function}} deps.live
 * @param {(id: string, tail: number, after?: number|null) => Promise<any>} deps.getEvents
 * @param {(id: string) => Promise<any>} deps.getPartial
 * @param {(id: string) => Promise<any>} deps.getReasoning
 * @param {(id: string) => Promise<any>} [deps.getCode]
 * @param {(id: string) => Promise<any>} deps.getTaskEvents
 * @param {() => any} deps.getPulse         the current pulse object (read untracked)
 * @param {() => string} deps.getPulseId    tracked: pulse id
 * @param {() => boolean} deps.isStopped    tracked: pulse.state === 'stopped'
 * @param {() => boolean} deps.isStreamOn   tracked: the stream-partials toggle
 * @param {() => boolean} deps.isReasoningEnabled  reasoning traces on for this pulse
 * @param {() => boolean} [deps.isCodeEnabled]  live code card on for this pulse
 * @param {() => boolean} deps.getFoldNoise tracked: the "fold watch chatter" pref
 * @param {() => any} deps.getChatEl        the scroll container, if mounted
 * @param {() => boolean} deps.getStuck     following the tail?
 * @param {(v: boolean) => void} deps.setStuck
 * @param {(keep: number, toBottom: boolean) => void} deps.anchor  scroll anchoring (chatscroll.js)
 * @param {(msg: string) => void} deps.setError  the pane's error strip
 * @param {(evs: any[]) => void} [deps.onEvents]  every applied window (last-you strip)
 * @param {() => void} [deps.onReset]       the pulse changed; drop per-pulse caches
 */
export function createChatFeed(deps) {
  const {
    live, getEvents, getPartial, getReasoning, getTaskEvents,
    getCode = async () => ({ ok: false }), isCodeEnabled = () => false,
    getPulse, getPulseId, isStopped, isStreamOn, isReasoningEnabled, getFoldNoise,
    getChatEl, getStuck, setStuck, anchor, setError,
    onEvents = () => {}, onReset = () => {}
  } = deps;

  let items = $state([]);
  // The pulse's task list. Folded from the WHOLE log's task-tool events
  // (GET /task-events, fetched once and again whenever the tail shows a task
  // call newer than what we have) merged with the tail —
  // the tail alone stamps every old item with the window start.
  let todo = $state(null);
  let taskHistory = [];
  let taskHistoryTs = null; // ts of the newest task event in taskHistory
  let taskFetching = false;
  function refoldTodo(evs) {
    todo = foldTodos(mergeTaskEvents(taskHistory, evs));
  }
  async function syncTaskHistory(evs) {
    if (taskFetching) return;
    let newest = null;
    for (const e of evs) if (isTaskEvent(e) && e.ts && (!newest || e.ts > newest)) newest = e.ts;
    if (taskHistoryTs !== null && !(newest && newest > taskHistoryTs)) return;
    taskFetching = true;
    try {
      const r = await getTaskEvents(getPulse().id);
      if (!r.ok) return;
      taskHistory = r.value.events ?? [];
      taskHistoryTs = '';
      for (const e of taskHistory) if (e.ts && e.ts > taskHistoryTs) taskHistoryTs = e.ts;
      refoldTodo(loadedEvs);
    } finally {
      taskFetching = false;
    }
  }

  let meta = $state({ lastDoneTs: null, lastUsage: null, liveTool: null, lastTurnMs: null, lastThinkTok: null, lastFailed: false });

  // Scrollback: the pane renders a tail window of the event log. Hitting the
  // top grows the window and re-fetches, anchored so the view doesn't jump; a
  // dim "older ↑" hint
  // shows whenever more history exists above the window.
  let tailLimit = $state(100);
  let moreAbove = $state(false);
  let loadingOlder = false;

  // Events fetched while the user is reading back, not yet rendered. See the
  // hold branch in applyEvents() for why they wait.
  let held = null;
  let heldNew = $state(false);
  let shownSig = '';
  // Which fold mode produced what's on screen (see the fold-mode guard in
  // applyEvents). Plain let, not state: nothing renders from it, it only
  // short-circuits the no-change return, and the toggle's own effect below is
  // what triggers the re-render when it flips.
  let shownFoldMode = getFoldNoise();

  // Byte cursor into events.jsonl (the `offset` of the last response). Sent
  // back as `after` so a poll on a quiet pulse costs the daemon one stat()
  // instead of a re-read and re-parse of the whole tail. Reset to null
  // whenever the window itself changes (tailLimit grows, the pulse is
  // recreated) so the next poll asks for a full tail again.
  let cursor = null;

  // "Has the tail changed" probe: count plus the last event verbatim, since
  // that is the one that grows while a reply streams. Compare the whole event,
  // not its length — a sliding window can swap in a same-length event and a
  // length check silently calls that unchanged.
  function evSig(evs) {
    const last = evs[evs.length - 1];
    return evs.length + ':' + (last ? JSON.stringify(last) : '');
  }

  // Safety net for the streaming-handover ordering race (found in review,
  // 2026-09-21): the daemon pushes events BEFORE the null-partial in
  // the same live tick, so an events window can carry the durable copy of a
  // reply while the ghost is still up — render() then shows both, and the
  // unchanged-tail short-circuits (poll's `unchanged`, applyEvents' evSig) can
  // leave it there: the ghost is retired inside render() only via partialGone,
  // which the null-partial may set too late (or the push can be lost). So the
  // handover must not depend on the flag alone — every fresh event window gets
  // compared to the ghost.
  //
  // Comparison is content-based but ONE-DIRECTIONAL and anchored (glm spot
  // check #2 refuted the first bidirectional version): retire only when the
  // ghost's text STARTS WITH a durable reply's full text — i.e. the pane has
  // the finished block on screen and the ghost is still showing that same text
  // (equal, or a longer tail only where a provider normalized markdown after
  // the last delta). The other directions are false-positive machines: an
  // older short reply ("Done.") is a PREFIX of any stream that re-acks the
  // same way, so `text.includes(prefix)` killed live ghosts mid-stream, and a
  // longer old reply is CONTAINED in a replayed stream, so `ghost.includes(
  // text)` did the same. Those match OLD copies anywhere in history, not this
  // turn's block. This predicate matches only a complete block the ghost is
  // still duplicating — never a live prefix — so it can retire late by at most
  // the same margin as before, but never blink the ghost off mid-stream. A
  // window arriving with no ghost up is never a duplicate: that content is
  // exactly what render() shows, and render() already retired any ghost via
  // partialGone. (Predicate extracted to ghostnet.js.)

  // The 2s timer keeps firing as a fallback, but while the /live socket is up
  // the daemon pushes this pane's tail (applyEvents below) and the timer poll
  // is a no-op. Explicit polls (scrollback, just-sent) still fetch: they carry
  // scroll intent the push path does not know about.
  // An archived pulse never changes, but its history still loads once: opened
  // from Audit it showed "no messages yet". Scrollback still fetches.
  let archivedLoaded = false;
  async function poll(anchorTop = false, follow = false) {
    const pulse = getPulse();
    if (pulse.state === 'archived' && archivedLoaded && !anchorTop) return;
    if (!anchorTop && !follow && live.connected && pushed) return;
    const r = await getEvents(pulse.id, tailLimit, anchorTop || follow ? null : cursor);
    if (!r.ok) {
      setError(`events: ${r.error}`);
      return;
    }
    if (getPulse().state === 'archived') archivedLoaded = true;
    if (typeof r.value.offset === 'number') cursor = r.value.offset;
    // Nothing appended since the last poll — the daemon did not even read the
    // file. Same conclusion as the evSig short-circuit below, reached cheaper;
    // the anchorTop/follow callers above sent a null cursor so they never land
    // here and keep their scroll behaviour.
    if (r.value.unchanged === true) return;
    const chatEl = getChatEl();
    const keep = anchorTop && chatEl ? chatEl.scrollHeight - chatEl.scrollTop : 0;
    applyEvents(r.value.events ?? [], anchorTop, follow, keep);
    // Safety net: applyEvents can short-circuit (the tail already matches the
    // screen), and then only this net retires a ghost the fetched window now
    // holds a durable copy of.
    retireGhostIfDup(loadedEvs);
  }

  // True once a /live push has landed for this pane, so the timer poll only
  // stands down when pushes are actually flowing (not merely socket-open).
  let pushed = false;
  // Last applied event window. /live growth pushes are deltas (`delta: true`)
  // that append here; HTTP polls and the first snapshot replace it.
  let loadedEvs = [];

  function applyEvents(evs, anchorTop = false, follow = false, keep = 0, delta = false) {
    if (delta) {
      if (!evs.length) return;
      // Not a blind concat: the /live cursor is independent of the HTTP
      // follow poll, so a delta can repeat events that poll already loaded
      // (your sent message showed twice). See evmerge.js.
      evs = mergeDelta(loadedEvs, evs, tailLimit);
    }
    loadedEvs = evs;
    onEvents(evs); // the last-you strip rescans
    const sig = evSig(evs);
    // Keep the status rail current even while the chat itself is held during
    // scrollback: these facts come from the same loaded tail as buildItems.
    meta = turnMeta(evs);
    moreAbove = evs.length >= tailLimit;
    setError('');

    // Following is user intent, not a measurement of a possibly half-flushed
    // layout. Only scroll input releases it; sending explicitly resumes it.
    if (follow) setStuck(true);

    if (!getStuck() && !anchorTop) {
      // Reading back through history: leave the DOM completely alone and stash
      // the events instead. Restoring scrollTop is NOT enough — getEvents
      // returns a tail window, so a streaming reply pushes old events off the
      // TOP of it. The content above the viewport shrinks, and the same
      // scrollTop then points at different text: the page appears to creep
      // upward while you read. Not re-rendering is the only way the words
      // actually hold still.
      held = evs;
      heldNew = sig !== shownSig;
      return;
    }

    // Tail unchanged: buildItems would fold the same events into the same
    // items, re-run markdown for every reply, and hand Svelte a new array to
    // diff — all to arrive at exactly what is on screen. Most polls on a quiet
    // pulse land here. anchorTop (older history just loaded) and follow (you
    // just sent) still fall through: those callers want the scroll.
    //
    // evSig compares EVENTS, and the rendered rows are a function of events
    // *and* uiPrefs.foldNoise — so a quiet pane whose tail didn't change must
    // still re-render when the fold mode differs from what is on screen, or the
    // transcript keeps showing the pre-toggle fold until a reload (found by the
    // visual verifier 2026-09-14: the checkbox stored pulsar-fold-noise=0 and
    // nothing moved).
    if (sig === shownSig && !anchorTop && !follow && shownFoldMode === getFoldNoise()) return;

    render(evs, sig);
    // Older history was prepended: hold the previously-visible content still by
    // measuring from the bottom instead of the top.
    anchor(keep, !anchorTop || getStuck());
  }

  function render(evs, sig = evSig(evs)) {
    items = keyItems(getFoldNoise() ? foldNoise(buildItems(evs)) : buildItems(evs));
    refoldTodo(evs);
    syncTaskHistory(evs);
    shownSig = sig;
    shownFoldMode = getFoldNoise();
    held = null;
    heldNew = false;
    // Streaming handover: the partial vanished server-side (the finished block
    // superseded it) and the events that replace it just rendered — NOW the
    // ghost can go, with no flicker gap in between.
    if (partialGone) {
      ghostText = null;
      partialGone = false;
    }
    // The mirror case of the above: the durable block arrived in this window
    // while the ghost was still up (the null-partial push has not landed, or
    // was lost) — retire on content, not on the handover flag.
    retireGhostIfDup(evs);
    // Same handover for the thinking ghost: its block became a durable
    // reasoning event (a card in `items`), so the ghost goes only now.
    if (reasoningGone) {
      ghostReasoning = null;
      reasoningGone = false;
    }
    // Mirror case for the thinking ghost: the
    // runner emits the durable reasoning event BEFORE it clears the live block
    // (runner._flush_reasoning_block), and /live pushes events before reasoning
    // in the same tick — so this window can already carry the card while the
    // thinking ghost is still up. Same bug class as the reply ghost, same net:
    // content comparison, one-directional and anchored.
    retireReasoningIfDup(evs);
    // Live code card: the tool_call it was typing is in this window now.
    if (codeGone) {
      ghostCode = null;
      codeGone = false;
    }
    retireCodeIfDone(evs);
  }

  // Content net for the thinking ghost: the ghost shows the OPEN block, so it
  // is a duplicate exactly when a reasoning event's full text starts the ghost
  // (equal = the byte-for-byte handover; longer ghost tail = the rare flush
  // that captured text the block file did not show yet). The reverse direction
  // is the false positive the reply-ghost net already learned about: the live
  // block is always CONTAINED in the full-turn trace, so never match on that.
  function retireReasoningIfDup(evs) {
    if (ghostSupersededBy(evs, ghostReasoning, 'reasoning')) {
      ghostReasoning = null;
      reasoningGone = false;
    }
  }

  /** Catch up to the newest events and resume following the tail. */
  function catchUp() {
    setStuck(true);
    if (held) render(held);
    heldNew = false;
    anchor(0, true);
  }

  /** Scroll handler body: at the top of a pane that is not following, grow the window. */
  function growWindow() {
    const chatEl = getChatEl();
    if (!chatEl) return;
    if (getStuck() || loadingOlder || !moreAbove) return;
    if (chatEl.scrollTop < 60) {
      loadingOlder = true;
      tailLimit += 400;
      live.setTail(getPulse().id, tailLimit); // widen the pushed window too
      cursor = null; // wider window = a full tail, not a delta
      console.debug('[pane] scrollback: growing event window to %d', tailLimit);
      poll(true).finally(() => (loadingOlder = false));
    }
  }

  /** Widen the window to at least `limit` and refetch it (jump to an old message). */
  async function widenTo(limit) {
    tailLimit = Math.max(tailLimit, limit);
    live.setTail(getPulse().id, tailLimit);
    cursor = null;
    await poll(true);
  }

  // --- live streaming (per-pulse, settings.stream_partials) --------------- //
  // When on, the ghost bubble reads the runner's ephemeral partial.json.
  // /live pushes {type:"partial"} on mtime/size change; the 500ms HTTP poll
  // is only the fallback when the socket is down. Unset defaults ON for
  // local pi/omp vessels so the ghost appears ~300ms after the model emits
  // instead of waiting for a finished block (median 7s).
  let ghostText = $state(null);
  let partialGone = false; // partial cleared server-side; awaiting final event

  function applyPartial(text) {
    if (!isStreamOn() || getPulse().state !== 'running') return;
    if (text) {
      ghostText = text;
      partialGone = false;
      // Follow the growing reply exactly like a new event would — through
      // anchorChat, so the pin measures AFTER the ghost's DOM has flushed
      // (a microtask pin ran before Svelte's batch flush and landed short,
      // leaving the growing tail below the fold) and gets the verify frame.
      if (getStuck()) anchor(0, true);
    } else if (ghostText && !partialGone) {
      // Finished: the runner cleared the partial BEFORE emitting the final
      // event, so pull events now and let render() retire the ghost — dropping
      // it here would blank the text for up to a whole 2s poll cycle. Do not
      // flush held events while reading history: catchUp retires the ghost
      // along with the final block when the reader returns to the tail.
      partialGone = true;
      if (held && getStuck()) {
        render(held);
        anchor(0, true);
      } else {
        poll();
      }
      // Safety net: both pulls above can short-circuit (the tail already
      // matches the screen), and then the ghost would never be retired. If the
      // loaded window already carries the durable copy of what the ghost
      // streamed, drop it now instead of leaving the duplicate until next turn.
      retireGhostIfDup(loadedEvs);
    }
  }

  // A fresh event window that carries a durable copy of the ghost's streamed
  // text ends the ghost immediately — no waiting for the done event, the next
  // turn, or the null-partial push.
  function retireGhostIfDup(evs) {
    if (ghostSupersededBy(evs, ghostText)) {
      ghostText = null;
      partialGone = false;
    }
  }

  async function pollPartial() {
    if (live.connected) return; // /live is feeding applyPartial
    if (!isStreamOn() || getPulse().state !== 'running') return;
    const r = await getPartial(getPulse().id);
    if (!r.ok) return; // display-only: next tick retries
    applyPartial(r.value.partial?.text);
  }

  // --- inline reasoning ghost -------------------------------------------- //
  // While the pane is in streaming mode, the model's thinking shows IN the
  // chat, above the reply ghost — same live feed and same reasoning.json the
  // ReasoningPanel reads, so the two always agree. Clamped to the trace tail
  // so a long turn can't flood the pane; the full trace is one click away.
  // Gated on streamOn and reasoningEnabled (the per-pulse trace toggle).
  let ghostReasoning = $state(null);
  let reasoningEl = $state(null);
  let reasoningGone = false; // block ended server-side; awaiting its card

  // What the ghost shows: the OPEN thinking block only (`block`, runners
  // since 2026-09-14) — finished blocks are already cards in the chat. An
  // older runner sends no `block`; then the whole trace shows, as before.
  function ghostBlock(r) {
    if (!r) return null;
    return typeof r.block === 'string' ? r.block : r.text;
  }

  function reasoningFollows() {
    if (!reasoningEl) return true;
    return reasoningEl.scrollHeight - reasoningEl.scrollTop - reasoningEl.clientHeight < 40;
  }

  function applyReasoning(text) {
    if (!isStreamOn() || !isReasoningEnabled() || getPulse().state !== 'running') return;
    const next = typeof text === 'string' && text.length ? text : null;
    if (!next && ghostReasoning && !reasoningGone) {
      // The block ended: the runner wrote its reasoning event BEFORE
      // clearing the block, so pull events now and let render() swap the
      // ghost for its card — dropping it here would blank the thinking for
      // up to a whole poll cycle. Keep held events frozen for a reader;
      // catchUp performs their ghost-to-card handover at the tail.
      reasoningGone = true;
      if (held && getStuck()) {
        render(held);
        anchor(0, true);
      } else {
        poll();
      }
      // Same short-circuit safety net as the reply ghost: the card is already
      // in the loaded window (the runner wrote the reasoning event before
      // clearing the block), so retire now rather than wait for the next push.
      if (reasoningGone) retireReasoningIfDup(loadedEvs);
      return;
    }
    ghostReasoning = next;
    if (getStuck()) anchor(0, true);
    if (next) reasoningGone = false;
    // Follow the growing trace inside its clamped box — same rule as the
    // panel: only when already parked at its bottom, so reading back is
    // never yanked.
    if (ghostReasoning && reasoningFollows()) {
      queueMicrotask(() => {
        if (reasoningEl) reasoningEl.scrollTop = reasoningEl.scrollHeight;
      });
    }
  }

  async function pollReasoning() {
    if (live.connected) return; // /live is feeding applyReasoning
    if (!isStreamOn() || !isReasoningEnabled() || getPulse().state !== 'running') return;
    const r = await getReasoning(getPulse().id);
    if (!r.ok) return; // display-only: next tick retries
    applyReasoning(ghostBlock(r.value.reasoning));
  }

  // --- live code card ---------------------------------------------------- //
  // The argument JSON of the tool call being generated (the runner's
  // code.json), shown as it streams — a Write's file, an Edit's diff, a
  // Bash command. The runner clears code.json just before it writes the
  // tool_call, so on the clearing push the card waits for that tool_call to
  // render (same handover as the reply ghost), with a short timer as the
  // net if the pull short-circuits. Gated on the per-pulse Live-code toggle.
  let ghostCode = $state(null);
  let codeEl = $state(null);
  let codeGone = false;
  let codeGoneTimer = null;

  function retireCodeIfDone(evs) {
    const id = ghostCode?.id;
    if (!id) return;
    for (let i = evs.length - 1, seen = 0; i >= 0 && seen < 5; i--) {
      if (evs[i].type !== 'tool_call') continue;
      seen++;
      if (evs[i].id === id) {
        ghostCode = null;
        codeGone = false;
        return;
      }
    }
  }

  function applyCode(code) {
    if (!isCodeEnabled() || getPulse().state !== 'running') {
      ghostCode = null;
      return;
    }
    if (code && typeof code.text === 'string') {
      clearTimeout(codeGoneTimer);
      const follow = !codeEl || codeEl.scrollHeight - codeEl.scrollTop - codeEl.clientHeight < 40;
      ghostCode = code;
      codeGone = false;
      if (getStuck()) anchor(0, true);
      if (follow) {
        queueMicrotask(() => {
          if (codeEl) codeEl.scrollTop = codeEl.scrollHeight;
        });
      }
      return;
    }
    if (ghostCode && !codeGone) {
      codeGone = true;
      if (held && getStuck()) {
        render(held);
        anchor(0, true);
      } else {
        poll();
      }
      retireCodeIfDone(loadedEvs);
      clearTimeout(codeGoneTimer);
      codeGoneTimer = setTimeout(() => {
        if (codeGone) {
          ghostCode = null;
          codeGone = false;
        }
      }, 2500);
    }
  }

  async function pollCode() {
    if (live.connected) return; // /live is feeding applyCode
    if (!isCodeEnabled() || getPulse().state !== 'running') return;
    const r = await getCode(getPulse().id);
    if (!r.ok) return; // display-only: next tick retries
    applyCode(r.value.code);
  }

  // Per-visible-pulse event poll every 2s. Elapsed chips read the shared
  // clock store (1s while this pane is on screen, 5s otherwise).
  //
  // A STOPPED pulse gets the first fetch and no timers: its history cannot
  // change and its status has nothing left to tick, so idle panes left open
  // do not waste event requests re-reading a frozen log. The effect re-runs
  // when `stopped` flips, so the timers
  // come back the moment it is running again.
  //
  // The three getter reads are the ONLY dependencies — the fetches are untracked
  // so the pulse prop's 3s identity churn (and tailLimit, which its own caller
  // already re-polls for) can't restart the timers underneath us.
  $effect(() => {
    const id = getPulseId();
    const frozen = isStopped();
    // $derived boolean: value-memoized, so this only restarts the timers when
    // the toggle actually flips, not on the 3s pulse-object churn.
    const streaming = isStreamOn();
    untrack(() => {
      ghostText = null;
      ghostReasoning = null;
      ghostCode = null;
      partialGone = false;
      reasoningGone = false;
      codeGone = false;
      pushed = false;
      loadedEvs = [];
      // The cursor must go too: it is a byte offset into the OLD window, and
      // the endpoint answers it with just the appended delta — applyEvents
      // would then REPLACE loadedEvs with that handful of events and the
      // transcript collapses to the last few rows (the "randomly jumps back"
      // report 2026-09-14; the flip fires at every turn boundary).
      cursor = null;
      onReset();
      poll();
    });
    if (frozen) return;
    const ev = setInterval(poll, 2000);
    const offLive = live.watch(
      id,
      untrack(() => tailLimit),
      (msg) => {
        if (getPulse().state === 'archived') return;
        pushed = true;
        if (typeof msg.offset === 'number') cursor = msg.offset;
        applyEvents(msg.events ?? [], false, false, 0, msg.delta === true);
      },
      // Always registered (never undefined): the HTTP fallback below stands
      // down whenever the socket is up, so if this callback were absent for a
      // stream-off pulse, a socket reconnect that outlives the last live
      // effect re-run would feed partials to nobody — the ghost would stay
      // dead until the stream toggle flips. applyPartial gates on streamOn.
      (msg) => applyPartial(msg.text),
      (msg) => applyReasoning(ghostBlock(msg)),
      (msg) => applyCode(msg.code)
    );
    return () => {
      offLive();
      clearInterval(ev);
      clearTimeout(codeGoneTimer);
    };
  });

  // A pulse between turns streams nothing: no reply, trace or tool call is
  // being written. Same busy test as pulseStatus (render.js); no live block
  // reads as unknown, so it keeps polling.
  function isQuiet() {
    const lv = getPulse().live;
    return !!lv && lv.socket_state !== 'busy' && lv.heartbeat?.state !== 'busy';
  }

  // HTTP partial poll is fallback only: while /live is connected the daemon
  // pushes partial.json changes. Track `live.connected` so a drop starts the
  // 500ms timer without resetting the ghost (that's the other effect).
  // It also tracks `streamOn` so a mid-turn toggle ON starts feeding the
  // ghost immediately when the socket is down, not at the next effect
  // re-mount. pushPartial is a no-op unless streamOn is true.
  //
  // A quiet pulse gets one round (it retires the ghosts at the busy-to-idle
  // edge), then no more until it is busy again. Every running pane polled
  // three files every 500ms while the socket was down, idle or not: 360
  // requests a minute per pane, and the iPhone's socket drops ~175 times a
  // day (2026-11-02). The check is inside the timer: the pulse prop changes
  // every 3s, and an effect dependency on it would restart the timer.
  $effect(() => {
    const streaming = isStreamOn();
    const coding = isCodeEnabled();
    const frozen = isStopped();
    const socketUp = live.connected;
    if (frozen || socketUp || (!streaming && !coding)) return;
    let quietRoundDone = false;
    const pp = setInterval(() => {
      if (!isQuiet()) quietRoundDone = false;
      else if (quietRoundDone) return;
      else quietRoundDone = true;
      pollPartial();
      pollReasoning();
      pollCode();
    }, 500);
    return () => clearInterval(pp);
  });

  // "fold watch chatter" flipped: every existing pane must re-fold live, not
  // on reload. Reading the pref here is what subscribes this effect to it;
  // untracked work avoids a re-render
  // loop, since render() itself writes `items`.
  //
  // Where the reader is decides what "live" means:
  //   - following the tail: re-render and stay at the newest.
  //   - reading back through history: re-render in place, measured FROM THE
  //     BOTTOM, exactly like applyEvents' anchorTop path. The fold changes the
  //     height above the viewport, so a raw scrollTop would point at different
  //     text afterwards. Going to the bottom on its own would yank a scrolled-up
  //     reader away from their line.
  //   - events HELD (a live stream pushed old events off the window while they
  //     read): don't touch the DOM at all — light the existing new-content arrow
  //     and let catchUp() re-fold when they return to the tail.
  $effect(() => {
    const mode = getFoldNoise();
    untrack(() => {
      if (!loadedEvs.length || mode === shownFoldMode) return;
      if (held) {
        heldNew = true;
        return;
      }
      const chatEl = getChatEl();
      const keep = chatEl ? chatEl.scrollHeight - chatEl.scrollTop : 0;
      const wasStuck = getStuck();
      render(loadedEvs);
      anchor(keep, wasStuck);
    });
  });

  return {
    get items() { return items; },
    get todo() { return todo; },
    get meta() { return meta; },
    get moreAbove() { return moreAbove; },
    get heldNew() { return heldNew; },
    get ghostText() { return ghostText; },
    get ghostReasoning() { return ghostReasoning; },
    get ghostCode() { return ghostCode; },
    /** `bind:this` target of the live code box. */
    get codeEl() { return codeEl; },
    set codeEl(el) { codeEl = el; },
    get tailLimit() { return tailLimit; },
    /** `bind:this` target of the inline reasoning box. */
    get reasoningEl() { return reasoningEl; },
    set reasoningEl(el) { reasoningEl = el; },
    poll,
    catchUp,
    growWindow,
    widenTo
  };
}
