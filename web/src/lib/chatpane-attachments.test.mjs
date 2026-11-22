import test from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';
import { PULSE_MIME } from './dnd.js';
import { composerDragOver, composerDrop, paneDragOver, paneDrop, fmtSize } from './composerdrop.js';

// The pane shell keeps the transcript side (reply button, quote, pane drag/drop);
// the Composer owns the picker, composer drop handlers, reply bar and send.
const source = readFileSync(new URL('./ChatPane.svelte', import.meta.url), 'utf8');
const replyMessage = readFileSync(new URL('./ReplyMessage.svelte', import.meta.url), 'utf8');
const composerSource = readFileSync(new URL('./Composer.svelte', import.meta.url), 'utf8');

test('attachment picker accepts audio and explicitly accepts macOS m4a voice memos', () => {
  const picker = composerSource.match(/<input\s+type="file"[\s\S]*?\/>/)[0];
  const accepts = picker.match(/accept="([^"]+)"/)[1].split(',');
  assert.ok(accepts.includes('audio/*'));
  assert.ok(accepts.includes('.m4a'));
  assert.ok(accepts.includes('image/png'));
  assert.match(picker, /multiple/);
  assert.match(picker, /onchange=\{onFilesPicked\}/);
});

const defaultState = {
  stopped: false,
  archived: false,
  sending: false,
  canReorder: true,
  dragging: false,
  pulseId: 'target'
};

function state(overrides = {}) {
  return { ...defaultState, ...overrides };
}

function event(types, files = [], id = '') {
  return { dataTransfer: { types, files, getData: () => id }, prevented: false, stopped: false,
    preventDefault() { this.prevented = true; }, stopPropagation() { this.stopped = true; } };
}

test('composer wires file drop and dragover handlers', () => {
  assert.match(composerSource, /class="composer"[^>]*ondragover=\{onComposerDragOver\}[^>]*ondrop=\{onComposerDrop\}/);
});
test('file dragover uses types, even while files are hidden by the browser', () => {
  const e = event(['Files']);
  composerDragOver(e, state());
  assert.equal(e.dataTransfer.dropEffect, 'copy');
  assert.ok(e.prevented && e.stopped);
});
test('multiple dropped files use the same stageFile path as the picker, in order', () => {
  const files = [{ name: 'memo.m4a' }, { name: 'image.png' }], staged = [];
  const e = event(['Files'], files);
  composerDrop(e, state(), { stageFile: f => staged.push(f) });
  assert.deepEqual(staged, files);
  assert.ok(e.prevented && e.stopped);
});
for (const flag of ['stopped', 'archived', 'sending']) {
  test(`${flag} composer rejects file drops without navigating away`, () => {
    const s = state({ [flag]: true });
    const e = event(['Files'], [{ name: 'memo.m4a' }]);
    composerDragOver(e, s);
    assert.equal(e.dataTransfer.dropEffect, 'none');
    composerDrop(e, s, { stageFile() { assert.fail('must not stage'); } });
    assert.ok(e.prevented && e.stopped);
  });
}
test('pulse drags bubble through the composer and still reorder the pane', () => {
  const e = event([PULSE_MIME, 'text/plain'], [], 'source'), calls = [];
  const s = state();
  composerDragOver(e, s);
  composerDrop(e, s, { stageFile() {} });
  assert.ok(!e.prevented && !e.stopped);
  assert.equal(paneDragOver(e, s), true);
  assert.equal(e.dataTransfer.dropEffect, 'move');
  assert.equal(paneDrop(e, s, { onreorder: (...args) => calls.push(args) }), false);
  assert.deepEqual(calls, [['source', 'target']]);
});
test('files and plain text cannot trigger pulse reordering', () => {
  const s = state();
  for (const types of [['Files', 'text/plain'], ['text/plain']]) {
    const e = event(types, [], 'not-a-pulse');
    assert.equal(paneDragOver(e, s), undefined);
    assert.equal(paneDrop(e, s, { onreorder() { assert.fail('must not reorder'); } }), false);
    assert.equal(e.prevented, false);
  }
});
test('text drops retain native textarea behavior', () => {
  const e = event(['text/plain']);
  const s = state();
  composerDragOver(e, s);
  composerDrop(e, s, { stageFile() {} });
  assert.ok(!e.prevented && !e.stopped);
});

test('reply button quotes a reply into the next send and the YOU bubble', () => {
  // Transcript side: the button on each finished reply bubble (ReplyMessage,
  // shared by the inline row and the folded row, wired to ChatPane's
  // startReply) and the quote on the sent YOU bubble (ChatPane).
  assert.match(replyMessage, /class="reply-btn"[\s\S]*?onclick=\{\(\) => onreply\?\.\(item\)\}/);
  assert.match(source, /<ReplyMessage [^>]*onreply=\{startReply\}/);
  assert.match(source, /<FoldRow [^>]*onreply=\{startReply\}/);
  assert.match(source, /\{#if item\.replyTo\}[\s\S]*?class="reply-quote"/);
  // Composer side: the chip with a cancel, Esc cancels, and send passes the
  // quote to both send paths.
  assert.match(composerSource, /\{#if activeReply\}[\s\S]*?class="reply-bar"[\s\S]*?onclick=\{\(\) => \(replyTo = null\)\}/);
  assert.match(composerSource, /e\.key === 'Escape' && activeReply\)[\s\S]*?replyTo = null/);
  assert.match(composerSource, /sendMessage\(pulse\.id, text, undefined, replyRef\(sentReply\)\)/);
  assert.match(composerSource, /sendMessageMultipart\(pulse\.id, text,\s*staged\.map[^\n]*\n?[^\n]*replyRef\(sentReply\)\)/);
});
test('the pane hands the Reply button to the Composer through a bindable replyTo', () => {
  assert.match(source, /<Composer[\s\S]*?bind:replyTo[\s\S]*?\/?>/);
  assert.match(source, /composer\?\.focus\(\)/);
});
test('fmtSize: bytes, KB rounded, MB with one decimal', () => {
  assert.equal(fmtSize(12), '12B');
  assert.equal(fmtSize(2048), '2KB');
  assert.equal(fmtSize(1572864), '1.5MB');
});
