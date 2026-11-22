import { test } from 'node:test';
import assert from 'node:assert/strict';
import { partialFields, liveCodeView } from './livecode.js';

test('partialFields reads a truncated object, last string partial', () => {
  const f = partialFields('{"file_path": "/a/b.py", "content": "def f():\\n    return \\"x');
  assert.equal(f.file_path, '/a/b.py');
  assert.equal(f.content, 'def f():\n    return "x');
});

test('partialFields drops a dangling escape and keeps non-string values raw', () => {
  assert.deepEqual(partialFields('{"n": 3, "o": {"a": [1, "}"]}, "s": "ab\\'), {
    n: '3', o: '{"a": [1, "}"]}', s: 'ab'
  });
  assert.deepEqual(partialFields('{"s": "\\u00e9\\u00'), { s: 'é' });
  assert.deepEqual(partialFields(''), {});
  assert.deepEqual(partialFields('{"file_pa'), {});
});

test('liveCodeView: Write shows path + content', () => {
  const v = liveCodeView({ name: 'Write', text: '{"file_path":"x.js","content":"let a = 1;\\nlet b'});
  assert.deepEqual(v, { tool: 'Write', path: 'x.js', body: 'let a = 1;\nlet b', lines: null });
});

test('liveCodeView: Edit (claude and pi keys) is a signed diff', () => {
  const v = liveCodeView({ name: 'Edit', text: '{"file_path":"x","old_string":"a\\nb","new_string":"c' });
  assert.deepEqual(v.lines, [
    { sign: '-', text: 'a' }, { sign: '-', text: 'b' }, { sign: '+', text: 'c' }
  ]);
  const p = liveCodeView({ name: 'edit', text: '{"path":"y","oldText":"o","newText":"n"}' });
  assert.equal(p.path, 'y');
  assert.deepEqual(p.lines, [{ sign: '-', text: 'o' }, { sign: '+', text: 'n' }]);
});

test('liveCodeView: Bash shows the command; unknown tools show raw JSON', () => {
  assert.equal(liveCodeView({ name: 'bash', text: '{"command":"ls -la' }).body, 'ls -la');
  assert.equal(liveCodeView({ name: 'Grep', text: '{"pattern":"x' }).body, '{"pattern":"x');
  assert.equal(liveCodeView({ name: null, text: '{' }).tool, 'tool');
  assert.equal(liveCodeView(null), null);
});
