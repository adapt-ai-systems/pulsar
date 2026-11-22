import assert from 'node:assert/strict';
import test from 'node:test';
import registry from './models.json' with { type: 'json' };
import {
  anyToolAvailable,
  defaultAvailableModelId,
  groupedModels,
  modelOption,
  resetRuntimeRegistry,
  setRuntimeRegistry,
  toolsLoaded,
  toolStatus
} from './models.js';
import { pulseStatus, runnerErrorLine, stoppedReasonText } from './render.js';

const NO_TOOLS = {
  'claude-code': { available: false, binary: 'claude', path: null, reason: 'claude not found' },
  codex: { available: false, binary: 'codex', path: null, reason: 'codex not found' },
  pi: { available: false, binary: 'pi', path: null, reason: 'pi not found' },
  antigravity: { available: false, binary: 'agy', path: null, reason: 'agy not found' },
  grok: { available: false, binary: 'grok', path: null, reason: 'grok not found' }
};

const PI_ONLY = {
  ...NO_TOOLS,
  pi: { available: true, binary: 'pi', path: '/opt/pi/bin/pi', reason: null }
};

const STRATA = {
  id: 'pi-qwen38-flash',
  label: 'Strata',
  vessel: 'pi',
  local: true,
  default_handoff: 175000,
  efforts: ['off', 'low', 'medium', 'xhigh'],
  default_effort: 'low'
};

test('models.js: zero tools disables every model with reason and reports no available tool', () => {
  try {
    assert.equal(toolsLoaded(), false);
    assert.equal(anyToolAvailable(), true);

    setRuntimeRegistry({ ...registry, tools: NO_TOOLS });
    assert.equal(toolsLoaded(), true);
    assert.equal(anyToolAvailable(), false);
    assert.equal(toolStatus('claude-code')?.reason, 'claude not found');
    assert.equal(defaultAvailableModelId(), '');

    const all = groupedModels().flatMap((g) => g.models);
    assert.ok(all.length > 0);
    for (const m of all) {
      const opt = modelOption(m);
      assert.equal(opt.disabled, true, m.id);
      assert.match(opt.text, /not available \(.+ not found\)/, m.id);
    }
  } finally {
    resetRuntimeRegistry();
  }
});

test('models.js: single installed tool selects its model by default and disables others', () => {
  try {
    setRuntimeRegistry({
      ...registry,
      models: [...registry.models, STRATA],
      tools: PI_ONLY
    });
    assert.equal(toolsLoaded(), true);
    assert.equal(anyToolAvailable(), true);
    assert.equal(defaultAvailableModelId(), 'pi-qwen38-flash');

    const all = groupedModels().flatMap((g) => g.models);
    const strata = all.find((m) => m.id === 'pi-qwen38-flash');
    const opus = all.find((m) => m.id === 'claude-opus-5-5');
    assert.ok(strata && opus);

    const strataOpt = modelOption(strata);
    assert.equal(strataOpt.disabled, false);
    assert.equal(strataOpt.text, 'Strata');

    const opusOpt = modelOption(opus);
    assert.equal(opusOpt.disabled, true);
    assert.equal(opusOpt.text, 'Opus 5.5 — not available (claude not found)');
    assert.equal(opusOpt.title, 'claude not found');
  } finally {
    resetRuntimeRegistry();
  }
});

test('render.js: pulseStatus reports cli_dead immediately; a normal stopped beat is not a failure', () => {
  const dead = pulseStatus(
    {
      socket_state: 'cli_dead',
      reachable: false,
      heartbeat_fresh: true,
      heartbeat: {
        state: 'cli_dead',
        reason: 'vessel failed to start: `claude` binary not found on PATH'
      }
    },
    1000
  );
  assert.equal(dead.phase, 'stopped');
  assert.equal(dead.stalled, true);
  assert.match(dead.evidence ?? '', /binary not found/);

  // Reset, handoff and pause leave a "stopped" beat while the pulse restarts.
  // That is not a failure: no "stopped" strip and no disabled composer.
  const stopped = pulseStatus(
    {
      socket_state: 'stopped',
      reachable: true,
      heartbeat_fresh: true,
      heartbeat: { state: 'stopped', reason: null }
    },
    1000
  );
  assert.notEqual(stopped.phase, 'stopped');
  assert.equal(stopped.stalled, false);
});

test('render.js: stoppedReasonText and runnerErrorLine name the next step for startup errors', () => {
  const raw = 'vessel failed to start: `claude` binary not found on PATH';
  const formatted = stoppedReasonText(raw);
  assert.match(formatted, /binary not found/);
  assert.match(formatted, /Install the tool or pick another model/);

  // Non-startup reasons stay verbatim.
  assert.equal(stoppedReasonText('hit memory cap'), 'hit memory cap');
  assert.equal(stoppedReasonText(''), '');

  const line = runnerErrorLine({ type: 'runner_error', where: 'vessel.start', reason: raw });
  assert.equal(line, formatted);
});
