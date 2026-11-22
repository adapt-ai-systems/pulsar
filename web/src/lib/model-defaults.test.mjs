import assert from 'node:assert/strict';
import test from 'node:test';

import { DEFAULT_EFFORT, DEFAULT_MODEL_ID, effortsFor } from './models.js';

test('new pulses preselect medium for the default model', () => {
  assert.equal(DEFAULT_EFFORT, 'medium');
  assert.ok(effortsFor(DEFAULT_MODEL_ID).includes(DEFAULT_EFFORT));
});
