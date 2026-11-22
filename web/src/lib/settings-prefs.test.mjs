import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

// D9: stream / reasoning / HTML-cards checkboxes live in the pane's ⋯ menu
// only. SettingsModal no longer carries a second copy.
const settings = readFileSync(new URL('./SettingsModal.svelte', import.meta.url), 'utf8');
const paneHead = readFileSync(new URL('./PaneHead.svelte', import.meta.url), 'utf8');

test('SettingsModal has no stream / reasoning / HTML-cards checkbox', () => {
  for (const id of ['set-stream', 'set-reasoning', 'set-html-cards']) {
    assert.ok(!settings.includes(`id="${id}"`), id);
  }
  assert.doesNotMatch(settings, /setStreamPartials|setStreamReasoning|setHtmlCards|streamPartialsOn|reasoningOn/);
});

test('the pane ⋯ menu keeps all three', () => {
  assert.match(paneHead, /stream\.set\(/);
  assert.match(paneHead, /reasoning\.set\(/);
  assert.match(paneHead, /setHtmlCards\(/);
});

test('SettingsModal closes on Escape and offers a None backup model option', () => {
  assert.match(settings, /<svelte:document onkeydown=\{[^}]*e\.key === 'Escape'[^}]*onclose\(\)/);
  assert.match(settings, /id="set-backup-model"[\s\S]*?<option value="">None<\/option>/);
  assert.match(settings, /backupModel \? \{ model: backupModel, effort: backupEffort \} : \{ model: null \}/);
});
