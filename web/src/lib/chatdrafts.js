export const DRAFT_PREFIX = 'pulsar.chatDraft.';

// Save in the input event itself, not a debounce/unmount handler: a reload
// must not beat the last keystroke to storage. Shared listeners also keep the
// grid pane and its expanded copy from restoring different drafts.
//
// The daemon settings KV keeps a second copy (key `chatDraft.<pulse id>`).
// localStorage alone can lose drafts on mobile (iOS can evict it, and Safari
// and the home-screen app do not share it). A pane whose local copy is empty
// adopts the daemon copy.
export const REMOTE_PREFIX = 'chatDraft.';
const REMOTE_DELAY_MS = 400;
export function createDraftStore(storage = () => localStorage, remote = null) {
  const values = new Map();
  const fetched = new Set();
  const pending = new Map(); // id -> timer for the debounced daemon write
  function push(id) {
    clearTimeout(pending.get(id));
    pending.delete(id);
    remote?.put(REMOTE_PREFIX + id, values.get(id) ?? '');
  }
  function schedule(id) {
    if (!remote) return;
    clearTimeout(pending.get(id));
    pending.set(id, setTimeout(() => push(id), REMOTE_DELAY_MS));
  }
  function flush() {
    for (const id of [...pending.keys()]) push(id);
  }
  async function pull(id) {
    if (!remote || fetched.has(id)) return;
    fetched.add(id);
    let text;
    try { text = await remote.get(REMOTE_PREFIX + id); } catch { return; }
    if (typeof text !== 'string' || !text) return;
    // Only fill an empty composer: what this device has wins.
    if (read(id) || pending.has(id)) return;
    write(id, text, { remote: false });
  }
  const listeners = new Map();
  const unsaved = new Set();
  function read(id) {
    if (values.has(id)) return values.get(id);
    try {
      const value = storage().getItem(DRAFT_PREFIX + id) ?? '';
      values.set(id, value);
      return value;
    } catch { return ''; }
  }
  function write(id, value, { remote: toRemote = true } = {}) {
    values.set(id, value);
    fetched.add(id); // a local edit beats a daemon copy still in flight
    if (toRemote) schedule(id);
    let saved = true;
    try {
      if (value) storage().setItem(DRAFT_PREFIX + id, value);
      else storage().removeItem(DRAFT_PREFIX + id);
    } catch { saved = false; }
    if (saved) unsaved.delete(id);
    else unsaved.add(id);
    for (const listener of listeners.get(id) ?? []) listener(value, saved);
    return saved;
  }
  function clearSent(id, sent) {
    // A second pane may have started the next message while this send was
    // in flight. An acknowledgement only clears the draft it actually sent.
    if (read(id) !== sent) return;
    write(id, '');
    if (remote) push(id); // a sent message must not come back on reload
  }
  function subscribe(id, listener) {
    if (!listeners.has(id)) listeners.set(id, new Set());
    listeners.get(id).add(listener);
    listener(read(id), !unsaved.has(id));
    pull(id);
    return () => {
      const group = listeners.get(id);
      group?.delete(listener);
      if (!group?.size) listeners.delete(id);
    };
  }
  return { read, write, clearSent, subscribe, flush };
}

function daemonRemote() {
  if (typeof window === 'undefined') return null;
  return {
    async get(key) {
      const { getSetting } = await import('./api.js');
      const r = await getSetting(key);
      return r.ok ? r.value : null;
    },
    async put(key, value) {
      const { putSetting } = await import('./api.js');
      await putSetting(key, value);
    }
  };
}

export const chatDrafts = createDraftStore(undefined, daemonRemote());
if (typeof window !== 'undefined') {
  // Leaving the app (switching apps on the phone) sends pending text now.
  const flush = () => chatDrafts.flush();
  window.addEventListener('pagehide', flush);
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') flush();
  });
}
