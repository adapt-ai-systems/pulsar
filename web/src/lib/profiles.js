/**
 * Pulse profiles — named presets for the New Pulse dialog.
 *
 * A profile stores the dialog's repetitive, per-install choices (model, effort,
 * fast mode, handoff threshold, memory cap, memory/skills diet, loop) so a new
 * pulse can be re-created with one selection. Profiles live in the daemon's
 * settings KV under `pulse.profiles` (a JSON array of these objects), so they
 * are identical on the phone and the desktop.
 *
 * Deliberately NOT stored: title, grid, wake schedule (per-occasion).
 *
 * Stored shape (no extra keys):
 *   {
 *     name, model, effort, fast, handoff_tokens, memory_max,
 *     memory, skills, cages, loop
 *   }
 *   effort: ''           => CLI default
 *   handoff_tokens: null  => use the model's default
 *   loop: null | {minutes: number, prompt: string, triggers?: string[], trigger_mode?: 'any'|'all'}
 *         (triggers/trigger_mode: loop triggers; absent = timer/any)
 *
 * All functions here are pure: they take values and return NEW arrays/objects,
 * never mutating their inputs.
 */
import {
  DEFAULT_MODEL_ID, modelById, defaultHandoffFor, effortsFor, supportsFastMode
} from './models.js';
import { triggerFormFromList, triggerListFromForm, normalizeTriggerMode } from './looptriggers.js';

/** Settings KV key that stores the profile array. */
export const KV_PROFILES = 'pulse.profiles';

/** Settings KV key for the profile auto-applied on every new pulse. */
export const KV_DEFAULT_PROFILE = 'pulse.default_profile';

const MEMORY_MAXES = ['2G', '4G', '6G'];

/** The shape a stored profile is reduced to, independently of garbage input. */
const STORED_KEYS = [
  'name', 'model', 'effort', 'fast', 'handoff_tokens',
  'memory_max', 'memory', 'skills', 'cages', 'loop'
];

function normalizeLoop(loop) {
  if (loop == null || typeof loop !== 'object' || Array.isArray(loop)) return null;
  const { minutes, prompt } = loop;
  if (typeof minutes !== 'number' || !Number.isInteger(minutes) || minutes <= 0) return null;
  if (typeof prompt !== 'string' || prompt.trim() === '') return null;
  const out = { minutes, prompt };
  if (Array.isArray(loop.triggers)) {
    out.triggers = triggerListFromForm(triggerFormFromList(loop.triggers)).list;
  }
  if (loop.trigger_mode === 'all') out.trigger_mode = 'all';
  return out;
}

/**
 * Reduce arbitrary input to a clean array of profiles. Never throws; never
 * lets garbage through. Duplicate names (case-insensitive) collapse, last one
 * wins.
 * @param {any} raw
 * @returns {Array<object>}
 */
export function sanitizeProfiles(raw) {
  if (!Array.isArray(raw)) return [];
  const out = [];
  for (const entry of raw) {
    if (entry == null || typeof entry !== 'object' || Array.isArray(entry)) continue;
    const name = typeof entry.name === 'string' ? entry.name.trim() : '';
    if (!name) continue;
    let model = typeof entry.model === 'string' ? entry.model.trim() : '';
    if (!model) model = DEFAULT_MODEL_ID; // present-but-unknown strings are kept (apply warns)
    const effort = typeof entry.effort === 'string' ? entry.effort : '';
    const fast = entry.fast === true;
    let handoff_tokens = null;
    if (typeof entry.handoff_tokens === 'number' && Number.isInteger(entry.handoff_tokens) && entry.handoff_tokens > 0) {
      handoff_tokens = entry.handoff_tokens;
    }
    const memory_max = MEMORY_MAXES.includes(entry.memory_max) ? entry.memory_max : '4G';
    const memory = entry.memory === true;
    let skills = [];
    if (Array.isArray(entry.skills)) {
      skills = entry.skills.filter((s) => typeof s === 'string');
    }
    let cages = [];
    if (Array.isArray(entry.cages)) {
      cages = entry.cages.filter((s) => typeof s === 'string');
    }
    const loop = normalizeLoop(entry.loop);
    out.push({ name, model, effort, fast, handoff_tokens, memory_max, memory, skills, cages, loop });
  }
  // Collapse duplicate names (case-insensitive), last one wins.
  const byKey = new Map();
  for (const p of out) byKey.set(p.name.toLowerCase(), p);
  return Array.from(byKey.values());
}

/**
 * Build a profile object from the dialog's live form state.
 * @param {string} name
 * @param {FormShape} form
 * @returns {object|null}
 */
