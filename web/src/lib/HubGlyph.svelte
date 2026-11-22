<script module>
  let seq = 0;
</script>

<script>
  // One Hub page's generated icon (see hubicons.js): gradient tile, texture,
  // then a topic glyph or a monogram. Same slug → same icon everywhere.
  import { hubIcon, GLYPHS } from './hubicons.js';

  let { slug = '', title = '', size = 24 } = $props();

  const icon = $derived(hubIcon(slug, title));
  // Two copies of one page (rail + catalog) must not share SVG ids.
  const uid = `${++seq}`;
  const gid = $derived(`${icon.id}-${uid}-g`);
  const cid = $derived(`${icon.id}-${uid}-c`);
  const DOTS = [5, 10, 15, 20];
</script>

<svg class="hub-glyph" viewBox="0 0 24 24" width={size} height={size} aria-hidden="true">
  <defs>
    <linearGradient id={gid} gradientTransform="rotate({icon.angle} .5 .5)">
      <stop offset="0" stop-color="hsl({icon.hue1} 72% 54%)" />
      <stop offset="1" stop-color="hsl({icon.hue2} 68% 36%)" />
    </linearGradient>
    <clipPath id={cid}><rect width="24" height="24" rx="6" /></clipPath>
  </defs>
  <g clip-path="url(#{cid})">
    <rect width="24" height="24" fill="url(#{gid})" />
    <g class="tex" fill="none" stroke="#fff">
      {#if icon.texture === 'stripes'}
        {#each [-12, -6, 0, 6, 12, 18] as o (o)}<path d="M{o} 24 {o + 24} 0" stroke-width="2" />{/each}
      {:else if icon.texture === 'dots'}
        {#each DOTS as x (x)}{#each DOTS as y (y)}<circle cx={x} cy={y} r="1" fill="#fff" stroke="none" />{/each}{/each}
      {:else if icon.texture === 'rings'}
        {#each [6, 11, 16, 21] as r (r)}<circle cx="24" cy="24" {r} stroke-width="1.6" />{/each}
      {:else if icon.texture === 'wedge'}
        <path d="M24 6 24 24 6 24Z" fill="#fff" stroke="none" />
      {:else if icon.texture === 'grid'}
        {#each [6, 12, 18] as p (p)}<path d="M{p} 0v24M0 {p}h24" stroke-width=".8" />{/each}
      {/if}
    </g>
  </g>
  {#if icon.glyph}
    <g
      transform="translate(4.6 4.6) scale(.617)"
      fill="none"
      stroke="#fff"
      stroke-width="2.3"
      stroke-linecap="round"
      stroke-linejoin="round"
    >
      <!-- eslint-disable-next-line svelte/no-at-html-tags -->
      {@html GLYPHS[icon.glyph]}
    </g>
  {:else}
    <text x="12" y="12.5" text-anchor="middle" dominant-baseline="middle" class="mono">{icon.mono}</text>
  {/if}
</svg>

<style>
  .hub-glyph { display: block; flex-shrink: 0; }
  .tex { opacity: 0.16; }
  .mono {
    fill: #fff;
    font-family: var(--font-mono, monospace);
    font-size: 9.5px;
    font-weight: 700;
    letter-spacing: -0.02em;
  }
</style>
