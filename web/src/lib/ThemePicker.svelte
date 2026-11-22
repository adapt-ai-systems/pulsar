<script>
  /**
   * Click-to-edit theme mode. While themePick.on: hovering outlines the part
   * under the cursor, clicking it opens a popover with one color control per
   * theme token that part is painted with. Edits apply live and persist to
   * this device's active profile. Esc or Done leaves the mode.
   */
  import { themePick, tokensAt, toHex } from './themepick.svelte.js';
  import { VARS, LABELS, MSG_NUMS, TEXT_VARS, TEXT_LABELS, TEXT_MIN, TEXT_MAX, normalizeText, FONT_VARS, FONT_CHOICES, applyProfile, loadActiveProfile, saveActiveProfile, normalizeMsgNums } from './theme.js';

  let hover = $state(null); // DOMRect of the hovered element
  let pop = $state(null); // {x, y, where, rows:[{token,label,kind,value}]}
  let profile = null;
  let saveTimer;

  const NAMES = Object.fromEntries(VARS.map((v, i) => [v, LABELS[i]]));
  const TEXT_KEYS = ['chat', 'tool', 'ui', 'icons'];
  const SCALE = Object.fromEntries(TEXT_VARS.map((v, i) => [v, { key: TEXT_KEYS[i], label: TEXT_LABELS[i] }]));
  const FILL = Object.fromEntries(MSG_NUMS.map((n) => [n.var, n]));

  $effect(() => {
    if (!themePick.on) { hover = null; pop = null; return; }
    loadActiveProfile().then((p) => { profile = structuredClone(p); });
    const inside = (e) => e.target.closest?.('[data-themepick]');
    const block = (e) => { if (!inside(e)) { e.preventDefault(); e.stopPropagation(); } };
    const onMove = (e) => {
      if (inside(e)) return;
      hover = e.target.getBoundingClientRect?.() ?? null;
    };
    const onClick = (e) => {
      if (inside(e)) return;
      block(e);
      openAt(e.target, e.clientX, e.clientY);
    };
    const onKey = (e) => {
      if (e.key === 'Escape') { e.stopPropagation(); if (pop) pop = null; else themePick.on = false; }
    };
    const opts = { capture: true };
    window.addEventListener('pointermove', onMove, opts);
    window.addEventListener('click', onClick, opts);
    for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'dblclick', 'contextmenu', 'submit']) window.addEventListener(t, block, opts);
    window.addEventListener('keydown', onKey, opts);
    return () => {
      window.removeEventListener('pointermove', onMove, opts);
      window.removeEventListener('click', onClick, opts);
      for (const t of ['pointerdown', 'mousedown', 'pointerup', 'mouseup', 'dblclick', 'contextmenu', 'submit']) window.removeEventListener(t, block, opts);
      window.removeEventListener('keydown', onKey, opts);
    };
  });

  function openAt(el, x, y) {
    const root = getComputedStyle(document.documentElement);
    const rows = [];
    for (const t of tokensAt(el)) {
      if (FONT_VARS[t.token]) {
        const cur = root.getPropertyValue(t.token).trim();
        rows.push({ ...t, label: FONT_VARS[t.token], kind: 'font', value: cur });
        continue;
      }
      if (SCALE[t.token]) {
        const n = parseFloat(root.getPropertyValue(t.token)) || 1;
        rows.push({ ...t, label: SCALE[t.token].label, kind: 'scale', value: n });
        continue;
      }
      if (FILL[t.token]) {
        const n = parseFloat(root.getPropertyValue(t.token)) || 0;
        rows.push({ ...t, kind: 'fill', value: n });
        continue;
      }
      const hex = toHex(root.getPropertyValue(t.token));
      if (hex) rows.push({ ...t, kind: 'color', value: hex });
    }
    pop = {
      x: Math.min(x + 12, window.innerWidth - 300),
      y: Math.min(y + 12, window.innerHeight - 40 - rows.length * 34),
      where: rows[0]?.where ?? el.tagName.toLowerCase(),
      rows
    };
  }

  function setColor(row, hex) {
    row.value = hex;
    if (!profile) return;
    profile.colors = { ...profile.colors, [row.token]: hex };
    profile.name = 'custom';
    applyProfile(profile);
    // Tokens outside the profile model (e.g. --peer-ish one-offs) still paint.
    document.documentElement.style.setProperty(row.token, hex);
    saveSoon();
  }

  function setFill(row, n) {
    row.value = n;
    if (!profile) return;
    profile.msg = { ...normalizeMsgNums(profile.msg), [FILL[row.token].key]: n };
    profile.name = 'custom';
    applyProfile(profile);
    saveSoon();
  }

  function setFont(row, stack) {
    row.value = stack;
    if (!profile) return;
    profile.colors = { ...profile.colors, [row.token]: stack };
    profile.name = 'custom';
    applyProfile(profile);
    saveSoon();
  }

  function setScale(row, n) {
    row.value = n;
    if (!profile) return;
    profile.text = { ...normalizeText(profile.text), [SCALE[row.token].key]: n };
    profile.name = 'custom';
    applyProfile(profile);
    saveSoon();
  }

  function saveSoon() {
    clearTimeout(saveTimer);
    saveTimer = setTimeout(() => saveActiveProfile(profile), 400);
  }
