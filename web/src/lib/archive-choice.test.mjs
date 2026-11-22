// The daemon's {error:{code,message}} 409 went through the plain-
// message path, lost its code, and the keep/stop choice never showed — the
// raw JSON instructions did. fetch is stubbed; nothing hits the daemon.
import test from 'node:test';
import assert from 'node:assert/strict';
import { archivePulse, isMonitoringChoice } from './api.js';

test.after(() => { delete globalThis.fetch; });

test('an archive refused for a monitoring choice is recognised', async () => {
  globalThis.fetch = async () => new Response(JSON.stringify({
    ok: false,
    error: { code: 'monitoring_choice_required', message: 'pulse x still watches 0 condition(s); send {"monitoring": "keep"}' }
  }), { status: 409, headers: { 'content-type': 'application/json' } });
  const r = await archivePulse('p1');
  assert.equal(r.ok, false);
  assert.equal(r.code, 'monitoring_choice_required');
  assert.equal(isMonitoringChoice(r), true);
});

test('other failures are not a monitoring choice', () => {
  assert.equal(isMonitoringChoice({ ok: false, error: 'not_owner' }), false);
  assert.equal(isMonitoringChoice({ ok: true }), false);
  assert.equal(isMonitoringChoice({ ok: false, error: 'HTTP 409: monitoring_choice_required' }), true);
});
