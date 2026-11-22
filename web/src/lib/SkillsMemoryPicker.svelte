<script>
  import { getSkills, getCages, getSetting, putSetting } from './api.js';

  /**
   * Clean-diet opt-in picker: a memory-file checkbox + a searchable
   * skills checklist. A pulse is CLEAN by default — nothing here checked means
   * no skills, no memory. There is intentionally NO "clean" toggle: clean IS
   * the baseline, this surface only exposes the opt-ins.
   *
   * Collapsed by default (a summary row the parent can open). The parent owns
   * the values via bound props so it can fold them into its own create/save
   * body; this component only fetches the skill list and live-filters it
   * (the list is ~60 names).
   *
   * @type {{
   *   memory: boolean,      // memory-file opt-in (default off)
   *   skills: string[],     // selected skill names (default [])
   *   cages: string[],      // selected cage names (default []) — pi-only
   * }}
   */
  let {
    memory = $bindable(false),
    skills = $bindable([]),
    cages = $bindable([]),
    // Skills work on EVERY vessel (2026-07-27): claude via .claude/skills
    // symlinks, qwen via its workspace .qwen/skills + disabled-list, codex via
    // a generated AGENTS.md index. Only the MEMORY file remains claude-only
    // (it is the claude CLI's auto-memory feature) — that one row is gated.
    vessel = 'claude-code',
    // Preselect the default skills. ON for the new-pulse modal, OFF everywhere
    // else — editing a running pulse's diet must never gain skills behind your
    // back just because they are someone's default.
    applyDefaults = false
  } = $props();

  const memoryUnavailable = $derived(vessel !== 'claude-code');
  // Cages are pi extensions: on any other vessel the daemon ignores
  // them, so the section says so instead of offering dead checkboxes.
  const cagesUnavailable = $derived(vessel !== 'pi');
  let allCages = $state([]);
  let cagesError = $state('');
  let cagesLoaded = $state(false);

  async function ensureCages() {
    if (cagesLoaded) return;
    cagesLoaded = true;
    const r = await getCages();
    if (r.ok) {
      allCages = (r.value ?? []).slice().sort();
    } else {
      cagesError = `cages: ${r.error}`;
      console.log('[skills-picker] getCages failed', r.error);
      cagesLoaded = false;
    }
  }

  function toggleCage(name) {
    cages = cages.includes(name)
      ? cages.filter((c) => c !== name)
      : [...cages, name];
  }

  let open = $state(false);
  let all = $state([]);        // every available skill name
  let loading = $state(true);
  let loadError = $state('');  // visible strip if the skills fetch fails
  let query = $state('');
  let loaded = $state(false);  // fetch once, lazily, when first opened

  async function ensureSkills() {
    if (loaded) return;
    loaded = true;
    loading = true;
    loadError = '';
    const r = await getSkills();
    loading = false;
    if (r.ok) {
      all = (r.value ?? []).slice().sort();
    } else {
      // Standing rule: never fail silently — log with context + show a strip.
      loadError = `skills: ${r.error}`;
      console.log('[skills-picker] getSkills failed', r.error);
      loaded = false; // allow a retry on next open
    }
  }

  function toggleOpen() {
    open = !open;
    if (open) { ensureSkills(); ensureCages(); }
  }

  function toggleSkill(name) {
    skills = skills.includes(name)
      ? skills.filter((s) => s !== name)
      : [...skills, name];
  }

  // --- skills that are ON BY DEFAULT --------------------------------------- //
  // The second checkbox on each row toggles default-on skills. The list is
  // GLOBAL — it lives in the daemon's settings KV, not
  // in any pulse — so it is the same on the phone and the desktop, and every
  // new pulse starts with those skills ticked.
  const KV_DEFAULT_SKILLS = 'pulse.default_skills';
  let defaults = $state([]);
  let defaultsError = $state('');

  // applyDefaults is read synchronously so the effect re-runs when the parent
  // finishes initializing the automatic profile.
  $effect(() => {
    const shouldApply = applyDefaults;
    (async () => {
      const r = await getSetting(KV_DEFAULT_SKILLS);
      if (!r.ok) {
        // Non-fatal: the picker still works, you just get no preselection.
        console.log('[skills-picker] default skills fetch failed', r.error);
        return;
      }
      defaults = Array.isArray(r.value) ? r.value.filter((s) => typeof s === 'string') : [];
      if (!shouldApply || defaults.length === 0) return;
      // Fetch the real list even while collapsed: a default naming a skill that
      // no longer exists must not be sent to the daemon, and the collapsed
      // summary should already say how many are loaded.
      await ensureSkills();
      const live = defaults.filter((n) => all.includes(n));
      const stale = defaults.filter((n) => !all.includes(n));
      if (stale.length) console.log('[skills-picker] default skills no longer exist', stale);
      if (live.length) {
        skills = [...new Set([...skills, ...live])];
        console.debug('[skills-picker] applied %d default skill(s)', live.length);
      }
    })();
  });

  async function toggleDefault(name) {
    const prev = defaults;
    const next = defaults.includes(name)
      ? defaults.filter((s) => s !== name)
      : [...defaults, name];
    defaults = next; // optimistic — reverted below if the write fails
    // Making something a default while composing a pulse means you want it on
    // THIS one too. Un-defaulting leaves your current pick alone.
    if (applyDefaults && next.includes(name) && !skills.includes(name)) {
      skills = [...skills, name];
    }
    const r = await putSetting(KV_DEFAULT_SKILLS, next);
    if (r.ok) {
      defaultsError = '';
      return;
    }
    defaults = prev;
    defaultsError = `default skills: ${r.error}`;
    console.log('[skills-picker] saving default skills failed', name, r.error);
  }

  const filtered = $derived.by(() => {
    const q = query.trim().toLowerCase();
    if (!q) return all;
    return all.filter((n) => n.toLowerCase().includes(q));
  });

  // Summary text on the collapsed row so the state is legible while closed.
  const summary = $derived.by(() => {
    const bits = [];
    if (memory) bits.push('memory');
    if (skills.length) bits.push(`${skills.length} skill${skills.length === 1 ? '' : 's'}`);
    if (cages.length) bits.push(`${cages.length} cage${cages.length === 1 ? '' : 's'}`);
    return bits.length ? bits.join(' · ') : 'clean · nothing loaded';
  });
