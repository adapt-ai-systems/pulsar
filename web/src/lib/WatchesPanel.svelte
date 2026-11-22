<script>
  /**
   * WatchesPanel — the pulse's open condition watches, read-only
   * list + delete. Lives inside SettingsModal under Schedules. Watches are
   * registered by the pulse itself over the API; this is where a human sees
   * what it is waiting on (and prunes the orphans that would otherwise wake
   * a dead-weight pulse forever).
   */
  import { listConditionWatches, deleteConditionWatch } from './api.js';
  import { watchLabel, watchDetail } from './watches.js';
  import { pollWhileVisible } from './pollvisible.js';

  let { pulseId } = $props();

  let watches = $state([]);
  let loading = $state(false);
  let error = $state('');
  let confirmId = $state(null);

  async function refresh() {
    loading = true;
    const r = await listConditionWatches(pulseId);
    loading = false;
    if (r.ok) {
      watches = r.value ?? [];
      error = '';
    } else {
      error = r.error;
      console.log('[watches] list failed', pulseId, r.error);
    }
  }

  async function remove(id) {
    const r = await deleteConditionWatch(id);
    confirmId = null;
    if (!r.ok) {
      error = r.error;
      console.log('[watches] delete failed', id, r.error);
      return;
    }
    watches = watches.filter((w) => w.id !== id);
  }

  // Load on open + whenever the modal is pointed at another pulse; poll
  // gently so a watch that fires/closes while the modal is open drops off.
  $effect(() => {
    pulseId;
    return pollWhileVisible(refresh, 10000);
  });
</script>

<div class="field">
  <label>Watches</label>
  {#if loading && watches.length === 0}
    <p class="note">loading…</p>
  {:else if watches.length === 0}
    <p class="note">nothing being watched — the pulse registers these itself (see “Watches” in its base prompt).</p>
  {:else}
    <div class="list">
      {#each watches as w (w.id)}
        <div class="row" class:bad={!!w.bad}>
          <span class="kind" title={w.kind}>{w.kind === 'pulse_state' ? '◉' : w.kind === 'file' ? '▤' : w.kind === 'log' ? '≡' : w.kind === 'http' ? '⇄' : '$'}</span>
          <div class="info">
            <div class="lbl">{watchLabel(w)}</div>
            <div class="sub" title={JSON.stringify(w.spec)}>{watchDetail(w)}</div>
          </div>
          <div class="acts">
            {#if confirmId === w.id}
              <button class="ico del armed" title="confirm delete" onclick={() => remove(w.id)}>confirm?</button>
              <button class="ico" title="cancel" onclick={() => (confirmId = null)}>✕</button>
            {:else}
              <button class="ico del" title="delete watch" onclick={() => (confirmId = w.id)}>×</button>
            {/if}
          </div>
        </div>
      {/each}
    </div>
  {/if}
  {#if error}<p class="note err">watches: {error}</p>{/if}
</div>

<style>
  .field { margin-bottom: 16px; }
  .field label {
    display: block;
    font-size: 11px;
    letter-spacing: 0.08em;
    text-transform: uppercase;
    opacity: 0.7;
    margin-bottom: 6px;
  }
  .note { font-size: 12px; opacity: 0.7; margin: 4px 0; }
  .note.err { opacity: 1; color: var(--danger, #e06c75); }
  .list { display: flex; flex-direction: column; gap: 4px; }
  .row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 6px 8px;
    border-radius: 6px;
    background: var(--panel-2, rgba(255, 255, 255, 0.04));
    font-size: 12px;
  }
  .row.bad { box-shadow: inset 2px 0 0 var(--warn, #e5c07b); }
  .kind { width: 16px; text-align: center; opacity: 0.8; flex: none; }
  .info { flex: 1; min-width: 0; }
  .lbl { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .sub {
    opacity: 0.6;
    font-family: ui-monospace, SFMono-Regular, Menlo, monospace;
    font-size: 11px;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .acts { display: flex; gap: 4px; flex: none; }
  .ico {
    background: transparent;
    border: 1px solid var(--border, rgba(255, 255, 255, 0.15));
    color: inherit;
    border-radius: 4px;
    font-size: 11px;
    padding: 2px 6px;
    cursor: pointer;
  }
  .ico.del.armed { border-color: var(--danger, #e06c75); color: var(--danger, #e06c75); }
</style>
