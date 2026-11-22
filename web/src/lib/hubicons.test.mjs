import { test } from 'node:test';
import assert from 'node:assert/strict';
import { hash32, hubIcon, pickGlyph, monogram, GLYPHS, GLYPH_RULES, TEXTURES } from './hubicons.js';

test('hash is stable and unsigned', () => {
  assert.equal(hash32('abc'), hash32('abc'));
  assert.notEqual(hash32('abc'), hash32('abd'));
  assert.ok(hash32('anything') >= 0);
});

test('every rule names a real glyph', () => {
  for (const r of GLYPH_RULES) assert.ok(GLYPHS[r.glyph], r.glyph);
});

test('glyph comes from slug words, specific topics first', () => {
  assert.equal(pickGlyph('acme-ads-update'), 'megaphone');
  assert.equal(pickGlyph('acme-seo-findings'), 'lens');
  assert.equal(pickGlyph('acme-intro-video'), 'play');
  assert.equal(pickGlyph('northwind-proposal'), 'doc');
  assert.equal(pickGlyph('acme-sops'), 'book');
  // "adapter" must not read as "ad".
  assert.equal(pickGlyph('adapter-xyz'), null);
});

test('slug beats title; title is the fallback', () => {
  assert.equal(pickGlyph('alpha-pilot-site', 'Monthly ads report'), 'flask');
  assert.equal(pickGlyph('zzz-qqq', 'Product Reviews archive'), 'star');
  assert.equal(pickGlyph('zzz-qqq', 'nothing here'), null);
});

test('monogram', () => {
  assert.equal(monogram('alpha-pilot'), 'AP');
  assert.equal(monogram('widget'), 'WI');
  assert.equal(monogram(''), '?');
});

test('hubIcon is deterministic and in range', () => {
  const a = hubIcon('cost-articles-pilot', 'Cost articles pilot');
  assert.deepEqual(a, hubIcon('cost-articles-pilot', 'Cost articles pilot'));
  assert.ok(a.hue1 >= 0 && a.hue1 < 360 && a.hue2 >= 0 && a.hue2 < 360);
  assert.notEqual(a.hue1, a.hue2);
  assert.ok(TEXTURES.includes(a.texture));
  assert.equal(a.glyph, 'dollar');
  assert.match(a.id, /^hg[0-9a-z]+$/);
});

test('similar slugs get different colors', () => {
  const hues = new Set(['acme-ads-plan', 'acme-ads-loop', 'acme-ads-research'].map((s) => hubIcon(s).hue1));
  assert.equal(hues.size, 3);
});
