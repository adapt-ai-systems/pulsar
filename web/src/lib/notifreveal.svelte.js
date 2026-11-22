import { listPulses } from './api.js';
import { rosterLoadedEmpty } from './notifpulse.js';
import { pulseLinkTarget } from './surface-handler.js';

/**
 * Deep-link pulse reveal state machine for +page.svelte.
 *
 * Owns the pending target id (`notifiedPulseId`), an archived pulse resolved
 * outside the live roster (`notifiedExtraPulse`), the inbox fallback item id
 * (`notifiedFallbackItem`), the intent counter (`_notifIntent`), the single-
 * flight archived lookup guard (`_notifPulseResolving`), and the document
 * click handler for in-app pulse hot links (`onPulseLinkClick`).
 *
 * @param {Object} opts
 * @param {string|null} [opts.initialPulseId]
 * @param {string|null} [opts.initialFallbackItem]
 * @param {() => { pulses: Array<{id: string, state?: string}>, connected: boolean, errored: boolean }} opts.getRoster
 * @param {(pulseId: string) => void} opts.onReveal
 * @param {(itemId: string|null) => void} opts.onFallback
 * @param {(found: {id: string, state?: string}) => void} [opts.onExtraFound]
 * @param {(opts: { include_archived: boolean }) => Promise<{ ok: boolean, value?: Array<any> }>} [opts.fetchPulses]
 */
export function createNotifReveal({
  initialPulseId = null,
  initialFallbackItem = null,
  getRoster,
  onReveal,
  onFallback,
  onExtraFound,
  fetchPulses = listPulses
}) {
  let notifiedPulseId = $state(initialPulseId);
  let notifiedExtraPulse = $state(null);
  let notifiedFallbackItem = $state(initialFallbackItem);
  let _notifPulseResolving = false;
  let _notifIntent = 0;

  function renderedList() {
    const { pulses = [] } = getRoster() ?? {};
    return notifiedExtraPulse && !pulses.some((p) => p.id === notifiedExtraPulse.id)
      ? [...pulses, notifiedExtraPulse]
      : pulses;
  }

  /**
   * Open the pulse a deep link or hot link named.
   * @param {string|null} pulseId
   * @returns {boolean}
   */
  function revealNotifiedPulse(pulseId) {
    if (!pulseId) return false;
    const target = renderedList().find((p) => p.id === pulseId);
    if (!target) return false;
    onReveal(pulseId);
    notifiedPulseId = null;
    return true;
  }

  /**
   * Pulse hot links in chat ([Title](/?pulse-focus=<id>)): open the pane in
   * place. Modifier clicks keep the new tab.
   */
  function onPulseLinkClick(e) {
    if (e.defaultPrevented || e.button !== 0 || e.metaKey || e.ctrlKey || e.shiftKey) return;
    const a = e.target?.closest?.('a[href]');
    const origin = typeof window !== 'undefined' ? window.location.origin : '';
    const id = a && pulseLinkTarget(a.getAttribute('href'), origin);
    if (!id) return;
    e.preventDefault();
    _notifIntent += 1;
    _notifPulseResolving = false;
    notifiedExtraPulse = null;
    notifiedFallbackItem = null;
    notifiedPulseId = id;
    revealNotifiedPulse(id);
  }

  $effect(() => {
    const id = notifiedPulseId;
    if (!id || _notifPulseResolving) return;
    const { pulses = [], connected = false, errored = false } = getRoster() ?? {};
    if (pulses.length === 0 && !rosterLoadedEmpty({ pulses, connected, errored })) return;
    if (revealNotifiedPulse(id)) return;
    _notifPulseResolving = true;
    const intent = _notifIntent;
    (async () => {
      const r = await fetchPulses({ include_archived: true });
      if (intent !== _notifIntent) {
        _notifPulseResolving = false;
        return;
      }
      const found = (r.ok ? r.value ?? [] : []).find((p) => p.id === id) ?? null;
      if (found) {
        notifiedExtraPulse = found;
        onExtraFound?.(found);
        revealNotifiedPulse(id);
      } else {
        console.log('[pulse-link] target pulse not on the server', id);
        onFallback(notifiedFallbackItem);
      }
      notifiedPulseId = null;
      _notifPulseResolving = false;
    })();
  });

  return {
    get notifiedPulseId() {
      return notifiedPulseId;
    },
    get notifiedExtraPulse() {
      return notifiedExtraPulse;
    },
    get notifiedFallbackItem() {
      return notifiedFallbackItem;
    },
    revealNotifiedPulse,
    onPulseLinkClick
  };
}
