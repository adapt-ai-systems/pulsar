/**
 * Pulsar daemon API client.
 *
 * Wraps the daemon's `{ok, value}` / `{ok:false, error}` envelope. Every call
 * returns a discriminated result `{ok: true, value}` or `{ok: false, error}` so
 * callers can surface failures in the UI instead of silently blanking.
 *
 * Standing logging rule: every failure path console.logs with context
 * (method, path, status/error) — no request fails silently.
 *
 * In dev, requests go through the Vite proxy at `/api` -> http://127.0.0.1:8726.
 */

import {
  streamPartialsOn,
  reasoningOn,
  codeOn
} from './pulse-settings.js';
import {
  DEVICE_PREFIX,
  getDeviceSetting,
  putDeviceSetting
} from './device.js';

export {
  streamPartialsOn,
  reasoningOn,
  codeOn,
  DEVICE_PREFIX,
  getDeviceSetting,
  putDeviceSetting
};

const BASE = '/api';

const P = (id, suffix = '') => `/pulses/${encodeURIComponent(id)}${suffix}`;

function withQuery(path, params = {}) {
  const q = new URLSearchParams();
  for (const [k, v] of Object.entries(params)) {
    if (v === undefined || v === null || v === '') continue;
    q.set(k, String(v));
  }
  const s = q.toString();
  return s ? `${path}?${s}` : path;
}

/**
 * @typedef {Object} Pulse
 * @property {string} id
 * @property {string} title
 * @property {string} model
 * @property {string|null} vessel
 * @property {string|null} grid_id
 * @property {string} state           running|stopped|archived
 * @property {number|null} handoff_tokens
 * @property {string} created_at
 * @property {string} updated_at
 * @property {string|null} archived_at
 * @property {Object} settings
 * @property {Object|null} live        live runtime info (socket_state, etc.) or null
 */

/**
 * @template T
 * @typedef {{ok: true, value: T} | {ok: false, error: string, code?: string}} Result
 */

/**
 * Low-level HTTP transport with timing and safe JSON parsing.
 * @param {string} method
 * @param {string} path
 * @param {{body?: any, headers?: Record<string, string>, raw?: boolean, parseJsonUnconditionally?: boolean, log?: boolean, logMethod?: string, signal?: AbortSignal}} [init]
 * @returns {Promise<{ok?: undefined, res: Response, json: any, dur: number} | {ok: false, error: string, res?: Response, dur?: number}>}
 */
async function send(method, path, init = {}) {
  const {
    body,
    headers,
    raw = false,
    parseJsonUnconditionally = false,
    log = true,
    logMethod = method,
    signal
  } = init;
  const url = BASE + path;
  const started = performance.now();
  let res;
  try {
    res = await fetch(url, { method, headers, body, signal });
  } catch (e) {
    const error = `network error: ${e?.message ?? e}`;
    if (log) console.log('[pulsar-api] FAIL', logMethod, path, error);
    return { ok: false, error };
  }

  const dur = Math.round(performance.now() - started);
  if (raw) {
    return { res, json: null, dur };
  }

  // Check status / body BEFORE parsing unless unconditional JSON parsing is
  // requested. An empty 2xx (204, or a bare 200 with no body) is a success
  // with nothing to return — parsing first reported it as "bad JSON". An HTTP
  // error is an HTTP error even when the body is not JSON (a gateway's HTML
  // 502) — report the status, not a parse failure.
  let json = null;
  if (
    parseJsonUnconditionally ||
    (res.status !== 204 && res.headers.get('content-type')?.includes('json'))
  ) {
    try {
      json = await res.json();
    } catch (e) {
      const error = parseJsonUnconditionally
        ? `bad JSON (${res.status}): ${e?.message ?? e}`
        : `bad JSON (${res.status}) after ${dur}ms: ${e?.message ?? e}`;
      if (log) console.log('[pulsar-api] FAIL', logMethod, path, error);
      return { ok: false, error, res, dur };
    }
  }

  return { res, json, dur };
}

/**
 * Core fetch wrapper. Never throws for HTTP/envelope errors — returns a Result.
 * @template T
 * @param {string} method
 * @param {string} path
 * @param {Object} [body]
 * @returns {Promise<Result<T>>}
 */
