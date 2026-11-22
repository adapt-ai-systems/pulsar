import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { createCatalog } from './models.js';

const fixtureRegistry = JSON.parse(
  readFileSync(new URL('../../../tests/fixtures/models_fixture.json', import.meta.url), 'utf8')
);
const { defaultEffortFor, effortsFor } = createCatalog(fixtureRegistry);

test('glm flash and qwen flash default low while explicit off remains offered', () => {
  assert.equal(defaultEffortFor('pi-glm-flash'), 'low');
  assert.ok(effortsFor('pi-glm-flash').includes('off'));
  assert.equal(defaultEffortFor('pi-glm-uncensored'), '');
  assert.equal(defaultEffortFor('pi-qwen38-flash'), 'low');
});
