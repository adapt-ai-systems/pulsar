import assert from 'node:assert/strict';
import test from 'node:test';
import { mkdtempSync, writeFileSync, rmSync, existsSync, readdirSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { loadRuneModule } from './runes-loader.mjs';

const here = dirname(fileURLToPath(import.meta.url));
const cacheDir = join(here, '..', '..', 'node_modules', '.cache');

test('two fixture modules with the same file name in two temp directories load as two different values and clean up cache', async (t) => {
  const dirA = mkdtempSync(join(tmpdir(), 'runes-a-'));
  const dirB = mkdtempSync(join(tmpdir(), 'runes-b-'));
  t.after(() => {
    rmSync(dirA, { recursive: true, force: true });
    rmSync(dirB, { recursive: true, force: true });
  });

  writeFileSync(
    join(dirA, 'fixture.svelte.js'),
    `import { label } from './sibling.js';\nexport const box = $state({ v: 'alpha', sibling: label });\n`
  );
  writeFileSync(join(dirA, 'sibling.js'), `export const label = 'from-A';\n`);

  writeFileSync(
    join(dirB, 'fixture.svelte.js'),
    `import { label } from './sibling.js';\nexport const box = $state({ v: 'beta', sibling: label });\n`
  );
  writeFileSync(join(dirB, 'sibling.js'), `export const label = 'from-B';\n`);

  const modA = await loadRuneModule('fixture.svelte.js', { dir: dirA });
  const modB = await loadRuneModule('fixture.svelte.js', { dir: dirB });

  assert.equal(modA.box.v, 'alpha');
  assert.equal(modA.box.sibling, 'from-A');
  assert.equal(modB.box.v, 'beta');
  assert.equal(modB.box.sibling, 'from-B');

  // Verify temporary build files for this pid were deleted after import.
  if (existsSync(cacheDir)) {
    const leftover = readdirSync(cacheDir).filter(
      (f) => f.startsWith('runes-') && f.includes(`-${process.pid}-`)
    );
    assert.deepEqual(leftover, []);
  }
});
