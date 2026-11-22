/**
 * Self-contained tests for theme.js (mixHex, message bubbles, palettes, rolls).
 *
 * No test framework is installed in web/, so this is a plain-node assertion
 * script (same pattern as usage.test.mjs / schedule.test.mjs):
 *
 *     node src/lib/theme.test.mjs
 *
 * The contract under test: an un-customized profile derives its message and
 * peer colors from the palette, and a pinned swatch must survive a palette
 * change.
 */

import assert from 'node:assert/strict';
import {
  mixHex, colorsFromPalette, PALETTES, builtinProfile,
  peerDefault, peerColor,
  MSG_VARS, MSG_LABELS, msgDefaults, msgOverrides, msgColors,
  MSG_NUMS, emptyMsgNums, clampMsgNum, normalizeMsgNums, msgShownOf,
  LIGHT_NAMES, DEFAULT_DARK, DEFAULT_LIGHT, VARS, hslHex, randomProfile, rollName,
  rollTagline,
  contrastRatio,
  BGS, bgUrl, bgThumb
} from './theme.js';
import { existsSync } from 'node:fs';

// --- mixHex ------------------------------------------------------------------
assert.equal(mixHex('#000000', '#ffffff', 0.5), '#808080');
assert.equal(mixHex('#ffffff', '#000000', 1), '#ffffff');
assert.equal(mixHex('#ffffff', '#000000', 0), '#000000');
// Clamps out-of-range percentages rather than producing impossible channels.
assert.equal(mixHex('#ffffff', '#000000', 5), '#ffffff');
assert.equal(mixHex('#ffffff', '#000000', -2), '#000000');
// Garbage in one slot falls back to the first color instead of throwing.
assert.equal(mixHex('#ff5ed2', 'not-a-color', 0.7), '#ff5ed2');
assert.equal(mixHex(null, '#000000', 0.5), '#000000');

const cyan = colorsFromPalette(PALETTES.find((p) => p[0] === DEFAULT_DARK));

// --- message bubbles ---------------------------------------------------------
// Contract: an un-customized profile washes chat rows exactly as the old
// hard-coded ChatPane CSS did — voice colors at 7/6/8 percent.
assert.equal(MSG_VARS.length, MSG_LABELS.length);
{
  const d = msgDefaults(cyan);
  assert.equal(d['--bub-you'], cyan['--user']);
  assert.equal(d['--bub-reply'], cyan['--dreamer']);
  assert.equal(d['--bub-peer'], peerDefault(cyan), 'peer bubble follows the peer color');
  // reply 6 -> 10 (2026-09-14 reply-first emphasis; app.css --bub-reply-fill tracks it)
assert.deepEqual(emptyMsgNums(), { you: 7, reply: 10, peer: 8 });
}

// A pinned peer color flows through to the unpinned peer bubble.
assert.equal(msgColors({ ...cyan, '--peer': '#abcdef' })['--bub-peer'], '#abcdef');
assert.equal(peerColor({ ...cyan, '--peer': '#abcdef' }), '#abcdef');

// A pinned bubble survives a palette swap; the rest re-derives.
{
  const light2 = colorsFromPalette(PALETTES.find((p) => p[0] === DEFAULT_LIGHT));
  const swapped2 = msgColors({ ...light2, '--bub-you': '#123456' });
  assert.equal(swapped2['--bub-you'], '#123456');
  assert.equal(swapped2['--bub-reply'], light2['--dreamer']);
  assert.deepEqual(msgOverrides(light2), {}, 'a plain palette pins no bubbles');
}

// The reply accent is never pinnable: the reply's left rule mixes from
// --bub-reply, so it must keep the theme's voice color (2026-09-14 verifier
// caught a pinned ink making the rule pale gray on gray).
{
  const gray = { ...colorsFromPalette(PALETTES[0]), '--bub-reply': '#e2e2e2', '--dreamer': '#00ff88' };
  assert.equal(msgColors(gray)['--bub-reply'], '#00ff88');
  assert.equal(msgColors(gray)['--bub-you'], gray['--user'], 'other bubbles stay pinnable');
}

// The appearance controls never offer a reply COLOR swatch (the accent is
// derived), but a legacy profile's pin must still apply to the wash.
{
  const gray = { ...colorsFromPalette(PALETTES[0]), '--bub-reply': '#123456' };
  assert.deepEqual(msgOverrides(gray), {}, 'a reply pin is not surfaced as a pin');
  assert.equal(msgShownOf(gray)['--bub-reply'], undefined, 'no stale reply swatch to show');
  assert.equal(msgShownOf(gray)['--bub-you'], gray['--user'], 'the other swatches still show');
}