async function call(method, path, body) {
  const t = await send(method, path, {
    // `body ?` would drop "", null, 0, false — all valid JSON values.
    headers: body === undefined ? undefined : { 'content-type': 'application/json' },
    body: body === undefined ? undefined : JSON.stringify(body)
  });
  if (t.ok === false) return { ok: false, error: t.error };
  const { res, json, dur } = t;

  const code = typeof json?.error?.code === 'string' ? json.error.code : undefined;

  if (!res.ok) {
    // Daemon ApiError speaks {ok:false, error:{code,message}}. Surface the
    // plain message instead of a JSON blob and preserve `code`.
    if (json && json.ok === false && json.error && typeof json.error === 'object') {
      const apiMsg =
        (typeof json.error.message === 'string' && json.error.message) ||
        (typeof json.error.code === 'string' && json.error.code) ||
        null;
      if (apiMsg !== null) {
        console.log('[pulsar-api] FAIL', method, path, apiMsg, `(${dur}ms)`);
        return code ? { ok: false, error: apiMsg, code } : { ok: false, error: apiMsg };
      }
    }
    // FastAPI validation / HTTP errors — try to pull a message out.
    const detail = json?.detail ?? json?.error ?? res.statusText;
    const error = `HTTP ${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`;
    console.log('[pulsar-api] FAIL', method, path, error, `(${dur}ms)`);
    return code ? { ok: false, error, code } : { ok: false, error };
  }

  // Daemon envelope: {ok, value} or {ok:false, error}.
  if (json && typeof json === 'object' && 'ok' in json) {
    if (json.ok) {
      // Most daemon endpoints use `value`; optional discovery endpoints use
      // `data` so null can be their explicit, non-error answer.
      return { ok: true, value: 'value' in json ? json.value : json.data };
    }
    const rawErr = json.error;
    const error =
      typeof rawErr === 'string'
        ? rawErr
        : typeof rawErr?.message === 'string' && rawErr.message
          ? rawErr.message
          : (code ?? 'daemon reported ok:false with no error');
    console.log('[pulsar-api] FAIL', method, path, error, `(${dur}ms)`);
    return code ? { ok: false, error, code } : { ok: false, error };
  }

  // Endpoint returned a bare object (e.g. /healthz shape variations) — pass through.
  return { ok: true, value: json };
}

/** @returns {Promise<Result<Pulse[]>>} */
export function listPulses(opts = {}) {
  return call('GET', withQuery('/pulses', {
    grid_id: opts.grid_id || undefined,
    state: opts.state || undefined,
    include_archived: opts.include_archived ? 'true' : undefined,
    // archived rows carry only settings.mode
    slim: opts.slim ? 'true' : undefined
  }));
}

/**
 * Vendor usage-limit snapshots for the FUEL rail.
 * @returns {Promise<Result<{vendors: {claude: Object|null, codex: Object|null, grok: Object|null}}>>}
 */
export function getUsage() {
  return call('GET', '/usage');
}

/**
 * Tail of a pulse's events, with a byte cursor.
 *
 * Pass `after` — the `offset` from the previous response — and the daemon
 * answers `unchanged: true` (without reading the file) when nothing was
 * appended since, or just the appended delta otherwise. `offset` is always
 * there to store for the next poll.
 * @returns {Promise<Result<{events: Array<Object>, count: number, offset: number, unchanged?: boolean}>>}
 */
export function getEvents(id, tail = 100, after = null, opts = {}) {
  const q = [`tail=${tail}`];
  if (after !== null && after !== undefined) q.push(`after=${after}`);
  if (opts.eventId) q.push(`event_id=${encodeURIComponent(opts.eventId)}`);
  if (opts.full) q.push('full=1');
  return call('GET', P(id, `/events?${q.join('&')}`));
}

/** Task-tool events from the whole log (TODO card timestamps). */
export function getTaskEvents(id) {
  return call('GET', P(id, '/task-events'));
}

/**
 * The model registry with live slot status (daemon GET /models; behind a
 * reverse proxy it serves the same shape at /api/models).
 * Slot entries carry {slot, status:{live, served_id, checked_at, error},
 * label_live}; other entries are the static registry.
 */
export function getModels() {
  return call('GET', '/models');
}

/** @returns {Promise<Result<Pulse>>} */
export function createPulse(body) {
  // body: {title, model, vessel?, grid_id?, memory_max?, handoff_tokens?,
  //        loop_prompt?, loop_minutes?}
  // loop_prompt + loop_minutes must be sent together (atomic create-with-loop);
  // the daemon 400s if only one is present.
  return call('POST', '/pulses', body);
}

