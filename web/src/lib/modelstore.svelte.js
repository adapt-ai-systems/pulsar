/**
 * Reactive bridge between GET /api/models and the pure catalog in models.js.
 * Importing this module registers a Svelte 5 revision signal so any component
 * reading modelLabel(), defaultHandoffFor(), groupedModels(), etc. re-renders
 * when runtime models (added after the web build) arrive from the daemon.
 */
import { getModels } from './api.js';
import { registerCatalogHooks, setRuntimeRegistry } from './models.js';

let rev = $state(0);

registerCatalogHooks({
  onRead: () => {
    void rev;
  },
  onWrite: () => {
    rev += 1;
  }
});

let inflight = null;

/**
 * Fetch the daemon's live model registry and update models.js.
 * Safe to call repeatedly; concurrent calls coalesce unless `force` is true.
 * @param {{ force?: boolean }} [opts]
 */
export async function loadModels({ force = false } = {}) {
  if (inflight && !force) return inflight;
  const run = (async () => {
    const r = await getModels();
    if (r.ok && r.value) setRuntimeRegistry(r.value);
    return r;
  })();
  inflight = run;
  try {
    return await run;
  } finally {
    if (inflight === run) inflight = null;
  }
}
