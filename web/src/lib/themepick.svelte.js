// Click-to-edit theme mode: while on, clicking any part of the UI opens a color popover for the theme tokens that part is painted with.
export const themePick = $state({ on: false });

const COLOR_PROPS = [
  'background', 'background-color', 'background-image', 'color', 'border',
  'border-color', 'border-top', 'border-right', 'border-bottom', 'border-left',
  'border-top-color', 'border-right-color', 'border-bottom-color', 'border-left-color',
  'border-left-width', 'outline', 'outline-color', 'box-shadow', 'fill', 'stroke',
  'caret-color', 'text-decoration-color', 'accent-color',
  'font-size', 'font', 'font-family', 'line-height', 'width', 'height'
];
const PROP_LABEL = (p) =>
  p.startsWith('background') ? 'Background'
    : p === 'color' ? 'Text'
    : p.startsWith('border') ? 'Border'
    : p.startsWith('outline') ? 'Outline'
    : p === 'box-shadow' ? 'Shadow'
    : p === 'fill' || p === 'stroke' ? 'Icon'
    : p === 'font-family' ? 'Font'
    : p.startsWith('font') || p === 'line-height' ? 'Text size'
    : p === 'width' || p === 'height' ? 'Size' : p;

const VAR_RE = /var\(\s*(--[\w-]+)/g;

function* walkRules(rules) {
  for (const r of rules) {
    if (r.cssRules && !(r instanceof CSSStyleRule)) {
      if (r.media && !window.matchMedia(r.media.mediaText).matches) continue;
      yield* walkRules(r.cssRules);
    } else if (r.selectorText) {
      yield r;
    }
  }
}

/** Every [prop, cssText] pair that styles `el` (stylesheet rules + inline). */
function declsFor(el) {
  const out = [];
  for (const sheet of document.styleSheets) {
    let rules;
    try { rules = sheet.cssRules; } catch { continue; } // cross-origin
    for (const r of walkRules(rules)) {
      let hit = false;
      try { hit = el.matches(r.selectorText); } catch { /* odd selector */ }
      if (!hit) continue;
      for (const p of COLOR_PROPS) {
        const v = r.style.getPropertyValue(p);
        if (v && v.includes('var(')) out.push([p, v]);
      }
    }
  }
  for (const p of COLOR_PROPS) {
    const v = el.style?.getPropertyValue(p);
    if (v && v.includes('var(')) out.push([p, v]);
  }
  return out;
}

function describe(el) {
  const cls = [...el.classList].find((c) => !c.startsWith('svelte-'));
  return el.tagName.toLowerCase() + (cls ? '.' + cls : '');
}

/**
 * The theme tokens painting `el`, nearest element first, deduped by token.
 * Walks up a few ancestors so clicking text inside a bubble also offers the
 * bubble's own background.
 * @returns {Array<{token:string, label:string, where:string}>}
 */
export function tokensAt(el, depth = 6) {
  const seen = new Set();
  const out = [];
  for (let e = el, i = 0; e && e !== document.documentElement && i < depth; e = e.parentElement, i++) {
    for (const [p, v] of declsFor(e)) {
      for (const m of v.matchAll(VAR_RE)) {
        const token = m[1];
        if (seen.has(token)) continue;
        seen.add(token);
        out.push({ token, label: PROP_LABEL(p), where: describe(e) });
      }
    }
  }
  return out;
}

/** Resolve any CSS color string to #rrggbb, or null if it is not a color. */
export function toHex(value) {
  const v = (value || '').trim();
  if (!v) return null;
  if (/^#[0-9a-f]{6}$/i.test(v)) return v.toLowerCase();
  const probe = document.createElement('i');
  probe.style.color = '';
  probe.style.color = v;
  if (!probe.style.color) return null;
  document.body.appendChild(probe);
  const rgb = getComputedStyle(probe).color;
  probe.remove();
  const m = rgb.match(/\d+(\.\d+)?/g);
  if (!m || m.length < 3) return null;
  return '#' + m.slice(0, 3).map((n) => Math.round(+n).toString(16).padStart(2, '0')).join('');
}
