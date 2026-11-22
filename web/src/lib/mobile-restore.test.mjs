import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const page = readFileSync(new URL('../routes/+page.svelte', import.meta.url), 'utf8');
const deck = readFileSync(new URL('./MobileDeck.svelte', import.meta.url), 'utf8');

test('mobile restore waits for both roster and workspace hierarchy', () => {
  assert.match(page, /navigationReady = \$derived\(pulsesLoaded && gridsLoaded\)/);
  assert.match(page, /ready=\{navigationReady && mobileLocationRestored\}/);
  assert.match(deck, /if \(!ready \|\| restoredFor === gridKey/);
  assert.match(deck, /if \(!ready \|\| restoredFor !== gridKey \|\| focusId\) return/);
});

test('normal launch restores a manually revealed pane; explicit links still win', () => {
  assert.match(page, /if \(embedMode \|\| surfDec.pulseFocus \|\| surfDec.fromUrlParam \|\| surface !== 'pulses'\) return/);
  assert.match(page, /revealPulse\(target.id, \{ gridId: target.grid_id, from: 'drawer' \}\)/);
});

test('the deck sends no daemon request per move (incident diagnostics removed)', () => {
  assert.doesNotMatch(deck, /voiceLog/);
  // The location backup is debounced: one PUT after the deck rests.
  assert.match(deck, /clearTimeout\(remoteTimer\)/);
});