/**
 * Post a message into a pulse's inbox. Pass fromPulseId to send AS another
 * pulse: the daemon names the sender in the delivered text and
 * wakes a stopped target; any reply is the target's own message back.
 * @param {string} id  the receiving pulse
 * @param {string} text
 * @param {string} [fromPulseId]  sending pulse id (pulse-to-pulse only)
 * @param {{text: string, ts?: string}|null} [replyTo]
 * @returns {Promise<Result<any>>}
 */
export function sendMessage(id, text, fromPulseId, replyTo = null) {
  /** @type {{text: string, from_pulse_id?: string, reply_to?: {text: string, ts?: string}}} */
  const body = fromPulseId ? { text, from_pulse_id: fromPulseId } : { text };
  // Reply button: the earlier message the user is answering, quoted to the pulse.
  if (replyTo) body.reply_to = replyTo;
  return call('POST', P(id, '/messages'), body);
}

/**
 * Send a message with attachments (pasted screenshots / attached files) as
 * multipart form data. The daemon saves the files under the pulse home and
 * appends their paths to the delivered text so the agent can Read them.
 * @param {string} id
 * @param {string} text          may be empty when files are present
 * @param {Array<{blob: Blob, name: string}>} files
 * @param {{text: string, ts?: string} | null} [replyTo]  message being replied to
 * @returns {Promise<Result<any>>}
 */
export async function sendMessageMultipart(id, text, files, replyTo = null) {
  const path = P(id, '/messages');
  const fd = new FormData();
  fd.append('text', text);
  if (replyTo) fd.append('reply_to', JSON.stringify(replyTo));
  for (const f of files) fd.append('file', f.blob, f.name);
  const t = await send('POST', path, {
    body: fd,
    parseJsonUnconditionally: true,
    logMethod: 'POST(multipart)',
    authRedirect: false
  });
  if (t.ok === false) return { ok: false, error: t.error };
  const { res, json } = t;
  if (!res.ok || json?.ok === false) {
    const detail = json?.error?.message ?? json?.detail ?? res.statusText;
    const error = `HTTP ${res.status}: ${typeof detail === 'string' ? detail : JSON.stringify(detail)}`;
    console.log('[pulsar-api] FAIL POST(multipart)', path, error);
    return { ok: false, error };
  }
  return { ok: true, value: json.value ?? json };
}

/**
 * Mid-session model switch. `vessel` is derived from the model
 * choice by the caller (models.js). Same vessel = respawn with resume; a vessel
 * change triggers a cross-CLI context handoff on the daemon.
 *
 * `effort` semantics: undefined = keep the stored level, '' = clear to the CLI
 * default, a value = set (daemon-validated per vessel). An effort-only change
 * rides the same respawn-with-resume path, so it applies immediately.
 * @returns {Promise<Result<Pulse>>}
 */
export function switchModel(id, model, vessel, effort, force = false) {
  const body = { model };
  if (vessel) body.vessel = vessel;
  if (effort !== undefined) body.effort = effort;
  if (force) body.force = true;
  return call('PATCH', P(id, '/model'), body);
}

/**
 * Set the auto-handoff threshold in tokens. Pass null to clear it (falls back
 * to the model-family default on the daemon side).
 * @param {string} id
 * @param {number|null} handoff_tokens
 * @returns {Promise<Result<Pulse>>}
 */
export function setHandoff(id, handoff_tokens) {
  return call('PATCH', P(id, '/handoff'), { handoff_tokens });
}

/**
 * Set (or clear) the pulse's backup model. Config
 * only.
 *
 * `model` names a model = enabled, `model: null` = disabled. The vessel is
 * DERIVED by the daemon from the model, so it is never sent. `effort` is
 * optional ('' / omitted = the CLI default), validated against the backup
 * model's vessel.
 * @param {string} id
 * @param {{model: string|null, effort?: string}} body
 * @returns {Promise<Result<Pulse>>}
 */
export function setBackup(id, body) {
  return call('PATCH', P(id, '/backup'), body);
}

/**
 * Per-pulse live-streaming toggle (settings.stream_partials). Purely a UI
 * gate — the runner writes partials regardless, so no respawn needed.
 * @param {string} id
 * @param {boolean} enabled
 * @returns {Promise<Result<Pulse>>}
 */
export function setStreamPartials(id, enabled) {
  return call('PATCH', P(id, '/stream'), { enabled });
}