export function profileFromForm(name, form) {
  const trimmed = typeof name === 'string' ? name.trim() : '';
  if (!trimmed) return null;
  let handoff_tokens = null;
  const handoffRaw = typeof form.handoffTokens === 'string' ? form.handoffTokens.trim() : '';
  if (handoffRaw !== '') {
    const n = parseInt(handoffRaw, 10);
    if (Number.isFinite(n) && n > 0) handoff_tokens = n;
  }
  let loop = null;
  if (form.loopOn) {
    const minutes = parseInt(String(form.loopMinutes ?? ''), 10);
    const prompt = String(form.loopPrompt ?? '').trim();
    if (Number.isInteger(minutes) && minutes > 0 && prompt !== '') {
      loop = { minutes, prompt };
      const trig = form.loopTriggers ? triggerListFromForm(form.loopTriggers) : null;
      if (trig && trig.list) loop.triggers = trig.list;
      if (normalizeTriggerMode(form.loopMode) === 'all') loop.trigger_mode = 'all';
    }
  }
  return {
    name: trimmed,
    model: form.model,
    effort: form.effort,
    fast: form.fast,
    handoff_tokens,
    memory_max: form.memoryMax,
    memory: form.memory,
    skills: Array.isArray(form.skills) ? [...form.skills] : [],
    cages: Array.isArray(form.cages) ? [...form.cages] : [],
    loop
  };
}

/**
 * Turn a stored profile into component-ready dialog values. Clamps effort/fast
 * to the target model, and warns when the model is no longer in the catalog.
 * @param {object} profile
 * @returns {object} {model, effort, fast, handoffTokens, memoryMax, memory,
 *   skills, cages, loopOn, loopMinutes, loopPrompt, warning}
 *
 * `automatic` (the default profile applied on open) restores the saved
 * skills too so a default profile never drops its skills on open. The picker
 * merges the global default skills on top, so nothing is bypassed.
 */
export function applyProfile(profile, { automatic = false } = {}) {
  const p = profile ?? {};
  let model = p.model;
  let warning = '';
  if (!modelById(model)) {
    warning = `unknown model "${model}" — using default`;
    model = DEFAULT_MODEL_ID;
  }
  const effort = effortsFor(model).includes(p.effort) ? p.effort : '';
  const fast = !!p.fast && supportsFastMode(model);
  const handoff = p.handoff_tokens == null ? null : p.handoff_tokens;
  const handoffTokens = handoff == null ? String(defaultHandoffFor(model) ?? '') : String(handoff);
  const loop = p.loop && p.loop.minutes > 0 && p.loop.prompt
    ? p.loop
    : null;
  return {
    model,
    effort,
    fast,
    handoffTokens,
    memoryMax: MEMORY_MAXES.includes(p.memory_max) ? p.memory_max : '4G',
    memory: !!p.memory,
    skills: Array.isArray(p.skills) ? [...p.skills] : [],
    cages: Array.isArray(p.cages) ? [...p.cages] : [],
    loopOn: !!loop,
    loopMinutes: loop ? String(loop.minutes) : '30',
    loopPrompt: loop ? loop.prompt : '',
    loopTriggers: loop && Array.isArray(loop.triggers) ? loop.triggers : ['timer'],
    loopMode: loop ? normalizeTriggerMode(loop.trigger_mode) : 'any',
    warning
  };
}

/**
 * Upsert a profile into a list. A name that already exists (case-insensitive)
 * is replaced in place; otherwise appended.
 * @param {Array<object>} list
 * @param {object} profile
 * @returns {Array<object>}
 */
export function upsertProfile(list, profile) {
  const out = list.slice();
  const key = profile.name.toLowerCase();
  const idx = out.findIndex((p) => p.name.toLowerCase() === key);
  if (idx >= 0) out[idx] = profile;
  else out.push(profile);
  return out;
}

/**
 * Remove a profile by name (case-insensitive). No-op for an absent name.
 * @param {Array<object>} list
 * @param {string} name
 * @returns {Array<object>}
 */
export function removeProfile(list, name) {
  const key = String(name).toLowerCase();
  return list.filter((p) => p.name.toLowerCase() !== key);
}

/**
 * Find a profile by name (case-insensitive).
 * @param {Array<object>} list
 * @param {string} name
 * @returns {object|undefined}
 */
export function findProfile(list, name) {
  const key = String(name).toLowerCase();
  return list.find((p) => p.name.toLowerCase() === key);
}

/**
 * Reduce the default-profile KV value to a name string. Garbage, blank, and
 * non-strings become '' (meaning: no default).
 * @param {any} raw
 * @returns {string}
 */
export function sanitizeDefaultProfile(raw) {
  return typeof raw === 'string' ? raw.trim() : '';
}

/**
 * Return a NEW list sorted alphabetically by name (no mutation of input).
 * @param {Array<object>} list
 * @returns {Array<object>}
 */
export function sortedProfiles(list) {
  return list.slice().sort((a, b) => a.name.localeCompare(b.name));
}
