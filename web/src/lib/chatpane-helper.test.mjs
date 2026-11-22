import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
const source = readFileSync(new URL('./PaneHead.svelte', import.meta.url), 'utf8');

// A helper pulse (settings.role === 'helper') is distinguished subtly but
// clearly — ring dot + "helper" tag naming the lead on hover.
test('helper pulses get a ring dot and a helper tag keyed off settings.role', () => {
  assert.match(source, /pulse\.settings\?\.role === 'helper'/);
  assert.match(source, /class:helper=\{!!helperLeadId\}/);
  assert.match(source, /class="helper-tag" title=\{`helper of \$\{helperLeadTitle \?\? 'pulse'\}/);
  assert.match(source, /\.dot\.helper \{[^}]*background: transparent;[^}]*border: 1\.5px solid/);
  assert.match(source, /\.dot\.helper\.stalled \{ border-color: var\(--bad\); \}/);
});

test('lead title resolves from the live roster, never a fetch per pane', () => {
  assert.match(source, /live\.roster\?\.find\(\(p\) => p\.id === helperLeadId\)\?\.title/);
});
