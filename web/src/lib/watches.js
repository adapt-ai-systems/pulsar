/**
 * Pure helpers for rendering condition watches — kept out of the
 * component so they can be node-tested without Svelte.
 */

const KIND_TEXT = {
  pulse_state: 'pulse',
  file: 'file',
  log: 'log',
  http: 'http',
  cmd: 'cmd'
};

/**
 * A watch note or label as people read it: no "pulsar-wait:" tool prefix and
 * absolute paths cut to their file name, which the narrow strip and shelf
 * otherwise clip off. The full path stays in the tooltip.
 * @param {string} text
 */
export function plainWatchText(text) {
  return String(text ?? '')
    .replace(/^pulsar-wait:\s*/, '')
    .replace(/(?<![\w:/.~])(?:~|\/)[^\s'"]*\/([^\s/'"]+)/g, '$1');
}

/** Headline: the note if the pulse gave one, else a kind-derived phrase. */
export function watchLabel(w) {
  if (w.note) return plainWatchText(w.note);
  const s = w.spec ?? {};
  switch (w.kind) {
    case 'pulse_state': {
      const parts = [];
      if (s.stopped) parts.push('stops');
      if (s.stale) parts.push('goes silent');
      if (s.max_ctx) parts.push(`ctx ≥ ${Number(s.max_ctx).toLocaleString()}`);
      return `pulse ${String(s.target_id ?? '').slice(0, 8)} ${parts.join(' / ') || 'changes'}`;
    }
    case 'file':
      return `${s.exists === false ? 'vanishes' : 'appears'}: ${plainWatchText(s.path ?? '')}`;
    case 'log':
      return `/${s.pattern ?? ''}/ in ${plainWatchText(s.path ?? '')}`;
    case 'http':
      return `${s.url ?? ''} ${s.invert ? 'comes up' : 'goes down'}`;
    case 'cmd':
      return `${s.cmd ?? ''} ${s.invert ? 'succeeds' : 'fails'}`;
    default:
      return KIND_TEXT[w.kind] ?? w.kind;
  }
}

/** Subtitle: mode · interval · checks · current observation. */
export function watchDetail(w) {
  const bits = [w.mode, `every ${w.interval_s}s`, `${w.checks_done ?? 0} ${(w.checks_done ?? 0) === 1 ? 'check' : 'checks'}`];
  if (w.spec?.invert) bits.push('inverted');
  const d = w.detail ?? {};
  if (w.kind === 'pulse_state' && d.state) {
    bits.push(`${d.state}${d.ctx != null ? ` · ${Number(d.ctx).toLocaleString()} ctx` : ''}`);
  } else if (d.problem) {
    bits.push(d.problem);
  } else if (d.status != null) {
    bits.push(`HTTP ${d.status}`);
  } else if (d.returncode != null) {
    bits.push(`exit ${d.returncode}`);
  }
  if (w.bad) bits.push('MET');
  return bits.join(' · ');
}
