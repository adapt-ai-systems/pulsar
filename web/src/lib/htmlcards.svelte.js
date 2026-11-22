/**
 * HTML cards in chat: an optional mode, off by default. When on, a reply's
 * ```html / ```svg code block renders as a live card in a
 * sandboxed iframe (scripts run, but no same-origin: the card cannot reach
 * the app, the session, or the daemon API). Stored in the daemon settings KV
 * so the `chat-html` skill joins the default skills only while the mode is
 * on — pulses never write HTML cards that the chat would show as raw code.
 */
import { getSetting, putSetting } from './api.js';

const KEY = 'chat.html_cards';
const SKILL = 'chat-html';
const LANGS = ['html', 'svg'];

export const htmlCards = $state({ on: false });

let loaded = false;
export async function loadHtmlCards() {
  if (loaded) return;
  loaded = true;
  const r = await getSetting(KEY);
  htmlCards.on = r.ok && r.value === true;
}

/** @param {boolean} on @returns {Promise<string>} '' or an error */
export async function setHtmlCards(on) {
  const r = await putSetting(KEY, !!on);
  if (!r.ok) return r.error || 'save failed';
  htmlCards.on = !!on;
  const d = await getSetting('pulse.default_skills');
  if (!d.ok) return d.error || 'skills read failed';
  const list = Array.isArray(d.value) ? d.value.filter((s) => s !== SKILL) : [];
  if (on) list.push(SKILL);
  const w = await putSetting('pulse.default_skills', list);
  return w.ok ? '' : w.error || 'skills save failed';
}

// Height reporter injected into every card: the iframe sizes to its content.
const SIZER = `<script>(()=>{const s=()=>parent.postMessage({pulsarCardH:document.documentElement.scrollHeight},'*');addEventListener('load',s);new ResizeObserver(s).observe(document.documentElement);})()<\/script>`;
// Theme inheritance: every card gets the live Pulsar theme as CSS variables,
// so a card written with var(--ink) / var(--bg) fits whatever theme is on. Cards re-render when the
// theme changes.
import { VARS } from './theme.js';
function themeCss() {
  const cs = getComputedStyle(document.documentElement);
  const vars = VARS.map((v) => `${v}:${cs.getPropertyValue(v).trim()}`).join(';');
  const font = getComputedStyle(document.body).fontFamily.replace(/</g, '');
  return `<meta charset="utf-8"><style>:root{${vars};--font:${font}}html,body{margin:0;padding:10px;background:var(--bg);color:var(--ink);font:14px/1.5 var(--font)}a{color:var(--dreamer)}table{border-collapse:collapse}th,td{border-bottom:1px solid var(--border);padding:4px 8px;text-align:left}th{color:var(--ink-dim)}</style>`;
}
function fill(frame) { frame.srcdoc = themeCss() + frame._src + SIZER; }
if (typeof window !== 'undefined') {
  new MutationObserver(() => document.querySelectorAll('iframe.html-card').forEach(fill))
    .observe(document.documentElement, { attributes: true, attributeFilter: ['style', 'class', 'data-theme'] });
}

if (typeof window !== 'undefined') {
  window.addEventListener('message', (e) => {
    const h = e.data?.pulsarCardH;
    if (typeof h !== 'number') return;
    for (const f of document.querySelectorAll('iframe.html-card')) {
      if (f.contentWindow === e.source) f.style.height = `${Math.min(Math.max(h, 40), 2000)}px`;
    }
  });
}

/** Svelte action for a rendered .md body: swap html/svg code blocks for cards.
 *  Param {on, text}: text is passed only so a re-rendered body re-runs it. */
export function cardify(node, param) {
  function apply({ on: enabled }) {
    for (const wrap of node.querySelectorAll('.code-wrap')) {
      const code = wrap.querySelector('code');
      const lang = code?.className.replace(/^lang-/, '');
      if (!code || !LANGS.includes(lang)) continue;
      let frame = wrap.querySelector('iframe.html-card');
      if (enabled && !frame) {
        frame = document.createElement('iframe');
        frame.className = 'html-card';
        frame.setAttribute('sandbox', 'allow-scripts');
        frame._src = code.textContent;
        fill(frame);
        wrap.querySelector('pre').before(frame);
        const btn = document.createElement('button');
        btn.type = 'button';
        btn.className = 'src-btn';
        btn.textContent = 'source';
        btn.onclick = () => wrap.classList.toggle('show-src');
        wrap.querySelector('.copy-btn').before(btn);
        wrap.classList.add('carded');
      } else if (!enabled && frame) {
        frame.remove();
        wrap.querySelector('.src-btn')?.remove();
        wrap.classList.remove('carded', 'show-src');
      }
    }
  }
  apply(param);
  return { update: apply };
}
