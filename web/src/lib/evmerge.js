// Merge a /live `delta: true` events push into the pane's loaded window.
//
// The /live socket tracks its OWN byte offset per subscription; the pane's
// HTTP polls (the follow poll right after you send, scrollback) fetch a full
// tail window independently. So a delta can repeat events the pane already
// holds: send → follow poll returns a window ending in your user_message →
// the next /live tick pushes the delta since ITS last offset, which carries
// that same user_message. A blind concat rendered "YOU …" twice on the
// sending browser until the older copy scrolled off the top of the tail
// window. Other watchers never did the follow poll, so never saw it.
//
// Fix: find where the delta's head lines up with the end of the loaded
// window and append only what comes after. Events carry microsecond `ts`, so
// whole-event equality is a safe identity; a delta that does not line up
// anywhere is appended in full (the normal case).
export function mergeDelta(loaded, delta, limit) {
  if (!delta.length) return loaded;
  const key = (ev) => JSON.stringify(ev);
  const head = key(delta[0]);
  let skip = 0;
  // Scan back from the end (any overlap sits at the tail); `ts` is a cheap
  // prefilter before the whole-event comparison.
  for (let j = loaded.length - 1; j >= 0; j--) {
    if (loaded[j]?.ts !== delta[0]?.ts || key(loaded[j]) !== head) continue;
    const n = Math.min(loaded.length - j, delta.length);
    let ok = true;
    for (let k = 1; k < n; k++) {
      if (key(loaded[j + k]) !== key(delta[k])) { ok = false; break; }
    }
    if (ok) { skip = n; break; }
  }
  let evs = skip ? loaded.concat(delta.slice(skip)) : loaded.concat(delta);
  if (evs.length > limit) evs = evs.slice(-limit);
  return evs;
}
