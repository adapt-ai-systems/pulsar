import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { parse } from 'svelte/compiler';

const source = readFileSync(new URL('./ChatPane.svelte', import.meta.url), 'utf8');
const ast = parse(source, { modern: true });
function collect(node, predicate) {
  if (!node || typeof node !== 'object') return [];
  return [...(predicate(node) ? [node] : []),
    ...Object.values(node).flatMap(value => Array.isArray(value)
      ? value.flatMap(child => collect(child, predicate)) : collect(value, predicate))];
}
const branches = collect(ast.fragment, node => node.type === 'IfBlock');
const condition = node => source.slice(node.test.start, node.test.end);
const components = node => collect(node, n => n.type === 'Component').map(n => n.name);

test('tool detail renders tool items independently of reasoning traces', () => {
  const branch = branches.find(node => condition(node) === "item.kind === 'tool' && uiPrefs.showToolCalls");
  assert.ok(branch, 'tool items need their own branch gated by tool detail');
  assert.deepEqual(components(branch.consequent), ['ToolCard', 'ToolLine']);
  const modes = collect(branch.consequent, node => node.type === 'IfBlock');
  assert.equal(condition(modes[0]), 'uiPrefs.toolCards');
  assert.deepEqual(components(modes[0].consequent), ['ToolCard']);
  assert.deepEqual(components(modes[0].alternate), ['ToolLine']);
});

test('thinking items render only thinking cards, never tool components', () => {
  const branch = branches.find(node => condition(node) === "item.kind === 'thinking' && reasoningEnabled");
  assert.ok(branch);
  assert.deepEqual(components(branch.consequent), ['ThinkingCard']);
});

test('ChatPane keys transcript items by stable item.key rather than array index', () => {
  assert.match(source, /\{#each feed\.items as item \(item\.key\)\}/);
});

test('ToolLine, ToolCard, and TodoBar toggle on Space with preventDefault', () => {
  for (const file of ['ToolLine.svelte', 'ToolCard.svelte', 'TodoBar.svelte']) {
    const text = readFileSync(new URL(`./${file}`, import.meta.url), 'utf8');
    assert.match(text, /onkeydown=\{[^}]*e\.key === ' '[^}]*e\.preventDefault\(\)[^}]*toggle\(\)/, file);
  }
});
