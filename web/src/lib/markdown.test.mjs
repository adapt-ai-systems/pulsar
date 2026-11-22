/**
 * Self-contained tests for markdown.js.
 *
 * No test framework is installed in web/, so this is a plain-node assertion
 * script (same pattern as usage.test.mjs / schedule.test.mjs):
 *
 *     node src/lib/markdown.test.mjs
 */

import assert from 'node:assert/strict';
import { renderMarkdown as md, splitFrontMatter } from './markdown.js';

let passed = 0;
function t(name, fn) {
  try {
    fn();
    passed++;
  } catch (e) {
    console.error(`FAIL: ${name}\n  ${e.message}`);
    process.exitCode = 1;
  }
}

// --- security: this is the {@html} trust boundary ------------------------- //

t('escapes raw HTML', () => {
  assert.equal(md('<img src=x onerror=alert(1)>'), '<p>&lt;img src=x onerror=alert(1)&gt;</p>');
});

t('escapes script tags', () => {
  assert.ok(!md('<script>alert(1)</script>').includes('<script>'));
});

t('drops javascript: links but keeps the label', () => {
  const out = md('[click](javascript:alert(1))');
  assert.ok(!out.includes('javascript:'));
  assert.ok(out.includes('click'));
  assert.ok(!out.includes('<a '));
});

t('drops javascript: obfuscated with control chars', () => {
  const out = md('[x](java\tscript:alert(1))');
  assert.ok(!out.includes('<a '));
});

t('drops data: URLs', () => {
  assert.ok(!md('[x](data:text/html;base64,PHNjcmlwdD4=)').includes('<a '));
});

t('allows https, mailto and relative links', () => {
  assert.ok(md('[a](https://x.com)').includes('href="https://x.com"'));
  assert.ok(md('[b](mailto:a@b.c)').includes('href="mailto:a@b.c"'));
  assert.ok(md('[c](/api/thing)').includes('href="/api/thing"'));
});

t('author text cannot forge a placeholder sentinel', () => {
  const out = md('0 `real`');
  assert.ok(out.includes('<code>real</code>'));
  assert.ok(!out.includes(''));
});

t('escapes quotes inside link text', () => {
  assert.ok(!md('["><b>](https://x.com)').includes('"><b>'));
});

// --- inline --------------------------------------------------------------- //

t('bold', () => assert.equal(md('**hi**'), '<p><strong>hi</strong></p>'));
t('italic', () => assert.equal(md('*hi*'), '<p><em>hi</em></p>'));
t('strikethrough', () => assert.equal(md('~~hi~~'), '<p><del>hi</del></p>'));
t('inline code', () => assert.equal(md('`x=1`'), '<p><code>x=1</code></p>'));

t('inline code is immune to emphasis and escaping', () => {
  assert.equal(md('`**not bold** <b>`'), '<p><code>**not bold** &lt;b&gt;</code></p>');
});

t('snake_case and __dunder__ survive untouched', () => {
  assert.equal(md('call some_var_name and __init__'), '<p>call some_var_name and __init__</p>');
});

t('bold wins over italic for **', () => {
  assert.equal(md('**a** and *b*'), '<p><strong>a</strong> and <em>b</em></p>');
});

t('bare URLs autolink without eating trailing punctuation', () => {
  const out = md('see https://x.com/a.');
  assert.ok(out.includes('href="https://x.com/a"'));
  assert.ok(out.endsWith('.</p>'));
});

t('autolinker does not re-scan an explicit link href', () => {
  const out = md('[label](https://x.com)');
  assert.equal(out.match(/<a /g).length, 1);
});

// Every URL handed over must be clickable — the three shapes models actually
// write. A dead link forces manual retyping.

t('a URL alone in a code span is clickable and stays styled as code', () => {
  const out = md('Live at `https://app.example.com:8080/mint/` now');
  assert.ok(out.includes('href="https://app.example.com:8080/mint/"'));
  assert.ok(out.includes('<code>https://app.example.com:8080/mint/</code>'));
});

t('a code span that merely contains a URL is left as code', () => {
  const out = md('run `curl https://x.com/a` first');
  assert.ok(!out.includes('<a '));
  assert.ok(out.includes('<code>curl https://x.com/a</code>'));
});

