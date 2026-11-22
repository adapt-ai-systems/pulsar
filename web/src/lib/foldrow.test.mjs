import { test } from 'node:test';
import assert from 'node:assert/strict';
import { readFileSync } from 'node:fs';

// A folded row looks and acts like an inline row because both render the SAME
// row components. Wiring checks on source,
// like chatpane-tools.test.mjs; the pixels are checked in the browser.
const src = (f) => readFileSync(new URL(`./${f}`, import.meta.url), 'utf8');
const fold = src('FoldRow.svelte');
const pane = src('ChatPane.svelte');
const reply = src('ReplyMessage.svelte');
const peer = src('PeerMessage.svelte');

test('FoldRow renders folded rows with the shared PeerMessage and ReplyMessage', () => {
  assert.match(fold, /<PeerMessage item=\{it\} \/>/);
  assert.match(fold, /<ReplyMessage item=\{it\} [^>]*\{activeReply\} \{onreply\}/);
  // no private copy of the row markup left in FoldRow
  assert.doesNotMatch(fold, /class="msg |renderMarkdown|class="lbl"/);
});

test('ChatPane renders inline rows with the same components and feeds FoldRow the same props', () => {
  assert.match(pane, /<PeerMessage \{item\} \/>/);
  assert.match(pane, /<ReplyMessage \{item\} \{archived\} \{activeReply\} onreply=\{startReply\} \/>/);
  assert.match(pane, /<FoldRow \{item\} \{archived\} \{activeReply\} onreply=\{startReply\} \/>/);
  assert.doesNotMatch(pane, /class="msg peer"|class="msg dreamer">/);
});

test('ReplyMessage carries what folded replies were missing', () => {
  assert.match(reply, /<div class="lbl">REPLY/);
  assert.match(reply, /use:cardify=/);
  assert.match(reply, /class="reply-btn"/);
  assert.match(peer, /class="msg peer"/);
});

test('rows inside an expanded fold do not toggle the fold', () => {
  // the clickable header and the rows are siblings
  const header = fold.match(/<div class="fold" [\s\S]*?\n  <\/div>/)[0];
  assert.doesNotMatch(header, /PeerMessage|ReplyMessage/);
  assert.match(fold, /<div class="rows">/);
});

test('FoldRow prevents default scrolling when toggled with Space', () => {
  assert.match(fold, /onkeydown=\{[^}]*e\.key === ' '[^}]*e\.preventDefault\(\)/);
});
