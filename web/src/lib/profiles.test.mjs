import { test } from 'node:test';
import assert from 'node:assert/strict';
import {
  KV_PROFILES, KV_DEFAULT_PROFILE, sanitizeProfiles, sanitizeDefaultProfile,
  profileFromForm, applyProfile, upsertProfile, removeProfile, findProfile,
  sortedProfiles
} from './profiles.js';
import { DEFAULT_MODEL_ID } from './models.js';

const defaults = () => ({
  model: DEFAULT_MODEL_ID,
  effort: '',
  fast: false,
  handoff_tokens: null,
  memory_max: '4G',
  memory: false,
  skills: [],
  cages: [],
  loop: null
});

test('exports the full required surface', () => {
  for (const fn of [sanitizeProfiles, sanitizeDefaultProfile, profileFromForm,
    applyProfile, upsertProfile, removeProfile, findProfile, sortedProfiles]) {
    assert.equal(typeof fn, 'function');
  }
  assert.equal(KV_PROFILES, 'pulse.profiles');
  assert.equal(KV_DEFAULT_PROFILE, 'pulse.default_profile');
});

test('sanitizeDefaultProfile: only a non-blank string survives', () => {
  assert.equal(sanitizeDefaultProfile('Fast Codex'), 'Fast Codex');
  assert.equal(sanitizeDefaultProfile('  Fast Codex  '), 'Fast Codex');
  for (const bad of [null, undefined, 7, true, {}, [], '', '   ']) {
    assert.equal(sanitizeDefaultProfile(bad), '', `raw=${String(bad)}`);
  }
});

test('sanitizeProfiles: non-array and garbage inputs -> []', () => {
  for (const bad of [null, undefined, {}, 'x', 42, true]) {
    assert.deepEqual(sanitizeProfiles(bad), []);
  }
});

test('sanitizeProfiles: drops non-object and nameless entries', () => {
  const out = sanitizeProfiles([
    'string', 42, true, null, undefined, [],
    { model: 'gpt-5.6-luna' },           // no name
    { name: '   ' },                     // whitespace name
    { name: 7 },
    { name: 'ok', model: 'gpt-5.6-luna' }
  ]);
  assert.equal(out.length, 1);
  assert.equal(out[0].name, 'ok');
});

test('sanitizeProfiles: fills missing fields with defaults', () => {
  const out = sanitizeProfiles([{ name: 'x' }]);
  assert.deepEqual(out[0], { name: 'x', ...defaults() });
});

test('sanitizeProfiles: keeps present-but-unknown model string', () => {
  const out = sanitizeProfiles([{ name: 'x', model: 'gpt-mystery' }]);
  assert.equal(out[0].model, 'gpt-mystery');
});

test('sanitizeProfiles: empty/blank model falls back to default', () => {
  assert.equal(sanitizeProfiles([{ name: 'x', model: '' }])[0].model, DEFAULT_MODEL_ID);
  assert.equal(sanitizeProfiles([{ name: 'x', model: 42 }])[0].model, DEFAULT_MODEL_ID);
});

test('sanitizeProfiles: memory_max outside the list -> 4G', () => {
  for (const bad of ['8G', '', 'G', 4, null]) {
    assert.equal(sanitizeProfiles([{ name: 'x', memory_max: bad }])[0].memory_max, '4G');
  }
  for (const good of ['2G', '4G', '6G']) {
    assert.equal(sanitizeProfiles([{ name: 'x', memory_max: good }])[0].memory_max, good);
  }
});

test('sanitizeProfiles: handoff_tokens only kept when positive integer', () => {
  const cases = [
    [123456, 123456], [0, null], [-5, null], [1.5, null],
    ['12', null], [null, null], [undefined, null], [NaN, null], [Infinity, null]
  ];
  for (const [given, want] of cases) {
    const out = sanitizeProfiles([{ name: 'x', handoff_tokens: given }]);
    assert.equal(out[0].handoff_tokens, want, `handoff_tokens=${String(given)}`);
  }
});

test('sanitizeProfiles: skills non-array -> [], non-string members dropped', () => {
  assert.deepEqual(sanitizeProfiles([{ name: 'x', skills: 'nope' }])[0].skills, []);
  const out = sanitizeProfiles([{ name: 'x', skills: ['a', 1, null, 'b', {}] }]);
  assert.deepEqual(out[0].skills, ['a', 'b']);
});

