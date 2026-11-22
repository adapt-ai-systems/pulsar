import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const app = readFileSync(join(HERE, '../app.css'), 'utf8');
const pane = readFileSync(join(HERE, 'ChatPane.svelte'), 'utf8');
const tabs = readFileSync(join(HERE, 'GridTabs.svelte'), 'utf8');
const wall = readFileSync(join(HERE, 'Wall.svelte'), 'utf8');

test('shared work-glow keyframes animate opacity, not box-shadow', () => {
  assert.match(app, /@keyframes work-glow/);
  assert.match(app, /\.work-glow::after/);
  const block = app.split('@keyframes work-glow')[1].split('}')[0];
  assert.match(block, /opacity/);
  assert.doesNotMatch(block, /box-shadow/);
  assert.match(app, /prefers-reduced-motion:\s*reduce/);
});

test('pane/chip/card no longer animate box-shadow in local keyframes', () => {
  assert.doesNotMatch(pane, /@keyframes paneWorking/);
  assert.doesNotMatch(tabs, /@keyframes chip-working/);
  assert.doesNotMatch(wall, /@keyframes card-working/);
  assert.doesNotMatch(wall, /@keyframes card-flash/);
  assert.match(pane, /class:work-glow=\{status\.thinking && paneInView\}/);
  assert.match(tabs, /class:work-glow=\{anyGridWorking/);
  assert.match(wall, /class:work-glow=\{/);
});
