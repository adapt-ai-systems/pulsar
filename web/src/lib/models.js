/**
 * Model catalog for the pickers — a VIEW over pulsar/models.json, the one
 * registry the daemon also reads (pulsar/models.py). Add a model there; this
 * file only shapes it for <select>s. `models.json` here is a symlink to it.
 *
 * @typedef {Object} ModelInfo
 * @property {string} id
 * @property {string} label
 * @property {string} vessel        DERIVED CLI for this model — the user never picks a vessel
 * @property {number} defaultHandoff
 * @property {boolean} slot         true for model SLOTS — served id is runtime-only
 * @property {string} baseUrl       slot base URL (for the "no model served on <base>" tooltip)
 */
import registry from './models.json' with { type: 'json' };

/**
 * Shape one raw registry entry into a picker ModelInfo. Slot entries carry no
 * wire_model — that is fine here; nothing in this view reads it. Exported so
 * the shape is testable against synthetic entries (the live registry may not
 * contain slots on every branch).
 * @param {any} m raw registry entry
 * @returns {ModelInfo}
 */
export function shapeModel(m) {
  return {
    id: m.id, label: m.label, vessel: m.vessel,
    defaultHandoff: m.default_handoff,
    slot: Boolean(m.slot),
    baseUrl: m.base_url ?? ''
  };
}

/**
 * Build a model catalog view from a raw registry object. Exported so tests can
 * exercise full/local/slot/alias registries alongside the shipped starter file.
 * @param {any} rawRegistry
 */
