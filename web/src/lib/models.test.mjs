import assert from 'node:assert/strict';
import test from 'node:test';
import { readFileSync } from 'node:fs';
import registry from './models.json' with { type: 'json' };
import {
  MODELS, DEFAULT_MODEL_ID, DEFAULT_EFFORT,
  createCatalog, defaultHandoffFor, effortOnModelChange, effortsFor, groupedModels, modelLabel, vesselFor,
  registerCatalogHooks, setRuntimeRegistry, resetRuntimeRegistry,
} from './models.js';

const fixtureRegistry = JSON.parse(
  readFileSync(new URL('../../../tests/fixtures/models_fixture.json', import.meta.url), 'utf8')
);
const fx = createCatalog(fixtureRegistry);
const fxPickerIds = () => fx.MODELS.map((m) => m.id);

// --- Shipped starter catalog (pulsar/models.json) --------------------------- //

test('shipped starter picker exposes exactly the 10 launch models', () => {
  assert.deepEqual(MODELS.map((m) => m.id), [
    'claude-opus-5-5',
    'claude-sonnet-5-5',
    'claude-fable-5-2',
    'gpt-6.2-astra',
    'gpt-6-sol',
    'gpt-6-luna',
    'agy-gemini-4-neon',
    'agy-gemini-4-carbon',
    'agy-gemini-4-argon',
    'grok-4.7',
  ]);
  assert.equal(DEFAULT_MODEL_ID, 'claude-opus-5-5');
  assert.equal(DEFAULT_EFFORT, 'medium');
  assert.ok(registry.models.every((m) => m.local === false));
  // Empty Pi vessel group is omitted until a local/pi model is added.
  assert.deepEqual(
    groupedModels().map((g) => g.label),
    ['Claude', 'Codex', 'Grok', 'Antigravity']
  );
  assert.equal(vesselFor('claude-fable-5-2'), 'claude-code');
  assert.equal(modelLabel('grok-4.7'), 'Grok 4.7');
});

test('Gemini 4 Neon offers xhigh effort in the Antigravity picker', () => {
  assert.deepEqual(effortsFor('agy-gemini-4-neon'), ['low', 'medium', 'high', 'xhigh']);
  assert.deepEqual(effortsFor('agy-gemini-4-carbon'), ['low', 'medium', 'high']);
  assert.equal(effortOnModelChange('xhigh', 'agy-gemini-4-neon'), 'xhigh');
});

