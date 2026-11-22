import { test as t } from 'node:test';
import assert from 'node:assert/strict';
import { tapSplit } from './tapsplit.js';

function fakeTimers() {
  let pending = null;
  return {
    setTimer: (fn) => { pending = fn; return 1; },
    clearTimer: () => { pending = null; },
    flush: () => { const f = pending; pending = null; f?.(); }
  };
}

t('one tap fires single after the wait, not double', () => {
  const ft = fakeTimers(); const calls = [];
  const h = tapSplit(() => calls.push('single'), () => calls.push('double'), ft);
  h();
  assert.deepEqual(calls, []);
  ft.flush();
  assert.deepEqual(calls, ['single']);
});

t('two quick taps fire double only', () => {
  const ft = fakeTimers(); const calls = [];
  const h = tapSplit(() => calls.push('single'), () => calls.push('double'), ft);
  h(); h(); ft.flush();
  assert.deepEqual(calls, ['double']);
});
