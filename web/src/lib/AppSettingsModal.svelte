<script>
  /**
   * App-wide settings modal: the daemon-backed settings stored under
   * /settings/{key} (owner.name, owner.timezone, hub.url, metrics.url,
   * guard.enabled, guard.extra_paths, default_backup). Reachable even with
   * zero pulses.
   *
   * @type {{ onclose: () => void }}
   */
  import { onMount } from 'svelte';
  import { portal } from './portal.js';
  import { appSettings, loadAppSettings, saveAppSettings } from './appsettings.svelte.js';
  import { groupedModels, effortsFor, offLevel, canonicalEffort } from './models.js';
  import { loadModels } from './modelstore.svelte.js';

  let { onclose } = $props();

  const modelGroups = $derived(groupedModels().filter((g) => g.models.length));

  let ownerName = $state(appSettings.ownerName);
  let ownerTimezone = $state(appSettings.ownerTimezone);
  let hubUrl = $state(appSettings.hubUrl);
  let metricsUrl = $state(appSettings.metricsUrl);
  let guardEnabled = $state(appSettings.guardEnabled);
  let guardExtraPathsText = $state(appSettings.guardExtraPaths.join('\n'));
  let defaultBackupModel = $state(appSettings.defaultBackup?.model ?? '');
  let defaultBackupEffort = $state(appSettings.defaultBackup?.effort ?? '');
  const backupEffortOpts = $derived(defaultBackupModel ? effortsFor(defaultBackupModel) : []);

  let loading = $state(!appSettings.loaded);
  let saving = $state(false);
  let error = $state('');
  let savedNote = $state('');

  $effect(() => {
    if (!defaultBackupModel) {
      defaultBackupEffort = '';
      return;
    }
    const opts = effortsFor(defaultBackupModel);
    if (defaultBackupEffort && !opts.includes(defaultBackupEffort)) {
      const canon = canonicalEffort(defaultBackupEffort, defaultBackupModel);
      defaultBackupEffort = opts.includes(canon) ? canon : offLevel(opts);
    }
  });

  function syncFromStore() {
    ownerName = appSettings.ownerName;
    ownerTimezone = appSettings.ownerTimezone;
    hubUrl = appSettings.hubUrl;
    metricsUrl = appSettings.metricsUrl;
    guardEnabled = appSettings.guardEnabled;
    guardExtraPathsText = appSettings.guardExtraPaths.join('\n');
    defaultBackupModel = appSettings.defaultBackup?.model ?? '';
    defaultBackupEffort = appSettings.defaultBackup?.effort ?? '';
  }

  onMount(async () => {
    await Promise.all([loadAppSettings({ force: true }), loadModels({ force: true })]);
    syncFromStore();
    loading = false;
  });

  async function onSubmit(e) {
    e.preventDefault();
    if (saving) return;
    saving = true;
    error = '';
    savedNote = '';
    const r = await saveAppSettings({
      ownerName,
      ownerTimezone,
      hubUrl,
      metricsUrl,
      guardEnabled,
      guardExtraPaths: guardExtraPathsText.split(/\r?\n/).map((s) => s.trim()).filter(Boolean),
      defaultBackup: defaultBackupModel
        ? (defaultBackupEffort
            ? { model: defaultBackupModel, effort: defaultBackupEffort }
            : { model: defaultBackupModel })
        : null
    });
    saving = false;
    if (r.ok) {
      syncFromStore();
      savedNote = 'Saved.';
    } else {
      error = r.error || 'Could not save settings.';
    }
  }
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') onclose(); }} />

