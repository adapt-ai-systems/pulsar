import { test } from 'node:test';
import assert from 'node:assert/strict';
import { speedMeta, turnMeta, SPEED_SANITY_TPS } from './render.js';

const M = (o) => ({ type: 'metrics', ts: '2026-09-11T00:00:00+0000', ...o });

test('speedMeta: medians over trustworthy calls, sanity flags, ghost drop', () => {
  const evs = [
    { type: 'user_message', ts: '2026-09-11T00:00:00+0000' },
    M({ ttft_s: 1.0, gen_s: 10, call_s: 11, in_tok: 40000, out_tok: 400, out_est: true, out_chars: 1600 }),
    M({ ttft_s: 0.5, gen_s: 5, call_s: 5.5, in_tok: 41000, out_tok: 100, out_est: false, out_chars: 400 }),
    M({ ttft_s: 0.4, gen_s: 4, call_s: 4.4, in_tok: 42000, out_tok: 160, out_est: false, out_chars: 640 }),
  ];
  const s = speedMeta(evs);
  assert.equal(s.n, 3);
  // last call keeps the RAW values (never smoothed)
  assert.equal(s.last.genTps, 40);
  assert.equal(s.last.prefillTps, 105000);
  // median over real-reported calls under the sanity cap: [20, 40] → 30;
  // the estimated call (100 t/s) and any absurd value are excluded
  assert.equal(Math.round(s.med.genTps * 10) / 10, 30);
  assert.equal(s.nReal, 2);
  assert.equal(s.excluded, 1); // the out_est call
  assert.equal(Math.round(s.med.ttftS * 100) / 100, 0.5);
  assert.ok(s.series[0].untrustworthy);
});

test('speedMeta: no ttft → no prefill; no metrics → null; turnMeta carries it', () => {
  const s = speedMeta([M({ ttft_s: null, gen_s: 4, call_s: 4, in_tok: 9000, out_tok: 250, out_chars: 1000 })]);
  assert.equal(s.last.ttftS, null);
  assert.equal(s.last.prefillTps, null);
  assert.equal(s.last.genTps, 62.5);
  assert.equal(speedMeta([]), null);
  assert.equal(turnMeta([]).speed, null);
});

test('speedMeta: meter-split ghosts are dropped, not averaged', () => {
  // the 2026-09-12 capture pattern: a real call followed by a zero-text
  // usage stub (gen≈0) and a fragment — 2.2M "t/s" came from these
  const evs = [
    M({ ttft_s: 0.9, gen_s: 10, call_s: 10.9, in_tok: 44000, out_tok: 500, out_chars: 2000 }),
    M({ ttft_s: null, gen_s: 0.004, call_s: 0.004, in_tok: 44100, out_tok: 505, out_chars: 0 }),
    M({ ttft_s: null, gen_s: 0, call_s: 0, in_tok: null, out_tok: null, out_chars: 0 }),
  ];
  const s = speedMeta(evs);
  assert.equal(s.n, 1);
  assert.equal(s.med.genTps, 50);
  assert.ok(s.med.genTps < SPEED_SANITY_TPS);
});

test('speedMeta: absurd speeds (usage landed on the wrong call) never reach the median', () => {
  const evs = [
    M({ ttft_s: 0.8, gen_s: 20, call_s: 20.8, in_tok: 20000, out_tok: 400, out_chars: 1600 }),   // 20 t/s
    M({ ttft_s: 0.9, gen_s: 0.16, call_s: 1.06, in_tok: 34000, out_tok: 410, out_chars: 1640 }), // 2562 t/s — over the cap
  ];
  const s = speedMeta(evs);
  assert.equal(s.med.genTps, 20);
  assert.equal(s.nReal, 1);
  assert.equal(s.excluded, 1);
});