</script>

<div class="picker">
  <button type="button" class="disclosure" onclick={toggleOpen} aria-expanded={open}>
    <span class="chev" class:open>▸</span>
    <span class="lead">skills · cages · memory</span>
    <span class="summary" class:clean={!memory && skills.length === 0 && cages.length === 0}>{summary}</span>
  </button>

  {#if open}
    <div class="body">
      {#if loadError}
        <div class="err-strip">⚠ {loadError}
          <button type="button" class="retry" onclick={() => { loaded = false; ensureSkills(); }}>retry</button>
        </div>
      {/if}
      {#if defaultsError}
        <div class="err-strip">⚠ {defaultsError}</div>
      {/if}

      {#if memoryUnavailable}
        <p class="note">memory file is Claude-only — pick a Claude model to use it.</p>
      {:else}
        <label class="mem-row">
          <input type="checkbox" bind:checked={memory} />
          <span class="mem-lbl">load the memory file</span>
        </label>
      {/if}

      <div class="skills-head">
        <span class="skills-title">skills</span>
        {#if skills.length}
          <button type="button" class="clear" onclick={() => (skills = [])}>clear ({skills.length})</button>
        {/if}
      </div>

      <input
        class="skill-search"
        type="text"
        placeholder="search skills…"
        bind:value={query}
        aria-label="search skills"
      />

      {#if loading}
        <p class="note">loading skills…</p>
      {:else if all.length === 0 && !loadError}
        <p class="note">no skills available.</p>
      {:else}
        <!-- Names the right-hand column, which is otherwise a bare checkbox
             with no way to guess what it does. -->
        <div class="skill-legend"><span>default</span></div>

        <div class="skill-list">
          {#each filtered as name (name)}
            <!-- Two independent checkboxes, so two labels: one <label> around
                 both would toggle both on any click. -->
            <div class="skill-row" class:on={skills.includes(name)}>
              <label class="pick">
                <input
                  type="checkbox"
                  checked={skills.includes(name)}
                  onchange={() => toggleSkill(name)}
                />
                <span class="skill-name">{name}</span>
              </label>
              <label class="def" title={`load ${name} on every new pulse`}>
                <input
                  type="checkbox"
                  checked={defaults.includes(name)}
                  onchange={() => toggleDefault(name)}
                  aria-label={`${name} on by default for new pulses`}
                />
              </label>
            </div>
          {/each}
          {#if filtered.length === 0}
            <p class="note">no skills match.</p>
          {/if}
        </div>
      {/if}

      <!-- Cages: mechanical tool guards, one checkbox each. -->
      <div class="skills-head cages-head">
        <span class="skills-title">cages</span>
        {#if cages.length}
          <button type="button" class="clear" onclick={() => (cages = [])}>clear ({cages.length})</button>
        {/if}
      </div>
      {#if cagesError}
        <div class="err-strip">⚠ {cagesError}
          <button type="button" class="retry" onclick={() => { cagesLoaded = false; ensureCages(); }}>retry</button>
        </div>
      {/if}
      {#if cagesUnavailable}
        <p class="note">cages are pi-only (local OMP models) — pick a pi model to use them.{cages.length ? ` ${cages.length} saved on this profile stay stored.` : ''}</p>
      {:else if allCages.length === 0 && !cagesError}
        <p class="note">no cages in ~/.claude/cages.</p>
      {:else}
        <div class="skill-list cage-list">
          {#each allCages as name (name)}
            <div class="skill-row" class:on={cages.includes(name)}>
              <label class="pick">
                <input
                  type="checkbox"
                  checked={cages.includes(name)}
                  onchange={() => toggleCage(name)}
                />
                <span class="skill-name">{name}</span>
              </label>
            </div>
          {/each}
        </div>
      {/if}
    </div>
  {/if}
</div>

<style>
  .picker {
    border: 1px solid var(--border);
    border-radius: 9px;
    overflow: hidden;
  }
  .disclosure {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 8px 10px;
    background: var(--surface-warm);
    text-align: left;
  }
  .chev {
    color: var(--ink-meta);
    font-size: calc(10px * var(--tscale-ui));
    transition: transform 0.12s ease;
    display: inline-block;
  }
  .chev.open { transform: rotate(90deg); }
  .lead {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-dim);
    text-transform: uppercase;
  }
  .summary {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--dreamer);
  }
  .summary.clean { color: var(--ink-meta); }

  .body {
    padding: 10px;
    border-top: 1px solid var(--border);
  }
  .err-strip {
    display: flex;
    align-items: center;
    gap: 8px;
    background: color-mix(in srgb, var(--bad) 16%, var(--surface));
    color: var(--bad);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    padding: 6px 10px;
    border-radius: 8px;
    margin-bottom: 10px;
    word-break: break-word;
  }
  .err-strip .retry {
    margin-left: auto;
    color: var(--bad);
    border: 1px solid var(--bad);
    border-radius: 999px;
    padding: 2px 8px;
    font-size: calc(9px * var(--tscale-ui));
    flex-shrink: 0;
  }

  .mem-row {
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 4px 0 10px;
    cursor: pointer;
  }
  .mem-row input { accent-color: var(--dreamer); }
  .mem-lbl { font-size: calc(12px * var(--tscale-ui)); color: var(--ink); }

  .skills-head {
    display: flex;
    align-items: center;
    margin-bottom: 6px;
  }
  .skills-title {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    color: var(--ink-meta);
    text-transform: uppercase;
  }
  .clear {
    margin-left: auto;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    padding: 2px 8px;
  }
  .clear:hover { color: var(--ink-dim); }

  .skill-search {
    width: 100%;
    margin-bottom: 8px;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 6px 10px;
    font-size: calc(12px * var(--tscale-ui));
    color: var(--ink);
  }
  .skill-search::placeholder { color: var(--ink-meta); }

  .skill-list {
    max-height: 200px;
    overflow-y: auto;
    border: 1px solid var(--border);
    border-radius: 9px;
  }
  /* Right-aligned caption for the default column, sitting directly above it. */
  .skill-legend {
    display: flex;
    justify-content: flex-end;
    padding: 0 12px 3px 0;
    font-family: var(--font-mono);
    font-size: calc(8.5px * var(--tscale-ui));
    letter-spacing: 0.14em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .skill-row {
    display: flex;
    align-items: center;
    border-bottom: 1px solid var(--border);
  }
  .skill-row:last-child { border-bottom: none; }
  .skill-row.on { background: color-mix(in srgb, var(--dreamer) 8%, transparent); }
  /* The name and its checkbox — takes the whole row minus the default column,
     so tapping anywhere along the name picks the skill. */
  .pick {
    display: flex;
    align-items: center;
    gap: 8px;
    flex: 1;
    min-width: 0;
    padding: 6px 10px;
    cursor: pointer;
  }
  .pick input { accent-color: var(--dreamer); flex-shrink: 0; }
  /* Its own hit area, wide enough for a thumb without stealing from the name. */
  .def {
    display: flex;
    align-items: center;
    padding: 6px 10px;
    cursor: pointer;
    flex-shrink: 0;
  }
  .def input { accent-color: var(--matron); flex-shrink: 0; }
  .skill-name {
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink);
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .skill-row.on .skill-name { color: var(--dreamer); }

  .cages-head { margin-top: 12px; }
  .cage-list { max-height: 120px; }

  .note {
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-meta);
    font-style: italic;
    padding: 6px 2px;
  }
</style>
