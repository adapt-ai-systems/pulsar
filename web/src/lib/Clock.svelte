<script>
  // Large digital clock in the header bar or rail footer.
  // Ticks once a second using owner.timezone when configured, falling back to
  // the browser's local time zone.
  import { onMount } from 'svelte';
  import { appSettings, activeTimeZone } from './appsettings.svelte.js';

  /** Shrink to fit a slim header row (desktop rail mode is 34px, not 48px). */
  let { compact = false, stack = false } = $props();
  const tz = $derived(activeTimeZone(appSettings.ownerTimezone));
  const tzLabel = $derived(
    tz || (typeof Intl !== 'undefined' ? Intl.DateTimeFormat().resolvedOptions().timeZone : '') || 'Local time'
  );
  const fmt = $derived(
    new Intl.DateTimeFormat('en-US', {
      ...(tz ? { timeZone: tz } : {}),
      hour: 'numeric',
      minute: '2-digit',
      second: '2-digit',
      hour12: true
    })
  );
  let hm = $state('');
  let sec = $state('');
  let ampm = $state('');
  function tick() {
    const parts = Object.fromEntries(fmt.formatToParts(new Date()).map((p) => [p.type, p.value]));
    hm = `${parts.hour}:${parts.minute}`;
    sec = parts.second;
    ampm = (parts.dayPeriod || '').toUpperCase();
  }
  $effect(() => {
    fmt;
    tick();
  });
  onMount(() => {
    tick();
    const id = setInterval(tick, 1000);
    return () => clearInterval(id);
  });
</script>

<div class="clock" class:compact class:stack title={tzLabel} aria-label={`${tzLabel} ${hm}:${sec} ${ampm}`}>
  <span class="hm">{hm}</span><span class="sec">:{sec}</span><span class="ampm">{ampm}</span>
</div>

<style>
  /* A flex child of .nav, so it shares the row instead of covering it: it sits
     on the nav's baseline, ends at the nav's own bottom border, and gets pushed
     right by the row's margin-left:auto. No height/line-height to keep in sync
     with the nav, and no border-bottom of its own - the nav already has one,
     and two rules 1px apart is what made the clock look detached. */
  .clock {
    flex: 0 0 auto;
    margin-left: auto;
    display: flex;
    align-items: baseline;
    gap: 3px;
    /* .nav already carries 12px of end padding — none of our own, or the
       digits sit off the edge of the bar. */
    padding: 0;
    font-family: var(--font-mono);
    font-variant-numeric: tabular-nums;
    color: var(--ink);
    pointer-events: none;
  }
  .hm { font-size: 18px; font-weight: 700; letter-spacing: 0.04em; }
  .sec { font-size: 12px; color: var(--ink-meta); }
  .ampm { font-size: 10px; color: var(--ink-meta); margin-left: 3px; letter-spacing: 0.08em; }
  .compact .hm { font-size: 14px; }
  .compact .sec { font-size: 10px; }
  .compact .ampm { font-size: 8px; }
  /* Stacked form for the 44px SideRail footer — h:mm on top, AM/PM under,
     no seconds (a ticking second digit is noise at this size). */
  .stack {
    margin-left: 0;
    flex-direction: column;
    align-items: center;
    gap: 0;
    line-height: 1.15;
    padding: 4px 0 2px;
  }
  .stack .hm { font-size: 13px; }
  .stack .sec { display: none; }
  .stack .ampm { margin-left: 0; font-size: 8px; }
  @media (max-width: 699px) {
    .clock { display: none; }
    /* The phone rail (sidebar checkbox on) carries the stacked clock too. */
    .clock.stack { display: flex; }
  }
</style>
