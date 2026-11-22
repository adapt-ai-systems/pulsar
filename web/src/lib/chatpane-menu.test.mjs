import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

const source = readFileSync(new URL('./PaneHead.svelte', import.meta.url), 'utf8');

test('compact panes retain stop/restore and stop keeps its confirmation', () => {
  assert.doesNotMatch(source, /\.inline-actions\s*\{[^}]*display:\s*none/);
  assert.match(source, /aria-label="stop pulse" onclick=\{pauseConfirm.arm\}/);
  assert.match(source, /aria-label="confirm stop" onclick=\{doPause\}/);
  assert.match(source, /aria-label="cancel stop" onclick=\{\(\) => pauseConfirm.disarm\(\)\}/);
  assert.match(source, /aria-label="restore" onclick=\{doRestore\}/);
});

test('menu uses the pane width rather than shrinking around its contents', () => {
  assert.match(source, /width: min\(300px, calc\(100cqw - 24px\)\)/);
  assert.match(source, /\.menu-mode-lbl\s*\{[^}]*flex-basis: 100%/);
  assert.match(source, /\.menu-check\s*\{[^}]*width: 100%/);
  assert.match(source, /\.menu-mode-opt\s*\{ min-width: 44px; min-height: 44px;/);
});

test('stop is a red square without a visible text label', () => {
  assert.match(source, /class="stop-btn"[^>]*><Icon name="pause" size=\{14\} \/><\/button>/);
  assert.match(source, /\.pane-head \.btns \.stop-btn\s*\{[^}]*justify-content: center;[^}]*color: var\(--bad\)/);
});

test('inline rename guards doRename so Escape cancels without saving on blur', () => {
  assert.match(source, /async function doRename\(\)\s*\{\s*if \(!renaming\) return;/);
  assert.match(source, /if \(e\.key === 'Escape'\) \{[^}]*cancelRename\(\)/);
});

test('workspace select in the pane menu styles background and option colors with theme tokens', () => {
  assert.match(source, /\.menu-mode select\s*\{[^}]*background:\s*var\(--surface-warm\)[^}]*color:\s*var\(--ink\)/);
  assert.match(source, /\.menu-mode select option\s*\{[^}]*background:\s*var\(--surface\)[^}]*color:\s*var\(--ink\)/);
});
