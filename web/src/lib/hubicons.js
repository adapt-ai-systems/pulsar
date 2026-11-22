/**
 * Generated icons for Hub pages so entries are easy to distinguish at a glance.
 *
 * Pure: a page's slug (and title) decide everything, so the same page gets
 * the same icon on every device and every load. Three layers, each from a
 * different part of the hash, so two pages rarely look alike:
 *   1. a two-hue gradient tile,
 *   2. a texture (stripes, dots, rings, wedge, grid, none),
 *   3. a glyph picked from the page's words (ads → megaphone, seo → lens …),
 *      or a two-letter monogram when no word matches.
 */

/** FNV-1a, 32-bit unsigned. */
export function hash32(s) {
  let h = 0x811c9dc5;
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i);
    h = Math.imul(h, 0x01000193) >>> 0;
  }
  return h >>> 0;
}

// 24×24 stroke paths. Order matters: the first rule whose word appears wins,
// so the specific topics sit above the generic ones (an "ads update" is ads,
// not a report).
export const GLYPH_RULES = [
  { glyph: 'megaphone', words: ['ads', 'ad', 'campaign', 'campaigns', 'pmax', 'facebook', 'meta', 'instagram', 'promo', 'marketing'] },
  { glyph: 'lens', words: ['seo', 'rank', 'ranking', 'keyword', 'keywords', 'serp', 'citations', 'search', 'findings'] },
  { glyph: 'pin', words: ['map', 'maps', 'heatmap', 'heatmaps', 'gbp', 'local', 'geo', 'grid'] },
  { glyph: 'play', words: ['video', 'videos', 'youtube', 'intro', 'shorts', 'reel', 'animation'] },
  { glyph: 'dollar', words: ['finance', 'tax', 'taxes', 'commission', 'invoice', 'cost', 'costs', 'budget', 'revenue', 'pricing', 'price', 'ltv', 'mrr'] },
  { glyph: 'doc', words: ['proposal', 'proposals', 'quote', 'contract', 'brief', 'article', 'articles', 'copy'] },
  { glyph: 'book', words: ['book', 'sop', 'sops', 'guide', 'docs', 'lesson', 'lessons', 'academy', 'playbook', 'wiki', 'process'] },
  { glyph: 'flask', words: ['research', 'experiment', 'lab', 'simulation', 'test', 'tests', 'pilot', 'backtest', 'trial'] },
  { glyph: 'gauge', words: ['dashboard', 'monitor', 'status', 'metrics', 'speed', 'bench', 'benchmark', 'tuning'] },
  { glyph: 'chip', words: ['model', 'models', 'llm', 'ai', 'gpu', 'tokens', 'agent', 'agents'] },
  { glyph: 'star', words: ['review', 'reviews', 'rating', 'ratings', 'testimonials'] },
  { glyph: 'bubble', words: ['chat', 'questions', 'question', 'a2a', 'phone', 'greeting', 'call', 'calls', 'faq'] },
  { glyph: 'mail', words: ['email', 'mail', 'inbox', 'newsletter', 'outreach'] },
  { glyph: 'calendar', words: ['calendar', 'schedule', 'weekly', 'monthly', 'daily', 'timeline'] },
  { glyph: 'check', words: ['audit', 'checklist', 'qa', 'check', 'compliance', 'cleanup'] },
  { glyph: 'compass', words: ['plan', 'plans', 'roadmap', 'strategy', 'ideas', 'options', 'decisions'] },
  { glyph: 'palette', words: ['design', 'designs', 'logo', 'brand', 'icons', 'theme', 'mockup', 'mockups', 'redesign', 'redesigns', 'style'] },
  { glyph: 'heart', words: ['health', 'bloodwork', 'labs', 'wellness', 'fitness'] },
  { glyph: 'chart', words: ['report', 'reports', 'update', 'analytics', 'results', 'stats', 'numbers', 'usage', 'data', 'performance'] },
  { glyph: 'browser', words: ['homepage', 'site', 'website', 'page', 'pages', 'preview', 'landing', 'display', 'home', 'web'] }
];

