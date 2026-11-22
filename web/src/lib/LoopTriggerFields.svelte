<script>
  /**
   * Loop trigger fields: which events fire the loop and how they
   * combine. Pure form surface — the parent binds `form` (see
   * looptriggers.js defaultTriggerForm) and `mode` ('any'|'all') and builds
   * the API body with triggerListFromForm().
   */
  import { appSettings, ownerDisplayName } from './appsettings.svelte.js';

  let { form = $bindable(), mode = $bindable('any'), idPrefix = 'lt' } = $props();
  const owner = $derived(ownerDisplayName(appSettings.ownerName));
</script>

<div class="trig">
  <div class="sub-lbl">fires on</div>
  <div class="checks">
    <label><input type="checkbox" bind:checked={form.timer} /> timer</label>
    <label><input type="checkbox" bind:checked={form.handoff} /> every handoff</label>
    <label><input type="checkbox" bind:checked={form.message} /> every message from {owner}</label>
    <label class="num">
      every
      <input type="text" inputmode="numeric" bind:value={form.replies} placeholder="N"
        aria-label="every N replies" id="{idPrefix}-replies" />
      replies
    </label>
    <label class="num">
      every
      <input type="text" inputmode="numeric" bind:value={form.tools} placeholder="N"
        aria-label="every N tool calls" id="{idPrefix}-tools" />
      tool calls
    </label>
  </div>
  <div class="mode-row">
    <span class="sub-lbl">combine</span>
    <div class="seg">
      <button type="button" class:on={mode === 'any'} onclick={() => (mode = 'any')}
        title="any one trigger fires the loop; a fire resets the timer and the counters">ANY</button>
      <button type="button" class:on={mode === 'all'} onclick={() => (mode = 'all')}
        title="every checked trigger must be due before it fires">ALL</button>
    </div>
  </div>
  <p class="note">
    {mode === 'any'
      ? 'any one trigger fires it; a fire resets the timer and the counters, so a tool-call fire pushes the next timer fire out.'
      : 'all checked triggers must be due at once; then everything resets.'}
    message and handoff fires inject the prompt directly and do not reset the timer.
  </p>
</div>

<style>
  .trig { margin-top: 8px; }
  .sub-lbl {
    display: block;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-meta);
    text-transform: uppercase;
    margin-bottom: 5px;
  }
  .checks { display: flex; flex-wrap: wrap; gap: 6px 14px; }
  .checks label {
    display: inline-flex; align-items: center; gap: 6px;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink-dim);
    text-transform: none;
    letter-spacing: 0;
    font-family: inherit;
    margin: 0;
  }
  .checks input[type='checkbox'] { accent-color: var(--dreamer); }
  .checks .num input[type='text'] {
    width: 44px;
    text-align: center;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 7px;
    padding: 3px 4px;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
  }
  .mode-row { display: flex; align-items: center; gap: 10px; margin-top: 8px; }
  .mode-row .sub-lbl { margin: 0; }
  .seg { display: flex; gap: 6px; }
  .seg button {
    padding: 4px 12px;
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-dim);
  }
  .seg button.on {
    border-color: var(--dreamer);
    color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 8%, transparent);
  }
  .note {
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.04em;
    color: var(--ink-meta);
  }
</style>
