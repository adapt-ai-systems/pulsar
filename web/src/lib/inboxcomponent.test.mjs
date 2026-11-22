import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const HERE = dirname(fileURLToPath(import.meta.url));
const SOURCE = readFileSync(join(HERE, 'Inbox.svelte'), 'utf8');

test('Inbox swipe wiring keeps final dy and gives vertical scrolling ownership', () => {
  assert.match(SOURCE, /let swipeDy = \$state\(0\)/);
  assert.match(SOURCE, /shouldArchiveOnSwipe\(swipeDx,\s*swipeDy\)/);
  assert.match(
    SOURCE,
    /if \(intent === 'vertical'\)[\s\S]{0,240}_swipeStart\.cancelled = true/,
  );
});

test('Inbox read-all archives explicit rendered targets, never an open-ended cutoff', () => {
  assert.match(SOURCE, /renderedReadAllTargets\(items,/);
  assert.doesNotMatch(SOURCE, /readAllInbox/);
});

test('Inbox detail uses an open-time thread snapshot for response actions', () => {
  assert.match(SOURCE, /threadSnapshot = detailThreadMembers\(\[\], items, target\)/);
  assert.match(SOURCE, /detailThreadMembers\(threadSnapshot, items, selectedItem\)/);
  assert.match(SOURCE, /flaggedResponseItems\(thread, (?:item|target)\)/);
});

test('Inbox opening an item marks ONLY that item read (2026-09-13)', () => {
  assert.match(SOURCE, /if \(isUnread\(target\)\) archiveItems\(\[target\], 'read'\)/);
  assert.doesNotMatch(
    SOURCE,
    /const affected = target\?\.thread_key\s*\n?\s*\? items\.filter/,
  );
});

test('Inbox deep link falls back to archive for already-read items and mute-until reflects active state', () => {
  assert.match(SOURCE, /setView\('archive'\)/);
  assert.match(SOURCE, /isMuteUntilActive\(ch\?\.mute_until,\s*now\)/);
  assert.match(SOURCE, /inset:\s*calc\(var\(--nav-h,\s*48px\)\s*\+\s*1px\)\s*0\s*0\s*0/);
});
