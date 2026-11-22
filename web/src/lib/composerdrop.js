/**
 * Composer file drag/drop and pane reorder drag/drop handlers for ChatPane.
 *
 * File drops belong only to the composer; pulse drags bubble through to the
 * pane shell's reorder handler. Disabled file drops are cancelled so the
 * browser does not navigate away to open the file.
 *
 * The pane functions return the new `dragOver` boolean (or `undefined` when
 * unchanged) so the reactive write stays in the component.
 */
import { isPulseDrag } from './dnd.js';

export function composerDragOver(e, { stopped, archived, sending }) {
  if (isPulseDrag(e) || !Array.from(e.dataTransfer?.types ?? []).includes('Files')) return;
  e.preventDefault();
  e.stopPropagation();
  e.dataTransfer.dropEffect = stopped || archived || sending ? 'none' : 'copy';
}

export function composerDrop(e, { stopped, archived, sending }, { stageFile }) {
  if (isPulseDrag(e) || !e.dataTransfer?.files?.length) return;
  e.preventDefault();
  e.stopPropagation();
  if (stopped || archived || sending) return;
  for (const file of Array.from(e.dataTransfer.files)) stageFile(file);
}

export function paneDragOver(e, { canReorder, dragging }) {
  if (!canReorder || dragging || !isPulseDrag(e)) return undefined;
  e.preventDefault(); // required, else the browser refuses the drop
  e.dataTransfer.dropEffect = 'move';
  return true;
}

export function paneDrop(e, { canReorder, pulseId }, { onreorder }) {
  if (!canReorder || !isPulseDrag(e)) return false;
  e.preventDefault();
  const fromId = e.dataTransfer.getData('text/plain');
  if (fromId && fromId !== pulseId) onreorder(fromId, pulseId);
  return false;
}

/** Byte count as the staged chip and the sent-attachment link show it. */
export function fmtSize(n) {
  if (n >= 1048576) return `${(n / 1048576).toFixed(1)}MB`;
  if (n >= 1024) return `${Math.round(n / 1024)}KB`;
  return `${n}B`;
}
