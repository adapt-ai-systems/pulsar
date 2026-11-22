// Diagnostic mobile tap trace: keeps the last 150 [wall]/[deck]/[pane]/[tap]
// lines and saves them to the daemon settings key `debug.mobileTaps`, so the
// iPhone's
// real event order can be read on the server. Remove once that bug is fixed.
import { putSetting } from './api.js';

const KEY = 'debug.mobileTaps';
const MAX = 150;
const HIGHLIGHT_MAX = 100;
const TAGS = /^\[(wall|deck|pane|tap|boot)\]/;

export function startTapLog() {
  if (typeof window === 'undefined' || window.__tapLog) return;
  window.__tapLog = true;
  const lines = [];
  // Keep navigation evidence even if later unrelated taps fill the rolling log.
  const highlights = [];
  let timer;
  const t0 = performance.now();
  const push = (s) => {
    const line = `${Math.round(performance.now() - t0)} ${s}`;
    lines.push(line);
    if (lines.length > MAX) lines.shift();
    if (/^\[(wall|pane)\]/.test(s) || /^\[deck\] (external focus|active)/.test(s)
        || (s.startsWith('[tap]') && s.includes(' card='))) {
      highlights.push(line);
      if (highlights.length > HIGHLIGHT_MAX) highlights.shift();
    }
    clearTimeout(timer);
    timer = setTimeout(() => putSetting(KEY, { at: new Date().toISOString(), ua: navigator.userAgent, lines, highlights }), 1000);
  };
  const fmt = (args) => {
    let i = 1;
    const s = String(args[0]).replace(/%[sdo]/g, () => String(args[i++]));
    return [s, ...args.slice(i).map(String)].join(' ');
  };
  const orig = console.debug.bind(console);
  console.debug = (...args) => {
    orig(...args);
    if (typeof args[0] === 'string' && TAGS.test(args[0])) push(fmt(args));
  };
  const desc = (el) => {
    if (!el?.closest) return String(el?.nodeName ?? el);
    const card = el.closest('[data-card-drop]');
    const c = (el.className && typeof el.className === 'string') ? '.' + el.className.split(' ')[0] : '';
    return `${el.nodeName.toLowerCase()}${c}${card ? ' card=' + card.getAttribute('data-card-drop').slice(0, 8) : ''}`;
  };
  for (const type of ['pointerdown', 'pointerup', 'pointercancel', 'click']) {
    window.addEventListener(type, (e) => {
      push(`[tap] ${type} ${e.pointerType ?? ''} ${Math.round(e.clientX)},${Math.round(e.clientY)} → ${desc(e.target)}`);
    }, true);
  }
  // Bubble-phase click: did the click survive to the bottom (not swallowed)?
  window.addEventListener('click', (e) => push(`[tap] click-end defaultPrevented=${e.defaultPrevented}`));
  push(`[boot] page start ${location.pathname}${location.search}`);
}
