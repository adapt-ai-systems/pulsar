<script>
  // Hub surface: optional published-pages browser inside the main content area.
  // Off until `hub.url` is configured in Settings. With no page open it shows
  // the catalog (search, open, pin to sidebar); with a page open it frames that
  // page through the same-origin /hub-proxy/ route with bottom-right zoom buttons.
  import Icon from './Icon.svelte';
  import HubGlyph from './HubGlyph.svelte';
  import { isHubEnabled } from './appsettings.svelte.js';
  import {
    hub, loadPages, openPage, closePage, togglePin, isPinned, titleFor,
    pageUrl, zoomOf, stepZoom
  } from './hubstore.svelte.js';

  let q = $state('');
  let frameKey = $state(0); // bump to reload the frame

  $effect(() => {
    if (isHubEnabled()) loadPages();
  });

  const shown = $derived.by(() => {
    const needle = q.trim().toLowerCase();
    if (!needle) return hub.pages;
    return hub.pages.filter((p) => p.slug.includes(needle) || p.title.toLowerCase().includes(needle));
  });
  const zoom = $derived(hub.open ? zoomOf(hub.open) : 1);

  function when(mtime) {
    const d = new Date(mtime * 1000);
    return d.toLocaleDateString(undefined, { month: 'short', day: 'numeric', year: 'numeric' });
  }
</script>

