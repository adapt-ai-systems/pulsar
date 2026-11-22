// Ghost-duplicate nets for live-streaming reply and reasoning ghosts in ChatPane
// (2026-09-21 "reply shows up twice"). Review (2026-09-21) refuted the first
// bidirectional-includes version: an older short reply is a PREFIX of any
// re-acked stream, so only the anchored one-directional form below is safe.
//
// KNOWN ACCEPTED EDGE: a stream that
// OPENS by repeating an older full block verbatim (a habit like "Done.", or an
// old reply quoted then continued) matches this anchored predicate. The blink
// RECURS — each events-file growth during that turn can re-retire the ghost
// after the next partial push re-arms it — never permanent, one refresh per
// growth, and only for the rare verbatim opener (a continuation with ONE extra
// token like "Done! " already fails startsWith). The alternatives were worse:
// no content net keeps the permanent duplicate; a last-turn filter cannot
// distinguish this turn's block from an old one without turn markers on the
// wire. Accepted trade.

export function ghostSupersededBy(evs, ghost, type = 'assistant_text') {
  if (!ghost) return false;
  if (type === 'assistant_text') {
    // A window that ENDS in the turn's done has no live stream left — any
    // ghost is a stale partial whose clearing push was lost ("QA▌"
    // sat under the finished reply until a reload; a shorter stale
    // ghost never matches the startsWith test below).
    // Bookkeeping after done (session/usage/metrics) still counts as ended:
    // pi-backed Qwen writes a session event after it, and the ghost stayed.
    for (let i = evs.length - 1; i >= 0; i--) {
      const t = evs[i].type;
      if (t === 'done') return true;
      if (t !== 'session' && t !== 'usage' && t !== 'metrics') break;
    }
  }
  for (let i = evs.length - 1, seen = 0; i >= 0 && seen < 3; i--) {
    const ev = evs[i];
    if (ev.type !== type) continue;
    seen++;
    const text = ev.text ?? '';
    if (text && ghost.startsWith(text)) return true;
  }
  return false;
}