export function createCatalog(rawRegistry) {
  /** @type {ModelInfo[]} Picker-visible models (legacy `hidden` ids excluded). */
  const MODELS = rawRegistry.models
    .filter((m) => !m.hidden)
    .map(shapeModel);

  /** Every model incl. hidden legacy ids — for labels/vessels of old pulses. */
  const ALL_MODELS = rawRegistry.models.map(shapeModel);

  /** The default model for new pulses. */
  const DEFAULT_MODEL_ID = rawRegistry.default_model;

  /** The default reasoning effort pre-selected for a new pulse. */
  const DEFAULT_EFFORT = rawRegistry.default_effort;

  /**
   * Optgroup display order + group labels for the <select> pickers.
   * @type {Array<{label: string, vessel: string}>}
   */
  const MODEL_GROUPS = Object.entries(rawRegistry.vessels).map(([vessel, v]) => ({
    label: v.label, vessel
  }));

  /** @param {string} id @returns {ModelInfo|undefined} */
  function modelById(id) {
    return ALL_MODELS.find((m) => m.id === id);
  }

  /** @param {string} id @returns {string} */
  function modelLabel(id) {
    return modelById(id)?.label ?? id;
  }

  /** @param {string} id @returns {string|undefined} */
  function vesselFor(id) {
    return modelById(id)?.vessel;
  }

  /** @param {string} id @returns {number|undefined} */
  function defaultHandoffFor(id) {
    return modelById(id)?.defaultHandoff;
  }

  /** Effort dial per vessel (registry `vessels[].efforts`). */
  const EFFORTS_BY_VESSEL = Object.fromEntries(
    Object.entries(rawRegistry.vessels).map(([vessel, v]) => [vessel, v.efforts ?? []])
  );

  /** Per-model overrides of the vessel dial (registry `models[].efforts`). */
  const EFFORTS_BY_MODEL = Object.fromEntries(
    rawRegistry.models.filter((m) => m.efforts).map((m) => [m.id, m.efforts])
  );

  /** @param {string} id @returns {string[]} */
  function effortsFor(id) {
    return EFFORTS_BY_MODEL[id] ?? EFFORTS_BY_VESSEL[vesselFor(id) ?? ''] ?? [];
  }

  /** Vessels whose CLI takes a fast-mode switch (registry `vessels[].fast`). */
  const FAST_VESSELS = Object.entries(rawRegistry.vessels)
    .filter(([, v]) => v.fast)
    .map(([vessel]) => vessel);

  /** @param {string} id @returns {boolean} */
  function supportsFastMode(id) {
    return FAST_VESSELS.includes(vesselFor(id) ?? '');
  }

  /** @returns {Array<{label: string, models: ModelInfo[]}>} */
  function groupedModels() {
    return MODEL_GROUPS.map((g) => ({
      label: g.label,
      models: MODELS.filter((m) => m.vessel === g.vessel)
    })).filter((g) => g.models.length > 0);
  }

  /** Model-specific effort default; absent means preserve historical UI fallback. */
  function defaultEffortFor(id) {
    return rawRegistry.models.find((m) => m.id === id)?.default_effort ?? '';
  }

  /** @param {string} effort @param {string} model @returns {string} */
  function canonicalEffort(effort, model) {
    const aliases = rawRegistry.models.find((m) => m.id === model)?.effort_aliases;
    return (effort && aliases?.[effort]) || effort;
  }

  /** @param {string[]} opts @returns {string} */
  function offLevel(opts) {
    return opts.includes('off') ? 'off' : opts.includes('none') ? 'none' : '';
  }

  /** @param {string} effort @param {string} model @returns {string} */
  function effortOnModelChange(effort, model) {
    const opts = effortsFor(model);
    const canon = canonicalEffort(effort, model);
    if (canon && opts.includes(canon)) return canon;
    return defaultEffortFor(model) || offLevel(opts);
  }

  /** Runtime vessel tool availability map from GET /api/models (`tools`). */
  const TOOLS = rawRegistry.tools && typeof rawRegistry.tools === 'object' ? rawRegistry.tools : null;

  /** @returns {Record<string, {available: boolean, binary?: string, path?: string|null, reason?: string|null}> | null} */
  function toolsMap() {
    return TOOLS;
  }

  /** @param {string} vessel */
  function toolStatus(vessel) {
    return TOOLS?.[vessel] ?? null;
  }

  /** @returns {boolean} */
  function toolsLoaded() {
    return TOOLS !== null;
  }

  /** @returns {boolean} */
  function anyToolAvailable() {
    if (!TOOLS) return true;
    return Object.values(TOOLS).some((t) => Boolean(t?.available));
  }

  return {
    MODELS,
    DEFAULT_MODEL_ID,
    DEFAULT_EFFORT,
    MODEL_GROUPS,
    modelById,
    modelLabel,
    vesselFor,
    defaultHandoffFor,
    EFFORTS_BY_VESSEL,
    EFFORTS_BY_MODEL,
    effortsFor,
    FAST_VESSELS,
    supportsFastMode,
    groupedModels,
    defaultEffortFor,
    effortOnModelChange,
    canonicalEffort,
    offLevel,
    toolsMap,
    toolStatus,
    toolsLoaded,
    anyToolAvailable,
  };
}

function registryFingerprint(raw) {
  if (!raw || typeof raw !== 'object' || !Array.isArray(raw.models) || !raw.vessels) return '';
  // Exclude volatile slot status fields so periodic /api/models probes don't
  // invalidate reactive subscribers when the catalog itself has not changed.
  const models = raw.models.map((m) => {
    if (!m || typeof m !== 'object') return m;
    const { status: _s, label_live: _l, ...rest } = m;
    return rest;
  });
  return JSON.stringify({
    default_model: raw.default_model,
    default_effort: raw.default_effort,
    vessels: raw.vessels,
    tools: raw.tools ?? null,
    models
  });
}

const _defaultCatalog = createCatalog(registry);
let _catalog = _defaultCatalog;
let _catalogFingerprint = registryFingerprint(registry);
let _onRead = null;
let _onWrite = null;

/**
 * Connect an external reactive store (modelstore.svelte.js) so Svelte components
 * reading catalog helpers re-evaluate when runtime /api/models data arrives.
 * Kept hook-based so models.js stays pure ESM for node --test.
 * @param {{ onRead?: (() => void) | null, onWrite?: (() => void) | null }} hooks
 */