<div class="hub">
  {#if !isHubEnabled()}
    <div class="bar">
      <span class="head">HUB</span>
    </div>
    <div class="list">
      <div class="msg">Pages hub is off. Set “Pages hub URL” in Settings to enable it.</div>
    </div>
  {:else if hub.open}
    {@const slug = hub.open}
    <div class="bar">
      <button class="btn" onclick={closePage} title="all hub pages" aria-label="all hub pages">‹ hub</button>
      <HubGlyph {slug} title={titleFor(slug)} size={20} />
      <span class="ttl" title={titleFor(slug)}>{titleFor(slug)}</span>
      <span class="sp"></span>
      <button
        class="btn"
        class:on={isPinned(slug)}
        onclick={() => togglePin(slug)}
        title={isPinned(slug) ? 'remove from sidebar' : 'add to sidebar'}
      >{isPinned(slug) ? '★ pinned' : '☆ pin'}</button>
      <button class="btn" onclick={() => frameKey++} title="reload page" aria-label="reload page">
        <Icon name="reset" size={13} stroke={1.8} />
      </button>
      <a class="btn" href={pageUrl(slug)} target="_blank" rel="noopener" title="open in a new browser tab">↗</a>
    </div>
    <div class="stage">
      {#key `${slug}:${frameKey}`}
        <iframe
          title={titleFor(slug)}
          src={pageUrl(slug)}
          style="width: {100 / zoom}%; height: {100 / zoom}%; transform: scale({zoom});"
        ></iframe>
      {/key}
      <div class="zoom" role="group" aria-label="zoom">
        <button onclick={() => stepZoom(slug, -1)} title="zoom out" aria-label="zoom out">−</button>
        <button class="pct" onclick={() => stepZoom(slug, 0)} title="reset to 100%" aria-label="reset zoom">{Math.round(zoom * 100)}%</button>
        <button onclick={() => stepZoom(slug, 1)} title="zoom in" aria-label="zoom in">+</button>
      </div>
    </div>
  {:else}
    <div class="bar">
      <span class="head">HUB</span>
      <span class="count">{hub.pages.length} pages</span>
      <span class="sp"></span>
      <input type="search" placeholder="search pages…" bind:value={q} aria-label="search hub pages" />
    </div>
    <div class="list">
      {#if hub.pagesError}
        <div class="msg">could not load pages: {hub.pagesError}</div>
      {:else if !hub.pagesLoaded}
        <div class="msg">loading…</div>
      {:else if shown.length === 0}
        <div class="msg">no pages match</div>
      {/if}
      {#each shown as p (p.slug)}
        <div class="row">
          <button class="open" onclick={() => openPage(p.slug)} title={p.title}>
            <HubGlyph slug={p.slug} title={p.title} size={28} />
            <span class="txt">
              <span class="name">{p.title}</span>
              <span class="meta">{when(p.mtime)} · /{p.slug}/</span>
            </span>
          </button>
          <button
            class="pin"
            class:on={isPinned(p.slug)}
            onclick={() => togglePin(p.slug)}
            title={isPinned(p.slug) ? 'remove from sidebar' : 'add to sidebar'}
            aria-label={isPinned(p.slug) ? 'remove from sidebar' : 'add to sidebar'}
            aria-pressed={isPinned(p.slug)}
          >{isPinned(p.slug) ? '★' : '☆'}</button>
        </div>
      {/each}
    </div>
  {/if}
</div>

<style>
  .hub {
    display: flex;
    flex-direction: column;
    height: 100%;
    min-height: 0;
    background: var(--page);
    color: var(--ink);
    font-family: var(--font-mono);
    font-size: calc(12px * var(--tscale-ui));
  }
  .bar {
    display: flex;
    align-items: center;
    gap: 8px;
    flex-shrink: 0;
    padding: 6px 10px;
    border-bottom: 1px solid var(--border);
    min-width: 0;
  }
  .head { color: var(--ink-dim); letter-spacing: 0.08em; }
  .count { color: var(--ink-meta); }
  .sp { flex: 1; }
  .ttl { min-width: 0; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .btn {
    display: inline-flex;
    align-items: center;
    gap: 4px;
    flex-shrink: 0;
    padding: 3px 8px;
    border: 1px solid var(--border);
    border-radius: 6px;
    color: var(--ink-dim);
    text-decoration: none;
    white-space: nowrap;
  }
  .btn:hover { color: var(--ink); background: var(--surface-warm); }
  .btn.on { color: var(--sprite); border-color: color-mix(in srgb, var(--sprite) 50%, var(--border)); }
  input[type='search'] {
    width: min(320px, 50%);
    background: var(--bg);
    border: 1px solid var(--border);
    color: var(--ink);
    font: inherit;
    padding: 4px 8px;
    border-radius: 6px;
  }
  .list { flex: 1; overflow-y: auto; padding: 6px; }
  .msg { padding: 12px; color: var(--ink-meta); }
  .row { display: flex; align-items: center; gap: 4px; border-radius: 8px; }
  .row:hover { background: var(--surface-warm); }
  .open {
    flex: 1;
    min-width: 0;
    display: flex;
    align-items: center;
    gap: 10px;
    padding: 6px 8px;
    text-align: left;
  }
  .txt { display: flex; flex-direction: column; min-width: 0; }
  .name { overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }
  .meta {
    color: var(--ink-meta);
    font-size: calc(10px * var(--tscale-ui));
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
  }
  .pin {
    flex-shrink: 0;
    width: 34px;
    height: 34px;
    border-radius: 8px;
    color: var(--ink-meta);
    font-size: calc(16px * var(--tscale-ui));
  }
  .pin:hover { color: var(--ink); }
  .pin.on { color: var(--sprite); }
  /* The frame is sized 1/zoom and scaled by zoom from the top-left corner,
     so it always fills the stage cleanly across any proxied page content. */
  .stage { position: relative; flex: 1; min-height: 0; overflow: hidden; background: #fff; }
  iframe { display: block; border: 0; transform-origin: 0 0; background: #fff; }
  .zoom {
    position: absolute;
    right: 14px;
    bottom: 14px;
    display: flex;
    align-items: center;
    overflow: hidden;
    border: 1px solid var(--border-strong);
    border-radius: 10px;
    background: var(--surface);
    box-shadow: 4px 4px 18px rgba(0, 0, 0, 0.45);
    z-index: 2;
  }
  .zoom button {
    min-width: 38px;
    height: 36px;
    padding: 0 8px;
    color: var(--ink);
    font-size: calc(16px * var(--tscale-ui));
  }
  .zoom button:hover { background: var(--surface-warm); }
  .zoom .pct {
    min-width: 54px;
    font-size: calc(11px * var(--tscale-ui));
    color: var(--ink-dim);
    border-left: 1px solid var(--border);
    border-right: 1px solid var(--border);
  }
</style>