t('an unsafe scheme in a code span never becomes a link', () => {
  const out = md('`javascript:alert(1)`');
  assert.ok(!out.includes('<a '));
});

t('a quote in a code-span URL cannot break out of the href attribute', () => {
  const out = md('`https://x.com/a"onmouseover=alert(1)`');
  assert.ok(out.includes('&quot;'));
  assert.ok(!/href="[^"]*"[^>]*onmouseover/.test(out));
});

// A query string must survive escaping EXACTLY once. Escaped twice, the
// browser reads &amp;amp; back as a literal "&amp;" and the link goes
// somewhere else entirely — worse than not linking at all.
for (const [label, src] of [
  ['bare', 'see https://x.com/?a=1&b=2'],
  ['explicit', '[x](https://x.com/?a=1&b=2)'],
  ['code span', '`https://x.com/?a=1&b=2`'],
  ['angle bracket', '<https://x.com/?a=1&b=2>'],
]) {
  t(`a ${label} URL keeps its query string intact`, () => {
    const href = md(src).match(/href="([^"]*)"/)?.[1];
    assert.equal(href, 'https://x.com/?a=1&amp;b=2');
  });
}

t('angle-bracket autolinks work and consume the brackets', () => {
  const out = md('<https://app.example.com:8080/a/>');
  assert.ok(out.includes('href="https://app.example.com:8080/a/"'));
  assert.ok(!out.includes('&lt;'));
});

t('an unsafe scheme in angle brackets stays plain text', () => {
  assert.ok(!md('<javascript:alert(1)>').includes('<a '));
});

t('a bolded URL autolinks inside the strong', () => {
  const out = md('**https://app.example.com:8080/slug/**');
  assert.ok(out.includes('href="https://app.example.com:8080/slug/"'));
  assert.ok(out.includes('<strong>'));
  assert.ok(!out.includes('/slug/**'), 'asterisks must not glue to the href');
});

t('an italicized URL autolinks inside the em', () => {
  const out = md('*https://x.com/a*');
  assert.ok(out.includes('href="https://x.com/a"'));
  assert.ok(out.includes('<em>'));
});

t('fenced code keeps URLs as literal text', () => {
  const out = md('```\ncurl https://x.com/a\n```');
  assert.ok(!out.includes('<a '));
});

// --- blocks --------------------------------------------------------------- //

t('headings', () => {
  assert.equal(md('# One'), '<h1>One</h1>');
  assert.equal(md('### Three'), '<h3>Three</h3>');
});

t('fenced code keeps content verbatim', () => {
  const out = md('```js\nconst a = 1 < 2;\n```');
  assert.ok(out.includes('<pre><code class="lang-js">const a = 1 &lt; 2;</code></pre>'));
});

t('fenced code without a language', () => {
  assert.ok(md('```\nplain\n```').includes('<pre><code>plain</code></pre>'));
});

t('unterminated fence still renders', () => {
  assert.ok(md('```\noops').includes('<pre><code>oops</code></pre>'));
});

t('code blocks carry a copy button', () => {
  const out = md('```\nnpm install\n```');
  assert.ok(out.includes('class="code-wrap"'));
  assert.ok(out.includes('class="copy-btn"'));
  assert.ok(out.includes('aria-label="copy code"'));
});

t('code bar shows the language when given', () => {
  assert.ok(md('```bash\nls\n```').includes('<span class="code-lang">bash</span>'));
  assert.ok(md('```\nls\n```').includes('<span class="code-lang"></span>'));
});

t('language name is escaped into the code bar', () => {
  const out = md('```a"><b>\nx\n```');
  assert.ok(!out.includes('<b>'));
});

t('inline code gets no copy button', () => {
  assert.ok(!md('use `npm i` here').includes('copy-btn'));
});

// Regression: these used to hang forever. A line that trips the paragraph
// loop's block-opener exclusion but fails that block's own parser must still
// consume a line, or the renderer spins until the tab dies.
t('malformed fence info string does not hang', () => {
  const out = md('```a"><b>\nx\n```');
  assert.ok(out.includes('x'));
  assert.ok(!out.includes('<b>'));
});

