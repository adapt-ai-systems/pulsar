import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('../routes/+page.svelte', import.meta.url), 'utf8');

test('sidebar mode keeps the compact header on phones as well as desktop', () => {
  assert.match(source, /const railMode = \$derived\(!isMobile \|\| uiPrefs.showMobileSidebar\)/);
  assert.match(source, /class:chips-only=\{railMode\}/);
  assert.doesNotMatch(source, /header-none/);
  assert.match(source, /\.nav\.chips-only\s*\{\s*height: 34px;/);
});

test('sidebar phone header renders workspace navigation rather than an empty strip', () => {
  assert.match(source, /\{#if railMode\}\s*<GridTabs/);
  assert.match(source, /\{#if !embedMode\}\s*<div\s+class="nav"/);
});
