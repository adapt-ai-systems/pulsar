// Picker greying for model slots (registry contract).
// Pure-function tests: the live registry may not carry slots on this branch,
// so slot behaviour is exercised with synthetic entries shaped by shapeModel.
import assert from 'node:assert/strict';
import test from 'node:test';

import {
  MODELS, shapeModel, mergeSlotStatus, slotOption, spawnErrorText
} from './models.js';

const SLOT = shapeModel({
  id: 'gpu-ring', label: 'GPU ring (TP4)', vessel: 'pi',
  default_handoff: 190000, slot: true, base_url: 'http://192.0.2.10:8000/v1'
});
const PLAIN = shapeModel({
  id: 'claude-fable-5-1', label: 'Fable 5.1', vessel: 'claude-code',
  default_handoff: 200000, wire_model: 'anthropic/claude-fable-5-1'
});

test('shapeModel tolerates a slot entry with no wire_model', () => {
  assert.equal(SLOT.slot, true);
  assert.equal(SLOT.baseUrl, 'http://192.0.2.10:8000/v1');
  assert.equal(PLAIN.slot, false);
  assert.equal(PLAIN.baseUrl, '');
  // Existing catalog entries keep working and are marked non-slot.
  assert.ok(MODELS.length > 0);
  assert.ok(MODELS.every((m) => m.slot === false || m.slot === true));
  assert.ok(MODELS.every((m) => typeof m.baseUrl === 'string'));
});

test('mergeSlotStatus decorates only slot entries and passes the rest through', () => {
  const runtime = [{
    id: 'gpu-ring', slot: true,
    status: { live: true, served_id: 'glm-5.3-flash', checked_at: '2026-09-22T00:00:00+00:00', error: null },
    label_live: 'GPU ring (TP4): glm-5.3-flash'
  }];
  const merged = mergeSlotStatus([PLAIN, SLOT], runtime);
  assert.equal(merged[0], PLAIN); // untouched object
  assert.equal(merged[1].status.live, true);
  assert.equal(merged[1].label_live, 'GPU ring (TP4): glm-5.3-flash');
  // Missing/garbage runtime lists never throw; the slot just has no status.
  for (const rt of [null, undefined, [], [{ no_id: true }]]) {
    const m = mergeSlotStatus([SLOT], rt);
    assert.equal(m[0].status, null);
    assert.equal(m[0].label_live, null);
  }
});

test('slotOption: live slot is enabled and shows the served id', () => {
  const live = {
    ...SLOT,
    status: { live: true, served_id: 'glm-5.3-flash', checked_at: 'x', error: null },
    label_live: 'GPU ring (TP4): glm-5.3-flash'
  };
  const opt = slotOption(live);
  assert.equal(opt.disabled, false);
  assert.equal(opt.text, 'GPU ring (TP4): glm-5.3-flash');
});

test('slotOption: down slot is disabled, says unavailable, tooltip carries the error', () => {
  const down = {
    ...SLOT,
    status: { live: false, served_id: null, checked_at: 'x', error: 'connect timeout' },
    label_live: 'GPU ring (TP4): unavailable'
  };
  const opt = slotOption(down);
  assert.equal(opt.disabled, true);
  assert.equal(opt.text, 'GPU ring (TP4): unavailable');
  assert.ok(opt.title.includes('connect timeout'));
});

test('slotOption: no probe answer yet greys out with the base-url fallback', () => {
  const pending = { ...SLOT, status: null, label_live: null };
  const opt = slotOption(pending);
  assert.equal(opt.disabled, true);
  assert.equal(opt.text, 'GPU ring (TP4): unavailable');
  assert.equal(opt.title, 'no model served on http://192.0.2.10:8000/v1');
  // Live without a label_live still names the served id.
  const bare = slotOption({ ...SLOT, status: { live: true, served_id: 'qwen38' }, label_live: null });
  assert.equal(bare.disabled, false);
  assert.equal(bare.text, 'GPU ring (TP4): qwen38');
});

test('slotOption: non-slot models are never disabled', () => {
  const opt = slotOption(PLAIN);
  assert.equal(opt.disabled, false);
  assert.equal(opt.text, 'Fable 5.1');
});

test('spawnErrorText: 409 slot_unavailable becomes one plain sentence', () => {
  assert.equal(
    spawnErrorText('slot_unavailable'),
    'That model slot is unavailable right now — pick another model.'
  );
  // The daemon's enveloped message (which embeds the code) reads the same.
  assert.equal(
    spawnErrorText("HTTP 409: slot_unavailable: slot 'gpu-ring' is unavailable").includes('unavailable'),
    true
  );
  assert.ok(spawnErrorText("slot 'gpu-pair' is unavailable").length > 0);
  // Every other error passes through verbatim.
  assert.equal(spawnErrorText('HTTP 400: bad effort'), 'HTTP 400: bad effort');
  assert.equal(spawnErrorText(undefined), '');
});