/**
 * Per-pulse reasoning-trace toggle (settings.stream_reasoning). Purely a UI
 * gate — the runner writes reasoning.json regardless, so no respawn needed.
 * @param {string} id
 * @param {boolean} enabled
 * @returns {Promise<Result<Pulse>>}
 */
export function setStreamReasoning(id, enabled) {
  return call('PATCH', P(id, '/reasoning'), { enabled });
}

/**
 * Per-pulse live-code toggle (settings.stream_code). Purely a UI gate — the
 * runner writes code.json regardless, so no respawn needed.
 * @param {string} id
 * @param {boolean} enabled
 * @returns {Promise<Result<Pulse>>}
 */
export function setStreamCode(id, enabled) {
  return call('PATCH', P(id, '/code'), { enabled });
}

/**
 * The in-progress reply while a turn streams: {ts, text} or null. Polled fast
 * (~500ms) by panes whose pulse has streaming enabled — cheap file read.
 * @param {string} id
 * @returns {Promise<Result<{partial: {ts: string, text: string} | null}>>}
 */
export function getPartial(id) {
  return call('GET', P(id, '/partial'));
}

/**
 * The current turn's reasoning trace: {ts, text} or null. The reasoning panel
 * fast-polls this while the live socket is down (cheap file read). Unlike the
 * partial it survives the turn boundary, so "last turn" stays readable until
 * the next turn starts.
 * @param {string} id
 * @returns {Promise<Result<{reasoning: {ts: string, text: string} | null}>>}
 */
export function getReasoning(id) {
  return call('GET', P(id, '/reasoning'));
}

/**
 * The tool call being generated right now: {ts, id, name, text, truncated}
 * or null (`text` is its argument JSON so far). Polled only while the live
 * socket is down.
 * @param {string} id
 * @returns {Promise<Result<{code: {ts: string, id: string|null, name: string|null, text: string, truncated: boolean} | null}>>}
 */
export function getCode(id) {
  return call('GET', P(id, '/code'));
}

/** The pane's bookmark button: first press sets a bookmark; each later press
 *  asks the pulse to summarize everything since the last one and sets a new
 *  bookmark. Value: {bookmark:{at,offset}, summary:null|{since,digest,events,queued}, pulse}.
 *  @returns {Promise<Result<any>>} */
export function bookmarkPulse(id) {
  return call('POST', P(id, '/bookmark'));
}

/** Double tap on 🔖: clear the bookmark, no summary. @returns {Promise<Result<any>>} */
export function clearBookmark(id) {
  return call('DELETE', P(id, '/bookmark'));
}

/** @returns {Promise<Result<any>>} */
export function pausePulse(id) {
  return call('POST', P(id, '/pause'));
}

/**
 * Abort the pulse's in-flight turn WITHOUT stopping the pulse (double-Enter).
 * Only vessels with a real turn interrupt honor it (codex/DS4); the runner
 * answers not_interruptible for the rest and noop when idle.
 * @returns {Promise<Result<{interrupted: boolean}>>}
 */
export function interruptPulse(id) {
  return call('POST', P(id, '/interrupt'));
}

/** @returns {Promise<Result<any>>} */
export function restorePulse(id) {
  return call('POST', P(id, '/restore'));
}

/**
 * Fresh-context restart: the daemon stops the runner and respawns a brand-new
 * CLI session (no resume, no handoff seed). The pulse keeps its title, grid,
 * loops and diet — skills re-materialize on spawn — but the conversation
 * context is gone. For pulses reused across one-off tasks.
 * @returns {Promise<Result<Pulse>>}
 */
export function resetPulse(id) {
  return call('POST', P(id, '/reset'));
}

/** Manually run the context-handoff engine for a pulse: the live session
 * writes its handoff prose and a fresh session continues with it. Mirrors
 * reset's two-step confirm (arm -> confirm), but carries context over.
 * @returns {Promise<Result<{started:boolean}>>} */
export function handoffPulse(id, { force = false } = {}) {
  // force: the old model is dead — the daemon seeds a successor with the last
  // ~100k tokens of transcript + the transcript path instead of asking it.
  return call('POST', P(id, '/handoff'), force ? { force: true } : undefined);
}

/** Branch the conversation into a NEW pulse (same history up to now).
 * @returns {Promise<Result<Pulse>>} the freshly created fork */
export function forkPulse(id) {
  return call('POST', P(id, '/fork'), {});
}

