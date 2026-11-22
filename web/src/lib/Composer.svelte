<script>
  import { chatDrafts } from './chatdrafts.js';
  import { sendMessage, sendMessageMultipart, interruptPulse, listSchedules, createSchedule, deleteSchedule, renamePulse } from './api.js';
  import { parseSlashCommand, slashMenu } from './loopcmd.js';
  import { runSlashCommand } from './slashrun.js';
  import { refreshSchedules } from './schedulestore.svelte.js';
  import { composerDragOver, composerDrop, fmtSize } from './composerdrop.js';
  import { fmtTs } from './time.js';
  import Icon from './Icon.svelte';

  /**
   * The pane's message composer: draft, staged attachments, reply bar, slash
   * menu, send, keys, drag/drop. Renders as a fragment of siblings (reply bar,
   * staged row, composer) so the pane's flex column and the
   * adjacent-sibling borders in the CSS below stay exactly as they were.
   *
   * @type {{
   *   pulse: any,
   *   stopped: boolean, archived: boolean, running: boolean,
   *   sending?: boolean, replyTo?: ({pulseId: string, text: string, ts: string|null})|null,
   *   draftSaveFailed?: boolean,
   *   onerror: (message: string) => void, onnotice: (message: string) => void,
   *   onsent: () => void, onchanged?: () => void
   * }}
   */
  let {
    pulse,
    stopped,
    archived,
    running,
    sending = $bindable(false),
    // The Reply button on a transcript bubble lives in the pane and sets this.
    replyTo = $bindable(null),
    // The pane renders the "draft not saved" strip above the log.
    draftSaveFailed = $bindable(false),
    onerror,
    onnotice,
    onsent,
    onchanged
  } = $props();

  const pulseId = $derived(pulse.id);

  let draft = $state('');
  $effect(() => chatDrafts.subscribe(pulseId, (text, saved) => {
    draft = text;
    draftSaveFailed = !saved;
  }));
  function onDraftInput(event) {
    draft = event.currentTarget.value;
    draftSaveFailed = !chatDrafts.write(pulse.id, draft);
    menuIdx = 0; // the filter changed the rows — restart the highlight at the top
    if (!draft.startsWith('/')) menuDismissed = false; // typing reopens
  }

  // --- slash-command picker -----------------------------------------------
  // '/' as the first key opens a menu above the composer. Cycle with ↑/↓,
  // accept with Tab/Enter, dismiss with Escape; tap/click works with thumbs
  // or mouse, and typing keeps filtering. Model comes from loopcmd.slashMenu,
  // execution is still runSlashCommand (slashrun.js) on the final send.
  let menuDismissed = $state(false); // Escape hides it until '/' is retyped
  let menuIdx = $state(0); // highlighted row
  const slashPick = $derived(menuDismissed ? null : slashMenu(draft));
  $effect(() => {
    if (!draft.startsWith('/')) menuDismissed = false;
  });
  function acceptSlash(it) {
    draft = it.insert;
    chatDrafts.write(pulse.id, draft);
    menuIdx = 0;
    // Keep the caret/focus in the textarea (acceptSlash can fire from a tap,
    // which would otherwise leave the on-screen keyboard down).
    taEl?.focus({ preventScroll: true });
  }

  // --- attachments (paste a screenshot / attach files) -------------------- //
  // Staged files live client-side until send; images and videos get an
  // object-URL preview which MUST be revoked on unstage/send/unmount to avoid
  // leaking object URLs.
  let staged = $state([]); // [{id, blob, name, size, mime, previewUrl}]

  // Reply button: the pulse message the user's next send answers.
  // `replyTo` carries pulseId so a pane reused for another pulse never sends it there.
  const activeReply = $derived(replyTo && replyTo.pulseId === pulse.id ? replyTo : null);

  /** Focus the textarea (the pane's Reply button calls this). */
  export function focus() {
    taEl?.focus();
  }

  function replyRef(r) {
    return r ? (r.ts ? { text: r.text, ts: r.ts } : { text: r.text }) : null;
  }
  let fileInput; // hidden <input type=file>

  function stageFile(blob, name) {
    const mime = blob.type || 'application/octet-stream';
    staged = [...staged, {
      id: crypto.randomUUID(),
      blob,
      name: name || blob.name || 'file',
      size: blob.size,
      mime,
      previewUrl: (mime.startsWith('image/') || mime.startsWith('video/'))
        ? URL.createObjectURL(blob)
        : null
    }];
  }

  function unstage(id) {
    const a = staged.find((s) => s.id === id);
    if (a?.previewUrl) URL.revokeObjectURL(a.previewUrl);
    staged = staged.filter((s) => s.id !== id);
  }

  function clearStaged() {
    for (const a of staged) if (a.previewUrl) URL.revokeObjectURL(a.previewUrl);
    staged = [];
  }

  // Bound to the message input itself (not window) — with many panes mounted
  // in the grid, a window listener would attach the paste to every pane at
  // once (scoping avoids needing a focus guard).
  function onPaste(e) {
    if (stopped || archived || sending) return;
    for (const item of e.clipboardData?.items ?? []) {
      if (item.type.startsWith('image/')) {
        const blob = item.getAsFile();
        if (blob) stageFile(blob, blob.name || 'pasted.png');
      }
    }
  }

  // File drops belong only to this composer; pulse drags still bubble to the
  // pane's reorder handler. Cancel disabled file drops too, so the browser
  // does not navigate away to open the file.
  function onComposerDragOver(e) {
    composerDragOver(e, { stopped, archived, sending });
  }

  function onComposerDrop(e) {
    composerDrop(e, { stopped, archived, sending }, { stageFile });
  }

  function onFilesPicked() {
    for (const f of Array.from(fileInput?.files ?? [])) stageFile(f);
    if (fileInput) fileInput.value = ''; // same file re-pickable
  }

  async function send() {
    const sentPulseId = pulse.id;
    const sentDraft = draft;
    let text = sentDraft.trim();
    if ((!text && staged.length === 0) || sending) return;
    // Slash commands (loopcmd.js) are handled locally, never sent to the pulse.
    const cmd = parseSlashCommand(text);
    if (cmd) {
      if (cmd.kind === 'literal') {
        text = cmd.text; // '//' escape — send with one slash eaten
      } else {
        onnotice('');
        await runCommand(cmd, sentDraft);
        return;
      }
    } else {
      onnotice('');
    }
    sending = true;
    const sentReply = activeReply;
    const r = staged.length === 0
      ? await sendMessage(pulse.id, text, undefined, replyRef(sentReply))
      : await sendMessageMultipart(pulse.id, text,
          staged.map((a) => ({ blob: a.blob, name: a.name })), replyRef(sentReply));
    sending = false;
    if (r.ok) {
      chatDrafts.clearSent(sentPulseId, sentDraft);
      clearStaged();
      if (replyTo === sentReply) replyTo = null;
      // follow=true: you just sent a message, so jump to it even if you were reading up.
      onsent();
    } else {
      // A blocked file type fails the whole send: say which file and mark
      // its chip, instead of a bare "HTTP 415: <mime> not allowed".
      const blocked = /not allowed \((.+)\)$/.exec(r.error ?? '');
      if (blocked) {
        staged = staged.map((a) => (a.name === blocked[1] ? { ...a, bad: true } : a));
        onerror(`${blocked[1]} can't be sent: that file type isn't allowed. Remove it (×) and send again.`);
      } else {
        onerror(`send: ${r.error}`);
        onchanged?.();
      }
    }
    // The disabled flip blurs the textarea, and the browser doesn't give
    // focus back. Focusing HERE would no-op: Svelte flushes DOM updates on a
    // microtask, so the element is still `disabled` at this line. The refocus
    // rides the refocusAfterSend flag into the draft/sending effect below,
    // which runs after the flush, when the textarea is enabled again.
    refocusAfterSend = true;
  }

  // Slash-command execution. Feedback lands in the local notice strip; the
  // typed command is cleared like a sent message on success.
  // The table itself is slashrun.js.
  async function runCommand(cmd, sentDraft) {
    sending = true;
    try {
      const r = await runSlashCommand(cmd, {
        pulse,
        api: { listSchedules, createSchedule, deleteSchedule, renamePulse },
        refreshSchedules,
        onchanged
      });
      if (r.error) onerror(r.error);
      if (r.notice !== undefined) onnotice(r.notice);
      if (r.clearDraft) chatDrafts.clearSent(pulse.id, sentDraft);
    } finally {
      sending = false;
      refocusAfterSend = true;
    }
  }

  // Double-Enter turn abort: first Enter sends as always; a second Enter on the
  // EMPTY composer within 2s aborts the running turn so the just-sent message
  // gets picked up immediately instead of waiting out a
  // long V4 turn. Gated on pulse.state (not the thinking chip) because the
  // busy heartbeat can lag the send by a poll tick — the runner answers
  // "noop" when it was idle anyway, and "not_interruptible" for vessels
  // without a turn abort (claude/qwen), which lands in the error strip.
  let lastEnterTs = 0;
  const DOUBLE_ENTER_MS = 2000;

  async function doInterrupt() {
    const r = await interruptPulse(pulse.id);
    if (!r.ok) onerror(`interrupt: ${r.error}`);
  }

  function onKey(e) {
    // Slash-command picker owns the keys while it's open — but Enter falls
    // through when the highlighted row would add nothing (command fully
    // typed), so a finished command still runs on Enter.
    if (slashPick && slashPick.length > 0) {
      const it = slashPick[Math.min(menuIdx, slashPick.length - 1)];
      if (e.key === 'ArrowDown' || e.key === 'ArrowUp') {
        e.preventDefault();
        const dir = e.key === 'ArrowDown' ? 1 : -1;
        menuIdx = (menuIdx + dir + slashPick.length) % slashPick.length;
        return;
      }
      // Accept ONLY when the row extends what is typed (completion, never
      // shortening — otherwise Enter on '/loop stop abc123' would wipe the
      // id back down to '/loop stop ', 2026-09-15). Anything else
      // falls through so Enter/Tab runs the finished command. Shift+Enter
      // always means a newline, menu or not.
      const extends_ = it.insert.trimEnd().toLowerCase().startsWith(draft.trim().toLowerCase()) &&
        it.insert.trim() !== draft.trim();
      if ((e.key === 'Tab' || (e.key === 'Enter' && !e.shiftKey)) && extends_) {
        e.preventDefault();
        acceptSlash(it);
        return;
      }
      if (e.key === 'Escape') {
        e.preventDefault();
        menuDismissed = true;
        return;
      }
    }
    if (e.key === 'Escape' && activeReply) {
      e.preventDefault();
      replyTo = null;
      return;
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault();
      const ts = Date.now();
      const empty = !draft.trim() && staged.length === 0;
      if (empty && running && ts - lastEnterTs <= DOUBLE_ENTER_MS) {
        lastEnterTs = 0; // one abort per gesture — a third Enter starts over
        doInterrupt();
      } else {
        lastEnterTs = ts;
        send();
      }
    }
  }

  // --- composer auto-grow ------------------------------------------------- //
  // The composer starts one row tall and grows with the message so a long one
  // is readable while you write it, capped at four rows so it never eats the
  // pane. Past the cap the textarea scrolls itself.
  const COMPOSER_MAX_ROWS = 4;
  /** @type {HTMLTextAreaElement | null} */
  let taEl = $state(null);

  function autoGrow() {
    if (!taEl) return;
    // Collapse first: scrollHeight only reports the content height when the
    // element is not already stretched to hold it.
    taEl.style.height = 'auto';
    const cs = getComputedStyle(taEl);
    // Row height comes from the computed line-height so this tracks the
    // Interface text-size slider instead of assuming a pixel size.
    const line = parseFloat(cs.lineHeight) || 18;
    const pad = (parseFloat(cs.paddingTop) || 0) + (parseFloat(cs.paddingBottom) || 0);
    const max = Math.round(line * COMPOSER_MAX_ROWS + pad);
    taEl.style.height = Math.min(taEl.scrollHeight, max) + 'px';
    taEl.style.overflowY = taEl.scrollHeight > max ? 'auto' : 'hidden';
  }

  // Covers typing, paste, and send() clearing the draft — all of them land as
  // a change to `draft`, so one effect handles the lot.
  //
  // It also carries the post-send refocus (see send()): this effect runs
  // AFTER Svelte has flushed the textarea's enabled state back, which is the
  // first moment a focus() actually sticks. Only restores the caret on the
  // desktop layout, and only if the user hasn't clicked elsewhere or typed.
  let refocusAfterSend = $state(false);
  $effect(() => {
    draft;
    taEl;
    autoGrow();
    if (refocusAfterSend && taEl && !taEl.disabled) {
      refocusAfterSend = false;
      if (window.matchMedia('(min-width: 700px)').matches &&
          document.activeElement !== taEl && !draft)
        taEl.focus({ preventScroll: true });
    }
  });

  // Staged-attachment preview URLs must be revoked on unmount; object URLs
  // outlive the component otherwise.
  $effect(() => {
    return () => clearStaged();
  });
