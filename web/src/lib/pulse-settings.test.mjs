import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  streamPartialsOn,
  reasoningOn,
  codeOn
} from './pulse-settings.js';

test('streamPartialsOn: on by default for every agent tool; an explicit false turns it off', () => {
  assert.equal(streamPartialsOn({ vessel: 'claude', model: 'claude-opus', settings: { stream_partials: true } }), true);
  assert.equal(streamPartialsOn({ vessel: 'pi', model: 'pi-glm53', settings: { stream_partials: false } }), false);
  assert.equal(streamPartialsOn({ vessel: 'claude-code', model: 'claude-opus', settings: { stream_partials: false } }), false);
  assert.equal(streamPartialsOn({ vessel: 'pi', model: 'anything' }), true);
  assert.equal(streamPartialsOn({ vessel: 'antigravity', model: 'anything' }), true);
  assert.equal(streamPartialsOn({ vessel: 'claude-code', model: 'claude-opus' }), true);
  assert.equal(streamPartialsOn({ vessel: 'codex', model: 'gpt', settings: {} }), true);
  assert.equal(streamPartialsOn({ vessel: 'grok', model: 'grok' }), true);
  assert.equal(streamPartialsOn(null), true);
});

test('reasoningOn: defaults to true unless explicitly false', () => {
  assert.equal(reasoningOn(null), true);
  assert.equal(reasoningOn({ settings: {} }), true);
  assert.equal(reasoningOn({ settings: { stream_reasoning: true } }), true);
  assert.equal(reasoningOn({ settings: { stream_reasoning: false } }), false);
});

test('codeOn: defaults to true unless explicitly false', () => {
  assert.equal(codeOn(null), true);
  assert.equal(codeOn({ settings: {} }), true);
  assert.equal(codeOn({ settings: { stream_code: true } }), true);
  assert.equal(codeOn({ settings: { stream_code: false } }), false);
});
