<script>
  // Custom UI icons (24x24 stroke paths).
  const PATHS = {
    // hub = four tiles, the pages catalog.
    hub: '<rect x="3.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="3.5" width="7" height="7" rx="1.5"/><rect x="3.5" y="13.5" width="7" height="7" rx="1.5"/><rect x="13.5" y="13.5" width="7" height="7" rx="1.5"/>',
    // bookmark = ribbon; filled while a bookmark is set.
    bookmark: '<path d="M6 3h12v18l-6-4.5L6 21Z"/>',
    // gear = app settings cog.
    gear: '<circle cx="12" cy="12" r="3"/><path d="M12 2.8v2.2M12 19v2.2M4.9 4.9l1.6 1.6M17.5 17.5l1.6 1.6M2.8 12H5M19 12h2.2M4.9 19.1l1.6-1.6M17.5 6.5l1.6-1.6"/>',
    send: '<g stroke-linecap="square" stroke-linejoin="miter"><path d="M21 3 3 11l6 2 2 6ZM9 13l5-5"/></g>',
    pause: '<rect x="5" y="5" width="14" height="14" rx="2.5"/>',
    play: '<g stroke-linecap="square" stroke-linejoin="miter"><path d="M8 4v16l12-8Z"/></g>',
    loop:
      '<path d="M12 12c-2-2.7-3.5-4-5.5-4a4 4 0 0 0 0 8c2 0 3.5-1.3 5.5-4Zm0 0c2 2.7 3.5 4 5.5 4a4 4 0 0 0 0-8c-2 0-3.5 1.3-5.5 4Z"/>',
    new: '<path d="M12 5v14M5 12h14"/>',
    // reply = curved back-arrow (chat Reply button) — not a gallery pick.
    reply: '<path d="M9 7 4 12l5 5"/><path d="M4 12h10a6 6 0 0 1 6 6v1"/>',
    settings:
      '<path d="M7 4v16M12 4v16M17 4v16"/><circle cx="7" cy="9" r="2" fill="var(--surface,#04040c)"/><circle cx="12" cy="15" r="2" fill="var(--surface,#04040c)"/><circle cx="17" cy="7" r="2" fill="var(--surface,#04040c)"/>',
    expand: '<g stroke-linecap="square" stroke-linejoin="miter"><path d="M4 8V4h4M16 4h4v4M20 16v4h-4M8 20H4v-4"/></g>',
    // shrink = expand mirrored inward (restore from full screen) — derived.
    shrink: '<g stroke-linecap="square" stroke-linejoin="miter"><path d="M9 4v5H4M15 4v5h5M20 15h-5v5M4 15h5v5"/></g>',
    screen: '<rect x="2.5" y="4.5" width="19" height="15" rx="2"/>',
    drawer: '<path d="M4 6h16M4 12h16M4 18h16"/>',
    archive:
      '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M12 10.5v5M9.8 13.5 12 15.7l2.2-2.2"/>',
    // unarchive = same folder, up arrow (mirror of archive down-arrow)
    unarchive:
      '<path d="M3 7a2 2 0 0 1 2-2h4l2 2.5h8a2 2 0 0 1 2 2V17a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2Z"/><path d="M12 15.5v-5M9.8 12.5 12 10.3l2.2 2.2"/>',
    delete: '<path d="M4 7h16M10 4h4M6.5 7l1 13h9l1-13"/><path d="M10 11v6M14 11v6"/>',
    search: '<circle cx="10.5" cy="10.5" r="6.5"/><path d="M15.5 15.5 21 21"/>',
    // reset = circular refresh arrow — not a gallery pick, derived here
    // (fresh-context restart button, ChatPane).
    reset:
      '<path d="M20 12a8 8 0 1 1-2.34-5.66"/><path d="M20 4v4.5h-4.5"/>',
    // fork = git-branch glyph — not a gallery pick, derived here (pulse fork).
    fork:
      '<circle cx="6" cy="5" r="2.2"/><circle cx="6" cy="19" r="2.2"/><circle cx="18" cy="5" r="2.2"/><path d="M6 7.2v9.6M18 7.2v3a4 4 0 0 1-4 4h-3.8"/>',
    // handoff = two-way transfer arrows — not a gallery pick, derived here
    // (manual context handoff button, ChatPane).
    handoff:
      '<path d="M4 7h16M4 7l4-4M4 7l4 4"/><path d="M20 17H4M20 17l-4-4M20 17l-4 4"/>',
    // rename = pencil — not a gallery pick, derived here (pulse rename).
    rename:
      '<path d="M4 20h4L18.5 9.5a2.1 2.1 0 0 0-3-3L5 17Z"/><path d="m13.5 6.5 3 3"/>',
    // clip = classic paperclip — not a gallery pick, derived here (attachments).
    clip:
      '<path d="M20.2 11.6 12 19.8a5.3 5.3 0 0 1-7.5-7.5l8.5-8.5a3.6 3.6 0 0 1 5.1 5.1l-8.2 8.2a1.8 1.8 0 0 1-2.6-2.6l7.6-7.6"/>',
    // dice = die showing 5 — not a gallery pick, derived here (theme
    // randomizer; replaces the greyscaled 🎲 emoji so it colors like an Icon).
    dice:
      '<rect x="4" y="4" width="16" height="16" rx="3.5"/><g fill="currentColor" stroke="none"><circle cx="8.6" cy="8.6" r="1.35"/><circle cx="15.4" cy="8.6" r="1.35"/><circle cx="12" cy="12" r="1.35"/><circle cx="8.6" cy="15.4" r="1.35"/><circle cx="15.4" cy="15.4" r="1.35"/></g>',
    // inbox = mail/envelope with flap.
    inbox:
      '<rect x="3" y="5" width="18" height="14" rx="2.5"/><path d="m3 7 9 6 9-6"/>',
    // watch = eye (sidebar Watches tab, condition watches).
    watch:
      '<path d="M2.5 12s3.5-6.5 9.5-6.5S21.5 12 21.5 12s-3.5 6.5-9.5 6.5S2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
    // clock = dial with hands — not a gallery pick, derived here (sidebar
    // Schedules tab — every loop and wake in one list).
    clock:
      '<circle cx="12" cy="12" r="8.5"/><path d="M12 7.5V12l3 2"/>'
  };

  /** @type {{ name: keyof typeof PATHS, size?: number, stroke?: number }} */
  let { name, size = 15, stroke = 1.8 } = $props();
</script>

<!-- width/height ride the appearance-settings icon-scale multiplier; the
     viewBox keeps every glyph crisp at any size. -->
<svg
  viewBox="0 0 24 24"
  style="width: calc({size}px * var(--iscale, 1)); height: calc({size}px * var(--iscale, 1));"
  fill="none"
  stroke="currentColor"
  stroke-width={stroke}
  stroke-linecap="round"
  stroke-linejoin="round"
  aria-hidden="true"
>
  <!-- eslint-disable-next-line svelte/no-at-html-tags -->
  {@html PATHS[name] ?? ''}
</svg>