/**
 * Archive a pulse. One that still owns live watches/schedules answers 409
 * `monitoring_choice_required` until `monitoring` is given: 'keep' switches
 * it to watching mode instead (NOT archived), 'stop' closes the watches,
 * disables the schedules, then archives (Watchers, 2026-09-07).
 * @param {string} id
 * @param {{monitoring?: 'keep'|'stop'}} [opts]
 * @returns {Promise<Result<any>>}
 */
export function archivePulse(id, opts = {}) {
  const body = opts.monitoring ? { monitoring: opts.monitoring } : undefined;
  return call('POST', P(id, '/archive'), body);
}

/**
 * True when archivePulse failed with 409 monitoring_choice_required because
 * the pulse still owns live condition watches or enabled schedules.
 * @param {{ok?: boolean, code?: string, error?: string}|null|undefined} r
 * @returns {boolean}
 */
export function isMonitoringChoice(r) {
  return (
    r?.ok === false &&
    (r.code === 'monitoring_choice_required' ||
      String(r.error ?? '').includes('monitoring_choice_required'))
  );
}

/**
 * Set or clear the pulse's organizational mode. 'watching' = finished
 * project that sleeps between watch/schedule wakes; 'paused' = set aside,
 * coming back later. Both leave the workspace grid for the Watchers shelf.
 * @param {string} id
 * @param {'watching'|'paused'|null} mode
 * @returns {Promise<Result<any>>}
 */
export function setPulseMode(id, mode) {
  return call('PATCH', P(id, '/mode'), { mode });
}

/**
 * @typedef {Object} Schedule
 * @property {string} id
 * @property {string} pulse_id
 * @property {string} kind             loop|once|daily
 * @property {string} prompt
 * @property {number|null} interval_minutes
 * @property {string|null} at_iso
 * @property {string|null} cron_hhmm
 * @property {string|null} days        mon..sun csv
 * @property {string[]} [triggers]     loop only: timer|handoff|message|replies:N|tools:N
 * @property {'any'|'all'} [trigger_mode]
 * @property {boolean} enabled
 * @property {string|null} last_fired_at
 * @property {string} created_at
 */

/**
 * List all schedules attached to a pulse.
 * @returns {Promise<Result<Schedule[]>>}
 */
export function listSchedules(pulseId) {
  return call('GET', P(pulseId, '/schedules'));
}

/**
 * Create one loop/once/daily schedule for a pulse.
 * body: {kind, prompt, interval_minutes?, at_iso?, cron_hhmm?, days?, triggers?, trigger_mode?}
 *   - kind "loop"  needs interval_minutes > 0; triggers[]/trigger_mode optional (default timer/any)
 *   - kind "once"  needs at_iso (ISO instant, UTC)
 *   - kind "daily" needs cron_hhmm "HH:MM" (UTC), optional days csv
 * @returns {Promise<Result<Schedule>>}
 */
export function createSchedule(pulseId, body) {
  return call('POST', P(pulseId, '/schedules'), body);
}

/**
 * List every schedule across all pulses in one call (drawer subtitles).
 * @param {boolean} [enabledOnly]
 * @returns {Promise<Result<Schedule[]>>}
 */
export function listAllSchedules(enabledOnly = false) {
  return call('GET', `/schedules${enabledOnly ? '?enabled_only=true' : ''}`);
}

/** Enable/disable a schedule. @returns {Promise<Result<Schedule>>} */
export function setScheduleEnabled(scheduleId, enabled) {
  return call('PATCH', `/schedules/${encodeURIComponent(scheduleId)}`, { enabled });
}

/**
 * Edit a schedule — loop prompt/interval and/or enabled state.
 * body: {enabled?, prompt?, interval_minutes?, triggers?, trigger_mode?} (all but enabled: loop only)
 * @returns {Promise<Result<Schedule>>}
 */
export function updateSchedule(scheduleId, body) {
  return call('PATCH', `/schedules/${encodeURIComponent(scheduleId)}`, body);
}

/** @returns {Promise<Result<any>>} */
export function deleteSchedule(scheduleId) {
  return call('DELETE', `/schedules/${encodeURIComponent(scheduleId)}`);
}

/**
 * @typedef {Object} Grid
 * @property {string} id
 * @property {string} name
 * @property {string} [created_at]
 */

/** @returns {Promise<Result<Grid[]>>} */
export function listGrids() {
  return call('GET', '/grids');
}