export const GLYPHS = {
  megaphone: '<path d="M4 10v4h3l8 4V6l-8 4H4Z"/><path d="M18 9.5a3.5 3.5 0 0 1 0 5"/><path d="M7 14l1 5h2.5l-1-4.5"/>',
  lens: '<circle cx="10.5" cy="10.5" r="5.5"/><path d="m15 15 5 5"/>',
  pin: '<path d="M12 21s-6-5.5-6-11a6 6 0 0 1 12 0c0 5.5-6 11-6 11Z"/><circle cx="12" cy="10" r="2.2"/>',
  play: '<path d="M8.5 5.5v13l10-6.5Z"/>',
  dollar: '<path d="M12 3.5v17"/><path d="M16 7.5c-.8-1.3-2.3-2-4-2-2.3 0-4 1.2-4 3s1.7 2.6 4 3.1 4 1.3 4 3.2-1.8 3.2-4 3.2c-1.9 0-3.4-.8-4.2-2.2"/>',
  doc: '<path d="M6 3h8l4 4v14H6Z"/><path d="M14 3v4h4M9 12h6M9 16h6"/>',
  book: '<path d="M3 5.5C5.5 4.5 9 4.5 12 6.5c3-2 6.5-2 9-1v13c-2.5-1-6-1-9 1-3-2-6.5-2-9-1Z"/><path d="M12 6.5v13"/>',
  flask: '<path d="M9.5 3h5M10.5 3v6L5 19a1.5 1.5 0 0 0 1.3 2h11.4a1.5 1.5 0 0 0 1.3-2l-5.5-10V3"/><path d="M7.5 15h9"/>',
  gauge: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="m12 17 4-5"/><circle cx="12" cy="17" r="1"/>',
  chip: '<rect x="6.5" y="6.5" width="11" height="11" rx="1.5"/><rect x="9.5" y="9.5" width="5" height="5"/><path d="M9.5 3v3.5M14.5 3v3.5M9.5 17.5V21M14.5 17.5V21M3 9.5h3.5M3 14.5h3.5M17.5 9.5H21M17.5 14.5H21"/>',
  star: '<path d="m12 3.5 2.6 5.4 5.9.8-4.3 4.1 1 5.8L12 16.8l-5.2 2.8 1-5.8-4.3-4.1 5.9-.8Z"/>',
  bubble: '<path d="M4 5h16v11H10l-4.5 3.5V16H4Z"/><path d="M8 9.5h8M8 12.5h5"/>',
  mail: '<rect x="3" y="5.5" width="18" height="13" rx="1.5"/><path d="m3.5 6.5 8.5 6.5 8.5-6.5"/>',
  calendar: '<rect x="3.5" y="5" width="17" height="15" rx="1.5"/><path d="M3.5 9.5h17M8 3v4M16 3v4"/><path d="M8 13.5h2M14 13.5h2M8 16.5h2"/>',
  check: '<path d="M9 5h11M9 12h11M9 19h11"/><path d="m3 5 1.3 1.3L6.5 4M3 12l1.3 1.3L6.5 11M3 19l1.3 1.3L6.5 18"/>',
  compass: '<circle cx="12" cy="12" r="8.5"/><path d="m15.5 8.5-2 5-5 2 2-5Z"/>',
  palette: '<path d="M12 3.5a8.5 8.5 0 0 0 0 17c1.2 0 1.8-.9 1.4-2-.5-1.3.4-2.5 1.8-2.5H18a2.5 2.5 0 0 0 2.5-2.5C20.5 7.3 16.7 3.5 12 3.5Z"/><circle cx="8" cy="11" r="1"/><circle cx="11" cy="7.5" r="1"/><circle cx="15.5" cy="8.5" r="1"/>',
  heart: '<path d="M12 20s-7.5-4.5-7.5-10A4.3 4.3 0 0 1 12 7.3 4.3 4.3 0 0 1 19.5 10c0 5.5-7.5 10-7.5 10Z"/>',
  chart: '<path d="M4 20h16"/><path d="M7 16.5v-5M12 16.5V6.5M17 16.5v-8"/>',
  browser: '<rect x="3" y="4.5" width="18" height="15" rx="1.5"/><path d="M3 8.5h18"/><circle cx="6" cy="6.5" r=".4"/><circle cx="8" cy="6.5" r=".4"/><path d="M7 12.5h6M7 15.5h10"/>'
};

export const TEXTURES = ['stripes', 'dots', 'rings', 'wedge', 'grid', 'none'];

/** Lower-case words of the slug and title, deduped, slug words first. */
export function pageWords(slug = '', title = '') {
  const raw = `${slug} ${title}`.toLowerCase().split(/[^a-z0-9]+/);
  return [...new Set(raw.filter(Boolean))];
}

function matchGlyph(words) {
  for (const rule of GLYPH_RULES) {
    if (rule.words.some((w) => words.has(w))) return rule.glyph;
  }
  return null;
}

/** The first matching glyph name, or null. The slug (chosen by whoever
 *  published the page) wins over the title (often a long SEO headline). */
export function pickGlyph(slug = '', title = '') {
  return matchGlyph(new Set(pageWords(slug))) ?? matchGlyph(new Set(pageWords('', title)));
}

/** Two letters from the slug's first two words ("alpha-pilot" → "AP"). */
export function monogram(slug = '') {
  const parts = slug.split(/[^A-Za-z0-9]+/).filter(Boolean);
  if (parts.length === 0) return '?';
  if (parts.length === 1) return parts[0].slice(0, 2).toUpperCase();
  return (parts[0][0] + parts[1][0]).toUpperCase();
}

/**
 * Everything HubGlyph needs to draw one page's icon.
 * @param {string} slug
 * @param {string} [title]
 * @returns {{hue1:number, hue2:number, angle:number, texture:string,
 *            glyph:string|null, mono:string, id:string}}
 */
export function hubIcon(slug = '', title = '') {
  const h = hash32(slug || title || '?');
  const hue1 = h % 360;
  // The second hue sits 40–130° away so the gradient always reads as two colors.
  const hue2 = (hue1 + 40 + ((h >>> 9) % 91)) % 360;
  const angle = ((h >>> 16) % 4) * 45;
  const texture = TEXTURES[(h >>> 19) % TEXTURES.length];
  return {
    hue1,
    hue2,
    angle,
    texture,
    glyph: pickGlyph(slug, title),
    mono: monogram(slug || title),
    id: `hg${h.toString(36)}`
  };
}
