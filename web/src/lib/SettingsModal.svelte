<script>
  import {
    listSchedules,
    createSchedule,
    setScheduleEnabled,
    updateSchedule,
    deleteSchedule,
    switchModel,
    setHandoff,
    setBackup,
    setDiet,
    sendMessage,
    listPulses
  } from './api.js';
  import WakeScheduleFields from './WakeScheduleFields.svelte';
  import LoopTriggerFields from './LoopTriggerFields.svelte';
  import LoopTemplatePicker from './LoopTemplatePicker.svelte';
  import { defaultTriggerForm, triggerFormFromList, triggerListFromForm } from './looptriggers.js';
  import WatchesPanel from './WatchesPanel.svelte';
  import SkillsMemoryPicker from './SkillsMemoryPicker.svelte';
  import {
    localDatetimeToUtcIso,
    localHhmmToUtcHhmm,
    scheduleLabel
  } from './schedule.js';
  import {
    groupedModels,
    modelLabel,
    vesselFor,
    defaultHandoffFor,
    effortsFor,
    effortOnModelChange,
    offLevel,
    canonicalEffort
  } from './models.js';
  import { loadModels } from './modelstore.svelte.js';
  import { portal } from './portal.js';
  import { confirmArm } from './confirmarm.svelte.js';

  /**
   * Per-pulse settings panel: schedules, model switch, and the diet
   * (skills, cages, memory).
   *
   * Lists the pulse's existing loop/wake schedules (GET), and lets the user
   * add, disable/enable, and delete them. Delete uses the same 5s auto-disarm
   * inline confirm as ChatPane/SessionDrawer. Every API failure surfaces in a strip.
   *
   * @type {{ pulse: any, onclose: () => void, onchanged?: () => void }}
   */
  let { pulse, onclose, onchanged } = $props();

  let schedules = $state([]);
  let error = $state(''); // panel-level visible error strip
  let loading = $state(true);
  let query = $state(''); // search over schedules when the list gets long

  // --- add-loop form ---
  let loopMinutes = $state('60');
  let loopPrompt = $state('');
  let loopTriggers = $state(defaultTriggerForm());
  let loopMode = $state('any');
  let addingLoop = $state(false);

  // --- edit-existing-loop (click a loop row to open) ---
  let editingId = $state(null);
  let editMinutes = $state('');
  let editPrompt = $state('');
  let editTriggers = $state(defaultTriggerForm());
  let editMode = $state('any');
  let savingEdit = $state(false);

  // --- add-wake form ---
  let wakeMode = $state('none');
  let wakeAtLocal = $state('');
  let wakeHhmm = $state('09:00');
  let wakeDays = $state([]);
  let wakePrompt = $state('');
  let addingWake = $state(false);
  // Optional scheduled cross-pulse message target for the add-wake form.
  // '' = ordinary self-wake. Populated from the same msgTargets.
  let wakeTargetId = $state('');

  // Per-row delete confirm; auto-disarms after 5s (stray-tap safety).
  const deleteConfirm = confirmArm({ armed: null });
  const armDelete = deleteConfirm.arm;
  const disarmDelete = deleteConfirm.disarm;

  // ChatPane re-passes a NEW pulse object identity on every 2s poll. Anything
  // reactive that touches `pulse` directly re-runs on each poll — so key off
  // the stable id value ($derived is equality-memoized) or the modal refetches
  // every 2s and, worse, an effect cleanup would kill the delete-confirm's 5s
  // disarm timer before it ever fires.
  const pulseId = $derived(pulse.id);

  // --- Model section (Milestone 4) ---------------------------------------- //
  const modelGroups = $derived(groupedModels().filter((g) => g.models.length));
  $effect(() => { loadModels(); });
  // The <select> value. Seeded from the pulse's current model and re-seeded
  // ONLY when the pulse identity changes (see the pulseId-keyed effect below),
  // never on the 2s poll — otherwise a poll would stomp an in-progress choice.
  let selectedModel = $state(pulse.model);
  // Reasoning effort rides the same SWITCH action ('' = CLI default).
  let selectedEffort = $state(pulse.settings?.effort ?? '');
  const effortOpts = $derived(effortsFor(selectedModel));
  // Picking a model keeps the chosen effort when the new model offers that
  // level, else takes the new model's default (D5) — never sends a level the
  // CLI doesn't speak (claude "max" -> codex).
  let prevEffortModel = selectedModel;
  $effect(() => {
    if (selectedModel !== prevEffortModel) {
      selectedEffort = effortOnModelChange(selectedEffort, selectedModel);
      prevEffortModel = selectedModel;
    }
    if (selectedEffort && !effortsFor(selectedModel).includes(selectedEffort)) {
      selectedEffort = effortOnModelChange(selectedEffort, selectedModel);
    }
  });
  let switching = $state(false);
  let forceSwitch = $state(pulse.state === 'stopped' && !!pulse.settings?.stop_reason);
  // Cross-vessel switches ask an inline confirm first (same style as delete).
  // Longer than the 12s default: the strip holds a sentence to read.
  const switchConfirm = confirmArm({ ms: 15000 });

  // --- Handoff threshold section ------------------------------------------ //
  // Empty input = null = model-family default. Seeded from the pulse's current
  // handoff_tokens; re-seeded on pulse-id change only.
  let handoffTokens = $state(
    pulse.handoff_tokens == null ? '' : String(pulse.handoff_tokens)
  );
  let savingHandoff = $state(false);

  // --- Backup model ------------------------------------------------------- //
  // settings.backup — the model this pulse fails over to. The vessel is derived by the daemon from the model, so
  // we never send one. Seeded from settings and re-seeded on pulse-id change
  // only (the same fresh-object trap every field above avoids).
  const seedBackup = () => (pulse.settings && pulse.settings.backup) || null;
  let backupModel = $state(seedBackup()?.model ?? '');
  let backupEffort = $state(seedBackup()?.effort ?? '');
  let savingBackup = $state(false);
  const backupEffortOpts = $derived(backupModel ? effortsFor(backupModel) : []);
  // A model whose CLI doesn't speak the chosen level resets to default rather
  // than sending a value the daemon would 400 on.
  $effect(() => {
    if (!backupModel) {
      backupEffort = '';
      return;
    }
    const opts = effortsFor(backupModel);
    if (backupEffort && !opts.includes(backupEffort)) {
      const canon = canonicalEffort(backupEffort, backupModel);
      backupEffort = opts.includes(canon) ? canon : offLevel(opts);
    }
  });

  async function saveBackup() {
    if (savingBackup) return;
    savingBackup = true;
    error = '';
    const body = backupModel ? { model: backupModel, effort: backupEffort } : { model: null };
    const r = await setBackup(pulse.id, body);
    savingBackup = false;
    if (r.ok) {
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `backup model: ${r.error}`;
      console.log('[settings] set backup failed', pulse.id, body, r.error);
    }
  }

  // --- Clean diet section ------------------------------------------------- //
  // A pulse is CLEAN by default. The diet is the opt-ins: {clean, memory,
  // skills[]}. Old pulses may have no settings.diet → treat as the clean
  // default (memory off, no skills). Seeded from pulse.settings.diet and
  // re-seeded ONLY on pulse-id change (never on the 2s poll, which stomps
  // edits). GOTCHA: as everywhere in this file, key off pulseId — reading
  // `pulse` inside an $effect re-runs every poll tick and would reset in-flight
  // checkbox edits (the same fresh-object trap the model/handoff fields avoid).
  const seedDiet = () => (pulse.settings && pulse.settings.diet) || {};
  let dietMemory = $state(!!seedDiet().memory);
  let dietSkills = $state(Array.isArray(seedDiet().skills) ? [...seedDiet().skills] : []);
  let dietCages = $state(Array.isArray(seedDiet().cages) ? [...seedDiet().cages] : []);
  let savingDiet = $state(false);
  // Saved diets don't hot-apply; the daemon applies on next spawn. Show the note
  // after a successful save so the honesty is visible, not buried in a tooltip.
  let dietSaved = $state(false);

  // --- Message-a-pulse section -------------------------------------------- //
  // Send another (non-archived, not-this) pulse a message AS this pulse; any
  // reply is the target's own message back into this chat. Targets are fetched
  // lazily when the section is opened. Keyed off pulseId like the other fields.
  let msgOpen = $state(false);
  let msgTargets = $state([]); // {id,title,state} of other non-archived pulses
  let msgTargetId = $state('');
  let msgText = $state('');
  let msgQuery = $state('');   // live-filter when the target list is long
  let msgSending = $state(false);
  let msgTargetsLoaded = $state(false);
  let msgSent = $state(null);  // {title} after a successful send, for confirm

  // Re-seed the model select + handoff field + diet when the pulse IDENTITY
  // changes. Keyed off pulseId (equality-memoized) so the 2s poll — which
  // re-passes a new pulse object with the SAME id — does not reset in-flight
  // edits.
  let seededFor = $state(null);
  $effect(() => {
    if (seededFor !== pulseId) {
      seededFor = pulseId;
      selectedModel = pulse.model;
      forceSwitch = pulse.state === 'stopped' && !!pulse.settings?.stop_reason;
      selectedEffort = pulse.settings?.effort
        ?? offLevel(effortsFor(pulse.model));
      handoffTokens = pulse.handoff_tokens == null ? '' : String(pulse.handoff_tokens);
      const b = seedBackup();
      backupModel = b?.model ?? '';
      backupEffort = b?.effort ?? '';
      const d = seedDiet();
      dietMemory = !!d.memory;
      dietSkills = Array.isArray(d.skills) ? [...d.skills] : [];
      dietCages = Array.isArray(d.cages) ? [...d.cages] : [];
      dietSaved = false;
      // Reset the message form for the new pulse and force a target re-fetch
      // (the exclude-self filter differs per pulse).
      msgTargetId = '';
      msgText = '';
      msgSent = null;
      wakeTargetId = '';
      msgTargetsLoaded = false;
      msgTargets = [];
    }
  });

  async function saveDiet() {
    if (savingDiet) return;
    savingDiet = true;
    error = '';
    // clean is always true — it IS the baseline; memory + skills are the opt-ins.
    const r = await setDiet(pulse.id, {
      clean: true,
      memory: dietMemory,
      skills: dietSkills,
      cages: dietCages
    });
    savingDiet = false;
    if (r.ok) {
      error = '';
      dietSaved = true;
      onchanged?.();
    } else {
      error = `diet: ${r.error}`;
      console.log('[settings] save diet failed', pulse.id, r.error);
    }
  }

  async function loadMsgTargets() {
    if (msgTargetsLoaded) return;
    msgTargetsLoaded = true;
    // Exclude archived (never messageable) and this pulse itself.
    const r = await listPulses();
    if (r.ok) {
      msgTargets = (r.value ?? [])
        .filter((p) => p.id !== pulse.id && p.state !== 'archived')
        .map((p) => ({ id: p.id, title: p.title, state: p.state }));
    } else {
      error = `message targets: ${r.error}`;
      console.log('[settings] load message targets failed', r.error);
      msgTargetsLoaded = false; // allow retry on next open
    }
  }

  function toggleMsg() {
    msgOpen = !msgOpen;
    if (msgOpen) loadMsgTargets();
  }

  const filteredTargets = $derived.by(() => {
    const q = msgQuery.trim().toLowerCase();
    if (!q) return msgTargets;
    return msgTargets.filter((t) => t.title.toLowerCase().includes(q));
  });

  async function doSendPulseMessage() {
    if (msgSending) return;
    if (!msgTargetId) {
      error = 'Pick a pulse to message.';
      return;
    }
    if (!msgText.trim()) {
      error = 'A message needs text — what are you asking?';
      return;
    }
    msgSending = true;
    error = '';
    const r = await sendMessage(msgTargetId, msgText.trim(), pulse.id);
    msgSending = false;
    if (r.ok) {
      error = '';
      const t = msgTargets.find((t) => t.id === msgTargetId);
      msgSent = { title: t?.title ?? 'the pulse' };
      msgText = '';
    } else {
      error = `message: ${r.error}`;
      console.log('[settings] message failed', pulse.id, msgTargetId, r.error);
    }
  }

  function armSwitch() {
    switchConfirm.arm();
  }
  function disarmSwitch() {
    switchConfirm.disarm();
  }

  async function doSwitch() {
    disarmSwitch();
    if (switching) return;
    const newVessel = vesselFor(selectedModel);
    switching = true;
    error = '';
    const r = await switchModel(pulse.id, selectedModel, newVessel, selectedEffort, forceSwitch);
    switching = false;
    if (r.ok) {
      error = '';
      await refresh();
      onchanged?.(); // parent re-fetches the pulse; header/select reflect it
    } else {
      error = `switch model: ${r.error}`;
      console.log('[settings] switch model failed', pulse.id, selectedModel, r.error);
    }
  }

  // Entry point for the SWITCH button: if the chosen model's vessel differs from
  // the pulse's current vessel, arm the cross-CLI confirm first; otherwise go.
  function onSwitchClick() {
    const curEffort = pulse.settings?.effort ?? '';
    if (!forceSwitch && selectedModel === pulse.model && selectedEffort === curEffort) {
      error = 'That is already the current model & effort.';
      return;
    }
    const newVessel = vesselFor(selectedModel);
    if (forceSwitch || (newVessel && newVessel !== pulse.vessel)) {
      armSwitch();
    } else {
      doSwitch();
    }
  }

  async function saveHandoff() {
    if (savingHandoff) return;
    let value = null;
    const raw = handoffTokens.trim();
    if (raw !== '') {
      value = parseInt(raw, 10);
      if (!Number.isFinite(value) || value <= 0) {
        error = 'Handoff threshold must be a positive number of tokens (or blank for the model default).';
        return;
      }
    }
    savingHandoff = true;
    error = '';
    const r = await setHandoff(pulse.id, value);
    savingHandoff = false;
    if (r.ok) {
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `handoff: ${r.error}`;
      console.log('[settings] set handoff failed', pulse.id, value, r.error);
    }
  }

  async function refresh() {
    // Also warm the message-target list so the add-wake "message another
    // pulse" picker has options without waiting for the message
    // section to be opened. Idempotent (guarded by msgTargetsLoaded).
    loadMsgTargets();
    const r = await listSchedules(pulseId);
    if (r.ok) {
      schedules = r.value ?? [];
      error = '';
    } else {
      error = `schedules: ${r.error}`;
      console.log('[settings] list schedules failed', pulseId, r.error);
    }
    loading = false;
  }

  $effect(() => {
    pulseId;
    refresh();
  });

  // An armed enabled loop drives the pane's ⟳ indicator; derive it for context.
  const armedLoop = $derived(
    schedules.find((s) => s.kind === 'loop' && s.enabled) ?? null
  );

  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return schedules;
    return schedules.filter((s) =>
      `${s.kind} ${s.prompt ?? ''} ${scheduleLabel(s)}`.toLowerCase().includes(q)
    );
  });

  async function addLoop() {
    if (addingLoop) return;
    const min = parseInt(loopMinutes, 10);
    if (!Number.isFinite(min) || min <= 0) {
      error = 'Loop interval must be a positive number of minutes.';
      return;
    }
    if (!loopPrompt.trim()) {
      error = 'A loop needs a prompt — what should it do each run?';
      return;
    }
    const trig = triggerListFromForm(loopTriggers);
    if (trig.error) {
      error = trig.error;
      return;
    }
    addingLoop = true;
    const r = await createSchedule(pulse.id, {
      kind: 'loop',
      prompt: loopPrompt.trim(),
      interval_minutes: min,
      triggers: trig.list,
      trigger_mode: loopMode
    });
    addingLoop = false;
    if (r.ok) {
      loopPrompt = '';
      loopTriggers = defaultTriggerForm();
      loopMode = 'any';
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `add loop: ${r.error}`;
      console.log('[settings] add loop failed', pulse.id, r.error);
    }
  }

  function applyLoopTemplate(t) {
    loopMinutes = String(t.minutes);
    loopPrompt = t.prompt;
    loopTriggers = triggerFormFromList(t.triggers);
    loopMode = t.trigger_mode === 'all' ? 'all' : 'any';
    error = '';
  }

  async function addWake() {
    if (addingWake) return;
    let body = null;
    if (wakeMode === 'once') {
      const at_iso = localDatetimeToUtcIso(wakeAtLocal);
      if (!at_iso) {
        error = 'Pick a valid one-shot wake date/time.';
        return;
      }
      body = { kind: 'once', prompt: wakePrompt.trim(), at_iso };
    } else if (wakeMode === 'daily') {
      const cron_hhmm = localHhmmToUtcHhmm(wakeHhmm);
      if (!cron_hhmm) {
        error = 'Pick a valid recurring time (HH:MM).';
        return;
      }
      body = { kind: 'daily', prompt: wakePrompt.trim(), cron_hhmm };
      if (wakeDays.length) body.days = wakeDays.join(',');
    } else {
      error = 'Choose ONE-SHOT or RECURRING first.';
      return;
    }
    if (!wakePrompt.trim()) {
      error = 'A wake schedule needs a prompt.';
      return;
    }
    // Scheduled cross-pulse message: if a target is chosen, the
    // prompt is delivered to the TARGET as a message from this pulse; any
    // reply is the target's own message back. Empty = self-wake.
    if (wakeTargetId) body.target_pulse_id = wakeTargetId;
    addingWake = true;
    const r = await createSchedule(pulse.id, body);
    addingWake = false;
    if (r.ok) {
      wakePrompt = '';
      wakeMode = 'none';
      wakeDays = [];
      wakeTargetId = '';
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `add wake: ${r.error}`;
      console.log('[settings] add wake failed', pulse.id, r.error);
    }
  }

  async function toggle(s) {
    const r = await setScheduleEnabled(s.id, !s.enabled);
    if (r.ok) {
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `toggle: ${r.error}`;
      console.log('[settings] toggle schedule failed', s.id, r.error);
    }
  }

  function startEdit(s) {
    editingId = s.id;
    editMinutes = s.interval_minutes == null ? '' : String(s.interval_minutes);
    editPrompt = s.prompt ?? '';
    editTriggers = triggerFormFromList(s.triggers);
    editMode = s.trigger_mode === 'all' ? 'all' : 'any';
    disarmDelete();
  }

  function cancelEdit() {
    editingId = null;
  }

  // Svelte action: grow the edit textarea to fit its content so long loop
  // prompts are fully readable while editing (min height via rows attr).
  function autogrow(node) {
    const fit = () => {
      node.style.height = 'auto';
      node.style.height = `${node.scrollHeight}px`;
    };
    fit();
    node.addEventListener('input', fit);
    return { destroy: () => node.removeEventListener('input', fit) };
  }

  async function saveEdit(s) {
    if (savingEdit) return;
    const min = parseInt(editMinutes, 10);
    if (!Number.isFinite(min) || min <= 0) {
      error = 'Loop interval must be a positive number of minutes.';
      return;
    }
    if (!editPrompt.trim()) {
      error = 'A loop needs a prompt — what should it do each run?';
      return;
    }
    const trig = triggerListFromForm(editTriggers);
    if (trig.error) {
      error = trig.error;
      return;
    }
    savingEdit = true;
    const r = await updateSchedule(s.id, {
      prompt: editPrompt.trim(),
      interval_minutes: min,
      triggers: trig.list,
      trigger_mode: editMode
    });
    savingEdit = false;
    if (r.ok) {
      editingId = null;
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `edit loop: ${r.error}`;
      console.log('[settings] edit loop failed', s.id, r.error);
    }
  }

  async function remove(id) {
    disarmDelete();
    const r = await deleteSchedule(id);
    if (r.ok) {
      error = '';
      await refresh();
      onchanged?.();
    } else {
      error = `delete: ${r.error}`;
      console.log('[settings] delete schedule failed', id, r.error);
    }
  }