/** @returns {Promise<Result<Grid>>} */
export function createGrid(name, parent_id = null) {
  return call('POST', '/grids', parent_id ? { name, parent_id } : { name });
}

/** @returns {Promise<Result<Grid>>} */
export function renameGrid(id, name) {
  return call('PATCH', `/grids/${encodeURIComponent(id)}`, { name });
}

/**
 * Persist a workspace drag-reorder. `ids` is the new left-to-right order and MAY
 * be a subset — the listed workspaces swap among their own slots.
 * @returns {Promise<Result<any>>}
 */
export function reorderGrids(ids) {
  return call('POST', '/grids/reorder', { ids });
}

/**
 * Delete a workspace. `detach` moves any pulses it still holds out of it (they
 * keep everything else and show up under ALL) instead of the daemon refusing
 * with grid_not_empty — deleting a workspace never deletes pulses.
 * @returns {Promise<Result<{deleted:string,detached:number}>>}
 */
export function deleteGrid(id, { detach = false } = {}) {
  const q = detach ? '?detach=true' : '';
  return call('DELETE', `/grids/${encodeURIComponent(id)}${q}`);
}

/**
 * Move a pulse into a different grid. grid_id may be null to detach.
 * @returns {Promise<Result<Pulse>>}
 */
export function movePulseToGrid(id, grid_id) {
  return call('PATCH', P(id, '/grid'), { grid_id });
}

export function renamePulse(id, title) {
  return call('PATCH', P(id, '/title'), { title });
}

/** @returns {Promise<Result<Pulse>>} */
export function unarchivePulse(id) {
  return call('POST', P(id, '/unarchive'));
}

/**
 * Delete a pulse. One that owns active watchers answers 409 has_watchers
 * unless `closeWatchers` is true — preview them with pulseWatchers() and
 * ask first.
 * @param {string} id
 * @param {{closeWatchers?: boolean}} [opts]
 * @returns {Promise<Result<any>>}
 */
export function deletePulse(id, opts = {}) {
  const q = opts.closeWatchers ? '?close_watchers=true' : '';
  return call('DELETE', P(id, q));
}

/**
 * Delete from the UI: a running pulse is stopped first (the daemon refuses
 * to delete a live one with a bare 409). Watchers are NOT closed unless
 * opts.closeWatchers — a `has_watchers` result (r.code) is the caller's cue
 * to show what will close and ask.
 * @param {string} id
 * @param {{closeWatchers?: boolean}} [opts]
 */
export async function deletePulseStopping(id, opts = {}) {
  let r = await deletePulse(id, opts);
  if (!r.ok && r.code === 'bad_state' && /must be stopped/.test(String(r.error))) {
    const stopped = await pausePulse(id);
    if (!stopped.ok) return stopped;
    r = await deletePulse(id, opts);
  }
  return r;
}

/**
 * The watchers a pulse owns (open condition watches + delegation watches it
 * spawned) — what deleting it would close.
 * @returns {Promise<Result<{condition: ConditionWatch[], delegation: any[]}>>}
 */
export function pulseWatchers(id) {
  return call('GET', P(id, '/watchers'));
}

/**
 * Persist a wall drag-reorder. `ids` is the new left-to-right order and MAY be
 * a grid subset — the daemon redistributes those pulses over their OWN position
 * slots, so pulses on other tabs keep their placement.
 * @returns {Promise<Result<any>>}
 */
export function reorderPulses(ids) {
  return call('POST', '/pulses/reorder', { ids });
}

// --- skills + per-pulse clean diet --------------------------------------- //
// A pulse is CLEAN by default: no skills, no memory file. The diet is the set
// of opt-ins — {clean, memory, skills[]}. `clean` is the baseline and is not a
// user-facing toggle here; we always send clean:true and let memory/skills be
// the only knobs. On old pulses pulse.settings.diet may be absent → treat as
// the clean default (memory off, no skills).

/**
 * The curated base skill names available for opt-in. ~60 names.
 * @returns {Promise<Result<string[]>>}
 */
export function getSkills() {
  return call('GET', '/skills');
}

/**
 * Cage names available for opt-in — dirs under ~/.claude/cages
 * holding a cage.ts. Pi-only mechanical tool guards.
 * @returns {Promise<Result<string[]>>}
 */
export function getCages() {
  return call('GET', '/cages');
}

