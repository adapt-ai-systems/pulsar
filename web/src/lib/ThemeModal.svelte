<script>
  /**
   * Appearance settings surface: Colors, Background image, Layout, Text size.
   * Everything applies live and persists per device (theme.js + uiprefs).
   *
   * @type {{ onclose: () => void }}
   */
  import {
    VARS, LABELS, BGS, bgUrl, bgThumb,
    emptyBg, applyBg, setToken,
    readLiveColors, saveActiveProfile, loadActiveProfile,
    TEXT_LABELS, TEXT_MIN, TEXT_MAX, emptyText, applyText, normalizeText,
    PEER_VAR, peerDefault, peerOverride, applyPeer,
    MSG_VARS, msgOverrides, normalizeMsgNums, applyMsg
  } from './theme.js';
  import { uiPrefs, setShowMetricsBar, setShowMobileSidebar } from './uiprefs.svelte.js';
  import { themePick } from './themepick.svelte.js';

  let { onclose } = $props();

  // Live editable state, seeded from what's currently on :root.
  let colors = $state(readLiveColors());
  let bg = $state(emptyBg());
  let text = $state(emptyText()); // {chat,tool,ui,icons} font-size multipliers
  let currentName = $state('custom');
  let error = $state('');
  // "Other pulses" pin — null follows the palette (--warden).
  let peerPin = $state(null);
  let peerShown = $derived(peerPin ?? peerDefault(colors));
  // Click-to-edit pins and message-bubble overrides carried through saves.
  let msgPins = {};
  let msgNums = normalizeMsgNums(undefined);
  let extraPins = {};

  function colorsOut() {
    return { ...extraPins, ...colors, ...(peerPin ? { [PEER_VAR]: peerPin } : {}), ...msgPins };
  }

  $effect(() => {
    (async () => {
      const active = await loadActiveProfile();
      if (active) {
        bg = { ...(active.bg ?? emptyBg()) };
        text = normalizeText(active.text);
        peerPin = peerOverride(active.colors);
        msgPins = msgOverrides(active.colors);
        msgNums = normalizeMsgNums(active.msg);
        const managed = new Set([...VARS, ...MSG_VARS, PEER_VAR]);
        extraPins = Object.fromEntries(Object.entries(active.colors ?? {}).filter(([k]) => !managed.has(k) && !k.startsWith('--cal-')));
        currentName = active.name ?? 'custom';
      }
    })();
  });

  // Live edits (swatch, bg, sliders) persist the ACTIVE theme — debounced so
  // slider drags don't hammer storage.
  let persistTimer;
  function persistActiveSoon() {
    clearTimeout(persistTimer);
    persistTimer = setTimeout(async () => {
      const r = await saveActiveProfile({
        name: currentName,
        colors: colorsOut(),
        bg: { ...bg },
        text: { ...text },
        msg: { ...msgNums }
      });
      if (!r.ok) error = `save active theme: ${r.error}`;
    }, 400);
  }

  // --- change feedback: flash the affected elements ---------------------- //
  function flashEls(els) {
    let n = 0;
    els.forEach((el) => {
      el.classList.add('ap-flash');
      n++;
      setTimeout(() => el.classList.remove('ap-flash'), 2000);
    });
    return n;
  }
  function flashRegion(sel) {
    const els = Array.from(document.querySelectorAll(sel)).filter((el) => el.offsetParent !== null);
    if (els.length) { flashEls(els); return ''; }
    return 'not on screen right now';
  }
  let layoutNote = $state('');
  let colorsNote = $state('');
  let textNote = $state('');
  let noteTimers = {};
  const NOTE_VARS = {
    layout: () => (layoutNote = ''),
    colors: () => (colorsNote = ''),
    text: () => (textNote = '')
  };
  const NOTE_SETTERS = {
    layout: (t) => (layoutNote = t),
    colors: (t) => (colorsNote = t),
    text: (t) => (textNote = t)
  };
  function showNote(which, msg) {
    if (!msg) return;
    NOTE_SETTERS[which](msg);
    clearTimeout(noteTimers[which]);
    noteTimers[which] = setTimeout(() => {
      noteTimers[which] = 0;
      NOTE_VARS[which]();
    }, 4000);
  }
  $effect(() => {
    return () => Object.values(noteTimers).forEach((t) => t && clearTimeout(t));
  });

  const COLOR_TARGETS = {
    colors: '.app',
    text: '.app'
  };
  const flashTimers = {};
  function flashSectionDebounced(which) {
    clearTimeout(flashTimers[which]);
    flashTimers[which] = setTimeout(() => {
      flashTimers[which] = 0;
      showNote(which, flashRegion(COLOR_TARGETS[which]));
    }, 300);
  }
  $effect(() => {
    return () => Object.values(flashTimers).forEach((t) => t && clearTimeout(t));
  });

  function onMobileSidebar(on) {
    setShowMobileSidebar(on);
    if (on) showNote('layout', flashRegion('.rail'));
  }
  function onMetricsBar(on) {
    setShowMetricsBar(on);
    if (on) showNote('layout', flashRegion('.metrics-bar'));
  }

  // --- floating window / phone bottom sheet ------------------------------ //
  let modalEl = $state(null);
  let pos = $state(null);
  let isPhone = $state(false);
  let sheetH = $state(0.4);
  let peeking = $state(false);
  const PHONE_MQ = '(max-width: 699px)';
  function clampSheet(f) {
    return Math.min(0.95, Math.max(0.15, f));
  }
  $effect(() => {
    if (typeof window === 'undefined' || !window.matchMedia) return;
    const mq = window.matchMedia(PHONE_MQ);
    const apply = () => {
      isPhone = mq.matches;
      if (!mq.matches) {
        pos = null;
        peeking = false;
      }
    };
    apply();
    mq.addEventListener('change', apply);
    return () => mq.removeEventListener('change', apply);
  });
  const sheetStyle = $derived(
    isPhone
      ? `left:0;right:0;top:auto;bottom:0;width:auto;min-width:0;min-height:0;height:${peeking ? '44px' : Math.round(sheetH * 100) + '%'};resize:none;border-radius:14px 14px 0 0;`
      : pos
        ? `left:${pos.x}px;top:${pos.y}px;right:auto;`
        : ''
  );
  function startDrag(e) {
    if (e.target.closest('button') || !modalEl) return;
    if (isPhone) return;
    const r = modalEl.getBoundingClientRect();
    const ox = e.clientX - r.left;
    const oy = e.clientY - r.top;
    const move = (ev) => {
      pos = {
        x: Math.max(4, Math.min(window.innerWidth - 60, ev.clientX - ox)),
        y: Math.max(4, Math.min(window.innerHeight - 40, ev.clientY - oy))
      };
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
    e.preventDefault();
  }
  function startSheetResize(e) {
    if (!isPhone) return;
    e.preventDefault();
    const startY = e.clientY;
    const startF = sheetH;
    let moved = false;
    const move = (ev) => {
      const f = startF + (startY - ev.clientY) / window.innerHeight;
      if (Math.abs(f - startF) > 0.02) moved = true;
      sheetH = clampSheet(f);
    };
    const up = () => {
      window.removeEventListener('pointermove', move);
      window.removeEventListener('pointerup', up);
      if (!moved) sheetH = 0.4;
    };
    window.addEventListener('pointermove', move);
    window.addEventListener('pointerup', up);
  }
  function startPeek(e) {
    if (!isPhone) return;
    e.preventDefault();
    peeking = true;
    const end = () => {
      peeking = false;
      window.removeEventListener('pointerup', end);
      window.removeEventListener('pointercancel', end);
    };
    window.addEventListener('pointerup', end);
    window.addEventListener('pointercancel', end);
  }

  // Live swatch editing: paint immediately, mark the theme as custom.
  function onSwatch(token, value) {
    setToken(token, value);
    colors = { ...colors, [token]: value };
    applyPeer(colorsOut());
    applyMsg(colorsOut(), msgNums);
    currentName = 'custom';
    flashSectionDebounced('colors');
    persistActiveSoon();
  }

  function onPeerSwatch(value) {
    setToken(PEER_VAR, value);
    peerPin = value;
    applyMsg(colorsOut(), msgNums);
    currentName = 'custom';
    flashSectionDebounced('colors');
    persistActiveSoon();
  }

  function selectBg(url) {
    bg = { ...bg, url };
    applyBg(bg);
    persistActiveSoon();
  }

  function onSlider(key, value) {
    bg = { ...bg, [key]: +value };
    applyBg(bg);
    persistActiveSoon();
  }

  function onTextSlider(key, value) {
    text = { ...text, [key]: +value };
    applyText(text);
    currentName = 'custom';
    flashSectionDebounced('text');
    persistActiveSoon();
  }

  function resetText() {
    text = emptyText();
    applyText(text);
    currentName = 'custom';
    flashSectionDebounced('text');
    persistActiveSoon();
  }
</script>

<svelte:window onkeydown={(e) => { if (e.key === 'Escape') onclose(); }} />
<div
  class="modal theme-modal" class:sheet={isPhone}
  bind:this={modalEl}
  role="dialog"
  aria-label="appearance settings"
  style={sheetStyle}
>
  {#if isPhone}
    <div class="sheet-handle" onpointerdown={startSheetResize} title="drag to resize · tap for 40%">
      <span></span>
    </div>
  {/if}
  <div class="head-row" onpointerdown={startDrag}>
    <h2>Appearance</h2>
    <span class="head-btns">
      {#if isPhone}
        <button
          class="peek"
          onpointerdown={startPeek}
          title="hold to peek at the app behind"
          aria-label="hold to peek at the app behind"
        >
          👁
        </button>
      {/if}
      <button
        class="close"
        onclick={() => { themePick.on = true; onclose(); }}
        title="click any part of the screen to change its colors"
        data-testid="theme-pick">🎯 click to edit</button>
      <button class="close" onclick={onclose} title="close" aria-label="close">✕</button>
    </span>
  </div>

  {#if error}
    <div class="err-strip">⚠ {error}</div>
  {/if}

  <!-- Colors --------------------------------------------------------------- -->
  <details class="field sec" open>
    <summary>Colors</summary>
    <div class="color-grid">
      {#each VARS as v, k (v)}
        <label class="color-field">
          <input type="color" value={colors[v] ?? '#000000'} oninput={(e) => onSwatch(v, e.currentTarget.value)} />
          {LABELS[k]}
        </label>
      {/each}
      <label class="color-field" class:pinned={peerPin} title="messages sent by other pulses">
        <input type="color" value={peerShown} oninput={(e) => onPeerSwatch(e.currentTarget.value)} />
        Other pulses
      </label>
    </div>
    {#if colorsNote}
      <p class="text-note flash-note">⚡ {colorsNote}</p>
    {/if}
  </details>

  <!-- Background image ----------------------------------------------------- -->
  <details class="field sec">
    <summary>Background image</summary>
    <div class="prof-row">
      <button class="bg-pill" class:on={!bg.url} onclick={() => selectBg(null)}>NONE</button>
      {#each BGS as g (g.url)}
        <button class="bg-pill" class:on={bgUrl(bg.url) === g.url} onclick={() => selectBg(g.url)}>
          <img src={bgThumb(g.url)} alt="" />{g.name.toUpperCase()}
        </button>
      {/each}
    </div>
    <div class="bg-sliders">
      <div><label>zoom</label><input type="range" min="20" max="260" value={bg.zoom} oninput={(e) => onSlider('zoom', e.currentTarget.value)} /></div>
      <div><label>left / right</label><input type="range" min="0" max="100" value={bg.x} oninput={(e) => onSlider('x', e.currentTarget.value)} /></div>
      <div><label>up / down</label><input type="range" min="0" max="100" value={bg.y} oninput={(e) => onSlider('y', e.currentTarget.value)} /></div>
      <div><label>opacity</label><input type="range" min="0" max="100" value={bg.opacity ?? 50} oninput={(e) => onSlider('opacity', e.currentTarget.value)} /></div>
    </div>
  </details>

  <!-- Layout --------------------------------------------------------------- -->
  <details class="field sec">
    <summary>Layout</summary>
    <label class="bar-toggle">
      <input
        type="checkbox"
        checked={uiPrefs.showMobileSidebar}
        onchange={(e) => onMobileSidebar(e.currentTarget.checked)}
      />
      <span>Sidebar on phone</span>
    </label>
    <p class="text-note">
      The desktop icon rail on the phone's left edge — watchers, inbox,
      hub (when configured), all pulses, new pulse, usage and clock, plus
      the view menu ("running only", appearance, settings) and theme dice.
    </p>
    <label class="bar-toggle">
      <input
        type="checkbox"
        checked={uiPrefs.showMetricsBar}
        onchange={(e) => onMetricsBar(e.currentTarget.checked)}
      />
      <span>Metrics bar</span>
    </label>
    <p class="text-note">
      The one-line metrics strip under the nav. Tap the strip to expand, or
      double-tap for the full dashboard.
    </p>
    {#if layoutNote}
      <p class="text-note flash-note">⚡ {layoutNote}</p>
    {/if}
  </details>

  <!-- Text size ------------------------------------------------------------ -->
  <details class="field sec">
    <summary>
      Text size
      <button class="reset-text" onclick={(e) => { e.preventDefault(); e.stopPropagation(); resetText(); }} title="back to 100%">reset</button>
    </summary>
    <div class="text-sliders">
      {#each [['chat', TEXT_LABELS[0]], ['tool', TEXT_LABELS[1]], ['ui', TEXT_LABELS[2]], ['icons', TEXT_LABELS[3]]] as [key, lbl] (key)}
        <div class="trow">
          <label for="tscale-{key}">{lbl}</label>
          <input
            id="tscale-{key}"
            type="range"
            min={TEXT_MIN}
            max={TEXT_MAX}
            step="0.05"
            value={text[key]}
            oninput={(e) => onTextSlider(key, e.currentTarget.value)}
          />
          <span class="tval">{Math.round(text[key] * 100)}%</span>
        </div>
      {/each}
    </div>
    <p class="text-note">
      Chat = message text and markdown · Tool = the grey activity lines ·
      Interface = nav, headers, drawers and modals (including this one) ·
      Icons = every button glyph.
    </p>
    {#if textNote}
      <p class="text-note flash-note">⚡ {textNote}</p>
    {/if}
  </details>
</div>

<style>
  .modal {
    position: fixed;
    top: 54px;
    right: 14px;
    width: min(440px, calc(100% - 24px));
    max-height: calc(100% - 70px);
    min-width: 300px;
    min-height: 180px;
    resize: both;
    overflow: auto;
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
    cursor: grab;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
  }
  .head-row:active { cursor: grabbing; }
  .sheet-handle {
    position: absolute;
    top: -13px;
    left: 50%;
    transform: translateX(-50%);
    width: 44px;
    height: 26px;
    display: flex;
    align-items: center;
    justify-content: center;
    cursor: grab;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
    z-index: 2;
  }
  .sheet-handle:active { cursor: grabbing; }
  .sheet-handle span {
    width: 34px;
    height: 4px;
    border-radius: 999px;
    background: var(--border-strong);
  }
  .modal.sheet .head-row {
    margin-top: 2px;
    position: sticky;
    top: -16px;
    z-index: 3;
    background: var(--surface);
  }
  .modal h2 {
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.2em;
    color: var(--ink-dim);
    text-transform: uppercase;
  }
  .close { color: var(--ink-meta); font-size: calc(12px * var(--tscale-ui)); padding: 2px 6px; border-radius: 5px; }
  .close:hover { background: var(--surface-warm); color: var(--ink-dim); }
  .head-btns { display: flex; align-items: center; gap: 4px; }
  .peek {
    color: var(--ink-meta);
    font-size: calc(11px * var(--tscale-ui));
    padding: 2px 7px;
    border-radius: 5px;
    touch-action: none;
    user-select: none;
    -webkit-user-select: none;
    -webkit-touch-callout: none;
  }
  .peek:active { background: var(--surface-warm); color: var(--dreamer); }
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
  .field { margin-bottom: 14px; }
  details.sec { margin-bottom: 10px; }
  details.sec > summary {
    list-style: none;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.14em;
    color: var(--ink-meta);
    text-transform: uppercase;
    padding: 4px 0;
    user-select: none;
    -webkit-user-select: none;
  }
  details.sec > summary::-webkit-details-marker { display: none; }
  details.sec > summary::before { content: '▸  '; }
  details.sec[open] > summary::before { content: '▾  '; }
  details.sec > summary:hover { color: var(--ink-dim); }
  details.sec[open] > summary { margin-bottom: 6px; color: var(--ink-dim); }
  .prof-row { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .color-grid { display: grid; grid-template-columns: 1fr 1fr; gap: 6px; }
  .color-field {
    display: flex;
    align-items: center;
    gap: 8px;
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-dim);
    border: 1px solid var(--border);
    border-radius: 8px;
    padding: 4px 8px;
  }
  .color-field.pinned { border-color: var(--dreamer); color: var(--ink); }
  .color-field input[type=color] {
    width: 26px; height: 20px;
    border: none; background: none; padding: 0;
    border-radius: 5px; cursor: pointer;
  }
  .bg-pill {
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 4px 10px 4px 4px;
    border: 1px solid var(--border-strong);
    border-radius: 999px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-dim);
  }
  .bg-pill img { width: 26px; height: 16px; object-fit: cover; border-radius: 6px; }
  .bg-pill.on { border-color: var(--dreamer); color: var(--ink); box-shadow: 0 0 0 1px var(--dreamer); }
  .bg-sliders { display: grid; grid-template-columns: 1fr 1fr 1fr 1fr; gap: 8px; margin-top: 10px; }
  .bg-sliders label {
    display: block;
    font-family: var(--font-mono);
    font-size: calc(8px * var(--tscale-ui));
    letter-spacing: 0.12em;
    color: var(--ink-meta);
    text-transform: uppercase;
  }
  .bg-sliders input { width: 100%; accent-color: var(--dreamer); }
  .text-sliders { display: flex; flex-direction: column; gap: 8px; margin-top: 6px; }
  .trow { display: grid; grid-template-columns: 74px 1fr 40px; align-items: center; gap: 8px; }
  .trow label {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.1em;
    text-transform: uppercase;
    color: var(--ink-meta);
  }
  .trow input { width: 100%; accent-color: var(--dreamer); }
  .tval {
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    color: var(--ink-dim);
    text-align: right;
    font-variant-numeric: tabular-nums;
  }
  .reset-text {
    float: right;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-meta);
    border: 1px solid var(--border-strong);
    border-radius: 4px;
    padding: 1px 7px;
    text-transform: none;
  }
  .reset-text:hover { color: var(--dreamer); border-color: var(--dreamer); }
  .flash-note { color: var(--dreamer) !important; }
  .text-note {
    margin-top: 8px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    line-height: 1.6;
    color: var(--ink-meta);
  }
  .bar-toggle {
    display: flex;
    align-items: center;
    gap: 7px;
    margin-top: 8px;
    cursor: pointer;
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-dim);
  }
  .bar-toggle:first-of-type { margin-top: 0; }
  .bar-toggle input { accent-color: var(--dreamer); }
</style>
