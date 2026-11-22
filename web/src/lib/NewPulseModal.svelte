<script>
  import { createPulse, createSchedule, listPulses, getSetting, putSetting, getModels } from './api.js';
  import {
    KV_PROFILES, KV_DEFAULT_PROFILE, sanitizeProfiles, sanitizeDefaultProfile,
    profileFromForm, applyProfile, upsertProfile, removeProfile, findProfile,
    sortedProfiles
  } from './profiles.js';
  import WakeScheduleFields from './WakeScheduleFields.svelte';
  import LoopTriggerFields from './LoopTriggerFields.svelte';
  import LoopTemplatePicker from './LoopTemplatePicker.svelte';
  import { defaultTriggerForm, triggerFormFromList, triggerListFromForm } from './looptriggers.js';
  import SkillsMemoryPicker from './SkillsMemoryPicker.svelte';
  import { localDatetimeToUtcIso, localHhmmToUtcHhmm } from './schedule.js';
  import { groupedModels, DEFAULT_MODEL_ID, DEFAULT_EFFORT, vesselFor, defaultHandoffFor, defaultEffortFor, effortsFor, effortOnModelChange, offLevel, supportsFastMode, mergeSlotStatus, modelOption, defaultAvailableModelId, toolsLoaded, spawnErrorText, setRuntimeRegistry } from './models.js';

  /**
   * @type {{
   *   onclose: () => void,
   *   oncreated: (created: any) => void,
   *   grids?: Array<{id:string,name:string}>,
   *   activeGrid?: string|null
   * }}
   */
  let { onclose, oncreated, grids = [], activeGrid = null } = $props();

  let title = $state('');
  let titleEl;
  // Keyboard: Escape closes, Ctrl/⌘+Enter creates from any field, Enter in the
  // title creates; everything else is Tab order.
  function onWinKey(e) {
    if (e.key === 'Escape') {
      e.preventDefault();
      onclose();
      return;
    }
    if ((e.ctrlKey || e.metaKey) && e.key === 'Enter') {
      e.preventDefault();
      create();
    }
  }
  // Opening this dialog may focus the title on desktop, but never summon the
  // iPhone keyboard merely because the modal appeared.
  $effect(() => {
    if (typeof window === 'undefined' || !window.matchMedia('(min-width: 700px)').matches) return;
    queueMicrotask(() => titleEl?.focus({ preventScroll: true }));
  });
  const initialModel = defaultAvailableModelId() || DEFAULT_MODEL_ID;
  let model = $state(initialModel);
  let memoryMax = $state('4G');
  // Reasoning effort ('' = CLI default). Starts at the house default when the
  // default model speaks it; switching model keeps the chosen level when the
  // new model offers it, else takes the new model's default (D5).
  let effort = $state(defaultEffortFor(initialModel) || (effortsFor(initialModel).includes(DEFAULT_EFFORT) ? DEFAULT_EFFORT : ''));
  let prevEffortModel = model;
  $effect(() => {
    if (model !== prevEffortModel) {
      effort = effortOnModelChange(effort, model);
      prevEffortModel = model;
    }
    if ((!effort && defaultEffortFor(model)) || (effort && !effortsFor(model).includes(effort))) {
      effort = defaultEffortFor(model) || offLevel(effortsFor(model));
    }
  });
  // Fast mode (codex only — the CLI's "fast" service tier). Switching to a
  // model whose vessel has no such switch drops it.
  let fastMode = $state(false);
  $effect(() => {
    if (fastMode && !supportsFastMode(model)) fastMode = false;
  });

  // Model SLOTS: the runtime GET /models answer carries {status,
  // label_live} per slot; mergeSlotStatus fuses it into the static entries so
  // the <option> can grey out unavailable slots (see slotOption).
  let slotRuntime = $state([]);
  async function refreshSlotStatus() {
    const r = await getModels();
    if (r.ok && Array.isArray(r.value?.models)) {
      setRuntimeRegistry(r.value);
      slotRuntime = r.value.models.filter((e) => e && e.slot);
    } else if (!r.ok) {
      console.log('[new-pulse] slot status refresh failed', r.error);
    }
  }
  $effect(() => {
    refreshSlotStatus();
    const t = setInterval(refreshSlotStatus, 30_000);
    return () => clearInterval(t);
  });

  const modelGroups = $derived(
    groupedModels()
      .map((g) => ({ ...g, models: mergeSlotStatus(g.models, slotRuntime) }))
      .filter((g) => g.models.length > 0)
  );
  // A model whose tool is missing or whose slot went down must not stay selected.
  const hasToolsInfo = $derived(toolsLoaded());
  const availableIds = $derived(
    modelGroups.flatMap((g) => g.models).filter((m) => !modelOption(m).disabled).map((m) => m.id)
  );
  const selectedAvailable = $derived(availableIds.includes(model));
  const noAvailableModels = $derived(hasToolsInfo && availableIds.length === 0);
  $effect(() => {
    if (availableIds.length && !availableIds.includes(model)) model = availableIds[0];
  });

  // "Handoff at" auto-fills the selected model's default but stays editable.
  // We only auto-fill while the user hasn't hand-edited it (handoffTouched), so
  // switching models updates the suggestion without clobbering a typed value.
  let handoffTokens = $state(String(defaultHandoffFor(initialModel) ?? ''));
  let handoffTouched = $state(false);
  $effect(() => {
    // Track model changes to refresh the default suggestion.
    const dflt = defaultHandoffFor(model);
    if (!handoffTouched && dflt != null) {
      handoffTokens = String(dflt);
    }
  });
  // Grid select defaults to the active tab (null/ALL -> "no grid").
  let gridId = $state(activeGrid ?? '');

  // Advanced section (layout only this round): collapsed by default so the
  // dialog only pays for the common fields' height. The rarely-touched fields
  // (handoff, grid, memory cap, loop, wake, fast) live behind it and auto-fill
  // with sane defaults today.
  let advancedOpen = $state(false);

  // Collapsed-row summary for the advanced disclosure: must name the memory
  // cap, the handoff threshold (abbreviated, 200000 -> 200k) and the grid, and
  // add a token for loop / wake / fast only when those toggles are on.
  const advSummary = $derived.by(() => {
    const bitGrid = grids.find((g) => g.id === gridId)?.name ?? 'no grid';
    const raw = handoffTokens.trim();
    const n = parseInt(raw, 10);
    const bitHandoff = raw === '' || !Number.isFinite(n)
      ? (raw === '' ? 'model default' : raw)
      : (n >= 1000 ? `${Math.round(n / 1000)}k` : String(n));
    const bits = [memoryMax, bitHandoff, bitGrid];
    if (loopOn) bits.push(`loop ${loopMinutes}m`);
    if (wakeMode === 'once') bits.push('wake once');
    else if (wakeMode === 'daily') bits.push('wake daily');
    if (fastMode) bits.push('fast');
    return bits.join(' \u00b7 ');
  });


  // Clean diet: opt-ins only. Default = clean, nothing checked.
  // `clean:true` is always sent — it IS the baseline, never a user toggle here.
  let dietMemory = $state(false);
  let dietSkills = $state([]);
  let dietCages = $state([]);   // cage names (pi-only)

  let creating = $state(false);
  let error = $state('');

  // --- Loop: OFF / EVERY N MIN. Passed atomically on create. ---
  let loopOn = $state(false);
  let loopMinutes = $state('30');
  let loopPrompt = $state('');
  let loopTriggers = $state(defaultTriggerForm());
  let loopMode = $state('any');

  // --- Wake schedule: NONE / ONE-SHOT / RECURRING. Times are LOCAL
  // and converted to UTC before POST. Created right after the pulse exists. ---
  let wakeMode = $state('none'); // 'none' | 'once' | 'daily'
  let wakeAtLocal = $state('');
  let wakeHhmm = $state('09:00');
  let wakeDays = $state([]);
  let wakePrompt = $state('');
  // Optional scheduled cross-pulse message target for the wake.
  // '' = self-wake.
  let wakeTargetId = $state('');
  // Other non-archived pulses the wake can be pointed at. Fetched once on mount;
  // failure is non-fatal (the picker just stays empty) but is logged.
  let msgTargets = $state([]);

  // --- Profiles (saved new-pulse presets) -------------------------------- //
  // Stored in the daemon settings KV (pulse.profiles) so they are identical
  // on the phone and the desktop. Loaded once on open; failure shows a strip
  // and leaves the picker empty but the dialog working.
  let savedProfiles = $state([]);
  let profileSel = $state('');          // '' = "— none —"
  let defaultProfile = $state('');      // name stored in pulse.default_profile
  // Apply global skill defaults after automatic profile initialization.
  let applySkillDefaults = $state(false);
  // Inline name box: follows the selected profile; Save writes the current
  // form under THIS name — an existing name updates that profile in place, a
  // new name creates one.
  let profileName = $state('');
  let profilesError = $state('');       // visible strip on load/save/delete failure
  let profileWarning = $state('');      // visible warning on apply (e.g. unknown model)
  $effect(() => {
    (async () => {
      const [pr, dr] = await Promise.all([
        getSetting(KV_PROFILES),
        getSetting(KV_DEFAULT_PROFILE)
      ]);
      if (pr.ok) {
        savedProfiles = sanitizeProfiles(pr.value);
      } else {
        profilesError = `profiles: ${pr.error}`;
        console.log('[new-pulse] load profiles failed', pr.error);
      }
      if (dr.ok) {
        defaultProfile = sanitizeDefaultProfile(dr.value);
      } else {
        // Non-fatal: the dialog still works, you just get no auto-apply.
        console.log('[new-pulse] load default profile failed', dr.error);
      }
      const def = findProfile(savedProfiles, defaultProfile);
      if (def) {
        defaultProfile = def.name; // canonical casing
        profileSel = def.name;
        profileName = def.name;
        applyProfileToForm(def, { automatic: true });
        // Automatic profiles must not bypass the DEFAULT skill checkboxes.
        // Explicit profile selection still restores its saved diet.
        applySkillDefaults = true;
      } else {
        if (defaultProfile) {
          console.log('[new-pulse] default profile no longer exists', defaultProfile);
        }
        applySkillDefaults = true;
      }
    })();
  });

  $effect(() => {
    let alive = true;
    listPulses().then((r) => {
      if (!alive) return;
      if (r.ok) {
        msgTargets = (r.value ?? [])
          .filter((p) => p.state !== 'archived')
          .map((p) => ({ id: p.id, title: p.title }));
      } else {
        console.log('[new-pulse] load message targets failed', r.error);
      }
    });
    return () => (alive = false);
  });

  // --- Profile handlers -------------------------------------------------- //
  function formState() {
    return {
      model, effort, fast: fastMode, handoffTokens, memoryMax,
      memory: dietMemory, skills: dietSkills, cages: dietCages,
      loopOn, loopMinutes, loopPrompt, loopTriggers, loopMode
    };
  }

  function applyProfileToForm(prof, options) {
    const values = applyProfile(prof, options);
    model = values.model;
    effort = values.effort;
    fastMode = values.fast;
    handoffTokens = values.handoffTokens;
    handoffTouched = true; // keep the typed/ported handoff — hide the default-overwrite effect
    memoryMax = values.memoryMax;
    dietMemory = values.memory;
    dietSkills = values.skills;
    dietCages = values.cages;
    loopOn = values.loopOn;
    loopMinutes = values.loopMinutes;
    loopPrompt = values.loopPrompt;
    loopTriggers = triggerFormFromList(values.loopTriggers);
    loopMode = values.loopMode;
    profileWarning = values.warning || '';
  }

  // Choosing a saved profile applies it immediately. "— none —" only clears.
  function onProfileSelect() {
    profileWarning = '';
    if (profileSel === '') { profileName = ''; return; } // "— none —" clears the selection only
    const prof = findProfile(savedProfiles, profileSel);
    if (!prof) return;
    profileName = prof.name;
    applyProfileToForm(prof);
  }

  function isDefaultName(name) {
    return !!name && name.toLowerCase() === defaultProfile.toLowerCase();
  }

  async function toggleDefault() {
    if (!profileSel) return;
    const prev = defaultProfile;
    const next = isDefaultName(profileSel) ? '' : profileSel;
    defaultProfile = next; // optimistic — reverted below if the write fails
    const r = await putSetting(KV_DEFAULT_PROFILE, next);
    if (r.ok) {
      profilesError = '';
      return;
    }
    defaultProfile = prev;
    profilesError = `default profile: ${r.error}`;
    console.log('[new-pulse] saving default profile failed', profileSel, r.error);
  }

  async function saveProfile() {
    const profile = profileFromForm(profileName, formState());
    if (!profile) {
      profilesError = 'Profile needs a name.';
      console.log('[new-pulse] save profile: no name');
      return;
    }
    const prev = savedProfiles;
    const prevSel = profileSel;
    const next = upsertProfile(savedProfiles, profile);
    savedProfiles = next; // optimistic — reverted below if the write fails
    profileSel = profile.name;
    profileName = profile.name;
    const r = await putSetting(KV_PROFILES, next);
    if (r.ok) {
      profilesError = '';
      return;
    }
    // Revert the optimistic local change, never fail silently.
    savedProfiles = prev;
    profileSel = prevSel;
    profilesError = `save profile: ${r.error}`;
    console.log('[new-pulse] save profile failed', profile.name, r.error);
  }

  async function deleteProfile() {
    if (!profileSel) return;
    const prev = savedProfiles;
    const prevDefault = defaultProfile;
    const deletedName = profileSel;
    const next = removeProfile(savedProfiles, profileSel);
    savedProfiles = next; // optimistic — reverted below if the write fails
    profileSel = '';
    profileName = '';
    const clearingDefault = isDefaultName(deletedName);
    if (clearingDefault) defaultProfile = '';
    const r = await putSetting(KV_PROFILES, next);
    if (!r.ok) {
      savedProfiles = prev;
      defaultProfile = prevDefault;
      profilesError = `delete profile: ${r.error}`;
      console.log('[new-pulse] delete profile failed', r.error);
      return;
    }
    profilesError = '';
    if (!clearingDefault) return;
    const d = await putSetting(KV_DEFAULT_PROFILE, '');
    if (!d.ok) {
      // Profile is already gone; a stale default name is ignored on next open.
      profilesError = `default profile: ${d.error}`;
      console.log('[new-pulse] clearing default profile failed', d.error);
    }
  }

  async function create() {
    if (!title.trim() || creating) return;
    if (hasToolsInfo && !selectedAvailable) {
      error = 'No agent tool is available for the selected model. Install a tool or pick another model.';
      return;
    }

    // Validate loop before we create anything, so a bad interval never spawns a
    // half-configured pulse.
    let loopMin = 0;
    let loopTrig = null;
    if (loopOn) {
      loopMin = parseInt(loopMinutes, 10);
      if (!Number.isFinite(loopMin) || loopMin <= 0) {
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
      loopTrig = trig.list;
    }

    // Build the wake schedule body up front so bad input fails before create.
    let wakeBody = null;
    if (wakeMode === 'once') {
      const at_iso = localDatetimeToUtcIso(wakeAtLocal);
      if (!at_iso) {
        error = 'Pick a valid one-shot wake date/time.';
        return;
      }
      if (!wakePrompt.trim()) {
        error = 'A wake schedule needs a prompt.';
        return;
      }
      wakeBody = { kind: 'once', prompt: wakePrompt.trim(), at_iso };
      if (wakeTargetId) wakeBody.target_pulse_id = wakeTargetId;
    } else if (wakeMode === 'daily') {
      const cron_hhmm = localHhmmToUtcHhmm(wakeHhmm);
      if (!cron_hhmm) {
        error = 'Pick a valid recurring time (HH:MM).';
        return;
      }
      if (!wakePrompt.trim()) {
        error = 'A wake schedule needs a prompt.';
        return;
      }
      wakeBody = { kind: 'daily', prompt: wakePrompt.trim(), cron_hhmm };
      if (wakeDays.length) wakeBody.days = wakeDays.join(',');
      if (wakeTargetId) wakeBody.target_pulse_id = wakeTargetId;
    }

    // Parse the handoff threshold up front — empty = model default (omit it),
    // any other value must be a positive integer or we refuse to create.
    let handoffValue = null;
    const handoffRaw = handoffTokens.trim();
    if (handoffRaw !== '') {
      handoffValue = parseInt(handoffRaw, 10);
      if (!Number.isFinite(handoffValue) || handoffValue <= 0) {
        error = 'Handoff threshold must be a positive number of tokens (or blank for the model default).';
        return;
      }
    }

    creating = true;
    error = '';
    const modelId = model.trim() || DEFAULT_MODEL_ID;
    const body = {
      title: title.trim(),
      model: modelId,
      // Vessel is DERIVED from the model choice — never picked directly.
      vessel: vesselFor(modelId),
      memory_max: memoryMax,
      grid_id: gridId || null,
      // Clean diet rides along atomically. clean is always true (the
      // baseline); memory + skills are the opt-ins. Defaults: memory off, [].
      clean: true,
      memory: dietMemory,
      skills: dietSkills,
      cages: dietCages
    };
    // CreatePulseBody accepts handoff_tokens directly (atomic create), so no
    // separate PATCH is needed. Blank = model-family default (omit the field).
    if (handoffValue != null) body.handoff_tokens = handoffValue;
    // Blank = CLI default (omit); the daemon validates the level per vessel.
    if (effort) body.effort = effort;
    // Fast mode rides along only when set (codex; daemon-validated).
    if (fastMode) body.fast = true;
    // Loop rides along atomically (CreatePulseBody.loop_prompt/loop_minutes).
    if (loopOn) {
      body.loop_prompt = loopPrompt.trim();
      body.loop_minutes = loopMin;
      body.loop_triggers = loopTrig;
      body.loop_trigger_mode = loopMode;
    }

    const r = await createPulse(body);
    if (!r.ok) {
      creating = false;
      // A 409 slot_unavailable says its own plain sentence; anything else keeps
      // the api.js message verbatim.
      error = spawnErrorText(r.error);
      return;
    }

    // The pulse exists; attach any wake schedule now. If this fails we surface
    // it but keep the pulse (it was created successfully) — no silent drop.
    if (wakeBody) {
      const sr = await createSchedule(r.value.id, wakeBody);
      if (!sr.ok) {
        creating = false;
        error = `Pulse created, but wake schedule failed: ${sr.error}`;
        console.log('[new-pulse] wake schedule failed', r.value.id, sr.error);
        oncreated?.(r.value); // reflect the new pulse in the grid regardless
        return;
      }
    }

    creating = false;
    // The created pulse rides along: the page opens it, rather than leaving you
    // on the pane you happened to be on.
    oncreated?.(r.value);
    onclose?.();
  }
</script>

<svelte:window onkeydown={onWinKey} />
<div class="scrim" onclick={onclose} role="presentation"></div>
<div class="modal" role="dialog" aria-modal="true" aria-label="new pulse">
  <h2>New Pulse</h2>

  {#if error}
    <div class="err-strip">⚠ {error}</div>
  {/if}

  <div class="field inline">
    <label for="np-profile">Profile</label>
    <select id="np-profile" bind:value={profileSel} onchange={onProfileSelect} class="grid-sel">
      <option value="">— none —</option>
      {#each sortedProfiles(savedProfiles) as prof (prof.name)}
        <option value={prof.name}>{prof.name}</option>
      {/each}
    </select>
    {#if profileSel}
      <button
        id="np-profile-default"
        class="star-btn"
        class:on={isDefaultName(profileSel)}
        type="button"
        onclick={toggleDefault}
        title={isDefaultName(profileSel) ? 'default profile for every new pulse — tap to unset' : 'make this the default profile for every new pulse'}
        aria-pressed={isDefaultName(profileSel)}
        aria-label={`${profileSel} default for new pulses`}
      >{isDefaultName(profileSel) ? '★' : '☆'}</button>
      <button id="np-profile-delete" class="row-btn danger" type="button" onclick={deleteProfile} title="delete profile" aria-label="delete profile">DEL</button>
    {/if}
  </div>
  <div class="profile-detail">
    <input id="np-profile-name" type="text" bind:value={profileName} placeholder={profileSel ? 'profile name' : 'name to save the current settings as…'} aria-label="profile name" onkeydown={(e) => e.key === 'Enter' && saveProfile()} />
    <button id="np-profile-save" type="button" onclick={saveProfile} title={profileSel && profileName.trim().toLowerCase() === profileSel.toLowerCase() ? `update "${profileSel}" with the current settings` : 'save the current settings as a new profile'}>
      {profileSel && profileName.trim().toLowerCase() === profileSel.toLowerCase() ? 'Update' : 'Save'}
    </button>
  </div>
  {#if profileWarning}
    <div class="err-strip">⚠ {profileWarning}</div>
  {/if}
  {#if profilesError}
    <div class="err-strip">⚠ {profilesError}</div>
  {/if}

  <div class="field inline">
    <label for="np-title">Title</label>
    <input id="np-title" type="text" bind:this={titleEl} bind:value={title} placeholder="e.g. web smoke" onkeydown={(e) => e.key === 'Enter' && create()} />
  </div>

  <div class="field inline">
    <label for="np-model">Model</label>
    <select id="np-model" bind:value={model} class="grid-sel">
      {#each modelGroups as g (g.label)}
        <optgroup label={g.label}>
          {#each g.models as m (m.id)}
            {@const opt = modelOption(m)}
            <option value={m.id} disabled={opt.disabled} title={opt.title}>{opt.text}</option>
          {/each}
        </optgroup>
      {/each}
    </select>
  </div>
  {#if noAvailableModels}
    <div class="err-strip">No agent tool found. Install and sign in to a tool (see README.md "Agent tools" or docs/setup/models.md) before creating a pulse.</div>
  {/if}

  {#if effortsFor(model).length}
    <div class="field inline">
      <label for="np-effort">Effort</label>
      <select id="np-effort" bind:value={effort} class="grid-sel">
        {#if !offLevel(effortsFor(model))}
          <option value="">CLI default</option>
        {/if}
        {#each effortsFor(model) as e (e)}
          <option value={e}>{e}</option>
        {/each}
      </select>
    </div>
  {/if}

  <div class="field skills-field">
    <SkillsMemoryPicker
      bind:memory={dietMemory}
      bind:skills={dietSkills}
      bind:cages={dietCages}
      vessel={vesselFor(model)}
      applyDefaults={applySkillDefaults}
    />
  </div>

  <div class="adv-wrap">
    <button type="button" id="np-advanced" class="adv-disclosure" onclick={() => (advancedOpen = !advancedOpen)} aria-expanded={advancedOpen}>
      <span class="adv-chev" class:open={advancedOpen}>▸</span>
      <span class="adv-lead">advanced</span>
      <span class="adv-summary">{advSummary}</span>
    </button>
    {#if advancedOpen}
      <div class="adv-body">
        {#if supportsFastMode(model)}
          <div class="field">
            <label>Fast mode</label>
            <div class="seg">
              <button class:on={!fastMode} onclick={() => (fastMode = false)}>OFF</button>
              <button class:on={fastMode} onclick={() => (fastMode = true)}>FAST</button>
            </div>
          </div>
        {/if}

        <div class="field">
          <label for="np-handoff">Handoff at</label>
          <div class="inline-num">
            <input
              id="np-handoff"
              type="text"
              inputmode="numeric"
              bind:value={handoffTokens}
              oninput={() => (handoffTouched = true)}
              aria-label="handoff threshold tokens"
            />
            tokens
          </div>
        </div>

        <div class="field">
          <label for="np-grid">Grid</label>
          <select id="np-grid" bind:value={gridId} class="grid-sel">
            <option value="">no grid</option>
            {#each grids as g (g.id)}
              <option value={g.id}>{g.parent_id ? `${grids.find((x) => x.id === g.parent_id)?.name ?? ''} › ${g.name}` : g.name}</option>
            {/each}
          </select>
        </div>

        <div class="field">
          <label for="np-mem">Memory cap</label>
          <div class="seg">
            {#each ['2G', '4G', '6G'] as m}
              <button class:on={memoryMax === m} onclick={() => (memoryMax = m)}>{m}</button>
            {/each}
          </div>
        </div>

        <div class="field">
          <label>Loop</label>
          <div class="seg">
            <button class:on={!loopOn} onclick={() => (loopOn = false)}>OFF</button>
            <button class:on={loopOn} onclick={() => (loopOn = true)}>EVERY N MIN</button>
          </div>
          {#if loopOn}
            <div class="loop-detail">
              <LoopTemplatePicker
                getForm={() => ({ minutes: loopMinutes, prompt: loopPrompt, triggerForm: loopTriggers, mode: loopMode })}
                onpick={(t) => {
                  loopMinutes = String(t.minutes);
                  loopPrompt = t.prompt;
                  loopTriggers = triggerFormFromList(t.triggers);
                  loopMode = t.trigger_mode === 'all' ? 'all' : 'any';
                }}
                onerror={(m) => (error = m)}
              />
              <div class="inline-num">
                every
                <input type="text" inputmode="numeric" bind:value={loopMinutes} aria-label="loop interval minutes" />
                minutes
              </div>
              <textarea
                rows="2"
                bind:value={loopPrompt}
                placeholder="loop prompt — what should it do each time?"
              ></textarea>
              <LoopTriggerFields bind:form={loopTriggers} bind:mode={loopMode} idPrefix="np-lt" />
            </div>
          {/if}
        </div>

        <div class="field">
          <label>Wake schedule</label>
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
              class="wake-prompt"
              rows="2"
              bind:value={wakePrompt}
              placeholder={wakeTargetId ? 'prompt to send the target pulse…' : 'wake prompt — what should it do when it wakes?'}
            ></textarea>
          {/if}
        </div>
      </div>
    {/if}
  </div>

  <div class="modal-actions">
    <button class="cancel" onclick={onclose}>Cancel</button>
    <button class="create" onclick={create} disabled={creating || !title.trim() || (hasToolsInfo && !selectedAvailable)}>
      {creating ? 'creating…' : 'Create'}
    </button>
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
    overflow-x: hidden;
    background: var(--surface);
    border: 1px solid var(--border-strong);
    border-radius: 14px;
    box-shadow: 0 18px 50px rgba(0, 0, 0, 0.6);
    z-index: 70;
    /* No bottom padding: the sticky action bar owns that space. Giving the
       bar a negative bottom margin instead made its box end below the scroll
       content, so sticky pinned it 8px too high and it clipped whatever row
       came last. */
    padding: 16px 16px 0;
  }
  .modal h2 {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.2em;
    color: var(--ink-dim);
    text-transform: uppercase;
    margin-bottom: 14px;
  }
  .err-strip {
    background: color-mix(in srgb, var(--bad) 16%, var(--surface));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 6px 10px;
    border-radius: 8px;
    margin-bottom: 10px;
    word-break: break-word;
  }
  .field { margin-bottom: 8px; }
  .field label {
    display: block;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    color: var(--ink-meta);
    text-transform: uppercase;
    margin-bottom: 5px;
  }
  /* Single-line rows: uppercase mono label in a fixed left column, control in
     the rest of the row. Identical across Profile/Title/Model/Effort. */
  .field.inline {
    display: flex;
    align-items: center;
    gap: 10px;
    margin-bottom: 8px;
  }
  .field.inline > label {
    flex: 0 0 72px;
    margin-bottom: 0;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    text-align: right;
  }
  .field.inline > .grid-sel,
  .field.inline > input[type="text"] {
    flex: 1;
    min-width: 0;
    min-height: 36px;
  }
  .field input[type="text"] {
    width: 100%;
    min-height: 36px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
  }
  .field input::placeholder { color: var(--ink-meta); }
  .grid-sel {
    width: 100%;
    min-height: 36px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
  }

  .seg { display: flex; gap: 6px; }
  /* Keyboard: every control shows where focus is. */
  .modal :is(button, select, input, textarea):focus-visible {
    outline: 2px solid var(--dreamer);
    outline-offset: 1px;
  }
  .seg button {
    flex: 1;
    min-height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
    padding: 7px 0;
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
  .loop-detail { margin-top: 8px; }
  .inline-num {
    display: flex;
    align-items: center;
    gap: 8px;
    color: var(--ink-dim);
    font-size: calc(12px * var(--tscale-ui));
  }
  .inline-num input {
    width: 64px;
    min-height: 36px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 7px 8px;
    text-align: center;
    color: var(--ink);
  }
  textarea {
    width: 100%;
    min-height: 36px;
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
  .wake-prompt { margin-top: 10px; }

  /* Advanced disclosure — styled as a sibling of the SkillsMemoryPicker
     disclosure (chevron + lead + right-aligned summary). */
  .adv-wrap { margin-bottom: 8px; }
  .adv-disclosure {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    min-height: 36px;
    padding: 8px 10px;
    background: var(--surface-warm);
    border: 1px solid var(--border);
    border-radius: 9px;
    text-align: left;
  }
  .adv-chev {
    color: var(--ink-meta);
    font-size: calc(10px * var(--tscale-ui));
    transition: transform 0.12s ease;
    display: inline-block;
  }
  .adv-chev.open { transform: rotate(90deg); }
  .adv-lead {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-dim);
    text-transform: uppercase;
  }
  .adv-summary {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--dreamer);
  }
  .adv-body {
    margin-top: 8px;
    padding-top: 10px;
    border-top: 1px solid var(--border);
  }
  /* Reach into WakeScheduleFields (a child component; scoped CSS cannot cross
     the boundary, so use :global scoped under this file's .adv-body). */
  .adv-body :global(.seg button),
  .adv-body :global(.days .day) {
    min-height: 36px;
    display: flex;
    align-items: center;
    justify-content: center;
  }
  .adv-body :global(.target-sel),
  .adv-body :global(input[type='datetime-local']),
  .adv-body :global(input[type='time']) {
    min-height: 36px;
  }

  /* Skills & memory disclosure lives in SkillsMemoryPicker (off limits) — size
     its controls from here, scoped under the .skills-field wrapper we own. */
  .skills-field :global(.picker .disclosure) { min-height: 36px; }
  .skills-field :global(.picker .skill-search) { min-height: 36px; }
  .skills-field :global(.picker .clear) { min-height: 36px; }
  .skills-field :global(.picker .retry) { min-height: 36px; }

  /* Sticky bottom action bar: always on screen, full-width, own background
     and a top hairline (C1). */
  .modal-actions {
    position: sticky;
    bottom: 0;
    display: flex;
    gap: 8px;
    justify-content: flex-end;
    align-items: center;
    margin: 8px -16px 0;
    padding: 12px 16px 16px;
    background: var(--surface);
    border-top: 1px solid var(--border-strong);
  }
  .modal-actions .cancel {
    min-height: 36px;
    padding: 8px 16px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--ink-dim);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.12em;
  }
  .modal-actions .create {
    min-height: 40px;
    display: inline-flex;
    align-items: center;
    justify-content: center;
    padding: 8px 20px;
    border-radius: 999px;
    background: var(--sprite);
    color: var(--bg);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.12em;
    font-weight: 700;
  }
  .modal-actions .create:disabled { opacity: 0.5; cursor: default; }

  /* Profile SAVE/DEL live inline in the PROFILE row (C2b) — no separate band. */
  .row-btn {
    flex-shrink: 0;
    min-height: 36px;
    min-width: 44px;
    padding: 5px 10px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--ink-dim);
    background: transparent;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.1em;
    text-transform: uppercase;
  }
  .row-btn:hover { color: var(--ink); }
  .row-btn.danger { color: var(--bad); border-color: var(--bad); }
  /* Default-profile star: one tap, no legend, no checkbox. */
  .star-btn {
    min-width: 36px;
    min-height: 36px;
    border-radius: 999px;
    border: 1px solid var(--border-strong);
    color: var(--ink-meta);
    font-size: calc(15px * var(--tscale-ui));
    line-height: 1;
    flex-shrink: 0;
  }
  .star-btn:hover { color: var(--ink); }
  .star-btn.on { color: var(--matron); border-color: var(--matron); }
  .profile-detail { display: flex; gap: 6px; margin-top: 6px; align-items: center; }
  .profile-detail input {
    flex: 1;
    min-height: 36px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
  }
  .profile-detail input::placeholder { color: var(--ink-meta); }
  .profile-detail button {
    min-height: 36px;
    padding: 5px 12px;
    border-radius: 999px;
    border: 1px solid var(--sprite);
    color: var(--sprite);
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.1em;
    text-transform: uppercase;
    background: transparent;
  }
</style>
