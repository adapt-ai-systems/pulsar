<script>
  import { pausePulse, bookmarkPulse, clearBookmark, restorePulse, resetPulse, handoffPulse, forkPulse, archivePulse, isMonitoringChoice, unarchivePulse, renamePulse, setPulseMode, listGrids, movePulseToGrid } from './api.js';
  import { live } from './live.svelte.js';
  import { doubleTap } from './doubletap.js';
  import { confirmArm } from './confirmarm.svelte.js';
  import { runPulseAction } from './pulsetoggle.svelte.js';
  import { PULSE_MIME } from './dnd.js';
  import { defaultHandoffFor, modelLabel } from './models.js';
  import { uiPrefs, setShowToolCalls, setToolCards, setFoldNoise, setHidePeer } from './uiprefs.svelte.js';
  import { htmlCards, setHtmlCards } from './htmlcards.svelte.js';
  import Icon from './Icon.svelte';
  import { tapSplit } from './tapsplit.js';
  import { fmtBookmarkTime } from './time.js';
  import { appSettings, activeTimeZone } from './appsettings.svelte.js';

  /**
   * The head of one chat pane (moved out of ChatPane.svelte): state
   * dot, title and rename, model/context chips, window buttons, the
   * stop/loop buttons and the ⋯ menu with every pulse action in it. It owns the
   * confirm arms, the menu, rename, archive choice, fork, mode and move; the
   * pane keeps the delete pre-flight (`ondelete`), the error strip (`onerror`),
   * the drag flags (`ondragbegin`, `ondragfinish`) and the settings dialog.
   *
   * @type {{
   *   pulse: any, status: any, ws?: any, stopped: boolean,
   *   archived: boolean, running: boolean, fullscreen?: boolean, canReorder: boolean,
   *   armedLoop?: any, stream: any, reasoning: any,
   *   reasoningEnabled: boolean, code: any, codeEnabled: boolean, onchanged: () => void, onerror: (m: string) => void,
   *   onsettings: () => void, ondelete: () => void, ondragbegin: () => void,
   *   ondragfinish: () => void, onoverview?: () => void, onexpand?: (pulse: any) => void,
   *   onfullscreen?: (pulse: any) => void, fsStage?: boolean
   * }}
   */
  let {
    pulse, status, ws = null, stopped, archived, running, fullscreen = false,
    canReorder, armedLoop = null, stream, reasoning, reasoningEnabled,
    code, codeEnabled, onchanged, onerror, onsettings, ondelete, ondragbegin, ondragfinish,
    onoverview, onexpand, onfullscreen,
    fsStage = false           // this pane IS the full-screen stage: the window button leaves the mode
  } = $props();

  // Armed during pointerdown so a press that STARTS on a button never becomes a drag
  // — otherwise you couldn't reliably tap pause/archive/delete.
  let headGrabbable = $state(false);

  // --- double-tap the title bar → overview -------------------------------- //
  // Opt-in via onoverview, which only the mobile deck passes. A double-tap
  // avoids triggering text selection (`user-select: none` on the bar stops
  // double-click's word-select) and does not compete with vertical scrolling.
  //
  // The detector lives in doubletap.js — the app nav carries the same gesture,
  // and it is the one place that knows to swallow the second tap's click (see
  // that file: without it the overview closed itself the instant it opened).
  const detectHeadDoubleTap = doubleTap(() => {
    console.debug('[pane] double-tap title bar → overview (%s)', pulse.id);
    onoverview();
  });

  function onHeadPointerUp(e) {
    // Desktop passes no onoverview: leave double-click on the head alone
    // there. While renaming, taps land in the title input — never a gesture.
    if (onoverview && !renaming) detectHeadDoubleTap(e);
  }

  function onHeadPointerDown(e) {
    headGrabbable = canReorder && !e.target.closest('button, a, input, select, textarea');
  }

  function onDragStart(e) {
    if (!headGrabbable) {
      e.preventDefault();
      return;
    }
    ondragbegin();
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', pulse.id);
    e.dataTransfer.setData(PULSE_MIME, pulse.id);
  }

  function onDragEnd() {
    ondragfinish();
    headGrabbable = false;
  }

  // An armed confirm must disarm itself — left armed, a stray tap minutes
  // later would hard-stop the pulse (found driving the real UI, 2026-07-26).
  const pauseConfirm = confirmArm();

  // Context readout: the runner's heartbeat carries last_ctx_tokens; the
  // ceiling is the pulse's handoff threshold (per-pulse override, else model
  // default). No heartbeat yet → no chip; never invent a number.
  const ctxTokens = $derived(pulse.live?.heartbeat?.last_ctx_tokens ?? null);
  const ctxCeiling = $derived(pulse.handoff_tokens ?? defaultHandoffFor(pulse.model) ?? null);
  const ctxFrac = $derived(
    ctxTokens !== null && ctxCeiling ? ctxTokens / ctxCeiling : 0
  );
  function kfmt(t) {
    return t >= 1000 ? `${Math.round(t / 1000)}k` : `${t}`;
  }

  // Handoffs fired for this pulse: the daemon's own counter
  // (settings.handoff_count, bumped in handoff.py when a handoff completes).
  const handoffCount = $derived(Number(pulse?.settings?.handoff_count) || 0);

  // One call for the head's pulse actions: refresh the roster on ok, show
  // "<label>: <error>" in the pane's error strip otherwise.
  const act = (label, fn) => runPulseAction(label, fn, { onok: () => onchanged?.(), onerr: (m) => onerror(m) });

  // Bookmark ("what happened since I left"). First press sets one; each later
  // press asks the pulse for a summary of everything since the last bookmark
  // and sets a new one. settings.bookmark rides the roster, so phone and
  // desktop show the same state.
  const bookmarkAt = $derived(pulse?.settings?.bookmark?.at ?? null);
  const bookmarkTz = $derived(activeTimeZone(appSettings.ownerTimezone));
  const bookmarkLabel = $derived(bookmarkAt ? fmtBookmarkTime(bookmarkAt, bookmarkTz) : '');
  const bookmarkTitleLabel = $derived(bookmarkAt ? fmtBookmarkTime(bookmarkAt, bookmarkTz, { withZone: true }) : '');
  let bookmarking = $state(false);
  async function doBookmark() {
    if (bookmarking) return;
    bookmarking = true;
    try {
      await act('bookmark', () => bookmarkPulse(pulse.id));
    } finally {
      bookmarking = false;
    }
  }
  async function doClearBookmark() {
    if (!bookmarkAt) return;
    await act('clear bookmark', () => clearBookmark(pulse.id));
  }
  const onBookmarkClick = tapSplit(doBookmark, doClearBookmark);

  async function doPause() {
    pauseConfirm.disarm();
    await act('pause', () => pausePulse(pulse.id));
  }

  // Rename swaps the header title for an input; Enter/blur saves, Escape
  // cancels. A live runner keeps its spawn-time title until next respawn.
  let renaming = $state(false);
  let renameDraft = $state('');
  function startRename() {
    renameDraft = pulse.title ?? '';
    renaming = true;
    menuOpen = false;
  }
  function cancelRename() {
    renaming = false;
  }
  async function doRename() {
    if (!renaming) return;
    const t = renameDraft.trim();
    renaming = false;
    if (!t || t === pulse.title) return;
    await act('rename', () => renamePulse(pulse.id, t));
  }
  /** The pane clips overflow, so a menu taller than the room below
   *  the ⋯ lost its last items (archive). Cap it to the room and scroll. */
  function fitMenu(node) {
    const pane = node.closest('.pane');
    if (!pane) return;
    const room = pane.getBoundingClientRect().bottom
      - node.getBoundingClientRect().top - 6;
    if (node.scrollHeight > room) {
      node.style.maxHeight = `${Math.max(room, 80)}px`;
      node.style.overflowY = 'auto';
    }
  }

  /** Focus+select the rename input the moment it mounts. */
  function focusSelect(node) {
    node.focus();
    node.select();
  }

  async function doRestore() {
    await act('restore', () => restorePulse(pulse.id));
  }

  // Archive is reversible (unarchive sits in its place afterwards), so no
  // confirm — EXCEPT when the pulse still owns live watches/schedules: the
  // daemon answers 409 and we ask (Watchers, 2026-09-07): keep monitoring
  // (it becomes a watching-mode pulse on the shelf) or stop it and archive.
  let archiveChoice = $state(false);
  async function doArchive(monitoring = undefined) {
    await runPulseAction('archive', () => archivePulse(pulse.id, monitoring ? { monitoring } : {}), {
      onok: () => {
        archiveChoice = false;
        onchanged?.();
      },
      onerr: (m, r) => {
        archiveChoice = isMonitoringChoice(r);
        if (!archiveChoice) onerror(m);
      }
    });
  }

  // Organizational mode (Watchers shelf): watching = finished project that
  // sleeps between wakes and keeps an eye on things; paused = set aside.
  // Both leave the grid; clearing brings the pulse back.
  const mode = $derived(pulse.settings?.mode ?? null);
  // Helper role: a pulse spawned with role=helper answers to a lead rather than
  // directly to the user — the state dot becomes a ring and a small "helper"
  // tag names the lead on hover.
  const helperLeadId = $derived(pulse.settings?.role === 'helper' ? (pulse.settings?.lead_id ?? null) : null);
  const helperLeadTitle = $derived(helperLeadId ? (live.roster?.find((p) => p.id === helperLeadId)?.title ?? null) : null);
  let savingMode = $state(false);
  async function setMode(next) {
    savingMode = true;
    await act('mode', () => setPulseMode(pulse.id, next));
    savingMode = false;
  }

  // "move to" in the ⋯ menu: drag-to-tab was the only way to move a
  // pulse between workspaces. Grids load when the menu opens.
  let menuGrids = $state([]);
  $effect(() => {
    if (!menuOpen) return;
    listGrids().then((r) => { if (r.ok) menuGrids = r.value ?? []; });
  });
  async function moveTo(gridId) {
    const r = await movePulseToGrid(pulse.id, gridId || null);
    if (r.ok) onchanged?.();
    else onerror(`move: ${r.error}`);
  }

  async function doUnarchive() {
    await act('unarchive', () => unarchivePulse(pulse.id));
  }

  // Reset throws away the CLI context (fresh session; skills/loops stay) —
  // irreversible, so it arms a confirm that auto-disarms like pause/delete.
  const resetConfirm = confirmArm();
  async function doReset() {
    resetConfirm.disarm();
    menuOpen = false;  // an open menu let the confirm re-arm at once
    await act('reset', () => resetPulse(pulse.id));
  }

  // Handoff captures the conversation context and continues it in a fresh
  // session (the handoff engine writes the prose, then respawns). Unlike reset
  // it CARRIES context over — non-destructive, but it still asks the live
  // session and queuing is one-way, so it arms a confirm like reset.
  const handoffConfirm = confirmArm();
  async function doHandoff() {
    handoffConfirm.disarm();
    menuOpen = false;
    await act('handoff', () => handoffPulse(pulse.id));
  }
  // Forced handoff: the handoff button on a stopped/failed pane asks
  // "force a handoff?" — the successor gets the last ~100k tokens of the
  // transcript plus the transcript file path, since the old model cannot
  // write its own handoff.
  const forceConfirm = confirmArm();
  async function doForceHandoff() {
    forceConfirm.disarm();
    await act('force handoff', () => handoffPulse(pulse.id, { force: true }));
  }

  // Fork branches the conversation into a NEW pulse (pi /tree, pulse-shaped).
  // Non-destructive to the source, so no confirm — but spawning takes a
  // moment, so guard against double-taps while in flight.
  let forking = $state(false);
  async function doFork() {
    if (forking) return;
    forking = true;
    await act('fork', () => forkPulse(pulse.id));
    forking = false;
  }


  // --- head actions menu -------------------------------------------------- //
  // Narrow panes collapse head buttons behind one ⋯ menu.
  // Deliberately NOT closed by clicks inside it: reset/pause/delete arm a
  // "confirm?" button in place, and closing on the first tap would hide the
  // confirmation the user is being asked for.
  let menuOpen = $state(false);

  $effect(() => {
    if (!menuOpen) return;
    const close = (e) => {
      if (!e.target.closest?.('.menu-wrap')) menuOpen = false;
    };
    const esc = (e) => {
      if (e.key === 'Escape') menuOpen = false;
    };
    document.addEventListener('click', close, true);
    document.addEventListener('keydown', esc);
    return () => {
      document.removeEventListener('click', close, true);
      document.removeEventListener('keydown', esc);
    };
  });

  // Delete IS terminal, so it arms a confirm that auto-disarms — same rule as
  // pause above: an armed confirm left sitting is a stray-tap hazard. The pane
  // owns the watcher pre-flight (it may open the delete-watchers dialog).
  const deleteConfirm = confirmArm();
  function doDelete() {
    deleteConfirm.disarm();
    ondelete();
  }