export function registerCatalogHooks({ onRead = null, onWrite = null } = {}) {
  _onRead = onRead;
  _onWrite = onWrite;
}

/**
 * Replace the live catalog with the daemon's runtime GET /api/models payload.
 * Returns true when the catalog changed, false if invalid or unchanged.
 * @param {any} rawRegistry
 * @returns {boolean}
 */
export function setRuntimeRegistry(rawRegistry) {
  if (!rawRegistry || typeof rawRegistry !== 'object') return false;
  if (!Array.isArray(rawRegistry.models) || !rawRegistry.vessels || typeof rawRegistry.vessels !== 'object') {
    return false;
  }
  const fp = registryFingerprint(rawRegistry);
  if (fp === _catalogFingerprint) return false;
  _catalog = createCatalog(rawRegistry);
  _catalogFingerprint = fp;
  _onWrite?.();
  return true;
}

/** Restore the build-time starter registry (primarily for tests). */
export function resetRuntimeRegistry() {
  _catalog = _defaultCatalog;
  _catalogFingerprint = registryFingerprint(registry);
  _onWrite?.();
}

export const MODELS = _defaultCatalog.MODELS;
export const DEFAULT_MODEL_ID = _defaultCatalog.DEFAULT_MODEL_ID;
export const DEFAULT_EFFORT = _defaultCatalog.DEFAULT_EFFORT;
export const MODEL_GROUPS = _defaultCatalog.MODEL_GROUPS;
export const EFFORTS_BY_VESSEL = _defaultCatalog.EFFORTS_BY_VESSEL;
export const EFFORTS_BY_MODEL = _defaultCatalog.EFFORTS_BY_MODEL;
export const FAST_VESSELS = _defaultCatalog.FAST_VESSELS;

export function modelById(id) { _onRead?.(); return _catalog.modelById(id); }
export function modelLabel(id) { _onRead?.(); return _catalog.modelLabel(id); }
export function vesselFor(id) { _onRead?.(); return _catalog.vesselFor(id); }
export function defaultHandoffFor(id) { _onRead?.(); return _catalog.defaultHandoffFor(id); }
export function effortsFor(id) { _onRead?.(); return _catalog.effortsFor(id); }
export function supportsFastMode(id) { _onRead?.(); return _catalog.supportsFastMode(id); }
export function groupedModels() { _onRead?.(); return _catalog.groupedModels(); }
export function defaultEffortFor(id) { _onRead?.(); return _catalog.defaultEffortFor(id); }
export function effortOnModelChange(effort, model) { _onRead?.(); return _catalog.effortOnModelChange(effort, model); }
export function canonicalEffort(effort, model) { _onRead?.(); return _catalog.canonicalEffort(effort, model); }
export function offLevel(opts) { return _catalog.offLevel(opts); }
export function toolsMap() { _onRead?.(); return _catalog.toolsMap(); }
export function toolStatus(vessel) { _onRead?.(); return _catalog.toolStatus(vessel); }
export function toolsLoaded() { _onRead?.(); return _catalog.toolsLoaded(); }
export function anyToolAvailable() { _onRead?.(); return _catalog.anyToolAvailable(); }

// --- Model slots: runtime status merging ------------------------------------ //
// The registry (models.json) is static; GET /models decorates slot entries
// with {status: {live, served_id, checked_at, error}, label_live}. These pure
// helpers fuse the two views for the picker. No fetching here.

/**
 * Merge runtime slot status (the `models` array from GET /models or a reverse
 * proxy's /api/models) into static picker entries. Non-slot entries pass
 * through untouched. A slot with no runtime entry (fetch pending/failed) gets
 * no status — slotOption then renders it unavailable.
 * @param {ModelInfo[]} models static picker entries
 * @param {Array<{id: string, slot?: boolean, status?: object, label_live?: string}>|null|undefined} runtimeEntries
 * @returns {ModelInfo[]} entries with `status` / `label_live` added on slots
 */