test('sanitizeProfiles: loop kept only with integer minutes>0 and non-empty prompt', () => {
  const good = { minutes: 30, prompt: 'do it' };
  assert.deepEqual(sanitizeProfiles([{ name: 'x', loop: good }])[0].loop, good);
  for (const bad of [
    { minutes: 0, prompt: 'p' }, { minutes: -3, prompt: 'p' },
    { minutes: 1.5, prompt: 'p' }, { minutes: '30', prompt: 'p' },
    { minutes: 30, prompt: '' }, { minutes: 30, prompt: '   ' },
    {}, 'loop', 42, null, []
  ]) {
    const out = sanitizeProfiles([{ name: 'x', loop: bad }]);
    assert.equal(out[0].loop, null, `loop=${JSON.stringify(bad)}`);
  }
});

test('sanitizeProfiles: drops unknown extra keys', () => {
  const out = sanitizeProfiles([{ name: 'x', title: 'nope', grid: 'g', snoop: true, wake: {} }]);
  assert.deepEqual(Object.keys(out[0]).sort(), ['cages','effort','fast','handoff_tokens','loop','memory','memory_max','model','name','skills']);
});

test('sanitizeProfiles: duplicate names collapse case-insensitively, last wins', () => {
  const out = sanitizeProfiles([
    { name: 'Fast', model: 'pi-qwen38-flash' },
    { name: 'fast', model: 'gpt-5.6-luna' }
  ]);
  assert.equal(out.length, 1);
  assert.equal(out[0].name, 'fast'); // stored casing = the one that won
  assert.equal(out[0].model, 'gpt-5.6-luna');
});

test('sanitizeProfiles: does not mutate its input', () => {
  const input = [{ name: 'x', skills: ['a'], loop: { minutes: 5, prompt: 'p' }, bogus: 1 }];
  const snapshot = JSON.parse(JSON.stringify(input));
  sanitizeProfiles(input);
  assert.deepEqual(input, snapshot);
});

test('profileFromForm: empty/whitespace name -> null', () => {
  for (const n of ['', '   ', null, undefined, 7]) {
    assert.equal(profileFromForm(n, { model: 'gpt-5.6-luna' }), null);
  }
});

test('profileFromForm: handoff blank/bad/<=0 -> null', () => {
  const base = { model: 'gpt-5.6-luna', effort: 'xhigh', fast: true,
    memoryMax: '6G', memory: true, skills: ['dev-rules'],
    loopOn: false, loopMinutes: '30', loopPrompt: '' };
  assert.equal(profileFromForm('x', { ...base, handoffTokens: '' }).handoff_tokens, null);
  assert.equal(profileFromForm('x', { ...base, handoffTokens: 'abc' }).handoff_tokens, null);
  assert.equal(profileFromForm('x', { ...base, handoffTokens: '0' }).handoff_tokens, null);
  assert.equal(profileFromForm('x', { ...base, handoffTokens: '-5' }).handoff_tokens, null);
  assert.equal(profileFromForm('x', { ...base, handoffTokens: '123456' }).handoff_tokens, 123456);
});

test('profileFromForm: loop kept only when on with valid minutes and prompt', () => {
  const base = { model: 'gpt-5.6-luna', effort: '', fast: false,
    memoryMax: '4G', memory: false, skills: [], handoffTokens: '' };
  assert.equal(profileFromForm('x', { ...base, loopOn: false, loopMinutes: '30', loopPrompt: 'p' }).loop, null);
  assert.equal(profileFromForm('x', { ...base, loopOn: true, loopMinutes: '-2', loopPrompt: 'p' }).loop, null);
  assert.equal(profileFromForm('x', { ...base, loopOn: true, loopMinutes: '30', loopPrompt: '' }).loop, null);
  assert.deepEqual(
    profileFromForm('x', { ...base, loopOn: true, loopMinutes: '30', loopPrompt: '  do it ' }).loop,
    { minutes: 30, prompt: 'do it' });
});

