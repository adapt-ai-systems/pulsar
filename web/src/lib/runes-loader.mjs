/**
 * Runes test loader for `.svelte.js` modules, isolated per worktree and process.
 *
 * Compiles `src/lib/<fileName>` (or `<dir>/<fileName>`) with Svelte's
 * `compileModule(src, { generate: 'client', filename })`, rewrites relative
 * imports to absolute paths under the source directory, writes the compiled
 * code to `node_modules/.cache/runes-<sha1(abs src path)>-<pid>-<name>.mjs`
 * (inside `node_modules` so `svelte/internal/client` resolves), imports it,
 * and deletes the temporary build file.
 */
import { readFileSync, writeFileSync, mkdirSync, unlinkSync } from 'node:fs';
import { createHash } from 'node:crypto';
import { fileURLToPath, pathToFileURL } from 'node:url';
import { basename, dirname, join, resolve } from 'node:path';
import { compileModule } from 'svelte/compiler';

const here = dirname(fileURLToPath(import.meta.url));
const cacheDir = join(here, '..', '..', 'node_modules', '.cache');

function compileToCache(absPath, { rewrite, builtPaths, seen }) {
  if (seen.has(absPath)) return seen.get(absPath);
  const baseDir = dirname(absPath);
  const baseName = basename(absPath);
  const src = readFileSync(absPath, 'utf8');
  const out = compileModule(src, { generate: 'client', filename: baseName });
  const sha = createHash('sha1').update(absPath).digest('hex');
  const safeName = baseName.replace(/[^a-zA-Z0-9._-]/g, '_');
  mkdirSync(cacheDir, { recursive: true });
  const built = join(cacheDir, `runes-${sha}-${process.pid}-${safeName}.mjs`);
  seen.set(absPath, built);
  builtPaths.push(built);

  let code = out.js.code.replace(
    /((?:from|import)\s*)(['"])(\.\.?\/[^'"]+)\2/g,
    (_, prefix, _q, rel) => {
      const target = resolve(baseDir, rel);
      const dest = target.endsWith('.svelte.js')
        ? compileToCache(target, { rewrite: null, builtPaths, seen })
        : target;
      return `${prefix}${JSON.stringify(dest)}`;
    }
  );
  if (rewrite) code = rewrite(code, baseDir);
  writeFileSync(built, code);
  return built;
}

/**
 * @param {string} fileName  file name relative to `dir` (default `src/lib`), or absolute path
 * @param {{ dir?: string, rewrite?: (code: string, baseDir: string) => string }} [opts]
 */
export async function loadRuneModule(fileName, { dir = here, rewrite } = {}) {
  const absPath = resolve(dir, fileName);
  const builtPaths = [];
  const seen = new Map();
  const built = compileToCache(absPath, { rewrite, builtPaths, seen });
  try {
    return await import(pathToFileURL(built).href);
  } finally {
    for (const p of builtPaths) {
      try { unlinkSync(p); } catch {}
    }
  }
}