export function mergeSlotStatus(models, runtimeEntries) {
  const byId = new Map();
  for (const e of runtimeEntries ?? []) {
    if (e && typeof e.id === 'string') byId.set(e.id, e);
  }
  return (models ?? []).map((m) => {
    if (!m.slot) return m;
    const rt = byId.get(m.id);
    return {
      ...m,
      status: rt?.status ?? null,
      label_live: typeof rt?.label_live === 'string' ? rt.label_live : null
    };
  });
}

/**
 * Picker <option> state for a model entry. Slot entries show `label_live`
 * ("Local ring (TP4): glm-5.3-flash" / "...: unavailable"); when the slot is
 * not live the option is disabled/greyed with a tooltip carrying the probe
 * error, or "no model served on <base>" when the probe had no message.
 * @param {ModelInfo & {status?: object|null, label_live?: string|null}} m
 * @returns {{disabled: boolean, text: string, title: string}}
 */
export function slotOption(m) {
  if (!m?.slot) return { disabled: false, text: m?.label ?? '', title: m?.label ?? '' };
  const live = Boolean(m.status?.live);
  const text = m.label_live || (live
    ? `${m.label}: ${m.status?.served_id ?? 'unknown'}`
    : `${m.label}: unavailable`);
  const title = live ? text : (m.status?.error || `no model served on ${m.baseUrl || 'the slot'}`);
  return { disabled: !live, text, title };
}

/**
 * Picker <option> state combining vessel CLI availability and slot status.
 * A model whose agent tool was not found on this machine is disabled with a
 * short reason (e.g. "claude not found").
 * @param {ModelInfo & {status?: object|null, label_live?: string|null}} m
 * @param {Record<string, {available: boolean, reason?: string|null}> | null} [toolsOverride]
 * @returns {{disabled: boolean, text: string, title: string, reason: string|null}}
 */
export function modelOption(m, toolsOverride = undefined) {
  const tools = toolsOverride === undefined ? toolsMap() : toolsOverride;
  const info = tools && m?.vessel ? tools[m.vessel] : null;
  if (info && !info.available) {
    const reason = info.reason || `${m.vessel} not found`;
    return {
      disabled: true,
      text: `${m?.label ?? ''} — not available (${reason})`,
      title: reason,
      reason
    };
  }
  const slot = slotOption(m);
  return { ...slot, reason: slot.disabled ? slot.title : null };
}

/**
 * Default model id for the New Pulse picker: prefers the catalog default if
 * its tool is found, otherwise the first available visible model, otherwise ''.
 * @param {Array<ModelInfo & {status?: object|null, label_live?: string|null}>} [modelsList]
 * @param {Record<string, {available: boolean, reason?: string|null}> | null} [toolsOverride]
 * @returns {string}
 */
export function defaultAvailableModelId(modelsList = undefined, toolsOverride = undefined) {
  _onRead?.();
  const list = modelsList ?? _catalog.groupedModels().flatMap((g) => g.models);
  const avail = list.filter((m) => !modelOption(m, toolsOverride).disabled);
  if (avail.length === 0) return '';
  if (avail.some((m) => m.id === _catalog.DEFAULT_MODEL_ID)) return _catalog.DEFAULT_MODEL_ID;
  return avail[0].id;
}

/**
 * Plain spawn-error text for the modal strip. A 409 slot_unavailable reaches
 * us as the code (or inside the daemon's message); say it in one sentence.
 * @param {unknown} error result.error from api.js
 * @returns {string}
 */
export function spawnErrorText(error) {
  const s = typeof error === 'string' ? error : String(error ?? '');
  if (s.includes('slot_unavailable')) {
    return 'That model slot is unavailable right now — pick another model.';
  }
  return s;
}