</script>

<div
  class="pane-head"
  class:grabbable={canReorder}
  draggable={headGrabbable}
  onpointerdown={onHeadPointerDown}
  onpointerup={onHeadPointerUp}
  ondragstart={onDragStart}
  ondragend={onDragEnd}
  role="presentation"
>
  <!-- Two rows: title plus the action controls on the first row, then
       model and context chips alone on the second so the model name gets
       the full pane width. -->
  <div class="head-row title-row">
    <span
      class="dot"
      class:working={status.thinking}
      class:stalled={status.stalled}
      class:idle={status.phase === 'idle'}
      class:stopped={stopped || archived}
      class:helper={!!helperLeadId}
      title={status.stalled ? status.evidence : pulse.state}
    ></span>
    {#if renaming}
      <input
        class="t-edit"
        type="text"
        bind:value={renameDraft}
        aria-label="pulse title"
        use:focusSelect
        onkeydown={(e) => {
          if (e.key === 'Enter') { e.preventDefault(); doRename(); }
          if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); cancelRename(); }
        }}
        onblur={doRename}
      />
    {:else}
      <span class="t" title={pulse.title}>{pulse.title}</span>
    {/if}
    {#if helperLeadId}<span class="helper-tag" title={`helper of ${helperLeadTitle ?? 'pulse'} (${helperLeadId.slice(0, 8)})`}>helper</span>{/if}
    {#if ws?.name}<span class="ws-tag" title={`workspace: ${ws.name}`}>{ws.name}</span>{/if}
    {#if onfullscreen || onexpand}
      <!-- One window button: click opens the floating window on a multi-row
           grid, Alt+click jumps straight to full screen; a single-row grid has
           no floating window, so click is full screen. -->
      <button
        class="expand-btn"
        onclick={(e) => {
          e.stopPropagation();
          if (onexpand && !e.altKey) onexpand(pulse);
          else if (onfullscreen) onfullscreen(pulse);
          else onexpand(pulse);
        }}
        title={fsStage ? 'leave full screen' : onexpand ? 'open in floating window · Alt+click: full screen' : 'open full screen'}
        aria-label={fsStage ? 'leave full screen' : onexpand ? 'expand pane' : 'open pane full screen'}
      ><Icon name={fsStage ? 'shrink' : onexpand ? 'expand' : 'screen'} size={13} /></button>
    {/if}
    <!-- Inline: loop state · stop/start · ⋯ — everything else lives in the
         ⋯ menu, grouped. In a fullscreen window the pane head stacks under the
         window head, so stop/start yields too and the ⋯ sits at the right edge. -->
    <div class="btns inline-actions">
      {@render inlineActions()}
    </div>
    <div class="menu-wrap">
      <button
        class="menu-btn"
        onclick={(e) => { e.stopPropagation(); menuOpen = !menuOpen; }}
        aria-expanded={menuOpen}
        aria-haspopup="menu"
        title="actions"
        aria-label="actions"
      >⋯</button>
      {#if menuOpen}
        <div class="menu-pop" role="menu" use:fitMenu>
          <!-- Work actions: reset / handoff / fork / rename / settings, then
               where the pulse lives. -->
          <div class="menu-group btns">
            {@render workActions()}
          </div>
          {#if !archived}
            <!-- Watchers shelf: where this pulse lives. watching / paused take
                 it off the grid; workspace brings it back. -->
            <div class="menu-mode" role="radiogroup" aria-label="where this pulse lives">
              <span class="menu-mode-lbl">lives in</span>
              {#each [[null, 'workspace'], ['watching', 'watchers'], ['paused', 'paused']] as [val, lbl] (lbl)}
                <button
                  class="menu-mode-opt"
                  class:on={mode === val}
                  role="radio"
                  aria-checked={mode === val}
                  disabled={savingMode || mode === val}
                  title={val === 'watching' ? 'finished project — sleeps between watch/schedule wakes, keeps its context, leaves the grid' : val === 'paused' ? 'set aside for later — leaves the grid, one tap to bring back' : 'back on the workspace grid'}
                  onclick={() => setMode(val)}
                >{lbl}</button>
              {/each}
            </div>
            <label class="menu-mode">
              <span class="menu-mode-lbl">workspace</span>
              <select value={pulse.grid_id ?? ''} onchange={(e) => moveTo(e.currentTarget.value)} aria-label="move to workspace">
                <option value="">none</option>
                {#each menuGrids as g (g.id)}
                  <option value={g.id}>{g.name}</option>
                {/each}
              </select>
            </label>
          {/if}
          <div class="menu-sep" role="separator"></div>
          <!-- Reading preferences, not pulse actions. The menu deliberately
               stays open on inside clicks (see the confirm-button note above
               the snippet), so ticking one shows the chat change live. -->
          <div class="menu-group">
          <label class="menu-check">
            <input
              type="checkbox"
              checked={uiPrefs.showToolCalls}
              onchange={(e) => setShowToolCalls(e.currentTarget.checked)}
            />
            tool detail
          </label>
          <label class="menu-check" title="diff cards for edits, file previews, command blocks, and a per-turn changes strip (all panes)">
            <input
              type="checkbox"
              checked={uiPrefs.toolCards}
              onchange={(e) => setToolCards(e.currentTarget.checked)}
            />
            visual tools
          </label>
          <label class="menu-check" title="show the reply as it's generated (this pulse only)">
            <input
              type="checkbox"
              checked={stream.value}
              disabled={stream.saving}
              onchange={(e) => stream.set(e.currentTarget.checked)}
            />
            live streaming
          </label>
          <label class="menu-check" title="collapse runs of daemon watch chatter into one tap-to-expand line, so replies stand out (all panes)">
            <input
              type="checkbox"
              checked={uiPrefs.foldNoise}
              onchange={(e) => setFoldNoise(e.currentTarget.checked)}
            />
            fold watch chatter
          </label>
          <label class="menu-check" title="hide messages from other pulses and watchers in the chat (all panes)">
            <input
              type="checkbox"
              checked={uiPrefs.hidePeer}
              onchange={(e) => setHidePeer(e.currentTarget.checked)}
            />
            hide pulse messages
          </label>
          <label class="menu-check" title="show html/svg blocks as live cards instead of code (all pulses)">
            <input
              type="checkbox"
              checked={htmlCards.on}
              onchange={(e) => setHtmlCards(e.currentTarget.checked)}
            />
            HTML cards
          </label>
          <label class="menu-check" title="let the working status strip open the model's reasoning trace (this pulse only)">
            <input
              type="checkbox"
              checked={reasoningEnabled}
              disabled={reasoning.saving}
              onchange={(e) => reasoning.set(e.currentTarget.checked)}
            />
            reasoning traces
          </label>
          <label class="menu-check" title="stream the code and commands the model is writing into the chat, live (this pulse only)">
            <input
              type="checkbox"
              checked={codeEnabled}
              disabled={code.saving}
              onchange={(e) => code.set(e.currentTarget.checked)}
            />
            live code
          </label>
          </div>
          <div class="menu-sep" role="separator"></div>
          <!-- Lifecycle actions: reversible → destructive. -->
          <div class="menu-group btns">
            {@render lifeActions()}
          </div>
        </div>
      {/if}
    </div>
  </div>
  <div class="head-row meta-row">
    <span class="model-chip" title={pulse.model}>{modelLabel(pulse.model)}</span>
    {#if ctxTokens !== null}
      <span
        class="ctx-chip"
        class:warn={ctxFrac >= 0.75}
        class:hot={ctxFrac >= 0.9}
        title={`context: ${ctxTokens.toLocaleString()} tokens (as of the last completed request — lags during long turns)` + (ctxCeiling ? ` · handoff threshold ${ctxCeiling.toLocaleString()} — NOT the model's context limit` : '')}
      >{kfmt(ctxTokens)}{ctxCeiling ? ` / ${kfmt(ctxCeiling)}` : ''}</span>
    {/if}
    {#if handoffCount > 0}
      <span class="ctx-chip handoff-chip" title={`${handoffCount} context handoff${handoffCount === 1 ? '' : 's'} so far — the runner has been restarted with a fresh context that many times`}>⇄ {handoffCount}</span>
    {/if}
  </div>
</div>

{#snippet inlineActions()}
  <div class="btns-inner">
    <!-- Loop-armed indicator: glows when an enabled loop exists. -->
    <button
      class="loop-armed"
      class:armed={armedLoop}
      title={armedLoop ? `loop every ${armedLoop.interval_minutes}m` : 'no loop armed'}
      aria-label="loop status"
      onclick={() => onsettings()}
    ><Icon name="loop" size={14} /></button>
    {#if !archived}
    <button
      class="bookmark"
      class:set={bookmarkAt}
      disabled={bookmarking}
      title={bookmarkAt
        ? `bookmarked ${bookmarkTitleLabel} — tap for a summary of everything since then (sets a new bookmark) · double-tap to clear`
        : 'bookmark — tap before you step away; tap again when you are back for a summary of what happened'}
      aria-label={bookmarkAt ? 'summary since bookmark' : 'set bookmark'}
      onclick={onBookmarkClick}
    ><Icon name="bookmark" size={14} />{#if bookmarkAt}<span class="bm-time">{bookmarkLabel}</span>{/if}</button>
    {/if}
    {#if !fullscreen}
      {#if stopped}
        <button title="restore" aria-label="restore" onclick={doRestore}><Icon name="play" size={14} /></button>
      {:else if !archived}
        {#if pauseConfirm.armed}
          <button class="paused" title="confirm stop" aria-label="confirm stop" onclick={doPause}>confirm?</button>
          <button title="cancel" aria-label="cancel stop" onclick={() => pauseConfirm.disarm()}>✕</button>
        {:else}
          <button class="stop-btn" title="stop pulse" aria-label="stop pulse" onclick={pauseConfirm.arm}><Icon name="pause" size={14} /></button>
        {/if}
      {/if}
    {/if}
  </div>
{/snippet}

{#snippet workActions()}
  {#if !archived}
    {#if resetConfirm.armed}
      <button class="reset armed" title="confirm fresh context" onclick={doReset}>confirm?</button>
      <button title="cancel" aria-label="cancel reset" onclick={() => resetConfirm.disarm()}>✕</button>
    {:else}
      <button
        class="reset"
        title="reset — fresh context, keeps skills & loops"
        aria-label="reset context"
        onclick={resetConfirm.arm}
      ><Icon name="reset" size={14} /></button>
    {/if}
  {/if}
  {#if !archived}
    {#if running}
      {#if handoffConfirm.armed}
        <button class="handoff armed" title="confirm handoff — pass context to a fresh session" onclick={doHandoff}>confirm?</button>
        <button title="cancel" aria-label="cancel handoff" onclick={() => handoffConfirm.disarm()}>✕</button>
      {:else}
        <button
          class="handoff"
          title="handoff — capture context and continue in a fresh session"
          aria-label="handoff"
          onclick={handoffConfirm.arm}
        ><Icon name="handoff" size={14} /></button>
      {/if}
    {:else if forceConfirm.armed}
      <button class="handoff armed" title="the old session is dead and cannot write a handoff — the successor gets the last ~100k tokens of the transcript plus the path to the full transcript file" onclick={doForceHandoff}>force a handoff?</button>
      <button title="cancel" aria-label="cancel force handoff" onclick={() => forceConfirm.disarm()}>✕</button>
    {:else}
      <button
        class="handoff"
        title="handoff — this session is dead; force a successor seeded from the transcript"
        aria-label="handoff"
        onclick={forceConfirm.arm}
      ><Icon name="handoff" size={14} /></button>
    {/if}
  {/if}
  {#if !archived}
    <button
      title="fork — branch this conversation into a new pulse"
      aria-label="fork pulse"
      disabled={forking}
      onclick={doFork}
    ><Icon name="fork" size={14} /></button>
  {/if}
  <button title="rename" aria-label="rename pulse" onclick={startRename}><Icon name="rename" size={14} /></button>
  <button title="settings" aria-label="settings" onclick={() => onsettings()}><Icon name="settings" size={14} /></button>
{/snippet}

{#snippet lifeActions()}
  {#if archived}
    <button title="unarchive" aria-label="unarchive" onclick={doUnarchive}><Icon name="unarchive" size={14} /></button>
  {:else if archiveChoice}
    <button class="handoff armed" title="keep its watches and schedules — it moves to the Watchers shelf instead of the archive" onclick={() => doArchive('keep')}>keep watching</button>
    <button class="del armed" title="close its watches, disable its schedules, then archive" onclick={() => doArchive('stop')}>stop &amp; archive</button>
    <button title="cancel" aria-label="cancel archive" onclick={() => (archiveChoice = false)}>✕</button>
  {:else}
    <button title="archive" aria-label="archive" onclick={() => doArchive()}><Icon name="archive" size={14} /></button>
  {/if}
  {#if deleteConfirm.armed}
    <button class="del armed" title="confirm delete" onclick={doDelete}>confirm?</button>
    <button title="cancel" aria-label="cancel delete" onclick={() => deleteConfirm.disarm()}>✕</button>
  {:else}
    <!-- The daemon refuses to delete a running pulse (409) — disable rather
         than let the tap fail with an error strip. -->
    <button
      class="del"
      title={running ? 'pause or archive before deleting' : 'delete'}
      aria-label="delete"
      disabled={running}
      onclick={deleteConfirm.arm}
    ><Icon name="delete" size={14} /></button>
  {/if}
{/snippet}

<style>
  /* Drag-reorder affordances: grab the header anywhere that isn't a button. */
  .pane-head.grabbable { cursor: grab; }
  .pane-head.grabbable:active { cursor: grabbing; }
  .pane-head.grabbable button { cursor: pointer; }
  :global(.pane.ws) .pane-head { border-bottom-color: color-mix(in srgb, var(--ws-color) 45%, transparent); }
  .pane-head .ws-tag {
    flex-shrink: 0;
    max-width: 38%;
    overflow: hidden;
    text-overflow: ellipsis;
    padding: 0 6px;
    border-radius: 4px;
    border: 1px solid var(--ws-color);
    color: var(--ws-color);
    background: color-mix(in srgb, var(--ws-color) 12%, transparent);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    text-transform: uppercase;
    line-height: 1.6;
  }
  .pane-head {
    display: flex;
    flex-direction: column;
    gap: 2px;
    padding: 5px 8px;
    border-bottom: 1px solid var(--border-strong);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-dim);
    flex-shrink: 0;
    /* This bar is a control surface, not content. Without these, the overview
       gesture drags its own mess along with it: double-click selects the title
       word, and a double-TAP asks the browser to zoom. */
    user-select: none;
    -webkit-user-select: none;
    touch-action: manipulation;
  }
  .head-row { display: flex; align-items: center; gap: 6px; min-width: 0; width: 100%; }
  .meta-row { flex-wrap: nowrap; line-height: 1.3; }
  .pane-head .t-edit {
    flex: 1;
    min-width: 0;
    background: var(--surface);
    border: 1px solid var(--dreamer);
    border-radius: 6px;
    padding: 1px 6px;
    color: var(--ink);
    font-size: calc(11px * var(--tscale-ui));
  }
  .pane-head .t-edit:focus { outline: none; }
  .pane-head .t {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    color: var(--ink);
    /* The only flexible thing on the row: everything else is flex:none, so the
       title absorbs all the slack instead of being the first to lose it. */
    flex: 1;
    min-width: 0;
    font-size: calc(11px * var(--tscale-ui));
    /* Push the chips and actions to the right edge. */
    margin-right: auto;
  }
  /* Switch on the pane's width rather than the viewport so narrow desktop
     grid columns get the compact header layout too. */
  /* Always visible since 2026-07-31: besides collapsing the actions on narrow
     panes, the menu now carries the tool-detail checkbox, which a wide pane
     needs a way to reach too. */
  .expand-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    flex-shrink: 0;
    padding: 2px 4px;
    border-radius: 5px;
    color: color-mix(in srgb, var(--sprite) 55%, var(--ink-meta));
  }
  .expand-btn:hover { color: var(--sprite); background: var(--surface-warm); }
  .menu-wrap { position: relative; margin-left: auto; flex-shrink: 0; }
  .menu-btn {
    display: flex;
    align-items: center;
    justify-content: center;
    width: 26px;
    height: 22px;
    border-radius: 6px;
    color: var(--ink-dim);
    font-size: calc(14px * var(--tscale-ui));
    line-height: 1;
  }
  .menu-btn:hover { color: var(--ink); background: var(--surface-warm); }
  .menu-pop {
    position: absolute;
    top: calc(100% + 4px);
    right: 0;
    /* Give the menu room without overflowing a narrow grid pane or phone. */
    box-sizing: border-box;
    width: min(300px, calc(100cqw - 24px));
    z-index: 40;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    box-shadow: 0 10px 30px rgb(0 0 0 / 0.5);
    padding: 10px 12px;
    /* Cap the menu height and scroll so lifecycle buttons remain reachable. */
    max-height: 75vh;
    overflow-y: auto;
  }
  .menu-mode {
    display: flex;
    align-items: center;
    gap: 4px;
    margin-top: 7px;
    padding-top: 7px;
    border-top: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-dim);
    white-space: nowrap;
    flex-wrap: wrap;
  }
  .menu-mode-lbl { color: var(--ink-meta); flex-basis: 100%; }
  .menu-mode-opt {
    flex: 1;
    border: 1px solid var(--border);
    border-radius: 4px;
    padding: 1px 6px;
    color: var(--ink-meta);
    font: inherit;
  }
  .menu-mode-opt:hover:not(:disabled) { color: var(--ink); background: var(--surface-warm); }
  .menu-mode-opt.on { color: var(--dreamer); border-color: var(--dreamer); }
  .menu-mode select {
    flex: 1;
    min-width: 0;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    padding: 2px 6px;
    color: var(--ink);
    font: inherit;
  }
  .menu-mode select option {
    background: var(--surface);
    color: var(--ink);
  }
  .menu-check {
    display: flex;
    width: 100%;
    align-items: center;
    gap: 7px;
    margin-top: 7px;
    padding-top: 7px;
    border-top: 1px solid var(--border);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-dim);
    cursor: pointer;
    white-space: nowrap;
  }
  .menu-check input {
    accent-color: var(--dreamer);
    margin: 0;
    cursor: pointer;
  }
  .menu-check select { font: inherit; color: var(--ink); margin-left: auto; }
  .dot { width: 6px; height: 6px; border-radius: 50%; flex-shrink: 0; background: var(--ink-meta); }
  .dot.working { background: var(--sprite); }
  :global(.pane-shell.work-glow) .dot.working,
  :global(.pane-shell.work-glow) .dot.stalled { animation: pulse 1.6s infinite; }
  .dot.idle { background: var(--ink-meta); }
  .dot.stalled { background: var(--bad); animation: pulse 1.6s infinite; }
  .dot.stopped { background: var(--border-strong); }
  /* Helper pulses: same state colors, drawn as a ring instead of a disc. */
  .dot.helper { width: 7px; height: 7px; box-sizing: border-box; background: transparent; border: 1.5px solid var(--ink-meta); }
  .dot.helper.working { border-color: var(--sprite); }
  .dot.helper.stalled { border-color: var(--bad); }
  .dot.helper.stopped { border-color: var(--border-strong); }
  .pane-head .helper-tag {
    flex-shrink: 0;
    padding: 0 5px;
    border-radius: 4px;
    border: 1px dashed var(--ink-meta);
    color: var(--ink-meta);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    text-transform: uppercase;
    line-height: 1.5;
  }
  .pane-head .btns { margin-left: auto; display: flex; gap: 12px; flex-shrink: 0; }
  .btns-inner { display: flex; align-items: center; gap: inherit; }
  /* The popup is grouped into work actions / reading prefs / lifecycle,
     separated by hairlines. */
  .menu-pop { display: flex; flex-direction: column; align-items: stretch; gap: 0; }
  .menu-pop .menu-group { display: flex; align-items: center; flex-wrap: wrap; gap: 7px; row-gap: 6px; padding: 5px 0; }
  .menu-pop .menu-sep { border-top: 1px solid var(--border); }
  .menu-pop .btns { margin-left: 0; }
  .menu-pop .btns-inner { gap: 7px; flex-wrap: wrap; row-gap: 6px; }
  .menu-pop .menu-check, .menu-pop .menu-mode { margin-top: 4px; padding-top: 0; border-top: none; }
  /* Reading prefs: two checkbox columns to keep the menu compact; selects span both. */
  .menu-pop .menu-group:has(.menu-check) {
    display: grid;
    grid-template-columns: 1fr 1fr;
    gap: 2px 8px;
  }
  .menu-pop .menu-group .menu-check { margin-top: 0; white-space: normal; line-height: 1.2; }
  .menu-pop .menu-group .menu-check:has(select) { grid-column: 1 / -1; }
  .menu-pop .menu-group .menu-check select { min-width: 0; flex: 1; text-overflow: ellipsis; }
  /* Desktop grew from 3 head buttons to 5; at 12px they sprawled across the
     header. Mobile keeps the roomier gap — it's a touch target there. */
  @media (min-width: 700px) {
    .pane-head .btns { gap: 2px; }
  }
  .pane-head .btns button {
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-meta);
    padding: 1px 5px;
    border-radius: 4px;
  }
  .pane-head .btns button:hover { background: var(--surface-warm); }
  .pane-head .btns .paused { color: var(--user); }
  /* Each head button carries its own hue (muted toward ink-meta so the row
     stays quiet); hover lifts to the full palette color. */
  .pane-head .btns [aria-label='settings'] { color: color-mix(in srgb, var(--dreamer) 55%, var(--ink-meta)); }
  .pane-head .btns [aria-label='settings']:hover { color: var(--dreamer); }
  .pane-head .btns .stop-btn { display: flex; align-items: center; justify-content: center; color: var(--bad); }
  .pane-head .btns [aria-label='restore'] { color: color-mix(in srgb, var(--sprite) 55%, var(--ink-meta)); }
  .pane-head .btns [aria-label='restore']:hover { color: var(--sprite); }
  .pane-head .btns [aria-label='archive'],
  .pane-head .btns [aria-label='unarchive'] { color: var(--ink-meta); }
  .pane-head .btns [aria-label='archive']:hover,
  .pane-head .btns [aria-label='unarchive']:hover { color: var(--ink-dim); }
  /* Delete is terminal — it's the only head button that goes hot on hover. */
  /* Reset (fresh context): warm warning tint — destructive to context, not to
     the pulse itself, so it reads softer than delete's red. */
  .pane-head .btns .reset { color: color-mix(in srgb, var(--user) 45%, var(--ink-meta)); }
  .pane-head .btns .reset:hover { color: var(--user); }
  .pane-head .btns .reset.armed { color: var(--user); font-size: calc(10px * var(--tscale-ui)); }
  /* Handoff: teal/cyan sprite accent — distinct from reset's warm tint, since
     handoff carries context over rather than wiping it. */
  .pane-head .btns .handoff { color: color-mix(in srgb, var(--sprite) 55%, var(--ink-meta)); }
  .pane-head .btns .handoff:hover { color: var(--sprite); }
  .pane-head .btns .handoff.armed { color: var(--sprite); font-size: calc(10px * var(--tscale-ui)); }
  .pane-head .btns .del { color: color-mix(in srgb, var(--bad) 45%, var(--ink-meta)); }
  .pane-head .btns .del:hover { color: var(--bad); }
  .pane-head .btns .del:disabled { color: var(--border-strong); cursor: default; }
  .pane-head .btns .del:disabled:hover { background: none; }
  .pane-head .btns .del.armed { color: var(--bad); font-size: calc(10px * var(--tscale-ui)); }
  /* Loop-armed indicator: muted sprite by default, full glow when a loop is enabled. */
  .pane-head .btns .loop-armed { color: color-mix(in srgb, var(--sprite) 40%, var(--ink-meta)); }
  .pane-head .btns .loop-armed:hover { color: var(--sprite); }
  .pane-head .btns .loop-armed.armed {
    color: var(--sprite);
    text-shadow: 0 0 8px var(--sprite);
  }
  /* Bookmark: muted ribbon when unset; filled + its time once set. */
  .pane-head .btns .bookmark { touch-action: manipulation; color: var(--ink-meta); display: inline-flex; align-items: center; gap: 3px; }
  .pane-head .btns .bookmark:hover { color: var(--sprite); }
  .pane-head .btns .bookmark.set { color: var(--sprite); }
  .pane-head .btns .bookmark.set :global(svg path) { fill: currentColor; }
  .pane-head .btns .bookmark .bm-time { font-size: calc(9px * var(--tscale-ui)); }
  .model-chip {
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0 6px;
    /* Shrinkable (ellipsis below) so chips do not overflow the meta-row and
       clip the ⋯ menu button off the right edge. */
    min-width: 0;
    white-space: nowrap;
  }
  .ctx-chip {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    border: 1px solid var(--border);
    border-radius: 999px;
    padding: 0 6px;
    /* Shrinkable — see .model-chip: unshrinkable chips pushed the actions
       menu past the pane's clip edge. */
    min-width: 0;
    white-space: nowrap;
  }
  .model-chip, .ctx-chip { overflow: hidden; text-overflow: ellipsis; }
  /* In a narrow pane the model name matters most: the handoff count gives
     way first, then ctx ("Qwen 3.8 Flash" squeezed to "Q…" while
     "⇄ 5" kept its width). */
  .meta-row .ctx-chip.handoff-chip { color: var(--ink-dim); flex-shrink: 8; }
  .meta-row .ctx-chip { flex-shrink: 3; }
  .ctx-chip.warn { color: var(--matron); border-color: var(--matron); }
  .ctx-chip.hot { color: var(--bad); border-color: var(--bad); }
  :global(.pane.fresh) .pane-head {
    background: color-mix(in srgb, var(--fresh-color, #7ff0ff) calc(var(--fresh-heat, 1) * 16%), transparent);
  }
  :global(.pane.fresh) .pane-head .t { color: var(--fresh-color, #7ff0ff); }
  @keyframes pulse {
    0%, 100% { transform: scale(1); opacity: 1; }
    50% { transform: scale(0.7); opacity: 0.6; }
  }
  @media (prefers-reduced-motion: reduce) {
    :global(.pane-shell.work-glow) .dot.working,
    :global(.pane-shell.work-glow) .dot.stalled { animation: none; }
  }
  @media (pointer: coarse) {
    .pane-head .btns button, .menu-btn, .menu-mode-opt { min-width: 44px; min-height: 44px; }
    /* Head buttons keep a tall tap target, with negative margins so they do not
       push the header row open. */
    .pane-head .btns button, .pane-head .menu-btn { min-height: 36px; margin-block: -9px; }
    .pane-head { gap: 4px; }
    .menu-check { min-height: 44px; }
    .menu-pop .menu-group .menu-check { min-height: 34px; }
  }
  /* Compact panes still need stop/restore. Only the unarmed loop indicator
     yields space; loop settings remain available from the menu. */
  @container pane (max-width: 460px) {
    .pane-head .inline-actions { gap: 4px; }
    .inline-actions .loop-armed:not(.armed) { display: none; }
    .inline-actions .bookmark .bm-time { display: none; }
  }
</style>
