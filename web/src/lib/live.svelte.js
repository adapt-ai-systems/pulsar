/**
 * The /live push channel — replaces the roster/grids/events polling loops
 * when the daemon socket is up, and falls back to them when it is not.
 *
 * Usage:
 *   import { live } from '$lib/live.svelte.js';
 *   live.connect();                        // once, from the page
 *   live.roster / live.grids               // $state, same shapes as the GETs
 *   const off = live.watch(pulseId, tail, onEvents, onPartial, onReasoning);
 *   live.connected                         // fall back to polling when false
 *
 * Reconnects with backoff (1s → 15s). Subscriptions are re-sent on every
 * (re)connect, so a pane never has to know the socket bounced.
 */

const RECONNECT_MIN_MS = 1000;
const RECONNECT_MAX_MS = 15000;
// The daemon pushes the roster every 3s: two frames plus slack.
const ROSTER_FRESH_MS = 7000;

function socketUrl() {
  const proto = location.protocol === 'https:' ? 'wss:' : 'ws:';
  return `${proto}//${location.host}/api/live`;
}

function createLive() {
  let connected = $state(false);
  let roster = $state(null);   // null until the first push
  let grids = $state(null);
  let ws = null;
  let backoff = RECONNECT_MIN_MS;
  let timer = null;
  let wanted = false;
  /** @type {Map<string, {tail: number, cbs: Set<Function>, partialCbs: Set<Function>, reasoningCbs: Set<Function>, codeCbs: Set<Function>}>} */
  const watches = new Map();
  const feedListeners = new Map();
  let lastMsgAt = 0;
  let rosterAt = null; // last roster frame's arrival; null while the socket is down
  let watchdog = null;
  let wakeListenersActive = false;
  // 15s fired whenever a roster tick stalled behind the single worker
  // (measured socket lifetime 14–18s). 45s sits in the 30–60s band so a
  // healthy socket with 3s roster pushes (and server WS pings) is never
  // killed for a brief stall.
  const SILENT_MS = 45000;

  function stopWatchdog() {
    clearInterval(watchdog);
    watchdog = null;
  }
  function wake() {
    // Network restored: bounce the socket so we resubscribe. Tab focus
    // must NOT close a healthy socket — that was the 1s reconnect storm
    // on visibilitychange.
    if (typeof document !== 'undefined' && document.visibilityState !== 'visible') return;
    try { ws?.close(); } catch {}
  }
  function addWakeListeners() {
    if (wakeListenersActive || typeof window === 'undefined') return;
    window.addEventListener('online', wake);
    wakeListenersActive = true;
  }
  function removeWakeListeners() {
    if (!wakeListenersActive || typeof window === 'undefined') return;
    window.removeEventListener('online', wake);
    wakeListenersActive = false;
  }

  function sendSubscribe() {
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    const pulses = [...watches.keys()];
    const tail = Math.max(100, ...[...watches.values()].map((w) => w.tail));
    ws.send(JSON.stringify({ type: 'subscribe', pulses, tail }));
  }

  function open() {
    if (!wanted || ws) return;
    let sock;
    try {
      sock = new WebSocket(socketUrl());
    } catch (e) {
      schedule();
      return;
    }
    ws = sock;
    sock.onopen = () => {
      lastMsgAt = Date.now();
      stopWatchdog();
      watchdog = setInterval(() => {
        if (ws === sock && Date.now() - lastMsgAt > SILENT_MS) {
          try { sock.close(); } catch {}
        }
      }, 5000);
      connected = true;
      backoff = RECONNECT_MIN_MS;
      sendSubscribe();
    };
    sock.onmessage = (ev) => {
      lastMsgAt = Date.now();
      let msg;
      try {
        msg = JSON.parse(ev.data);
      } catch {
        return;
      }
      if (msg.type === 'roster') {
        roster = msg.pulses ?? [];
        grids = msg.grids ?? [];
        rosterAt = Date.now();
      } else if (msg.type === 'metrics_sample') {
        for (const cb of feedListeners.get(msg.type) ?? []) cb(msg);
      } else if (msg.type === 'events') {
        const w = watches.get(msg.pulse_id);
        if (w) for (const cb of w.cbs) cb(msg);
      } else if (msg.type === 'partial') {
        const w = watches.get(msg.pulse_id);
        if (w) for (const cb of w.partialCbs) cb(msg);
      } else if (msg.type === 'reasoning') {
        const w = watches.get(msg.pulse_id);
        if (w) for (const cb of w.reasoningCbs) cb(msg);
      } else if (msg.type === 'code') {
        const w = watches.get(msg.pulse_id);
        if (w) for (const cb of w.codeCbs) cb(msg);
      }
    };
    sock.onclose = () => {
      if (ws === sock) {
        stopWatchdog();
        ws = null;
      }
      connected = false;
      rosterAt = null;
      schedule();
    };
    sock.onerror = () => sock.close();
  }

  function schedule() {
    if (!wanted || timer) return;
    timer = setTimeout(() => {
      timer = null;
      open();
    }, backoff);
    backoff = Math.min(RECONNECT_MAX_MS, backoff * 2);
  }

  return {
    get connected() {
      return connected;
    },
    get roster() {
      return roster;
    },
    get grids() {
      return grids;
    },
    /**
     * True while roster frames keep arriving, so a roster GET would only
     * fetch the same list again. Reads no $state: an effect that calls it
     * gains no dependency.
     */
    rosterFresh(maxAgeMs = ROSTER_FRESH_MS) {
      return rosterAt !== null && Date.now() - rosterAt < maxAgeMs;
    },
    /** Subscribe to lightweight feed invalidations; returns cleanup. */
    onFeed(type, cb) {
      if (!feedListeners.has(type)) feedListeners.set(type, new Set());
      const listeners = feedListeners.get(type);
      listeners.add(cb);
      return () => {
        listeners.delete(cb);
        if (!listeners.size) feedListeners.delete(type);
      };
    },
    connect() {
      wanted = true;
      addWakeListeners();
      open();
    },
    disconnect() {
      wanted = false;
      stopWatchdog();
      removeWakeListeners();
      clearTimeout(timer);
      timer = null;
      const s = ws;
      ws = null;
      s?.close();
      connected = false;
      rosterAt = null;
    },
    /**
     * Receive events (and optional partial/reasoning) pushes for a pulse.
     * Returns an unsubscribe function.
     * @param {string} pulseId
     * @param {number} tail
     * @param {(payload: {events: any[], offset: number, delta?: boolean}) => void} cb
     * @param {(payload: {text: string|null, pulse_id: string}) => void} [partialCb]
     * @param {(payload: {text: string|null, pulse_id: string}) => void} [reasoningCb]
     * @param {(payload: {code: any, pulse_id: string}) => void} [codeCb]
     */
    watch(pulseId, tail, cb, partialCb, reasoningCb, codeCb) {
      let w = watches.get(pulseId);
      if (!w) {
        w = { tail, cbs: new Set(), partialCbs: new Set(), reasoningCbs: new Set(), codeCbs: new Set() };
        watches.set(pulseId, w);
      }
      w.cbs.add(cb);
      if (partialCb) w.partialCbs.add(partialCb);
      if (reasoningCb) w.reasoningCbs.add(reasoningCb);
      if (codeCb) w.codeCbs.add(codeCb);
      if (tail > w.tail) w.tail = tail;
      sendSubscribe();
      return () => {
        const cur = watches.get(pulseId);
        if (!cur) return;
        cur.cbs.delete(cb);
        if (partialCb) cur.partialCbs.delete(partialCb);
        if (reasoningCb) cur.reasoningCbs.delete(reasoningCb);
        if (codeCb) cur.codeCbs.delete(codeCb);
        if (cur.cbs.size === 0 && cur.partialCbs.size === 0 && cur.reasoningCbs.size === 0
            && cur.codeCbs.size === 0) {
          watches.delete(pulseId);
        }
        sendSubscribe();
      };
    },
    /** A pane loaded more history: widen the tail for that pulse. */
    setTail(pulseId, tail) {
      const w = watches.get(pulseId);
      if (w && tail !== w.tail) {
        w.tail = tail;
        sendSubscribe();
      }
    }
  };
}

export const live = createLive();
