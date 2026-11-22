/**
 * Drag-and-drop vocabulary shared by the pulse panes (the drag source) and the
 * workspace chips (the drop target) — "drag a pulse into a workspace".
 *
 * A custom mime type, not just text/plain: during `dragover` the browser exposes
 * only the TYPES on the drag, never the data, so a drop target that wants to
 * highlight itself while the pointer is still moving has nothing else to test.
 * text/plain carries the same id for the pane-to-pane reorder path.
 */
export const PULSE_MIME = 'application/x-pulsar-pulse';
/** A workspace chip being dragged — to reorder it, or onto the bin to delete it. */
export const GRID_MIME = 'application/x-pulsar-grid';

function carries(e, mime) {
  const t = e?.dataTransfer?.types;
  if (!t) return false;
  // `types` is a DOMStringList on older engines — no .includes there.
  return Array.prototype.includes.call(t, mime);
}

/** True when a drag event is carrying one of our pulses. */
export function isPulseDrag(e) {
  return carries(e, PULSE_MIME);
}

/** True when a drag event is carrying a workspace. */
export function isGridDrag(e) {
  return carries(e, GRID_MIME);
}