// Opacities clamp to 0..100; garbage/absent falls back; legacy profiles with
// no msg block read as the defaults, i.e. the look they already had.
assert.equal(clampMsgNum('you', 250), 100);
assert.equal(clampMsgNum('you', -5), 0);
assert.equal(clampMsgNum('reply', 'nonsense'), 10);
assert.deepEqual(normalizeMsgNums(undefined), emptyMsgNums());
assert.deepEqual(normalizeMsgNums({ peer: 40 }), { you: 7, reply: 10, peer: 40 });
for (const n of MSG_NUMS) {
  assert.ok(n.min < n.max && n.fallback >= n.min && n.fallback <= n.max, `${n.key} range`);
  assert.ok(n.label && n.var && n.unit, `${n.key} is displayable`);
}

// --- built-in palette roster -------------------------------------------------
// 13 curated palettes: 7 dark + 6 light, every cell a hex color, defaults real.
assert.equal(PALETTES.length, 13);
assert.equal(LIGHT_NAMES.length, 6);
for (const row of PALETTES) {
  assert.equal(row.length, 16, `${row[0]} has a name + 15 tokens`);
  for (const c of row.slice(1)) assert.match(c, /^#[0-9a-f]{6}$/i, `${row[0]} cell`);
}
const names = PALETTES.map((r) => r[0]);
assert.equal(new Set(names).size, 13, 'palette names are unique');
for (const n of LIGHT_NAMES) assert.ok(names.includes(n), `${n} exists`);
assert.equal(DEFAULT_DARK, 'Adapt Dark');
assert.equal(DEFAULT_LIGHT, 'Adapt Light');
assert.ok(names.includes(DEFAULT_DARK) && !LIGHT_NAMES.includes(DEFAULT_DARK));
assert.ok(LIGHT_NAMES.includes(DEFAULT_LIGHT));
for (const defName of [DEFAULT_DARK, DEFAULT_LIGHT]) {
  const c = builtinProfile(defName).colors;
  assert.ok(contrastRatio(c['--ink'], c['--bg']) >= 7, `${defName} ink contrast`);
  assert.ok(contrastRatio(c['--ink-dim'], c['--bg']) >= 4.5, `${defName} ink-dim contrast`);
  assert.ok(contrastRatio(c['--ink-meta'], c['--bg']) >= 4.5, `${defName} ink-meta contrast`);
  for (const acc of ['--dreamer', '--user', '--sprite', '--bad', '--matron', '--warden']) {
    assert.ok(contrastRatio(c[acc], c['--bg']) >= 4.5, `${defName} ${acc} on bg`);
    assert.ok(contrastRatio(c[acc], c['--surface']) >= 4.5, `${defName} ${acc} on surface`);
  }
}

// --- hslHex / randomProfile --------------------------------------------------
assert.equal(hslHex(0, 100, 50), '#ff0000');
assert.equal(hslHex(120, 100, 50), '#00ff00');
assert.equal(hslHex(240, 100, 25), '#000080');
assert.equal(hslHex(0, 0, 100), '#ffffff');

// Deterministic rand → a complete, well-formed profile with every token set.
{
  const seq = [0.1, 0.4, 0.6, 0.2, 0.8, 0.3, 0.5, 0.7, 0.9, 0.25];
  let i = 0;
  const rand = () => seq[i++ % seq.length];
  const p = randomProfile(rand);
  assert.ok(p.name && typeof p.name === 'string');
  for (const v of VARS) assert.match(p.colors[v], /^#[0-9a-f]{6}$/i, `random ${v}`);
  // Names are evocative now, drawn from the mood/hue/setting vocabulary.
  assert.match(p.name, /^[A-Za-z]+ [A-Za-z]+ [A-Za-z]+$/);
  // Every roll also carries a one-line tagline (flavor text).
  assert.ok(p.tagline && typeof p.tagline === 'string' && p.tagline.length > 0);
  // Same sequence rolls the same theme — the generator is pure in `rand`.
  i = 0;
  assert.deepEqual(randomProfile(rand), p);
}

// rollName and rollTagline are deterministic and never crash on any hue.
{
  const rand2 = () => 0.75;
  assert.equal(rollName(120, true, rand2), rollName(120, true, rand2));
  assert.match(rollName(120, true, rand2), /^[A-Za-z]+ [A-Za-z]+ [A-Za-z]+$/);
  assert.equal(rollTagline(rand2), rollTagline(rand2));
  assert.ok(rollTagline(rand2).length > 0);
  for (let h = 0; h < 360; h += 30) {
    assert.match(rollName(h, true, rand2), /^[A-Za-z]+ [A-Za-z]+ [A-Za-z]+$/);
    assert.match(rollName(h, false, rand2), /^[A-Za-z]+ [A-Za-z]+ [A-Za-z]+$/);
  }
}

// The legibility rules hold on EVERY roll, not just lucky ones: text ≥7:1 on
// the background, dim text ≥4.5:1, meta ≥3:1, every accent ≥3:1. 300 seeded
// rolls via a deterministic PRNG (mulberry32) so failures are reproducible.
{
  const mulberry32 = (a) => () => {
    a |= 0; a = (a + 0x6d2b79f5) | 0;
    let t = Math.imul(a ^ (a >>> 15), 1 | a);
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
  };
  for (let seed = 1; seed <= 300; seed++) {
    const p = randomProfile(mulberry32(seed));
    const bg = p.colors['--bg'];
    const cr = (v) => contrastRatio(p.colors[v], bg);
    assert.ok(cr('--ink') >= 7, `seed ${seed}: ink ${cr('--ink')}`);
    assert.ok(cr('--ink-dim') >= 4.5, `seed ${seed}: ink-dim ${cr('--ink-dim')}`);
    assert.ok(cr('--ink-meta') >= 3, `seed ${seed}: ink-meta ${cr('--ink-meta')}`);
    for (const v of ['--dreamer', '--user', '--sprite', '--bad', '--matron', '--warden'])
      assert.ok(cr(v) >= 3, `seed ${seed}: ${v} ${cr(v)}`);
    for (const v of VARS) assert.match(p.colors[v], /^#[0-9a-f]{6}$/i, `seed ${seed}: ${v}`);
  }
}

// contrastRatio sanity: black on white is the WCAG maximum 21:1, self is 1:1.
assert.ok(Math.abs(contrastRatio('#000000', '#ffffff') - 21) < 0.01);
assert.ok(Math.abs(contrastRatio('#808080', '#808080') - 1) < 0.001);

// --- background files ----------------------------------------------------------
// Every built-in background and its Theme menu thumbnail exist in static/;
// the menu shows thumbnails, never the full images (35 MB for all 25).
{
  const staticDir = new URL('../../static', import.meta.url);
  for (const b of BGS) {
    assert.ok(existsSync(new URL('.' + b.url, staticDir + '/')), `missing static${b.url}`);
    const th = bgThumb(b.url);
    assert.match(th, /^\/bg-thumbs\/bg-[a-z0-9-]+\.webp$/);
    assert.ok(existsSync(new URL('.' + th, staticDir + '/')), `missing static${th}`);
  }
  assert.ok(!BGS.some((b) => b.url.endsWith('.png')), 'space set ships as WebP');
  // A profile saved before the WebP switch still paints its picture.
  assert.equal(bgUrl('/bg-amethyst-veil.png'), '/bg-amethyst-veil.webp');
  assert.equal(bgUrl('/bg-void-eclipse.jpg'), '/bg-void-eclipse.jpg');
  assert.equal(bgUrl('/my-own.png'), '/my-own.png');
  assert.equal(bgUrl(null), null);
  assert.equal(bgThumb('/bg-void-eclipse.jpg'), '/bg-thumbs/bg-void-eclipse.webp');
  assert.equal(bgThumb('/bg-amethyst-veil.png'), '/bg-thumbs/bg-amethyst-veil.webp');
  assert.equal(bgThumb('https://example.com/x.png'), 'https://example.com/x.png');
}

// builtinProfile matches case-insensitively and returns null for unknown names.
assert.equal(builtinProfile('Porcelain')?.name, 'Porcelain');
assert.equal(builtinProfile('porcelain')?.name, 'Porcelain');
assert.equal(builtinProfile('  obsidian teal  ')?.name, 'Obsidian Teal');
assert.equal(builtinProfile('Porcelain')?.colors['--bg'], '#f6f8fb');
assert.equal(builtinProfile('unknown-theme'), null);
assert.equal(builtinProfile(''), null);

console.log('theme.test.mjs: all assertions passed');