</script>

{#if themePick.on}
  {#if hover && !pop}
    <div class="hl" style="left:{hover.left}px;top:{hover.top}px;width:{hover.width}px;height:{hover.height}px"></div>
  {/if}
  <div class="bar" data-themepick>
    <span>Theme edit mode: click any part of the screen to change its colors.</span>
    <button onclick={() => (themePick.on = false)}>Done</button>
  </div>
  {#if pop}
    <div class="pop" data-themepick style="left:{pop.x}px;top:{pop.y}px">
      <div class="pop-head">
        <span>{pop.where}</span>
        <button onclick={() => (pop = null)} aria-label="close">✕</button>
      </div>
      {#if pop.rows.length === 0}
        <div class="empty">No theme colors on this part. Try clicking its edge or a parent area.</div>
      {/if}
      {#each pop.rows as row (row.token)}
        <label class="row">
          <span class="lbl">{row.label} <em>{NAMES[row.token] ?? row.token}</em></span>
          {#if row.kind === 'color'}
            <input type="color" value={row.value} oninput={(e) => setColor(row, e.currentTarget.value)} />
            <code>{row.value}</code>
          {:else if row.kind === 'font'}
            <select class="font" onchange={(e) => setFont(row, e.currentTarget.value)}>
              {#if !FONT_CHOICES.some(([, st]) => st === row.value)}<option value={row.value} selected>current</option>{/if}
              {#each FONT_CHOICES as [name, st] (name)}
                <option value={st} selected={st === row.value} style="font-family:{st}">{name}</option>
              {/each}
            </select>
            <span></span>
          {:else if row.kind === 'scale'}
            <input type="range" min={TEXT_MIN} max={TEXT_MAX} step="0.05" value={row.value} oninput={(e) => setScale(row, +e.currentTarget.value)} />
            <code>{Math.round(row.value * 100)}%</code>
          {:else}
            <input type="range" min="0" max="100" value={row.value} oninput={(e) => setFill(row, +e.currentTarget.value)} />
            <code>{row.value}%</code>
          {/if}
        </label>
      {/each}
      <div class="note">Other parts that use the same color, font, or text size change too.</div>
    </div>
  {/if}
{/if}

<style>
  .hl {
    position: fixed; pointer-events: none; z-index: 9998;
    outline: 2px dashed var(--sprite); outline-offset: -1px;
    background: color-mix(in srgb, var(--sprite) 8%, transparent);
  }
  .bar {
    position: fixed; top: 8px; left: 50%; transform: translateX(-50%); z-index: 10000;
    display: flex; gap: 12px; align-items: center; padding: 6px 12px; border-radius: 8px;
    background: var(--surface); color: var(--ink); border: 1px solid var(--sprite);
    font-size: 13px; box-shadow: 0 4px 20px rgba(0, 0, 0, 0.5);
  }
  .bar button, .pop-head button {
    background: var(--surface-warm); color: var(--ink); border: 1px solid var(--border-strong);
    border-radius: 5px; padding: 2px 10px; cursor: pointer;
  }
  .pop {
    position: fixed; z-index: 10000; width: 280px; padding: 8px 10px; border-radius: 8px;
    background: var(--surface); color: var(--ink); border: 1px solid var(--border-strong);
    font-size: 12px; box-shadow: 0 8px 30px rgba(0, 0, 0, 0.6);
  }
  .pop-head { display: flex; justify-content: space-between; align-items: center; margin-bottom: 6px; color: var(--ink-dim); }
  .row { display: grid; grid-template-columns: 1fr auto 64px; gap: 8px; align-items: center; padding: 3px 0; }
  .lbl em { color: var(--ink-meta); font-style: normal; margin-left: 4px; }
  .row input[type='color'] { width: 34px; height: 22px; padding: 0; border: none; background: none; cursor: pointer; }
  .row input[type='range'] { width: 90px; }
  .font { grid-column: span 1; background: var(--surface-warm); color: var(--ink); border: 1px solid var(--border-strong); border-radius: 4px; font-size: 12px; }
  code { color: var(--ink-dim); font-size: 11px; }
  .empty, .note { color: var(--ink-meta); padding: 4px 0; }
</style>
