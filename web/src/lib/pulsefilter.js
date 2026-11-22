/**
 * Grid visibility filter for the home page.
 *
 * ALL view: panes grouped by workspace in tab order so one screen shows
 * every workspace as a colored block; wsColor() gives each pane its outline.
 * Manual/notification opens must not bypass a named workspace's membership.
 * focusPulse switches to ALL when opening a pulse outside the selected tab.
 */
import { groupByWorkspace } from './wscolor.js';

export function visiblePulsesFor({
  selectedGrid,
  visibleFamily,
  renderedGridPulses,
  grids,
  wsHidden,
  openedIds,
  notifiedExtraPulse
}) {
  return selectedGrid === null
    ? groupByWorkspace(
        renderedGridPulses.filter((p) => !wsHidden.ids.includes(p.grid_id) || openedIds.has(p.id)),
        grids
      )
    : visibleFamily.size > 1
      ? groupByWorkspace(renderedGridPulses.filter((p) => visibleFamily.has(p.grid_id)), grids)
      : renderedGridPulses.filter((p) => p.grid_id === selectedGrid);
}