test('D5: NewPulseModal and SettingsModal both use the one helper', () => {
  for (const f of ['NewPulseModal.svelte', 'SettingsModal.svelte']) {
    const src = readFileSync(new URL(`./${f}`, import.meta.url), 'utf8');
    assert.match(src, /effortOnModelChange\(/, f);
    assert.doesNotMatch(src, /vesselFor\(\w+\) === 'qwen'\) \{?\s*(effort|selectedEffort) = ''/, f);
  }
});

// --- Full fixture registry (local, hidden, alias, and effort mechanics) ----- //

test('fixture: DeepSeek V4.1 Flash is hidden but still resolves', () => {
  assert.ok(!fxPickerIds().includes('pi-ds41-flash'));
  assert.equal(fx.modelLabel('pi-ds41-flash'), 'DeepSeek V4.1 Flash (TP4 ring · 1M ctx)');
  assert.equal(fx.vesselFor('pi-ds41-flash'), 'pi');
});

test('fixture: GLM 5.3 Flash rides pi with a 150k handoff', () => {
  assert.equal(fx.vesselFor('pi-glm-flash'), 'pi');
  assert.equal(fx.defaultHandoffFor('pi-glm-flash'), 150000);
  assert.deepEqual(fx.effortsFor('pi-glm-flash'), ['off', 'low', 'high', 'max']);
  assert.ok(!fxPickerIds().includes('pi-glm-52'), 'legacy GLM 5.2 stays hidden');
});

test('fixture: cloud GLM 5.3 is pickable and uses Z.ai wire model', () => {
  const id = 'pi-glm-53';
  assert.ok(fxPickerIds().includes(id));
  assert.ok(fx.groupedModels().some((g) => g.models.some((m) => m.id === id)));
  assert.equal(fx.modelLabel(id), 'GLM 5.3 (Z.ai cloud)');
  assert.equal(fx.vesselFor(id), 'pi');
  assert.equal(fx.defaultHandoffFor(id), 250000);
  const cloud = fixtureRegistry.models.find((m) => m.id === id);
  assert.equal(cloud.wire_model, 'zai/glm-5.3');
  assert.equal(cloud.pi_rpc, true);
  assert.notEqual(cloud.wire_model, fixtureRegistry.models.find((m) => m.id === 'pi-glm53-full').wire_model);
});

test('fixture: retired models are absent from the picker while unknown ids return defaults', () => {
  for (const id of ['claude-sonnet-5', 'claude-haiku-4-5-20251001', 'gpt-5.6-terra',
    'codex-ds4-flash', 'pi-v4-flash', 'dsh-qwen38-flash', 'pi-qwen35-interface',
    'qwen27-codex', 'qwen35-codex', 'grok-4.5']) {
    assert.ok(!fxPickerIds().includes(id), id);
    assert.ok(!fx.groupedModels().some((g) => g.models.some((m) => m.id === id)), id);
  }
  assert.equal(fx.defaultHandoffFor('grok-4.5'), undefined);
  assert.equal(fx.modelLabel('grok-4.5'), 'grok-4.5');
  assert.equal(fx.defaultHandoffFor('gpt-5.6-sol'), 225000);
  assert.equal(fx.defaultHandoffFor('gpt-5.6-luna'), 225000);
  assert.equal(fx.defaultHandoffFor('grok-4.6'), 300000);
});

test('fixture: OMP interface model uses the compact 100k checkpoint window', () => {
  assert.equal(fx.defaultHandoffFor('pi-qwen27-interface'), 100000);
});

test('fixture: Qwen 3.8 Flash rides pi with its effort levels', () => {
  assert.equal(fx.vesselFor('pi-qwen38-flash'), 'pi');
  assert.equal(fx.defaultHandoffFor('pi-qwen38-flash'), 175000);
  assert.deepEqual(fx.effortsFor('pi-qwen38-flash'), ['off', 'low', 'medium', 'xhigh']);
});

test('fixture: hidden legacy claude-fable entries resolve labels and vessels', () => {
  assert.equal(fx.vesselFor('claude-fable-5-1'), 'claude-code');
  assert.equal(fx.defaultHandoffFor('claude-fable-5-1'), 200000);
  assert.ok(!fxPickerIds().includes('claude-fable-5'));
  assert.equal(fx.modelLabel('claude-fable-5'), 'Fable 5');
  assert.equal(fx.vesselFor('claude-fable-5'), 'claude-code');
});

test('fixture: retired local recipes leave pickers without breaking saved ids', () => {
  for (const id of ['pi-qwen38-flash-tp4', 'pi-glm-exl3-uncensored-tp4',
    'pi-glm-exl3-tp4', 'pi-glm-uncensored']) {
    assert.ok(!fxPickerIds().includes(id), id);
    assert.ok(!fx.groupedModels().some((g) => g.models.some((m) => m.id === id)), id);
    assert.equal(fx.vesselFor(id), 'pi');
    assert.notEqual(fx.modelLabel(id), id);
  }
  for (const id of ['pi-glm53-full', 'pi-glm53-flash-exlr8']) {
    assert.ok(fxPickerIds().includes(id), id);
  }
  for (const id of ['pi-glm-flash', 'pi-qwen38-flash', 'pi-qwen27']) {
    assert.ok(!fxPickerIds().includes(id), id);
    assert.equal(fx.vesselFor(id), 'pi');
  }
});

test('fixture: 27B Pi entries expose thinking tiers including off', () => {
  assert.ok(!fxPickerIds().includes('pi-qwen27'));
  assert.equal(fx.vesselFor('pi-qwen27'), 'pi');
  assert.deepEqual(fx.effortsFor('pi-qwen27'), ['off', 'low', 'medium', 'xhigh']);
  assert.ok(!fxPickerIds().includes('pi-qwen27-interface'));
  assert.notEqual(fx.modelLabel('pi-qwen27-interface'), 'pi-qwen27-interface');
});

test('fixture: D5 effort on model change keeps a level the new model offers', () => {
  assert.equal(fx.effortOnModelChange('high', 'gpt-5.6-luna'), 'high');
  assert.equal(fx.effortOnModelChange('xhigh', 'pi-qwen27'), 'xhigh');
  assert.equal(fx.effortOnModelChange('low', 'pi-glm-flash'), 'low');
});

test('fixture: D5 effort on model change falls back to the new model default', () => {
  assert.ok(!fx.effortsFor('pi-glm53-full').includes('medium'));
  assert.equal(fx.effortOnModelChange('medium', 'pi-glm53-full'), 'high');
  assert.ok(!fx.effortsFor('pi-tuned-flash').includes('max'));
  assert.equal(fx.effortOnModelChange('max', 'pi-tuned-flash'), 'low');
  assert.equal(fx.effortOnModelChange('max', 'pi-qwen27'), 'off');
  const noDial = fx.MODELS.find((m) => fx.effortsFor(m.id).length === 0 && !fx.defaultEffortFor(m.id));
  if (noDial) assert.equal(fx.effortOnModelChange('high', noDial.id), '');
});

test('fixture: DeepSeek dial shows 80-70-50-30-none and old names map to numbers', () => {
  assert.deepEqual(fx.effortsFor('pi-alt-ds41'), ['80', '70', '50', '30', 'none']);
  assert.equal(fx.offLevel(fx.effortsFor('pi-alt-ds41')), 'none');
  assert.equal(fx.offLevel(fx.effortsFor('pi-glm-flash')), 'off');
  assert.equal(fx.canonicalEffort('low', 'pi-alt-ds41'), '50');
  assert.equal(fx.effortOnModelChange('low', 'pi-alt-ds41'), '50');
  assert.equal(fx.effortOnModelChange('xhigh', 'pi-alt-ds41'), '80');
  assert.equal(fx.effortOnModelChange('', 'pi-alt-ds41'), 'none');
  assert.equal(fx.canonicalEffort('low', 'pi-glm-flash'), 'low');
});

test('runtime registry update exposes models added after build and notifies hooks on real changes only', () => {
  let reads = 0;
  let writes = 0;
  registerCatalogHooks({ onRead: () => { reads += 1; }, onWrite: () => { writes += 1; } });
  try {
    assert.equal(modelLabel('pi-runtime-strata'), 'pi-runtime-strata');
    assert.equal(defaultHandoffFor('pi-runtime-strata'), undefined);
    assert.ok(reads >= 2);

    const added = {
      ...registry,
      models: [
        ...registry.models,
        {
          id: 'pi-runtime-strata',
          label: 'Strata Runtime',
          vessel: 'pi',
          local: true,
          default_handoff: 200000,
          efforts: ['off', 'low', 'medium', 'xhigh'],
          default_effort: 'low',
        }
      ]
    };
    assert.equal(setRuntimeRegistry(added), true);
    assert.equal(writes, 1);
    assert.equal(modelLabel('pi-runtime-strata'), 'Strata Runtime');
    assert.equal(defaultHandoffFor('pi-runtime-strata'), 200000);
    assert.equal(vesselFor('pi-runtime-strata'), 'pi');
    assert.deepEqual(effortsFor('pi-runtime-strata'), ['off', 'low', 'medium', 'xhigh']);
    assert.ok(groupedModels().some((g) => g.label === 'Pi' && g.models.some((m) => m.id === 'pi-runtime-strata')));

    // Identical payload (even with volatile slot status timestamps) does not re-notify.
    const sameWithStatus = {
      ...added,
      models: added.models.map((m) => ({ ...m, status: { live: true, checked_at: '2026-11-19T00:00:00Z' } }))
    };
    assert.equal(setRuntimeRegistry(sameWithStatus), false);
    assert.equal(writes, 1);
    assert.equal(setRuntimeRegistry(null), false);
  } finally {
    resetRuntimeRegistry();
    registerCatalogHooks({ onRead: null, onWrite: null });
  }
  assert.equal(modelLabel('pi-runtime-strata'), 'pi-runtime-strata');
  assert.equal(defaultHandoffFor('pi-runtime-strata'), undefined);
});