test('profileFromForm: output is round-trip stable through sanitizeProfiles', () => {
  const form = {
    model: 'gpt-5.6-luna', effort: 'xhigh', fast: true, handoffTokens: '123456',
    memoryMax: '6G', memory: true, skills: ['dev-rules'],
    loopOn: true, loopMinutes: '30', loopPrompt: 'check in'
  };
  const profile = profileFromForm('  Fast Codex  ', form);
  assert.equal(profile.name, 'Fast Codex');
  assert.deepEqual(sanitizeProfiles([profile])[0], profile);
});

test('applyProfile: model with custom effort dial keeps valid effort, drops fast', () => {
  const out = applyProfile({
    name: 'q', model: 'agy-gemini-4-neon',
    effort: 'xhigh', fast: true, handoff_tokens: null,
    memory_max: '6G', memory: false, skills: [], loop: null
  });
  assert.equal(out.model, 'agy-gemini-4-neon');
  assert.equal(out.effort, 'xhigh');
  assert.equal(out.fast, false);
  // Dial-invalid values still clamp to off (empty).
  const bad = applyProfile({
    name: 'q2', model: 'agy-gemini-4-neon',
    effort: 'max', fast: false, handoff_tokens: null,
    memory_max: '6G', memory: false, skills: [], loop: null
  });
  assert.equal(bad.effort, '');
});

test('applyProfile: unknown model -> default model with a warning naming it', () => {
  const out = applyProfile({
    name: 'u', model: 'gpt-mystery', effort: 'xhigh', fast: true,
    handoff_tokens: null, memory_max: '4G', memory: false, skills: ['a'], loop: null
  });
  assert.equal(out.model, DEFAULT_MODEL_ID);
  assert.ok(out.warning.length > 0);
  assert.ok(out.warning.includes('gpt-mystery'));
});

test('applyProfile: handoff null -> model default string; fast/effort clamped to model', () => {
  const out = applyProfile({
    name: 'f', model: 'gpt-6-luna', effort: 'low', fast: true,
    handoff_tokens: null, memory_max: '6G', memory: true, skills: ['a'], loop: null
  });
  assert.equal(out.model, 'gpt-6-luna');
  assert.equal(out.effort, 'low');
  assert.equal(out.fast, true);
  assert.equal(out.handoffTokens, '225000');
  assert.equal(out.memoryMax, '6G');
  assert.equal(out.memory, true);
  assert.deepEqual(out.skills, ['a']);
  assert.equal(out.loopOn, false);
  assert.equal(out.loopMinutes, '30');
  assert.equal(out.warning, '');
});

test('applyProfile: handoff explicit kept; loop applied', () => {
  const out = applyProfile({
    name: 'l', model: 'claude-fable-5-2', effort: 'max', fast: false,
    handoff_tokens: 999, memory_max: '2G', memory: false, skills: [],
    loop: { minutes: 45, prompt: 'ping me' }
  });
  assert.equal(out.handoffTokens, '999');
  assert.equal(out.loopOn, true);
  assert.equal(out.loopMinutes, '45');
  assert.equal(out.loopPrompt, 'ping me');
});

test('applyProfile: returns a new skills array', () => {
  const skills = ['a', 'b'];
  const out = applyProfile({ name: 'x', model: 'gpt-6-luna', skills });
  assert.notEqual(out.skills, skills);
});

test('upsertProfile: replaces in place (case-insensitive), position + length preserved', () => {
  const list = [
    { name: 'alpha', model: 'gpt-5.6-luna' },
    { name: 'beta', model: 'claude-fable-5' }
  ];
  const out = upsertProfile(list, { name: 'ALPHA', model: 'pi-qwen38-flash' });
  assert.equal(out.length, 2);
  assert.equal(out[0].name, 'ALPHA');
  assert.equal(out[0].model, 'pi-qwen38-flash');
  assert.equal(out[1].name, 'beta');
  assert.notEqual(out, list);
});

test('upsertProfile: new name appends', () => {
  const out = upsertProfile([{ name: 'alpha' }], { name: 'zeta' });
  assert.deepEqual(out.map((p) => p.name), ['alpha', 'zeta']);
});

test('removeProfile: case-insensitive; no-op for absent name; new array', () => {
  const list = [{ name: 'Alpha' }, { name: 'Beta' }];
  const out = removeProfile(list, 'alpha');
  assert.deepEqual(out.map((p) => p.name), ['Beta']);
  assert.notEqual(out, list);
  assert.deepEqual(removeProfile(list, 'Gamma').map((p) => p.name), ['Alpha', 'Beta']);
});

