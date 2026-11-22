/**
 * Pure pulse-reveal planning for +page.svelte.
 *
 * Encodes the per-entry-point rules for bringing a pulse onto the grid/deck:
 * whether to reveal a shelved (watching/paused) pulse, untick "running only",
 * clear the workspace filter to ALL, add the pulse to the drawer's openedIds,
 * and focus the composer after scrolling.
 */

/**
 * @param {Object|null|undefined} target  the pulse row (or null if not in `pulses` yet)
 * @param {Object} opts
 * @param {string} [opts.from='focus']    'drawer' | 'panel' | 'metrics' | 'activity' | 'new-pulse' | 'focus'
 * @param {string|null} [opts.gridId]     explicit grid_id override (e.g. from newly created pulse or drawer row)
 * @param {boolean} [opts.hideStopped=false]
 * @param {Set<string>} [opts.revealedShelvedIds]
 * @param {string|null} [opts.selectedGrid=null]
 * @param {Set<string>|null} [opts.visibleFamily=null]
 * @returns {{
 *   revealShelved: boolean,
 *   untickHideStopped: boolean,
 *   clearGrid: boolean,
 *   addOpened: boolean,
 *   focusComposer: boolean
 * }}
 */
export function revealPlan(
  target,
  {
    from = 'focus',
    gridId,
    hideStopped = false,
    revealedShelvedIds = new Set(),
    selectedGrid = null,
    visibleFamily = null
  } = {}
) {
  const id = target?.id ?? null;
  const isShelved = !!target?.settings?.mode && !(id && revealedShelvedIds.has(id));
  const revealShelved = isShelved;

  const isStopped = !!target && target.state !== 'running';
  const untickSources =
    from === 'drawer' ||
    from === 'panel' ||
    from === 'metrics' ||
    from === 'new-pulse';
  const untickHideStopped = !!(hideStopped && isStopped && untickSources);

  const grid = gridId === undefined ? target?.grid_id : gridId;
  const clearGrid = selectedGrid !== null && !(visibleFamily && visibleFamily.has(grid));

  return {
    revealShelved,
    untickHideStopped,
    clearGrid,
    addOpened: from === 'drawer',
    focusComposer: from === 'drawer'
  };
}