</script>

<svelte:document onkeydown={(e) => { if (e.key === 'Escape') { e.preventDefault(); onclose(); } }} />

<!-- Portalled to <body>: the mobile deck is a transformed track, which
     would otherwise be the containing block for these fixed elements.
     See portal.js. -->
<div use:portal>
  <div class="scrim" onclick={onclose} role="presentation"></div>
  <div class="modal" role="dialog" aria-modal="true" aria-label="pulse settings">
    <div class="head-row">
      <h2>Pulse · {pulse.title}</h2>
      <button class="close" onclick={onclose} title="close" aria-label="close">✕</button>
    </div>

    {#if error}
      <div class="err-strip">⚠ {error}</div>
    {/if}

    <!-- Model — switch mid-session (Milestone 4). ------------------------- -->
    <div class="field">
      <label for="set-model">Model</label>
      <div class="cur">current · {modelLabel(pulse.model)}{pulse.settings?.effort ? ` · effort ${pulse.settings.effort}` : ''}</div>
      <div class="model-row">
        <select id="set-model" bind:value={selectedModel} class="grid-sel" disabled={switching}>
          {#each modelGroups as g (g.label)}
            <optgroup label={g.label}>
              {#each g.models as m (m.id)}
                <option value={m.id}>{m.label}</option>
              {/each}
            </optgroup>
          {/each}
        </select>
        {#if effortOpts.length}
          <select
            class="grid-sel effort-sel"
            bind:value={selectedEffort}
            disabled={switching}
            aria-label="reasoning effort"
          >
            {#if !offLevel(effortOpts)}
              <option value="">effort · default</option>
            {/if}
            {#each effortOpts as e (e)}
              <option value={e}>{e}</option>
            {/each}
          </select>
        {/if}
        <button class="switch-btn" onclick={onSwitchClick} disabled={switching}>
          {switching ? 'switching…' : 'SWITCH'}
        </button>
      </div>
      <label class="cur">
        <input type="checkbox" bind:checked={forceSwitch} disabled={switching} />
        Skip the notes — start the new model from the chat history (use this if the current model isn't answering)
      </label>
      {#if switchConfirm.armed}
        <div class="switch-confirm">
          <span>{forceSwitch || pulse.state === 'stopped'
            ? 'Start the new model from the chat history, without notes from the current one. Switch?'
            : 'The current model writes notes for the new one first (up to a minute). Switch?'}</span>
          <div class="confirm-acts">
            <button class="ico del armed" onclick={doSwitch}>confirm?</button>
            <button class="ico" onclick={disarmSwitch}>✕</button>
          </div>
        </div>
      {/if}
    </div>

    <!-- Backup model ------------------------------------------------------- -->
    <div class="field">
      <label for="set-backup-model">Backup model</label>
      <div class="cur">
        {#if pulse.settings?.backup?.model}
          backup · {modelLabel(pulse.settings.backup.model)}{pulse.settings.backup
            .effort
            ? ` · effort ${pulse.settings.backup.effort}`
            : ''}
        {:else}
          backup off
        {/if}
      </div>
      <div class="model-row">
        <select
          id="set-backup-model"
          bind:value={backupModel}
          class="grid-sel"
          disabled={savingBackup}
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
            class="grid-sel effort-sel"
            bind:value={backupEffort}
            disabled={savingBackup}
            aria-label="backup reasoning effort"
          >
            {#if !offLevel(backupEffortOpts)}
              <option value="">effort · default</option>
            {/if}
            {#each backupEffortOpts as e (e)}
              <option value={e}>{e}</option>
            {/each}
          </select>
        {/if}
        <button class="switch-btn" onclick={saveBackup} disabled={savingBackup}>
          {savingBackup ? 'saving…' : 'SAVE'}
        </button>
      </div>
      <p class="spawn-note">
        if this pulse's model fails, Pulsar moves the pulse to this model and
        re-sends the message it was working on. It stays there until you
        switch it back.
      </p>
    </div>

    <!-- Handoff threshold ---------------------------------------------------- -->
    <div class="field">
      <label for="set-handoff">Handoff at</label>
      <div class="cur">
        {#if pulse.handoff_tokens == null}
          model default ({defaultHandoffFor(pulse.model) ?? '—'})
        {:else}
          {pulse.handoff_tokens} tokens
        {/if}
      </div>
      <div class="model-row">
        <input
          id="set-handoff"
          class="handoff-input"
          type="text"
          inputmode="numeric"
          placeholder="blank = model default"
          bind:value={handoffTokens}
          aria-label="handoff threshold tokens"
        />
        <button class="switch-btn" onclick={saveHandoff} disabled={savingHandoff}>
          {savingHandoff ? 'saving…' : 'SAVE'}
        </button>
      </div>
    </div>

    <!-- Clean diet — skills + memory opt-ins ------------------------------- -->
    <div class="field">
      <label>Skills, cages &amp; memory</label>
      <SkillsMemoryPicker bind:memory={dietMemory} bind:skills={dietSkills} bind:cages={dietCages} vessel={pulse.vessel} />
      <button class="add-btn" onclick={saveDiet} disabled={savingDiet}>
        {savingDiet ? 'saving…' : '+ save diet'}
      </button>
      {#if dietSaved}
        <!-- Honest applies-on-next-spawn note (PATCH stores now, spawns later). -->
        <p class="spawn-note">
          saved · applies on this pulse's next spawn (restore, model switch,
          handoff, or scheduled wake) — a running pulse keeps its current skills
          until then.
        </p>
      {/if}
    </div>

    <!-- Message — send to another pulse ------------------------------------ -->
    <div class="field">
      <label>Message another pulse</label>
      <button type="button" class="disclosure" onclick={toggleMsg} aria-expanded={msgOpen}>
        <span class="chev" class:open={msgOpen}>▸</span>
        <span class="lead">send a pulse a message</span>
      </button>
      {#if msgOpen}
        <div class="msg-body">
          {#if msgSent}
            <p class="spawn-note ok">
              message sent to {msgSent.title} — if you asked for a reply, it
              arrives in this chat as a message from that pulse.
            </p>
          {/if}
          {#if msgTargets.length > 4}
            <input
              class="sched-search"
              type="text"
              placeholder="search pulses…"
              bind:value={msgQuery}
              aria-label="search pulses"
            />
          {/if}
          {#if msgTargets.length === 0}
            <p class="note">no other pulses to message.</p>
          {:else}
            <select class="grid-sel" bind:value={msgTargetId} aria-label="message target">
              <option value="">choose a pulse…</option>
              {#each filteredTargets as t (t.id)}
                <option value={t.id}>{t.title}{t.state === 'stopped' ? ' (paused — will wake)' : ''}</option>
              {/each}
            </select>
            <textarea
              rows="3"
              bind:value={msgText}
              placeholder="what do you want to tell this pulse?"
            ></textarea>
            <button class="add-btn" onclick={doSendPulseMessage} disabled={msgSending}>
              {msgSending ? 'sending…' : '+ send'}
            </button>
          {/if}
        </div>
      {/if}
    </div>

    <!-- Existing schedules --------------------------------------------------- -->
    <div class="field">
      <label>Schedules</label>
      {#if schedules.length > 4}
        <input
          class="sched-search"
          type="text"
          placeholder="search schedules…"
          bind:value={query}
          aria-label="search schedules"
        />
      {/if}
      {#if loading}
        <p class="note">loading…</p>
      {:else if schedules.length === 0}
        <p class="note">no schedules yet — add a loop or wake below.</p>
      {:else}
        <div class="sched-list">
          {#each filtered as s (s.id)}
            <div class="sched-row" class:off={!s.enabled} class:editing={editingId === s.id}>
              <span
                class="kind"
                class:loop={s.kind === 'loop'}
                class:armed={s.kind === 'loop' && s.enabled}
              >{s.kind === 'loop' ? '⟳' : '☾'}</span>
              {#if editingId === s.id && s.kind === 'loop'}
                <div class="info edit-form">
                  <div class="inline-num">
                    every
                    <input
                      type="text"
                      inputmode="numeric"
                      bind:value={editMinutes}
                      aria-label="edit loop interval minutes"
                    />
                    minutes
                  </div>
                  <textarea
                    rows="8"
                    use:autogrow
                    bind:value={editPrompt}
                    aria-label="edit loop prompt"
                  ></textarea>
                  <LoopTriggerFields bind:form={editTriggers} bind:mode={editMode} idPrefix="edit-lt" />
                  <div class="edit-acts">
                    <button class="ico save" disabled={savingEdit} onclick={() => saveEdit(s)}>save</button>
                    <button class="ico" onclick={cancelEdit}>cancel</button>
                  </div>
                </div>
              {:else if s.kind === 'loop'}
                <div
                  class="info clickable"
                  role="button"
                  tabindex="0"
                  title="click to read + edit"
                  onclick={() => startEdit(s)}
                  onkeydown={(e) => e.key === 'Enter' && startEdit(s)}
                >
                  <div class="lbl">{scheduleLabel(s)}{s.enabled ? '' : ' · off'}</div>
                  <div class="prompt" title={s.prompt}>{s.prompt}</div>
                </div>
              {:else}
                <div class="info">
                  <div class="lbl">{scheduleLabel(s)}{s.enabled ? '' : ' · off'}</div>
                  <div class="prompt" title={s.prompt}>{s.prompt}</div>
                </div>
              {/if}
              <div class="acts">
                <button
                  class="ico"
                  title={s.enabled ? 'disable' : 'enable'}
                  onclick={() => toggle(s)}
                >{s.enabled ? 'ON' : 'OFF'}</button>
                {#if deleteConfirm.armed === s.id}
                  <button class="ico del armed" title="confirm delete" onclick={() => remove(s.id)}>confirm?</button>
                  <button class="ico" title="cancel" onclick={disarmDelete}>✕</button>
                {:else}
                  <button class="ico del" title="delete" onclick={() => armDelete(s.id)}>×</button>
                {/if}
              </div>
            </div>
          {/each}
          {#if filtered.length === 0}
            <p class="note">no schedules match.</p>
          {/if}
        </div>
      {/if}
    </div>

    <!-- Condition watches -------------------------------------------------- -->
    <WatchesPanel {pulseId} />

    <!-- Add loop ------------------------------------------------------------- -->
    <div class="field">
      <label>Add loop</label>
      <LoopTemplatePicker
        getForm={() => ({ minutes: loopMinutes, prompt: loopPrompt, triggerForm: loopTriggers, mode: loopMode })}
        onpick={applyLoopTemplate}
        onerror={(m) => (error = m)}
      />
      <div class="inline-num">
        every
        <input type="text" inputmode="numeric" bind:value={loopMinutes} aria-label="loop interval minutes" />
        minutes
        {#if armedLoop}
          <span class="armed-tag">ARMED ⟳</span>
        {/if}
      </div>
      <textarea
        rows="2"
        bind:value={loopPrompt}
        placeholder="loop prompt — what should it do each run?"
      ></textarea>
      <LoopTriggerFields bind:form={loopTriggers} bind:mode={loopMode} idPrefix="add-lt" />
      <button class="add-btn" onclick={addLoop} disabled={addingLoop}>
        {addingLoop ? 'adding…' : '+ add loop'}
      </button>
    </div>

    <!-- Add wake ------------------------------------------------------------- -->
    <div class="field">
      <label>Add wake schedule</label>
      <WakeScheduleFields
        bind:mode={wakeMode}
        bind:atLocal={wakeAtLocal}
        bind:hhmm={wakeHhmm}
        bind:days={wakeDays}
        bind:targetId={wakeTargetId}
        targets={msgTargets}
      />
      {#if wakeMode !== 'none'}
        <textarea
          rows="2"
          bind:value={wakePrompt}
          placeholder={wakeTargetId ? 'prompt to send the target pulse…' : 'wake prompt — what should it do when it wakes?'}
        ></textarea>
        <button class="add-btn" onclick={addWake} disabled={addingWake}>
          {addingWake ? 'adding…' : '+ add wake'}
        </button>
      {/if}
    </div>

    <div class="modal-actions">
      <button class="cancel" onclick={onclose}>Close</button>
    </div>
  </div>
</div>

<style>
  .scrim {
    position: fixed;
    inset: 0;
    background: rgba(2, 3, 5, 0.6);
    z-index: 50;
  }
  .modal {
    position: fixed;
    top: 50%;
    left: 50%;
    transform: translate(-50%, -50%);
    width: min(420px, calc(100% - 24px));
    max-height: calc(100% - 40px);
    overflow-y: auto;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.6);
    z-index: 70;
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
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .close {
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
    padding: 2px 6px;
    border-radius: 5px;
    flex-shrink: 0;
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

  /* Model + handoff sections (Milestone 4). */
  .cur {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--ink-dim);
    margin-bottom: 6px;
  }
  .model-row { display: flex; flex-wrap: wrap; gap: 6px; align-items: stretch; } /* wraps so the model name isn't cut ('Qwen 3.8 Fla') */
  /* Effort shares the row with the model select; the model name is the long
     text, so let effort take the smaller share. */
  /* Basis auto = the widest option's width, so the row wraps before the
     model name is cut ('Qwen 3.8 Fla' — flex:1's zero basis shrank it). */
  .model-row #set-model, .model-row #set-backup-model { flex: 1 1 auto; }
  .model-row .effort-sel { flex: 0 0 auto; min-width: 9.5em; } /* 'effort · default' was cut to 'effort · defau' */
  .grid-sel {
    flex: 1;
    min-width: 0;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
  }
  .handoff-input {
    flex: 1;
    min-width: 0;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
  }
  .handoff-input::placeholder { color: var(--ink-meta); }
  .switch-btn {
    flex-shrink: 0;
    padding: 6px 14px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--ink-dim);
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.12em;
  }
  .switch-btn:hover:not(:disabled) { border-color: var(--dreamer); color: var(--dreamer); }
  .switch-btn:disabled { opacity: 0.5; cursor: default; }
  .switch-confirm {
    margin-top: 8px;
    display: flex;
    align-items: center;
    justify-content: space-between;
    gap: 8px;
    background: color-mix(in srgb, var(--bad) 12%, var(--surface));
    border: 1px solid var(--bad);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-dim);
  }
  .switch-confirm .confirm-acts { display: flex; gap: 3px; flex-shrink: 0; }
  .switch-confirm .ico {
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 3px 7px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    line-height: 1;
  }
  .switch-confirm .ico:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .switch-confirm .ico.del.armed { color: var(--bad); border-color: var(--bad); }

  .field { margin-bottom: 16px; }
  .field label {
    display: block;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    color: var(--ink-meta);
    text-transform: uppercase;
    margin-bottom: 6px;
  }
  .note {
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-meta);
    font-style: italic;
  }
  /* Streaming toggle: a single checkbox row, so it overrides the block label. */
  .field label.stream-row {
    display: flex;
    align-items: center;
    gap: 8px;
    text-transform: none;
    letter-spacing: 0.04em;
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-dim);
    cursor: pointer;
    margin-bottom: 0;
  }
  .stream-row input { accent-color: var(--dreamer); margin: 0; cursor: pointer; }

  .sched-search {
    width: 100%;
    margin-bottom: 8px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 6px 10px;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
  }
  .sched-search::placeholder { color: var(--ink-meta); }

  .sched-list {
    display: flex;
    flex-direction: column;
    border: 1px solid var(--border);
    border-radius: 9px;
    overflow: hidden;
  }
  .sched-row {
    display: flex;
    align-items: flex-start;
    gap: 9px;
    padding: 8px 10px;
    border-bottom: 1px solid var(--border);
  }
  .sched-row:last-child { border-bottom: none; }
  .sched-row.off { opacity: 0.5; }
  .kind {
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink-meta);
    flex-shrink: 0;
    line-height: 1.3;
  }
  .kind.armed { color: var(--sprite); text-shadow: 0 0 8px var(--sprite); }
  .info { min-width: 0; flex: 1; }
  .info.clickable { cursor: pointer; }
  .info.clickable:hover .prompt { color: var(--ink); }
  .sched-row.editing { flex-wrap: nowrap; }
  .edit-form textarea {
    width: 100%;
    margin-top: 5px;
    resize: vertical;
    overflow: hidden;
    min-height: 12em;
    font-size: calc(13px * var(--tscale-ui));
    line-height: 1.45;
  }
  .edit-acts { display: flex; gap: 5px; margin-top: 5px; }
  .edit-acts .ico {
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 3px 9px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    line-height: 1;
  }
  .edit-acts .ico.save:hover { color: var(--ink); background: var(--surface-warm); }
  .edit-acts .ico:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .info .lbl {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--ink);
  }
  .info .prompt {
    font-size: calc(11.5px * var(--tscale-ui));
    color: var(--ink-dim);
    margin-top: 2px;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .acts { display: flex; gap: 3px; align-items: center; flex-shrink: 0; }
  .acts .ico {
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 3px 7px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    line-height: 1;
  }
  .acts .ico:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .acts .del:hover { color: var(--bad); border-color: var(--bad); }
  .acts .del.armed {
    color: var(--bad);
    border-color: var(--bad);
  }

  .inline-num {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--ink-dim);
    font-size: calc(12px * var(--tscale-ui));
  }
  .inline-num input {
    width: 64px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 7px 8px;
    text-align: center;
    color: var(--ink);
  }
  .armed-tag {
    color: var(--sprite);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.06em;
  }

  textarea {
    width: 100%;
    margin-top: 8px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
    resize: none;
    font-family: var(--font-sans);
  }
  textarea::placeholder { color: var(--ink-meta); }

  .add-btn {
    margin-top: 8px;
    padding: 6px 14px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--ink-dim);
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.12em;
  }
  .add-btn:hover { border-color: var(--dreamer); color: var(--dreamer); }
  .add-btn:disabled { opacity: 0.5; cursor: default; }

  /* Applies-on-next-spawn note + message confirm. */
  .spawn-note {
    margin-top: 8px;
    font-size: calc(10.5px * var(--tscale-ui));
    line-height: 1.45;
    color: var(--ink-dim);
    background: color-mix(in srgb, var(--dreamer) 6%, var(--surface));
    border: 1px solid color-mix(in srgb, var(--dreamer) 30%, var(--border));
    border-radius: 8px;
    padding: 7px 10px;
  }
  .spawn-note.ok {
    color: var(--ink);
    background: color-mix(in srgb, var(--sprite) 8%, var(--surface));
    border-color: color-mix(in srgb, var(--sprite) 30%, var(--border));
  }

  /* Message disclosure (matches SkillsMemoryPicker's collapsed row). */
  .disclosure {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 10px;
    background: var(--surface-warm);
    border: 1px solid var(--border);
    border-radius: 9px;
    text-align: left;
  }
  .disclosure .chev {
    color: var(--ink-meta);
    font-size: calc(10px * var(--tscale-ui));
    transition: transform 0.12s ease;
    display: inline-block;
  }
  .disclosure .chev.open { transform: rotate(90deg); }
  .disclosure .lead {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-dim);
    text-transform: uppercase;
  }
  .msg-body { margin-top: 8px; }
  .msg-body .grid-sel { width: 100%; margin-bottom: 4px; }

  .modal-actions {
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    margin-top: 16px;
  }
  .modal-actions .cancel {
    padding: 8px 16px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--ink-dim);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.12em;
  }
</style>
