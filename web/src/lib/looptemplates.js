/**
 * Loop templates — named, reusable loop definitions so a standing loop with
 * triggers can be dropped onto any pulse in one pick. Stored in the daemon settings
 * KV under `loop.templates` as a JSON array:
 *   { name, minutes, prompt, triggers: string[], trigger_mode: 'any'|'all' }
 * All functions are pure and never throw on garbage.
 */
import { triggerFormFromList, triggerListFromForm, normalizeTriggerMode } from './looptriggers.js';

export const KV_LOOP_TEMPLATES = 'loop.templates';

/**
 * Reduce arbitrary input to clean templates. Duplicate names (case-insensitive)
 * collapse, last one wins.
 * @param {any} raw
 * @returns {Array<{name:string, minutes:number, prompt:string, triggers:string[], trigger_mode:string}>}
 */
export function sanitizeTemplates(raw) {
  if (!Array.isArray(raw)) return [];
  const byKey = new Map();
  for (const t of raw) {
    if (t == null || typeof t !== 'object' || Array.isArray(t)) continue;
    const name = typeof t.name === 'string' ? t.name.trim() : '';
    const prompt = typeof t.prompt === 'string' ? t.prompt.trim() : '';
    const minutes = Number(t.minutes);
    if (!name || !prompt || !Number.isInteger(minutes) || minutes <= 0) continue;
    const triggers = triggerListFromForm(triggerFormFromList(t.triggers)).list;
    const trigger_mode = normalizeTriggerMode(t.trigger_mode);
    byKey.set(name.toLowerCase(), { name, minutes, prompt, triggers, trigger_mode });
  }
  return Array.from(byKey.values());
}

/**
 * Build a template from form values, or null when invalid.
 * @param {string} name
 * @param {{minutes: string|number, prompt: string, triggerForm: object, mode: string}} form
 */
export function templateFromForm(name, form) {
  const trimmed = typeof name === 'string' ? name.trim() : '';
  if (!trimmed) return null;
  const minutes = parseInt(String(form?.minutes ?? ''), 10);
  const prompt = String(form?.prompt ?? '').trim();
  if (!Number.isInteger(minutes) || minutes <= 0 || !prompt) return null;
  const tr = triggerListFromForm(form?.triggerForm ?? {});
  if (tr.error) return null;
  return { name: trimmed, minutes, prompt, triggers: tr.list, trigger_mode: normalizeTriggerMode(form?.mode) };
}

export function upsertTemplate(list, tpl) {
  const out = list.slice();
  const key = tpl.name.toLowerCase();
  const idx = out.findIndex((t) => t.name.toLowerCase() === key);
  if (idx >= 0) out[idx] = tpl;
  else out.push(tpl);
  return out;
}

export function removeTemplate(list, name) {
  const key = String(name).toLowerCase();
  return list.filter((t) => t.name.toLowerCase() !== key);
}

export function findTemplate(list, name) {
  const key = String(name).toLowerCase();
  return list.find((t) => t.name.toLowerCase() === key);
}

export function sortedTemplates(list) {
  return list.slice().sort((a, b) => a.name.localeCompare(b.name));
}
