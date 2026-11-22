<script>
  import { DAY_KEYS } from './schedule.js';

  /**
   * Wake-schedule segmented control (NONE / ONE-SHOT / RECURRING), matching the
   * mockup's tpl-create / tpl-settings "Wake schedule" field. Purely a form
   * surface — the parent reads back via bound props and builds the API body.
   *
   * Optional (scheduled cross-pulse messages): if `targets` is
   * passed, the field offers a "message another pulse" select that binds
   * `targetId`. When set, the parent puts it on the schedule body as
   * target_pulse_id — at fire time the prompt is delivered to the TARGET as a
   * message from THIS pulse; any reply is the target's own message back.
   * Empty = an ordinary self-wake. The picker only shows once a wake mode is on.
   *
   * @type {{
   *   mode: 'none'|'once'|'daily',
   *   atLocal: string,        // datetime-local value (one-shot)
   *   hhmm: string,           // local "HH:MM" (recurring)
   *   days: string[],         // selected day keys (recurring); empty = every day
   *   targetId?: string,      // optional message target pulse id ('' = self-wake)
   *   targets?: Array<{id:string,title:string}>, // other non-archived pulses
   * }}
   */
  let {
    mode = $bindable('none'),
    atLocal = $bindable(''),
    hhmm = $bindable('09:00'),
    days = $bindable([]),
    targetId = $bindable(''),
    targets = []
  } = $props();

  function toggleDay(key) {
    days = days.includes(key) ? days.filter((d) => d !== key) : [...days, key];
  }
</script>

<div class="seg">
  <button class:on={mode === 'none'} onclick={() => (mode = 'none')}>NONE</button>
  <button class:on={mode === 'once'} onclick={() => (mode = 'once')}>ONE-SHOT</button>
  <button class:on={mode === 'daily'} onclick={() => (mode = 'daily')}>RECURRING</button>
</div>

{#if mode === 'once'}
  <div class="wake-detail">
    <label class="sub-lbl" for="wake-at">wake at (your local time)</label>
    <input id="wake-at" type="datetime-local" bind:value={atLocal} />
  </div>
{:else if mode === 'daily'}
  <div class="wake-detail">
    <label class="sub-lbl" for="wake-hhmm">time (your local time)</label>
    <input id="wake-hhmm" type="time" bind:value={hhmm} />
    <div class="days">
      {#each DAY_KEYS as key (key)}
        <button
          type="button"
          class="day"
          class:on={days.includes(key)}
          onclick={() => toggleDay(key)}
        >{key.charAt(0).toUpperCase() + key.slice(1)}</button>
      {/each}
    </div>
    <p class="days-note">
      {days.length === 0 ? 'every day' : 'selected days only'}
    </p>
  </div>
{/if}

{#if mode !== 'none' && targets.length}
  <!-- Scheduled cross-pulse message: deliver this schedule's prompt
       to another pulse as a message from this one. Empty = ordinary self-wake. -->
  <div class="wake-detail">
    <label class="sub-lbl" for="wake-target">message another pulse (optional)</label>
    <select id="wake-target" class="target-sel" bind:value={targetId}>
      <option value="">self — this pulse wakes itself</option>
      {#each targets as t (t.id)}
        <option value={t.id}>{t.title}</option>
      {/each}
    </select>
  </div>
{/if}

<style>
  .seg { display: flex; gap: 6px; }
  .seg button {
    flex: 1;
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
  .wake-detail { margin-top: 10px; }
  .sub-lbl {
    display: block;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.1em;
    color: var(--ink-meta);
    text-transform: uppercase;
    margin-bottom: 5px;
  }
  .target-sel {
    width: 100%;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
  }
  input[type='datetime-local'],
  input[type='time'] {
    width: 100%;
    background: var(--surface-warm);
    border: 1px solid var(--border-strong);
    border-radius: 9px;
    padding: 8px 10px;
    font-size: calc(13px * var(--tscale-ui));
    color: var(--ink);
    color-scheme: dark;
  }
  .days {
    display: flex;
    gap: 4px;
    margin-top: 10px;
    flex-wrap: wrap;
  }
  .days .day {
    flex: 1;
    min-width: 38px;
    padding: 6px 0;
    border: 1px solid var(--border-strong);
    border-radius: 8px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.06em;
    color: var(--ink-dim);
  }
  .days .day.on {
    border-color: var(--dreamer);
    color: var(--dreamer);
    background: color-mix(in srgb, var(--dreamer) 8%, transparent);
  }
  .days-note {
    margin-top: 6px;
    font-family: var(--font-mono);
    font-size: calc(9px * var(--tscale-ui));
    letter-spacing: 0.08em;
    color: var(--ink-meta);
  }
</style>