</script>

{#if activeReply}
  <!-- The message this send replies to (Reply button). × or Esc cancels. -->
  <div class="reply-bar" title={activeReply.text}>
    <span class="reply-bar-lbl"><Icon name="reply" size={11} stroke={2} /> replying to{#if activeReply.ts}{` · ${fmtTs(activeReply.ts)}`}{/if}</span>
    <span class="reply-bar-text">{activeReply.text}</span>
    <button class="staged-x" onclick={() => (replyTo = null)} title="cancel reply (Esc)" aria-label="cancel reply">×</button>
  </div>
{/if}

{#if staged.length}
  <!-- Staged attachments: pasted screenshots + picked/dropped files, removable
       before send. Thumbnails are object URLs (revoked on unstage/send). -->
  <div class="staged-row">
    {#each staged as a (a.id)}
      <div class="staged-chip" class:bad={a.bad} title={a.bad ? `${a.name} — this file type can't be sent` : `${a.name} · ${fmtSize(a.size)}`}>
        {#if a.previewUrl && a.mime.startsWith('image/')}
          <img class="staged-thumb" src={a.previewUrl} alt={a.name} />
        {:else if a.previewUrl && a.mime.startsWith('video/')}
          <video class="staged-thumb" src={a.previewUrl} muted preload="metadata" aria-label={a.name}></video>
        {:else}
          <span class="staged-icon"><Icon name="clip" size={12} /></span>
        {/if}
        <span class="staged-name">{a.name}</span>
        <button class="staged-x" onclick={() => unstage(a.id)} title="remove" aria-label={`remove ${a.name}`}>×</button>
      </div>
    {/each}
  </div>
{/if}

<!-- svelte-ignore a11y_no_static_element_interactions -->
<div class="composer" data-composer-zone ondragover={onComposerDragOver} ondrop={onComposerDrop}>
  {#if slashPick}
    <!-- Slash-command picker: '/' opens it, ↑/↓ cycle, Tab/Enter accept,
         Esc dismisses; tap (thumbs) or click (mouse) picks directly, and
         typing keeps filtering. pointerdown is swallowed so tapping on the
         phone doesn't drop the textarea focus / on-screen keyboard. -->
    <div class="slash-menu" role="listbox" aria-label="slash commands">
      {#each slashPick as it, i (it.insert)}
        <button
          type="button"
          role="option"
          aria-selected={i === menuIdx}
          class="slash-item"
          class:active={i === menuIdx}
          onpointerdown={(e) => e.preventDefault()}
          onpointerenter={() => (menuIdx = i)}
          onclick={() => acceptSlash(it)}
        >
          <span class="slash-cmd">{it.label}</span>
          <span class="slash-hint">{it.hint}</span>
        </button>
      {/each}
    </div>
  {/if}
  <!-- Hidden picker behind the paperclip. Accept mirrors the daemon's
       allowlist (images, videos, audio, text, pdf, csv/json/markdown, zip archives,
       3D scan/mesh exports). -->
  <input
    type="file"
    multiple
    accept="image/png,image/jpeg,image/gif,image/webp,video/*,.mkv,.avi,audio/*,.m4a,text/plain,text/markdown,.md,.markdown,text/csv,application/json,application/pdf,.zip,application/zip,application/x-zip-compressed,.obj,.mtl,.stl,.ply,.glb,.gltf,.usdz,.torrent,application/x-bittorrent"
    bind:this={fileInput}
    onchange={onFilesPicked}
    style="display:none"
  />
  <div class="ta-wrap">
    <button
      class="clip"
      onclick={() => fileInput?.click()}
      disabled={stopped || archived || sending}
      title="attach files, audio or video"
      aria-label="attach files, audio or video"
    ><Icon name="clip" size={15} /></button>
    <div class="ta-field">
      <!-- A textarea, not an input, so a long message is readable while you
           write it. Height is driven by autoGrow() up to COMPOSER_MAX_ROWS,
           then it scrolls. Enter still sends; shift+Enter makes a newline. -->
      <textarea
        rows="1"
        bind:this={taEl}
        placeholder={stopped ? 'restore to send…' : archived ? 'archived' : 'message…'}
        value={draft}
        oninput={onDraftInput}
        onkeydown={onKey}
        onpaste={onPaste}
        disabled={stopped || archived || sending}
      ></textarea>
    </div>
    <button class="send" onclick={send} disabled={stopped || archived || sending} title="send" aria-label="send"><Icon name="send" size={15} /></button>
  </div>
</div>

<style>
  .composer {
    position: relative;
    display: flex;
    align-items: center;
    gap: 8px;
    padding: 8px 10px;
    background: var(--surface);
    border-top: 1px solid var(--border);
    flex-shrink: 0;
  }

  /* Slash-command picker: floats above the composer, inside the pane so it
     never spills over the pane edge. */
  .slash-menu {
    position: absolute;
    left: 10px;
    right: 10px;
    bottom: calc(100% - 2px);
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 6px 6px 0 0;
    box-shadow: 0 -6px 18px rgba(0, 0, 0, 0.45);
    overflow-y: auto;
    max-height: 40vh;
    z-index: 5;
  }
  .slash-item {
    display: flex;
    align-items: baseline;
    gap: 10px;
    width: 100%;
    text-align: left;
    padding: 7px 12px;
    background: none;
    border: none;
    border-bottom: 1px solid var(--border);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: calc(11px * var(--tscale-ui));
    cursor: pointer;
  }
  .slash-item:last-child { border-bottom: none; }
  .slash-item.active { background: color-mix(in srgb, var(--dreamer) 16%, var(--surface-warm)); }
  .slash-cmd { font-weight: 600; white-space: nowrap; }
  .slash-hint {
    color: var(--ink-dim);
    font-size: calc(10px * var(--tscale-ui));
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .ta-wrap {
    flex: 1;
    display: flex;
    /* flex-end, not center: the textarea grows downward, so the clip and send
       buttons stay on the bottom row beside the newest line of text. */
    align-items: flex-end;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 18px;
    padding: 7px 12px;
    color: var(--ink-meta);
    font-size: calc(12px * var(--tscale-ui));
  }
  .ta-wrap textarea::placeholder { color: var(--ink-meta); }
  .ta-field { flex: 1; min-width: 0; display: flex; flex-direction: column; }
  .ta-wrap textarea {
    width: 100%;
    background: none;
    border: none;
    outline: none;
    resize: none;
    padding: 0;
    /* A textarea does NOT inherit the page font the way an input does — without
       this it renders in the browser's default monospace at the wrong size. */
    font: inherit;
    /* Do NOT inherit colour: .ta-wrap is --ink-meta so its placeholder and
       icons read dim; inheriting that made typed text dim too (an <input>
       never inherited colour, unlike <textarea>). */
    color: var(--ink);
    /* autoGrow() reads this to size a row, so keep it a plain number. */
    line-height: 1.4;
    /* JS sets the real height; this is only the pre-hydration first paint. */
    height: calc(1.4em);
    overflow-y: hidden;
    display: block;
  }
  .ta-wrap .send {
    margin-left: auto;
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--dreamer);
  }
  .ta-wrap .send:disabled { color: var(--ink-meta); cursor: default; }
  .ta-wrap :global(.clip) {
    display: flex;
    align-items: center;
    justify-content: center;
    color: var(--ink-meta);
    margin-right: 8px;
    flex-shrink: 0;
  }
  .ta-wrap :global(.clip:hover:not(:disabled)) { color: var(--ink-dim); }
  .ta-wrap :global(.clip:disabled) { opacity: 0.45; cursor: default; }
  /* Touch devices: render the 15px clip/send icons ~50% larger for easier thumb
     targets. CSS overrides the svg's own width/height attributes; desktop keeps
     the compact size. */
  @media (pointer: coarse) {
    .ta-wrap :global(.clip svg), .ta-wrap .send :global(svg) { width: 23px; height: 23px; }
    .ta-wrap :global(.clip), .ta-wrap .send { padding: 2px; }
  }

  /* The quoted message above the composer while replying (the copy on the sent
     YOU bubble is .reply-quote in ChatPane). Clamped: the pulse gets the full
     text, this is a reminder. */
  .reply-bar {
    display: flex;
    flex-direction: column;
    gap: 1px;
    min-width: 0;
    border-left: 2px solid color-mix(in srgb, var(--dreamer) 70%, transparent);
    background: color-mix(in srgb, var(--dreamer) 6%, var(--surface));
    border-radius: 0 6px 6px 0;
  }
  .reply-bar {
    position: relative;
    flex-shrink: 0;
    margin: 6px 10px 0;
    padding: 4px 26px 4px 8px;
  }
  .reply-bar + .staged-row, .reply-bar + .composer { border-top: none; }
  .reply-bar .staged-x { position: absolute; top: 3px; right: 4px; }
  .reply-bar-lbl {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    font-family: var(--font-mono);
    font-size: calc(8.5px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--dreamer);
  }
  .reply-bar-text {
    font-family: var(--font-serif);
    font-size: calc(11px * var(--tscale-chat));
    line-height: 1.35;
    color: var(--ink-dim);
    white-space: pre-wrap;
    overflow-wrap: anywhere;
    display: -webkit-box;
    -webkit-box-orient: vertical;
    -webkit-line-clamp: 2;
    line-clamp: 2;
    overflow: hidden;
  }

  /* Staged attachments — chips above the composer, removable before send. */
  .staged-row {
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
    padding: 6px 10px 0;
    background: var(--surface);
    border-top: 1px solid var(--border);
    flex-shrink: 0;
  }
  .staged-row + .composer { border-top: none; }
  .staged-chip.bad { border-color: var(--bad); color: var(--bad); }
  .staged-chip {
    display: flex;
    align-items: center;
    gap: 6px;
    max-width: 180px;
    padding: 3px 6px;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    background: var(--surface-warm);
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--ink-dim);
  }
  .staged-thumb {
    width: 26px;
    height: 26px;
    object-fit: cover;
    border-radius: 4px;
    flex-shrink: 0;
  }
  .staged-icon { display: flex; color: var(--ink-meta); flex-shrink: 0; }
  .staged-name {
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .staged-x {
    color: var(--ink-meta);
    font-size: calc(13px * var(--tscale-ui));
    line-height: 1;
    padding: 0 2px;
    flex-shrink: 0;
  }
  .staged-x:hover { color: var(--bad); }
</style>
