import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';

// The iOS Home Screen badge is the TOTAL unread count and is set
// from exactly one place. Page-handler scrape, like mobile-restore.test.mjs.
const page = readFileSync(new URL('../routes/+page.svelte', import.meta.url), 'utf8');

test('app badge is set from one place, with the total unread count', () => {
  const sets = page.match(/navigator\.setAppBadge\(([^)]*)\)/g) ?? [];
  assert.deepEqual(sets, ['navigator.setAppBadge(counts.total)']);
  const clears = page.match(/navigator\.clearAppBadge\(\)/g) ?? [];
  assert.equal(clears.length, 1);
});

test('no effect sets the badge from the capped visible count', () => {
  assert.doesNotMatch(page, /setAppBadge\(\s*inboxUnread/);
});