t('over-indented heading does not hang', () => {
  assert.ok(md('     #### deep').includes('deep'));
});

t('fence with trailing words does not hang', () => {
  assert.ok(md('```js run this\ncode').includes('code'));
});

t('markdown inside a fence is not parsed', () => {
  assert.ok(md('```\n# not a heading\n```').includes('# not a heading'));
});

t('unordered list', () => {
  assert.equal(md('- a\n- b'), '<ul><li>a</li><li>b</li></ul>');
});

t('ordered list', () => {
  assert.equal(md('1. a\n2. b'), '<ol><li>a</li><li>b</li></ol>');
});

t('nested list', () => {
  assert.equal(md('- a\n  - b'), '<ul><li>a<ul><li>b</li></ul></li></ul>');
});

t('blockquote', () => {
  assert.equal(md('> quoted'), '<blockquote><p>quoted</p></blockquote>');
});

t('nested blockquote', () => {
  assert.equal(md('>> deep'), '<blockquote><blockquote><p>deep</p></blockquote></blockquote>');
});

t('absurd blockquote nesting renders instead of blowing the stack', () => {
  // A model reply of 10k '>' used to throw RangeError mid-render and take the
  // tab down with it. Past the depth cap the leftover markers are just text.
  const out = md('>'.repeat(10000) + ' deep');
  assert.ok(out.startsWith('<blockquote>'));
  assert.ok(out.includes('&gt;'));
  assert.ok(!out.includes('<script'));
});

t('horizontal rule', () => assert.equal(md('---'), '<hr />'));

t('table', () => {
  const out = md('| a | b |\n|---|---|\n| 1 | 2 |');
  assert.ok(out.includes('<th>a</th>'));
  assert.ok(out.includes('<td>2</td>'));
});

t('hard line breaks inside a paragraph', () => {
  assert.equal(md('one\ntwo'), '<p>one<br />two</p>');
});

t('blank line separates paragraphs', () => {
  assert.equal(md('one\n\ntwo'), '<p>one</p><p>two</p>');
});

t('empty and null input', () => {
  assert.equal(md(''), '');
  assert.equal(md(null), '');
  assert.equal(md(undefined), '');
});

t('CRLF is normalized', () => {
  assert.equal(md('a\r\n\r\nb'), '<p>a</p><p>b</p>');
});

t('a table-escaped pipe shows as a pipe, in and out of tables', () => {
  assert.match(md('[Acme Door Services \\| BBB](https://x.com)'), />Acme Door Services \| BBB<\/a>/);
  const html = md('| a | b |\n|---|---|\n| [X \\| Y](https://x.com) | z |');
  assert.match(html, /<td><a [^>]+>X \| Y<\/a><\/td><td>z<\/td>/);
});


t('splitFrontMatter pulls YAML front matter off the top', () => {
  assert.deepEqual(splitFrontMatter('---\nname: x\n---\n# Hi'), { front: 'name: x', body: '# Hi' });
  assert.deepEqual(splitFrontMatter('# Hi\n---\nx'), { front: '', body: '# Hi\n---\nx' });
});

t('inline code wrapped across a source line still renders', () => {
  const p = md('run `team-data sheets --match\n"tab=This Week"` now');
  assert.ok(p.includes('<code>team-data sheets --match &quot;tab=This Week&quot;</code>') || p.includes('<code>team-data sheets --match "tab=This Week"</code>'), p);
  assert.ok(!p.includes('`'), p);
  const l = md('- list `a\n  b` end');
  assert.ok(l.includes('<code>a b</code>'), l);
  // A stray backtick with nothing to close it keeps the line breaks.
  assert.ok(md("it's ` odd\nnext line").includes('<br />'));
});

t('code spans: 3-line spans join, double backticks pair by length', () => {
  assert.ok(md('see `a\nb\nc` end').includes('<code>a b c</code>'));
  const dbl = md('use ``x ` y`` now\nnext `z` line');
  assert.ok(dbl.includes('<br />'), dbl); // the double-tick line is closed, no join
});

console.log(`markdown.js: ${passed} passed`);
