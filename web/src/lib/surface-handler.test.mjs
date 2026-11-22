import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  VALID_SURFACES,
  validSurface,
  pulseLinkTarget,
  readDeepLink,
  resolveSurface,
  shouldStripDeepLink,
  readEmbedLink,
  EMBED_THEME_DEFAULT,
} from './surface-handler.js';

test('validSurface accepts the supported surfaces, rejects the rest', () => {
  assert.deepEqual(VALID_SURFACES, ['pulses', 'inbox', 'hub']);
  assert.equal(validSurface('hub'), 'hub');
  assert.equal(validSurface('inbox'), 'inbox');
  assert.equal(validSurface('threads'), null);
  assert.equal(validSurface('pulses'), 'pulses');
  assert.equal(validSurface('settings'), null);
  assert.equal(validSurface(''), null);
  assert.equal(validSurface(null), null);
  assert.equal(validSurface(undefined), null);
});

test('readDeepLink parses surface + item from a raw query string', () => {
  assert.deepEqual(readDeepLink('?surface=inbox&item=abc123'), { surface: 'inbox', item: 'abc123', pulseFocus: null });
  assert.deepEqual(readDeepLink('surface=inbox'), { surface: 'inbox', item: null, pulseFocus: null });
  // unknown surface is nulled; item without surface still parsed
  assert.deepEqual(readDeepLink('?surface=bogus&item=abc'), { surface: null, item: 'abc', pulseFocus: null });
  // accepts a URLSearchParams too
  assert.deepEqual(readDeepLink(new URLSearchParams('surface=inbox')), { surface: 'inbox', item: null, pulseFocus: null });
  // no params
  assert.deepEqual(readDeepLink(''), { surface: null, item: null, pulseFocus: null });
  assert.deepEqual(readDeepLink('?other=1'), { surface: null, item: null, pulseFocus: null });
});

test('readDeepLink parses the pulse-focus link a notification tap opens', () => {
  assert.deepEqual(readDeepLink('?pulse-focus=abc'), { surface: null, item: null, pulseFocus: 'abc' });
  assert.deepEqual(readDeepLink('?pulse-focus=%20abc%20'), { surface: null, item: null, pulseFocus: 'abc' });
  assert.deepEqual(readDeepLink('?pulse-focus='), { surface: null, item: null, pulseFocus: null });
  assert.deepEqual(readDeepLink('?pulse-focus=%20%20'), { surface: null, item: null, pulseFocus: null });
});

test('shouldStripDeepLink: true for a surface link or a pulse-focus link', () => {
  assert.equal(shouldStripDeepLink('?surface=inbox&item=x'), true);
  assert.equal(shouldStripDeepLink('?pulse-focus=abc'), true);
  assert.equal(shouldStripDeepLink('?other=1'), false);
});

test('resolveSurface: URL deep link wins over saved surface', () => {
  assert.deepEqual(resolveSurface({ saved: 'threads', url: 'inbox' }), { surface: 'inbox', fromUrlParam: true });
  assert.deepEqual(resolveSurface({ saved: 'pulses', url: 'hub' }), { surface: 'hub', fromUrlParam: true });
});

test('resolveSurface: no URL param → restore saved surface incl. inbox', () => {
  assert.deepEqual(resolveSurface({ saved: 'inbox' }), { surface: 'inbox', fromUrlParam: false });
  assert.deepEqual(resolveSurface({ saved: 'threads' }), { surface: 'pulses', fromUrlParam: false });
  // restore gate accepts 'inbox' and rejects unknown saved surfaces
});

test('resolveSurface: nothing valid → pulses default', () => {
  assert.deepEqual(resolveSurface({}), { surface: 'pulses', fromUrlParam: false });
  assert.deepEqual(resolveSurface({ saved: 'bogus', url: null }), { surface: 'pulses', fromUrlParam: false });
  assert.deepEqual(resolveSurface({ saved: null, url: 'bogus!' }), { surface: 'pulses', fromUrlParam: false });
});

test('shouldStripDeepLink is true only for a valid surface param', () => {
  assert.equal(shouldStripDeepLink('?surface=inbox&item=x'), true);
  assert.equal(shouldStripDeepLink('?surface=threads'), false);
  assert.equal(shouldStripDeepLink('?surface=bogus'), false);
  assert.equal(shouldStripDeepLink(''), false);
  assert.equal(shouldStripDeepLink('?item=1'), false);
});

test('readEmbedLink requires embed=1 and a pulse id', () => {
  const T = EMBED_THEME_DEFAULT;
  assert.deepEqual(readEmbedLink('?pulse=abc&embed=1'), { pulse: 'abc', embed: true, theme: T });
  assert.deepEqual(readEmbedLink('pulse=abc&embed=1'), { pulse: 'abc', embed: true, theme: T });
  assert.deepEqual(readEmbedLink(new URLSearchParams('pulse=abc&embed=1')), { pulse: 'abc', embed: true, theme: T });
  // embed without a pulse is not an embed
  assert.deepEqual(readEmbedLink('?embed=1'), { pulse: null, embed: false, theme: null });
  assert.deepEqual(readEmbedLink('?pulse=abc'), { pulse: 'abc', embed: false, theme: null });
  assert.deepEqual(readEmbedLink('?pulse=abc&embed=true'), { pulse: 'abc', embed: false, theme: null });
  assert.deepEqual(readEmbedLink(''), { pulse: null, embed: false, theme: null });
  // whitespace-only id
  assert.deepEqual(readEmbedLink('?pulse=%20&embed=1'), { pulse: null, embed: false, theme: null });
});

test('readEmbedLink leaves theme unset by default and honors &theme= when provided', () => {
  assert.equal(EMBED_THEME_DEFAULT, null);
  assert.equal(readEmbedLink('?pulse=abc&embed=1&theme=Black%20Chat').theme, 'Black Chat');
  // blank override → default (null); no embed → no theme
  assert.equal(readEmbedLink('?pulse=abc&embed=1&theme=%20').theme, null);
  assert.equal(readEmbedLink('?pulse=abc&theme=Black%20Chat').theme, null);
});

test('pulseLinkTarget: same-origin pulse-focus link → id, else null', () => {
  assert.equal(pulseLinkTarget('/?pulse-focus=abc', 'https://x'), 'abc');
  assert.equal(pulseLinkTarget('https://x/?pulse-focus=abc', 'https://x'), 'abc');
  assert.equal(pulseLinkTarget('https://y/?pulse-focus=abc', 'https://x'), null);
  assert.equal(pulseLinkTarget('/?surface=inbox', 'https://x'), null);
  assert.equal(pulseLinkTarget(null, 'https://x'), null);
});
