<script>
  import { watchLabel } from './watches.js';

  /**
   * The "delete this pulse and close its watchers" dialog (moved out of
   * ChatPane.svelte). Presentational: the pane decides when it is open and
   * what confirming does.
   *
   * @type {{
   *   pulse: any, watchers: {condition: any[], delegation: any[]}, deleting: boolean,
   *   onconfirm: (closeWatchers: boolean) => void, oncancel: () => void
   * }}
   */
  let { pulse, watchers, deleting, onconfirm, oncancel } = $props();
</script>

  <div class="scrim" onclick={oncancel} role="presentation"></div>
  <div class="modal delete-modal" role="dialog" aria-modal="true" aria-label="delete pulse — close its watchers">
    <h2>Delete “{pulse.title}”?</h2>
    <p class="delete-note">This pulse is still keeping an eye on things. Deleting it closes these — nothing will wake anyone for them again:</p>
    <ul class="delete-list">
      {#each watchers.condition as w (w.id)}
        <li><span class="k">watch</span> {watchLabel(w)}</li>
      {/each}
      {#each watchers.delegation as d (d.id)}
        <li><span class="k">helper</span> following “{d.target_title ?? d.target_id.slice(0, 8)}” ({d.kind}, {d.phase})</li>
      {/each}
    </ul>
    <div class="modal-actions">
      <button class="cancel" onclick={oncancel} disabled={deleting}>keep it</button>
      <button class="del armed" onclick={() => onconfirm(true)} disabled={deleting}>close {watchers.condition.length + watchers.delegation.length} and delete</button>
    </div>
  </div>

<style>
  .scrim { position: fixed; inset: 0; background: rgba(2, 3, 5, 0.6); z-index: 50; }
  .delete-modal {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(440px, calc(100% - 24px));
    max-height: calc(100% - 40px);
    overflow-y: auto;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.6);
    z-index: 70;
    padding: 16px;
  }
  .delete-modal h2 {
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    letter-spacing: 0.12em;
    color: var(--bad);
    text-transform: uppercase;
    margin: 0 0 10px;
  }
  .delete-note { font-size: calc(12px * var(--tscale-ui)); color: var(--ink-dim); line-height: 1.45; margin: 0 0 10px; }
  .delete-list { list-style: none; margin: 0; padding: 0; font-size: calc(12px * var(--tscale-ui)); color: var(--ink); }
  .delete-list li { padding: 5px 0; border-top: 1px solid var(--border); overflow-wrap: anywhere; }
  .delete-list .k {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-meta);
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 0 4px;
    margin-right: 6px;
  }
  .delete-modal .modal-actions { display: flex; gap: 8px; justify-content: flex-end; margin-top: 14px; }
  .delete-modal .modal-actions button { padding: 7px 14px; border: 1px solid var(--border); border-radius: 8px; color: var(--ink-dim); }
  .delete-modal .modal-actions .del.armed { border-color: var(--bad); color: var(--bad); }
</style>