<div use:portal>
  <div class="scrim" onclick={onclose} role="presentation"></div>
  <div class="modal" role="dialog" aria-modal="true" aria-label="app settings">
    <div class="head-row">
      <h2>Settings</h2>
      <button class="close" type="button" onclick={onclose} title="close" aria-label="close">✕</button>
    </div>

    {#if error}
      <div class="err-strip">⚠ {error}</div>
    {/if}
    {#if savedNote}
      <div class="ok-strip">{savedNote}</div>
    {/if}

    <form onsubmit={onSubmit}>
      <div class="field">
        <label for="app-owner-name">Owner name</label>
        <input
          id="app-owner-name"
          type="text"
          bind:value={ownerName}
          placeholder="the user"
          disabled={loading || saving}
        />
        <p class="note">Name shown in UI labels and pulse context. Blank uses “the user”.</p>
      </div>

      <div class="field">
        <label for="app-owner-tz">Time zone</label>
        <input
          id="app-owner-tz"
          type="text"
          bind:value={ownerTimezone}
          placeholder="Browser default (e.g. America/New_York, UTC)"
          disabled={loading || saving}
        />
        <p class="note">IANA time zone name. Blank uses the browser or machine time zone.</p>
      </div>

      <div class="field">
        <label for="app-hub-url">Pages hub URL</label>
        <input
          id="app-hub-url"
          type="text"
          bind:value={hubUrl}
          placeholder="Off until set (e.g. http://127.0.0.1:8080)"
          disabled={loading || saving}
        />
        <p class="note">Base URL of your published-pages server. Leave blank to keep the hub off.</p>
      </div>

      <div class="field">
        <label for="app-metrics-url">Metrics bar URL</label>
        <input
          id="app-metrics-url"
          type="text"
          bind:value={metricsUrl}
          placeholder="Off until set (e.g. http://127.0.0.1:9100/metrics)"
          disabled={loading || saving}
        />
        <p class="note">Base directory URL of a metrics dashboard serving data.json. Leave blank to hide the bar.</p>
      </div>

      <div class="field">
        <label class="check-row" for="app-guard-enabled">
          <input
            id="app-guard-enabled"
            type="checkbox"
            bind:checked={guardEnabled}
            disabled={loading || saving}
          />
          <span>Delete guard enabled</span>
        </label>
        <p class="note">Blocks destructive shell deletes targeting protected directories.</p>
      </div>

      <div class="field">
        <label for="app-guard-paths">Extra protected paths</label>
        <textarea
          id="app-guard-paths"
          rows="3"
          bind:value={guardExtraPathsText}
          placeholder="/path/to/protect (one per line)"
          disabled={loading || saving}
        ></textarea>
        <p class="note">Additional directories guarded from deletion, one path per line.</p>
      </div>

      <div class="field">
        <label for="app-default-backup-model">Default backup model</label>
        <div class="model-row">
          <select
            id="app-default-backup-model"
            bind:value={defaultBackupModel}
            disabled={loading || saving}
          >
            <option value="">None</option>
            {#each modelGroups as g (g.label)}
              <optgroup label={g.label}>
                {#each g.models as m (m.id)}
                  <option value={m.id}>{m.label}</option>
                {/each}
              </optgroup>
            {/each}
          </select>
          {#if backupEffortOpts.length}
            <select
              class="effort-sel"
              bind:value={defaultBackupEffort}
              disabled={loading || saving}
              aria-label="default backup reasoning effort"
            >
              {#if !offLevel(backupEffortOpts)}
                <option value="">effort · default</option>
              {/if}
              {#each backupEffortOpts as e (e)}
                <option value={e}>{e}</option>
              {/each}
            </select>
          {/if}
        </div>
        <p class="note">New pulses get this backup model; a pulse can change its own backup in its settings.</p>
      </div>

      <div class="actions">
        <button class="btn secondary" type="button" onclick={onclose} disabled={saving}>Close</button>
        <button class="btn primary" type="submit" disabled={loading || saving}>
          {saving ? 'Saving…' : 'Save'}
        </button>
      </div>
    </form>
  </div>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: rgba(0, 0, 0, 0.55);
    z-index: 80;
  }
  .modal {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(440px, calc(100vw - 24px));
    max-height: calc(100dvh - 32px);
    overflow-y: auto;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.6);
    z-index: 81;
    padding: 16px;
  }
  .head-row {
    display: flex;
    align-items: center;
    justify-content: space-between;
    margin-bottom: 14px;
  }
  .modal h2 {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.2em;
    color: var(--ink-dim);
    text-transform: uppercase;
  }
  .close {
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
    padding: 2px 6px;
    border-radius: 5px;
  }
  .close:hover { background: var(--surface-warm); color: var(--ink-dim); }
  .err-strip {
    background: color-mix(in srgb, var(--bad) 16%, var(--surface));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 6px 10px;
    border-radius: 8px;
    margin-bottom: 12px;
    word-break: break-word;
  }
  .ok-strip {
    background: color-mix(in srgb, var(--sprite) 14%, var(--surface));
    color: var(--sprite);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 6px 10px;
    border-radius: 8px;
    margin-bottom: 12px;
  }
  .field { margin-bottom: 12px; }
  .field > label {
    display: block;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    color: var(--ink-meta);
    text-transform: uppercase;
    margin-bottom: 5px;
  }
  .field input[type='text'],
  .field textarea,
  .field select {
    width: 100%;
    box-sizing: border-box;
    background: var(--bg);
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    padding: 7px 10px;
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: calc(12px * var(--tscale-ui));
  }
  .field textarea { resize: vertical; line-height: 1.4; }
  .model-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: stretch; }
  .model-row #app-default-backup-model { flex: 1 1 auto; width: auto; min-width: 0; }
  .model-row .effort-sel { flex: 0 0 auto; width: auto; min-width: 9.5em; }
  .field label.check-row {
    display: flex;
    align-items: center;
    gap: 8px;
    text-transform: none;
    letter-spacing: 0.04em;
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-dim);
    cursor: pointer;
    margin-bottom: 2px;
  }
  .field label.check-row input { accent-color: var(--dreamer); }
  .note {
    margin-top: 4px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    line-height: 1.45;
    color: var(--ink-meta);
  }
  .actions {
    display: flex;
    justify-content: flex-end;
    gap: 8px;
    margin-top: 14px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }
  .btn {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.12em;
    text-transform: uppercase;
    padding: 6px 14px;
    border-radius: 8px;
    border: 1px solid var(--border-strong);
    color: var(--ink-dim);
  }
  .btn:hover:not(:disabled) { background: var(--surface-warm); color: var(--ink); }
  .btn.primary {
    border-color: var(--dreamer);
    color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 10%, transparent);
  }
  .btn.primary:hover:not(:disabled) {
    background: color-mix(in srgb, var(--dreamer) 18%, transparent);
    color: var(--ink);
  }
  .btn:disabled { opacity: 0.55; cursor: default; }
</style>
