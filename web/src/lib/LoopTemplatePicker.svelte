<script>
  import { getSetting, putSetting } from './api.js';
  import {
    KV_LOOP_TEMPLATES, sanitizeTemplates, templateFromForm, upsertTemplate,
    removeTemplate, findTemplate, sortedTemplates
  } from './looptemplates.js';

  /**
   * Loop template picker: pick a saved template (fills the parent's loop
   * form via onpick), save the current form as a template, delete one.
   * Templates live in settings KV `loop.templates` (shared across devices).
   *
   * @type {{
   *   getForm: () => {minutes, prompt, triggerForm, mode},  // current loop form
   *   onpick: (tpl) => void,
   *   onerror?: (msg: string) => void,
   * }}
   */
  let { getForm, onpick, onerror } = $props();

  let templates = $state([]);
  let selected = $state('');
  let saveName = $state('');
  let saving = $state(false);
  let deleteArmed = $state(false);

  const sorted = $derived(sortedTemplates(templates));

  $effect(() => {
    let alive = true;
    getSetting(KV_LOOP_TEMPLATES).then((r) => {
      if (!alive) return;
      templates = sanitizeTemplates(r.ok ? r.value : []);
    });
    return () => (alive = false);
  });

  function pick(name) {
    selected = name;
    deleteArmed = false;
    const t = findTemplate(templates, name);
    if (t) onpick?.(t);
  }

  async function persist(next) {
    const r = await putSetting(KV_LOOP_TEMPLATES, next);
    if (r.ok) templates = next;
    else onerror?.(`loop templates: ${r.error}`);
    return r.ok;
  }

  async function save() {
    if (saving) return;
    const tpl = templateFromForm(saveName, getForm());
    if (!tpl) {
      onerror?.('To save a template: give it a name, and the loop needs valid minutes, a prompt and a trigger.');
      return;
    }
    saving = true;
    const ok = await persist(upsertTemplate(templates, tpl));
    saving = false;
    if (ok) { selected = tpl.name; saveName = ''; }
  }

  async function del() {
    if (!selected) return;
    if (!deleteArmed) { deleteArmed = true; return; }
    deleteArmed = false;
    const ok = await persist(removeTemplate(templates, selected));
    if (ok) selected = '';
  }
</script>

<div class="tpl">
  <div class="row">
    <select bind:value={selected} onchange={(e) => pick(e.currentTarget.value)} aria-label="loop template">
      <option value="">— template —</option>
      {#each sorted as t (t.name)}
        <option value={t.name}>{t.name}</option>
      {/each}
    </select>
    {#if selected}
      <button type="button" class="ico del" class:armed={deleteArmed} onclick={del}
        title="delete this template">{deleteArmed ? 'confirm?' : '× delete'}</button>
    {/if}
  </div>
  <div class="row">
    <input type="text" bind:value={saveName} placeholder="save current loop as template…" aria-label="template name" />
    <button type="button" class="ico" onclick={save} disabled={saving}>{saving ? 'saving…' : 'save'}</button>
  </div>
</div>

<style>
  .tpl { display: flex; flex-direction: column; gap: 6px; margin-bottom: 8px; }
  .row { display: flex; gap: 6px; align-items: center; }
  select, input[type='text'] {
    flex: 1;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 6px 10px;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
  }
  .ico {
    padding: 5px 10px;
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-dim);
    white-space: nowrap;
  }
  .ico.del.armed { border-color: var(--danger, #c55); color: var(--danger, #c55); }
</style>
