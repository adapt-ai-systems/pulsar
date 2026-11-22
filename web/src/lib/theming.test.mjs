import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { dirname, join } from 'node:path';

/**
 * Theming contract self-check.
 *
 * Every font-size in the Inbox surface's own web code MUST be written as
 * `calc(Npx * var(--tscale-...))` (UI chrome uses --tscale-ui; item bodies /
 * rendered markdown use --tscale-chat). Zero literal `font-size: 13px` etc.
 *
 * This is a grep-style lint over the .svelte source rather than a runtime
 * check: it fails fast if a raw unscaled size slips in.
 */

const HERE = dirname(fileURLToPath(import.meta.url));
const SRC = join(HERE, 'Inbox.svelte');

const RAW_FONT_SIZE =
  /font-size:\s*(?:[^c]|[0-9.]+(?:px|em|rem|%)|inherit|initial|unset|var\(--[^)]*\)(?!\s*\*))/g;
// A compliant declaration looks like:  font-size: calc(10px * var(--tscale-ui));
// Anything that is not exactly that pattern is flagged.

function nonCompliantFontDeclarations(source) {
  const out = [];
  const re = /font-size:\s*([^;{}]+)/g;
  let m;
  while ((m = re.exec(source))) {
    const value = m[1].trim();
    const compliant =
      /^calc\([0-9.]+px\s*\*\s*var\(--tscale-(?:ui|chat|tool)\)\)$/.test(value);
    if (!compliant) out.push(value);
  }
  return out;
}

test('Inbox.svelte exists and every font-size is --tscale-based', () => {
  const source = readFileSync(SRC, 'utf8');
  const bad = nonCompliantFontDeclarations(source);
  assert.deepEqual(
    bad,
    [],
    `Inbox.svelte has raw/non-scaled font-size declarations: ${JSON.stringify(bad)}`,
  );
});

test('Inbox.svelte uses --tscale-ui for chrome and --tscale-chat for bodies', () => {
  const source = readFileSync(SRC, 'utf8');
  assert.match(source, /--tscale-ui/);
  // The spec calls for message/markdown bodies through --tscale-chat (same as
  // ChatPane). We don't require it in every rule (bodies can ride a shared
  // class), but the component must reference chat scaling somewhere for the
  // rendered markdown (e.g. its .body rule).
  assert.match(source, /--tscale-chat/);
});

test('Inbox.svelte contains no hex/rgb/hsl literal colors', () => {
  const source = readFileSync(SRC, 'utf8');
  // The negative lookahead keeps Svelte directives such as {#each} from
  // masquerading as three-digit hex colors.
  const literals = source.match(
    /#[0-9a-fA-F]{3,8}(?![0-9a-zA-Z_-])|\b(?:rgb|rgba|hsl|hsla)\s*\(/g,
  ) ?? [];
  assert.deepEqual(
    literals,
    [],
    `Inbox.svelte has literal colors instead of theme tokens: ${JSON.stringify(literals)}`,
  );
});