/**
 * Update a pulse's clean diet. Stored now, APPLIES ON NEXT SPAWN (restore /
 * model switch / handoff / scheduled wake) — the daemon does not restart a
 * live pulse to apply it, so the UI must say so honestly when saving.
 * @param {string} id
 * @param {{clean?: boolean, memory?: boolean, skills?: string[], cages?: string[]}} diet
 * @returns {Promise<Result<Pulse>>}
 */
export function setDiet(id, diet) {
  return call('PATCH', P(id, '/diet'), diet);
}

// --- settings KV --------------------------------------------------------- //
// The daemon exposes a raw key/value store (GET/PUT /settings/{key}). Values
// are arbitrary JSON; a missing key returns {ok:true, value:null}. PUT's body is
// the raw JSON value. Used by the theme system to persist profiles
// instead of localStorage. localStorage may only cache for instant paint.

/**
 * Read a settings key. Missing key resolves to {ok:true, value:null}.
 * @param {string} key
 * @returns {Promise<Result<any>>}
 */
export function getSetting(key) {
  return call('GET', `/settings/${encodeURIComponent(key)}`);
}

/**
 * Write a settings key. The body IS the raw JSON value (not wrapped).
 * @param {string} key
 * @param {any} value  any JSON-serializable value
 * @returns {Promise<Result<any>>}
 */
export function putSetting(key, value) {
  return call('PUT', `/settings/${encodeURIComponent(key)}`, value);
}

/**
 * Delete a settings key (unset it).
 * @param {string} key
 * @returns {Promise<Result<null>>}
 */
export function deleteSetting(key) {
  return call('DELETE', `/settings/${encodeURIComponent(key)}`);
}

/**
 * Device value first, else the shared KV value.
 * @param {string} key
 * @returns {Promise<Result<any>>}
 */
export async function getSettingForDevice(key) {
  const mine = getDeviceSetting(key);
  if (mine !== undefined) return { ok: true, value: mine };
  return getSetting(key);
}

// --- inbox (consuming + channels) ---------------------------------------- //
// Durable items addressed to the user from pulses/the daemon/scripts. Envelope
// {ok, value} + error-logging conventions match every other client above.
// Two states: read_at == null ⇒ Inbox(unread); set ⇒ Archive. Starring sorts
// to top. Channels are auto-created by senders; their mode controls muting.

/**
 * List inbox items. `filters` may include unread_only, channel, thread_key,
 * starred, needs_response, q, limit, before (matching GET /inbox).
 * @returns {Promise<Result<Array<Object>>>}
 */
export function listInbox(filters = {}) {
  return call('GET', withQuery('/inbox', filters));
}

/**
 * Total unread + per-channel + per-thread maps — drives the nav badge,
 * channel chips, and the app badge.
 * @returns {Promise<Result<{total: number, channels: Object, threads: Object}>>}
 */
export function getInboxUnreadCount() {
  return call('GET', '/inbox/unread-count');
}

/**
 * Whole-archive search (both states) — matches every item, not just the
 * current view page. Server caps each call; the UI pages deeper with terms.
 * @returns {Promise<Result<Array<Object>>>}
 */
export function searchInbox(q) {
  return call('GET', `/inbox/search?q=${encodeURIComponent(q)}`);
}

/** Mark an item read (⇒ archived). @returns {Promise<Result<Object>>} */
export function markInboxRead(id) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/read`);
}

/** Mark an item unread (return to Inbox). @returns {Promise<Result<Object>>} */
export function markInboxUnread(id) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/unread`);
}

/** Star an item. @returns {Promise<Result<Object>>} */
export function starInboxItem(id) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/star`);
}

/** Unstar an item. @returns {Promise<Result<Object>>} */
export function unstarInboxItem(id) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/unstar`);
}

/**
 * Score an item 1 (don't want this kind in the inbox), 2 (ok), 3 (notify),
 * or null to unlearn. The daemon learns a rule for that KIND of message
 * (channel + sender + title prefix) and, for 1, archives every unread item
 * of the kind. Returns { item, rule, affected }.
 * @returns {Promise<Result<{item: Object, rule: Object|null, affected: number}>>}
 */
export function scoreInboxItem(id, score) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/score`, { score });
}

/**
 * Dismiss an item's needs_response flag (sender no longer "blocked on you")
 * WITHOUT replying. Only meaningful for pulse items; envelopes an ok:false for
 * sources that can't carry the flag.
 * @returns {Promise<Result<Object>>}
 */
export function dismissInboxResponse(id) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/dismiss-response`);
}

