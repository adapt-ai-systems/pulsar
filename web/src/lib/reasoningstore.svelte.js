/**
 * The reasoning panel's open/close state. One panel is mounted at the page level; a pane's "working" status strip and a wall card's
 * "working" label both call openReasoning(). Kept out of the panes so either
 * surface can open it without prop drilling.
 */
export const reasoningPanel = $state({
  /** @type {string|null} */
  pulseId: null,
  /** @type {string} */
  title: ''
});

/**
 * @param {string} pulseId
 * @param {string} [title]
 */
export function openReasoning(pulseId, title = '') {
  reasoningPanel.pulseId = pulseId;
  reasoningPanel.title = title;
}

export function closeReasoning() {
  reasoningPanel.pulseId = null;
  reasoningPanel.title = '';
}