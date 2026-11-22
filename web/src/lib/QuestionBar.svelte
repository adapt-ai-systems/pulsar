<script>
  /**
   * QuestionBar — the strip ABOVE the watch bar where a pulse's open
   * questions wait for the user so they stay visible.
   * Open by default: each question shows its choices as buttons plus a
   * free-text box. The head minimizes the strip to one thin line and
   * reopens it. Renders nothing when the pulse has no open questions.
   */
  import { questionStore, subscribeQuestions, questionsFor, answer, dismiss, ageLabel } from './questionstore.svelte.js';

  let { pulseId } = $props();

  let min = $state(false);
  let barEl = $state(null);
  // The rail "?" badge asks for this pulse's strip: open it if
  // minimized, bring it into view and put the cursor in the answer box.
  $effect(() => {
    const onAsk = (e) => {
      if (e.detail?.pulseId !== pulseId) return;
      min = false;
      setTimeout(() => {
        barEl?.scrollIntoView({ block: 'nearest', behavior: 'smooth' });
        barEl?.querySelector('.free input')?.focus({ preventScroll: true });
      }, 80);
    };
    window.addEventListener('pulsar:open-question', onAsk);
    return () => window.removeEventListener('pulsar:open-question', onAsk);
  });
  let drafts = $state({});
  let busy = $state({});
  let failed = $state({});
  let tick = $state(Date.now());

  const mine = $derived(questionsFor(questionStore.questions, pulseId));

  $effect(() => subscribeQuestions());
  $effect(() => {
    const t = setInterval(() => (tick = Date.now()), 30000);
    return () => clearInterval(t);
  });

  async function send(q, text) {
    const t = (text ?? '').trim();
    if (!t || busy[q.id]) return;
    busy[q.id] = true;
    failed[q.id] = '';
    const r = await answer(q.id, t);
    busy[q.id] = false;
    if (!r.ok) failed[q.id] = r.error;
    else delete drafts[q.id];
  }
  async function drop(q) {
    if (busy[q.id]) return;
    busy[q.id] = true;
    const r = await dismiss(q.id);
    busy[q.id] = false;
    if (!r.ok) failed[q.id] = r.error;
  }
</script>

{#if mine.length > 0}
  <div class="qbar" class:min bind:this={barEl}>
    <button class="qhead" onclick={() => (min = !min)} aria-expanded={!min}>
      <span class="n">{mine.length}</span>
      <span class="lbl">waiting on you</span>
      {#if min}<span class="peek" title={mine[0].text}>· {mine[0].text}</span>{/if}
      <span class="sp"></span>
      <span class="chev">{min ? '▴ open' : '▾ minimize'}</span>
    </button>
    {#if !min}
      <div class="list">
        {#each mine as q (q.id)}
          <div class="q">
            <div class="meta">
              <span class="age">{ageLabel(q.created_at, tick)}</span>
              <button class="x" onclick={() => drop(q)} disabled={busy[q.id]} title="dismiss without answering">dismiss</button>
            </div>
            <div class="text">{q.text}</div>
            {#if q.context}<div class="ctx">{q.context}</div>{/if}
            {#if q.choices?.length}
              <div class="choices">
                {#each q.choices as c (c)}
                  <button class="choice" class:def={c === q.default_choice} onclick={() => send(q, c)} disabled={busy[q.id]}>{c}</button>
                {/each}
              </div>
            {/if}
            <div class="free">
              <input
                type="text"
                placeholder={q.choices?.length ? 'or answer in your own words…' : 'your answer…'}
                bind:value={drafts[q.id]}
                disabled={busy[q.id]}
                onkeydown={(e) => { if (e.key === 'Enter') send(q, drafts[q.id]); }}
              />
              <button class="go" onclick={() => send(q, drafts[q.id])} disabled={busy[q.id] || !(drafts[q.id] ?? '').trim()}>send</button>
            </div>
            {#if failed[q.id]}<div class="err">could not deliver: {failed[q.id]}</div>{/if}
          </div>
        {/each}
      </div>
    {/if}
  </div>
{/if}

<style>
  .qbar {
    --q: #f2c57a;
    display: flex;
    flex-direction: column;
    /* Never shrinks (a question outranks chat in a squeezed pane); the
       list below caps its own height and scrolls. */
    flex-shrink: 0;
    border-top: 1px solid color-mix(in srgb, var(--q) 55%, var(--border));
    background: color-mix(in srgb, var(--q) 5%, var(--surface));
    font-family: var(--font-mono);
    font-size: calc(10px * var(--tscale-ui));
    color: var(--q);
  }
  .qhead {
    display: flex;
    align-items: center;
    gap: 8px;
    width: 100%;
    padding: 3px 12px;
    text-align: left;
    color: inherit;
    letter-spacing: 0.06em;
    text-transform: uppercase;
    min-width: 0;
  }
  .qhead:hover { background: color-mix(in srgb, var(--q) 10%, var(--surface)); }
  .n { border: 1px solid var(--q); padding: 0 6px; flex-shrink: 0; }
  .lbl { flex-shrink: 0; }
  .peek {
    color: var(--ink);
    text-transform: none;
    letter-spacing: 0;
    flex: 1;
    min-width: 0;
    white-space: nowrap;
    overflow: hidden;
    text-overflow: ellipsis;
  }
  .sp { flex: 1; }
  .chev { color: var(--ink-dim); flex-shrink: 0; }
  .list {
    display: flex;
    flex-direction: column;
    max-height: min(320px, 45vh);
    overflow-y: auto;
  }
  .q {
    padding: 6px 12px 10px;
    border-top: 1px dashed color-mix(in srgb, var(--q) 30%, var(--border));
  }
  .meta { display: flex; align-items: center; gap: 8px; color: var(--ink-meta); margin-bottom: 4px; }
  .x { margin-left: auto; color: var(--ink-meta); border: 1px solid var(--border); padding: 0 6px; }
  .x:hover { color: var(--ink-dim); background: var(--surface-warm); }
  .text {
    color: var(--ink);
    font-family: var(--font-sans);
    font-size: calc(13px * var(--tscale-ui));
    line-height: 1.45;
    margin-bottom: 6px;
    white-space: pre-wrap;
    user-select: text;
    -webkit-user-select: text;
  }
  .ctx { color: var(--ink-dim); font-size: calc(11px * var(--tscale-ui)); margin-bottom: 8px; }
  .choices { display: flex; flex-wrap: wrap; gap: 6px; margin-bottom: 6px; }
  .choice {
    border: 1px solid var(--q);
    color: var(--q);
    font-size: calc(12px * var(--tscale-ui));
    padding: 4px 12px;
    border-radius: 6px;
  }
  .choice.def { background: color-mix(in srgb, var(--q) 18%, transparent); }
  .choice:hover:not(:disabled) { background: var(--q); color: #000; }
  .free { display: flex; gap: 6px; }
  .free input {
    flex: 1;
    min-width: 0;
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--ink);
    font: inherit;
    font-size: calc(12px * var(--tscale-ui));
    padding: 5px 8px;
    border-radius: 6px;
  }
  .free input:focus { outline: none; border-color: var(--q); }
  .go { border: 1px solid var(--border-strong); color: var(--user); padding: 4px 10px; border-radius: 6px; }
  .go:disabled { opacity: 0.4; }
  .err { color: var(--bad); margin-top: 4px; }
  button:disabled { cursor: default; }
</style>