test('findProfile: case-insensitive', () => {
  const list = [{ name: 'Alpha', model: 'x' }];
  assert.equal(findProfile(list, 'alpha').model, 'x');
  assert.equal(findProfile(list, 'ALPHA').model, 'x');
  assert.equal(findProfile(list, 'nope'), undefined);
});

test('sortedProfiles: alphabetical, does not mutate input', () => {
  const list = [{ name: 'zebra' }, { name: 'Alpha' }, { name: 'mango' }];
  const snapshot = list.map((p) => p.name);
  const out = sortedProfiles(list);
  assert.deepEqual(out.map((p) => p.name), ['Alpha', 'mango', 'zebra']);
  assert.deepEqual(list.map((p) => p.name), snapshot);
});

test('automatic and explicit profile loading both restore saved skills', () => {
  for (const model of [DEFAULT_MODEL_ID, 'gpt-6.2-astra', 'claude-fable-5-2', 'agy-gemini-4-neon']) {
    const profile = { ...defaults(), model, skills: ['plain-style'] };
    assert.deepEqual(applyProfile(profile, { automatic: true }).skills, ['plain-style']);
    assert.deepEqual(applyProfile(profile).skills, ['plain-style']);
    assert.deepEqual(profile.skills, ['plain-style']);
  }
});

test('cages: sanitized, round-trip through form and apply', () => {
  const [p] = sanitizeProfiles([{ name: 'op', cages: ['operator', 3, null] }]);
  assert.deepEqual(p.cages, ['operator']);
  const [q] = sanitizeProfiles([{ name: 'op', cages: 'operator' }]);
  assert.deepEqual(q.cages, []);
  const fromForm = profileFromForm('op', { model: DEFAULT_MODEL_ID, effort: '', fast: false,
    handoffTokens: '', memoryMax: '4G', memory: false, skills: ['a'], cages: ['operator'],
    loopOn: false });
  assert.deepEqual(fromForm.cages, ['operator']);
  assert.deepEqual(applyProfile(fromForm).cages, ['operator']);
  assert.deepEqual(applyProfile({ name: 'x', model: DEFAULT_MODEL_ID }).cages, []);
});

test('applyProfile: the automatic (default) profile keeps its saved skills and cages', () => {
  const prof = { name: 'op', model: DEFAULT_MODEL_ID, skills: ['operator', 'pulsar-ask'], cages: ['operator'] };
  const v = applyProfile(prof, { automatic: true });
  assert.deepEqual(v.skills, ['operator', 'pulsar-ask']);
  assert.deepEqual(v.cages, ['operator']);
});

test('profiles: loop triggers + trigger_mode round-trip; absent = timer/any', () => {
  const base = { model: 'gpt-6-luna', effort: '', fast: false,
    memoryMax: '4G', memory: false, skills: [], handoffTokens: '' };
  const form = { ...base, loopOn: true, loopMinutes: '40', loopPrompt: 'rules',
    loopTriggers: { timer: true, handoff: false, message: true, replies: '', tools: '20' }, loopMode: 'all' };
  const p = profileFromForm('x', form);
  assert.deepEqual(p.loop, { minutes: 40, prompt: 'rules', triggers: ['timer', 'message', 'tools:20'], trigger_mode: 'all' });
  const clean = sanitizeProfiles([p])[0];
  assert.deepEqual(clean.loop, p.loop);
  const v = applyProfile(clean);
  assert.deepEqual(v.loopTriggers, ['timer', 'message', 'tools:20']);
  assert.equal(v.loopMode, 'all');
  // Old-shape loop (no triggers) applies as a plain timer loop.
  const old = applyProfile({ model: 'gpt-6-luna', loop: { minutes: 5, prompt: 'p' } });
  assert.deepEqual(old.loopTriggers, ['timer']);
  assert.equal(old.loopMode, 'any');
  assert.deepEqual(sanitizeProfiles([{ name: 'x', loop: { minutes: 5, prompt: 'p' } }])[0].loop, { minutes: 5, prompt: 'p' });
});