/**
 * Archive every item in a filtered view up to and including `before` (the max
 * created_at the client rendered — a read-all cutoff). Accepts
 * the same filters as GET /inbox. `before` is required by the daemon.
 * @returns {Promise<Result<{archived: number}>>}
 */
export function readAllInbox(filters = {}) {
  return call('POST', '/inbox/read-all', filters);
}

/** All messages in a mail conversation, including sent replies. @returns {Promise<Result<Object[]>>} */
export function getInboxConversation(id) {
  return call('GET', `/inbox/items/${encodeURIComponent(id)}/conversation`);
}

/**
 * Reply to a pulse-sourced item. The daemon wraps and delivers to the source
 * pulse's queue, sets replied_at, clears needs_response. Pulse-sourced only —
 * script/system items return ok:false not_replyable, which the UI surfaces as
 * a disabled composer.
 * @param {string} id
 * @param {string} text
 * @returns {Promise<Result<Object>>}
 */
export function replyInbox(id, text) {
  return call('POST', `/inbox/items/${encodeURIComponent(id)}/reply`, { text });
}

/**
 * List every channel with its mode (notify|quiet|muted) + mute_until.
 * @returns {Promise<Result<Array<Object>>>}
 */
export function listInboxChannels() {
  return call('GET', '/inbox/channels');
}

/**
 * Set a channel's mode and/or mute_until.
 * @param {string} name
 * @param {{mode?: string, mute_until?: string|null}} patch
 * @returns {Promise<Result<Object>>}
 */
export function patchInboxChannel(name, patch) {
  return call('PATCH', `/inbox/channels/${encodeURIComponent(name)}`, patch);
}

/**
 * @typedef {Object} ConditionWatch
 * @property {string} id
 * @property {string} pulse_id
 * @property {string} kind             pulse_state|file|log|http|cmd
 * @property {Object} spec
 * @property {string} mode             once|edge
 * @property {number} interval_s
 * @property {string|null} note
 * @property {string} state            watching|closed
 * @property {number} checks_done
 * @property {number|null} bad
 * @property {Object|null} detail
 * @property {string} next_check_at
 * @property {string|null} outcome
 * @property {string} created_at
 */

/**
 * List a pulse's condition watches (open ones by default).
 * @returns {Promise<Result<ConditionWatch[]>>}
 */
export function listConditionWatches(pulseId, state = 'watching') {
  const q = new URLSearchParams();
  if (pulseId) q.set('pulse_id', pulseId); // null = every pulse (fleet views)
  if (state) q.set('state', state);
  return call('GET', `/watches/conditions?${q}`);
}

/** @returns {Promise<Result<any>>} */
export function deleteConditionWatch(watchId) {
  return call('DELETE', `/watches/conditions/${encodeURIComponent(watchId)}`);
}

// --- questions ------------------------------------------------------------ //
/** Open questions across every pulse (`pulseId` narrows). */
export function listQuestions(pulseId = null) {
  const q = new URLSearchParams();
  if (pulseId) q.set('pulse_id', pulseId);
  return call('GET', `/questions?${q}`);
}
/** Answer one question; the daemon relays the text to the asking pulse. */
export function answerQuestion(id, text) {
  return call('POST', `/questions/${encodeURIComponent(id)}/answer`, { text });
}
/** Close a question without an answer; the pulse is told to move on. */
export function dismissQuestion(id) {
  return call('POST', `/questions/${encodeURIComponent(id)}/dismiss`);
}

// --- hub (catalog + sidebar pins) ----------------------------------------- //

/** List published Hub pages. @returns {Promise<Result<Array<Object>>>} */
export function listHubPages() {
  return call('GET', '/hub/pages');
}

/** List sidebar-pinned Hub pages. @returns {Promise<Result<Array<{slug: string, title: string}>>>} */
export function listHubPins() {
  return call('GET', '/hub/pins');
}

/** Pin a Hub page to the sidebar. @returns {Promise<Result<Array<{slug: string, title: string}>>>} */
export function pinHubPage(slug) {
  return call('POST', '/hub/pins', { slug });
}

/** Unpin a Hub page from the sidebar. @returns {Promise<Result<Array<{slug: string, title: string}>>>} */
export function unpinHubPage(slug) {
  return call('DELETE', `/hub/pins/${encodeURIComponent(slug)}`);
}
