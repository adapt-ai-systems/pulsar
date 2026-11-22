/**
 * "Done but not seen": a pulse that finishes a turn while nobody is looking
 * stays marked until it is looked at — opened from a list, focused, or on
 * screen in the selected workspace.
 * Per device, kept in localStorage so a reload doesn't lose the marks.
 */
const KEY = 'pulsar.doneUnseen';

function load() {
  try {
    const raw = localStorage.getItem(KEY);
    const arr = raw ? JSON.parse(raw) : [];
    return new Set(Array.isArray(arr) ? arr : []);
  } catch {
    return new Set();
  }
}

export const seenStore = $state({ done: typeof localStorage === 'undefined' ? new Set() : load() });

function save() {
  try {
    localStorage.setItem(KEY, JSON.stringify([...seenStore.done]));
  } catch {
    /* storage may be unavailable; the marks just don't survive a reload */
  }
}

let prevBusy = new Map(); // pulse id -> was busy on the last roster
// A pulse must read not-busy on IDLE_SAMPLES rosters in a row before it counts
// as done, since the busy flag can dip briefly between a turn's model calls.
const IDLE_SAMPLES = 2;
let idleRun = new Map(); // pulse id -> consecutive not-busy rosters since it was busy

/**
 * Feed every roster refresh. A busy→not-busy crossing on a pulse that is NOT
 * currently on screen marks it done; a pulse on screen is being looked at.
 * `onDone` (optional) hears every confirmed turn end, on screen or not.
 * @param {any[]} pulses
 * @param {(p:any)=>boolean} onScreen
 * @param {(p:any)=>void} [onDone]
 */
export function trackDone(pulses, onScreen, onDone) {
  const next = new Map();
  let changed = false;
  for (const p of pulses) {
    const busy = !!(p.live && (p.live.socket_state === 'busy' || p.live.heartbeat?.state === 'busy'));
    next.set(p.id, busy);
    if (busy) {
      idleRun.delete(p.id);
      if (seenStore.done.has(p.id)) {
        seenStore.done.delete(p.id); // working again — the old "done" is stale
        changed = true;
      }
    } else if (prevBusy.get(p.id) === true || idleRun.has(p.id)) {
      const n = (idleRun.get(p.id) ?? 0) + 1;
      if (n < IDLE_SAMPLES) {
        idleRun.set(p.id, n);
      } else {
        idleRun.delete(p.id);
        if (p.state === 'running') onDone?.(p);
        if (!onScreen(p) && p.state === 'running') {
          seenStore.done.add(p.id);
          changed = true;
        }
      }
    }
  }
  prevBusy = next;
  if (changed) {
    seenStore.done = new Set(seenStore.done);
    save();
  }
}

export function markSeen(id) {
  if (!seenStore.done.has(id)) return;
  seenStore.done.delete(id);
  seenStore.done = new Set(seenStore.done);
  save();
}
