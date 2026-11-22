/**
 * Minimal, dependency-free markdown → HTML for chat replies.
 *
 * WHY HAND-ROLLED: web/ has zero runtime dependencies on purpose. Pulling in
 * marked + DOMPurify to bold some text would be the first two, plus their
 * supply chain, for a subset we can cover in one file.
 *
 * SAFETY MODEL — the output goes through {@html}, so this is the trust
 * boundary. Two rules make injection impossible rather than merely unlikely:
 *   1. Every scrap of author text is HTML-escaped before it lands in output.
 *      Tags in the output exist ONLY because this file emitted them.
 *   2. Link hrefs are allow-listed by scheme (http/https/mailto + same-origin
 *      relative). `javascript:` and `data:` are dropped to a plain span.
 * Placeholder sentinels use private-use codepoints and are stripped from input
 * first, so a message can't forge one to smuggle markup past the escaper.
 *
 * SUPPORTED: fenced + inline code, ATX headings, bold, italic, strikethrough,
 * links, bare-URL autolinking, nested ordered/unordered lists, blockquotes,
 * horizontal rules, pipe tables, paragraphs with hard line breaks.
 *
 * DELIBERATELY UNSUPPORTED: `_underscore italics_`. This app's chats are full
 * of snake_case identifiers and __dunder__ names; treating `_` as emphasis
 * mangled more real text than it ever formatted. Use * for italics.
 */

const CODE_OPEN = '';
const CODE_CLOSE = '';

/** Escape the five characters that can break out of HTML text/attribute context. */
function esc(s) {
  return String(s)
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

/**
 * Allow-list link schemes. Anything else (javascript:, data:, vbscript:, or a
 * scheme we simply don't know) returns null and the caller renders plain text.
 */
function safeHref(url) {
  // Strip control chars and whitespace before the scheme test: "java\tscript:"
  // and "java\nscript:" are evasions browsers still resolve.
  const flat = String(url).replace(/[\u0000-\u0020\u007f]/g, '');
  if (/^(https?:\/\/|mailto:)/i.test(flat)) return flat;
  if (/^[/#]/.test(flat)) return flat; // same-origin relative or in-page anchor
  return null;
}

/** Inline pass. Input is RAW (unescaped) text for one block. */
/** One line of inline markdown (bold, code, links) with no <p> wrapper. */
export function renderInline(raw) {
  return inline(raw);
}

function inline(raw) {
  /** @type {string[]} */
  const stash = [];
  const keep = (html) => {
    stash.push(html);
    return CODE_OPEN + (stash.length - 1) + CODE_CLOSE;
  };

  // 1. Inline code first — its contents must be immune to every rule below.
  //    EXCEPT a code span that is nothing but a URL: models hand over links
  //    in backticks constantly (`https://host/slug/`), and rendering those as
  //    dead text means the reader retypes them by hand. Keep the code
  //    styling, wrap it in the anchor. Still stashed, so no later rule — and
  //    not the autolinker below — can re-scan what we just emitted.
  let s = String(raw).replace(/`([^`\n]+)`/g, (_, code) => {
    const el = `<code>${esc(code)}</code>`;
    const bare = /^\s*https?:\/\/\S+\s*$/.test(code) ? safeHref(code.trim()) : null;
    return keep(
      bare ? `<a href="${esc(bare)}" target="_blank" rel="noreferrer noopener">${el}</a>` : el
    );
  });

  // 2. Escape everything that is still author text.
  s = esc(s);
  // 2b. A table-escaped pipe is just a pipe once the cell is split
  //     ("Acme Door Services \\| BBB" showed its backslash).
  s = s.replace(/\\\|/g, '|');

  // Rules 3-4b run on text that step 2 ALREADY escaped, so the href they build
  // must NOT be escaped a second time. Escaping "?a=1&b=2" twice yields
  // "?a=1&amp;amp;b=2", which the browser resolves back to a literal "&amp;" —
  // a live link to the wrong URL, which is worse than no link. Once-escaped
  // text is already attribute-safe: step 2 turned every quote into an entity,
  // so there is no way out of href="…". (Rule 1 is the exception — it sees RAW
  // text, so it escapes there.)
  const anchor = (href, body) =>
    keep(`<a href="${href}" target="_blank" rel="noreferrer noopener">${body}</a>`);

  // 3. Explicit links. Stashed as finished anchors so the autolinker below
  //    can't re-scan the href it just produced.
  s = s.replace(/\[([^\]\n]*)\]\(([^)\s]+)\)/g, (whole, text, url) => {
    const href = safeHref(url);
    if (!href) return text; // unsafe scheme → keep the label, drop the link
    return anchor(href, text);
  });

  // 4. Angle-bracket autolinks: <https://x>. CommonMark's explicit "this is a
  //    URL" form, and what you get when something wraps a link to keep trailing
  //    punctuation out of it. Runs before rule 4b so the brackets are consumed
  //    rather than left stranded around the anchor.
  //    Non-greedy to the first &gt; rather than "anything but &", so a query
  //    string's own &amp; entities stay inside the URL.
  s = s.replace(/&lt;(https?:\/\/\S+?)&gt;/g, (whole, url) => {
    const href = safeHref(url);
    return href ? anchor(href, url) : whole;
  });

  // 4b. Bare URLs. Trailing punctuation is excluded so "see https://x.com."
  //    doesn't swallow the sentence's period. `*` counts as both a leading
  //    and a trailing delimiter because **https://x/** is how models emphasize
  //    a link they want you to click — without it the asterisks glue to the
  //    URL, autolinking misses, and the most-emphasized link on screen is the
  //    one you can't click. Cost: a URL genuinely ending in `*` loses it.
  s = s.replace(/(^|[\s(*])(https?:\/\/[^\s<>()"']+)/g, (whole, lead, url) => {
    const trimmed = url.replace(/[.,;:!?*]+$/, '');
    const tail = url.slice(trimmed.length);
    const href = safeHref(trimmed);
    if (!href) return whole;
    return lead + anchor(href, trimmed) + tail;
  });

  // 5. Emphasis. Bold before italic so ** isn't eaten as two single *.
  s = s.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  s = s.replace(/(^|[^*])\*([^*\n]+)\*(?!\*)/g, '$1<em>$2</em>');
  s = s.replace(/~~([^~\n]+)~~/g, '<del>$1</del>');

  // 6. Restore stashed HTML.
  return s.replace(
    new RegExp(CODE_OPEN + '(\\d+)' + CODE_CLOSE, 'g'),
    (_, i) => stash[Number(i)] ?? ''
  );
}

/** A table needs a `| --- | --- |` style separator on its second line. */
function isTableSeparator(line) {
  return /^\s*\|?[\s:|-]*-[\s:|-]*\|?\s*$/.test(line) && line.includes('-');
}

function splitRow(line) {
  return line
    .trim()
    .replace(/^\|/, '')
    .replace(/\|$/, '')
    .split(/(?<!\\)\|/)
    .map((c) => c.trim());
}

/**
 * How deep '>' nesting is honoured. Real quotes are one or two levels; the cap
 * exists because each level is a recursive call (see the blockquote branch).
 */
const MAX_QUOTE_DEPTH = 24;

/**
 * Render markdown to an HTML string.
 * @param {string} src
 * @param {number} [depth] blockquote nesting level — internal, set by recursion
 * @returns {string} HTML safe to pass to {@html}
 */
export function renderMarkdown(src, depth = 0) {
  if (src == null) return '';
  // Strip our sentinels so author text can never impersonate a placeholder,
  // and normalize line endings before any line-based parsing.
  const text = String(src)
    .split(CODE_OPEN).join('')
    .split(CODE_CLOSE).join('')
    .replace(/\r\n?/g, '\n');

  const lines = text.split('\n');
  const out = [];
  let i = 0;

  while (i < lines.length) {
    const line = lines[i];

    // --- fenced code ------------------------------------------------------ //
    const fence = line.match(/^\s*```+\s*([\w+-]*)\s*$/);
    if (fence) {
      const lang = fence[1];
      const body = [];
      i++;
      while (i < lines.length && !/^\s*```+\s*$/.test(lines[i])) body.push(lines[i++]);
      i++; // consume the closing fence (or fall off the end — unterminated is fine)
      const cls = lang ? ` class="lang-${esc(lang)}"` : '';
      // The copy button ships inside the markup rather than as a Svelte
      // component because this is an {@html} string. ChatPane catches the click
      // by delegation and reads the sibling <code>'s textContent, so no code
      // text is duplicated into an attribute where it could drift or re-escape.
      const head = lang ? `<span class="code-lang">${esc(lang)}</span>` : '<span class="code-lang"></span>';
      out.push(
        '<div class="code-wrap">' +
          `<div class="code-bar">${head}` +
          '<button class="copy-btn" type="button" aria-label="copy code">copy</button>' +
          '</div>' +
          `<pre><code${cls}>${esc(body.join('\n'))}</code></pre>` +
        '</div>'
      );
      continue;
    }

    // --- blank ------------------------------------------------------------ //
    if (!line.trim()) {
      i++;
      continue;
    }

    // --- horizontal rule -------------------------------------------------- //
    if (/^\s*([-*_])(\s*\1){2,}\s*$/.test(line)) {
      out.push('<hr />');
      i++;
      continue;
    }

    // --- heading ---------------------------------------------------------- //
    const h = line.match(/^\s{0,3}(#{1,6})\s+(.*?)\s*#*\s*$/);
    if (h) {
      const level = h[1].length;
      out.push(`<h${level}>${inline(h[2])}</h${level}>`);
      i++;
      continue;
    }

    // --- blockquote ------------------------------------------------------- //
    if (/^\s*>/.test(line)) {
      const body = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) {
        body.push(lines[i].replace(/^\s*>\s?/, ''));
        i++;
      }
      if (depth >= MAX_QUOTE_DEPTH) {
        // Past the cap the leftover '>' markers stay in the text as escaped
        // characters. Recursing instead is an availability hole: nesting is one
        // stack frame per level, and a reply with 10k of them threw
        // RangeError mid-render and took the tab with it (audit 2026-08-07).
        out.push(`<p>${body.map((l) => esc(l)).join('<br />')}</p>`);
      } else {
        out.push(`<blockquote>${renderMarkdown(body.join('\n'), depth + 1)}</blockquote>`);
      }
      continue;
    }

    // --- table ------------------------------------------------------------ //
    if (line.includes('|') && i + 1 < lines.length && isTableSeparator(lines[i + 1])) {
      const head = splitRow(line);
      i += 2;
      const rows = [];
      while (i < lines.length && lines[i].includes('|') && lines[i].trim()) {
        rows.push(splitRow(lines[i]));
        i++;
      }
      const th = head.map((c) => `<th>${inline(c)}</th>`).join('');
      const tb = rows
        .map((r) => `<tr>${r.map((c) => `<td>${inline(c)}</td>`).join('')}</tr>`)
        .join('');
      out.push(`<table><thead><tr>${th}</tr></thead><tbody>${tb}</tbody></table>`);
      continue;
    }

    // --- lists ------------------------------------------------------------ //
    const listItem = line.match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (listItem) {
      const consumed = parseList(lines, i, out);
      i = consumed;
      continue;
    }

    // --- paragraph -------------------------------------------------------- //
    const para = [];
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^\s*(```|>|#{1,6}\s)/.test(lines[i]) &&
      !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i]) &&
      !/^\s*([-*_])(\s*\1){2,}\s*$/.test(lines[i])
    ) {
      para.push(lines[i]);
      i++;
    }
    // FORWARD-PROGRESS GUARD. A line can look like a block opener to the
    // paragraph loop's exclusion test yet fail that block's own parser — e.g.
    // "```not a lang" (bad info string) or a 4-space-indented "#### x". Without
    // this, the loop pushes an empty <p> and never advances: a hang, not a typo.
    if (!para.length) {
      para.push(lines[i]);
      i++;
    }
    // Single newlines inside a paragraph become <br>: chat text is hard-wrapped
    // by humans and models alike, and collapsing it reflows into soup.
    out.push(`<p>${joinOpenCode(para).map((l) => inline(l.trim())).join('<br />')}</p>`);
  }

  return out.join('');
}

/**
 * Hard-wrapped lines that split an inline `code span` across a newline are
 * re-joined (with a space, as CommonMark does) so the span renders instead of
 * leaving raw backticks. A line with an odd backtick count is still open.
 */
function joinOpenCode(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    let cur = lines[i];
    let j = i;
    // Join only when a following line (up to 3) actually closes the span.
    if (openCodeRun(cur)) {
      let joined = cur;
      for (let k = i + 1; k < lines.length && k <= i + 3; k++) {
        joined = `${joined.trimEnd()} ${lines[k].trim()}`;
        if (!openCodeRun(joined)) { cur = joined; j = k; break; }
      }
    }
    out.push(cur);
    i = j + 1;
  }
  return out;
}

/** True when `text` leaves a code span open: a backtick run of length N is
 *  only closed by a later run of exactly N (CommonMark). */
function openCodeRun(text) {
  let open = 0;
  for (const m of text.matchAll(/`+/g)) {
    if (!open) open = m[0].length;
    else if (m[0].length === open) open = 0;
  }
  return open > 0;
}

/**
 * Parse a (possibly nested) list starting at `start`. Appends one list element
 * to `out` and returns the index of the first line after the list.
 */
function parseList(lines, start, out) {
  const first = lines[start].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
  const baseIndent = first[1].length;
  const ordered = /\d/.test(first[2]);
  const items = [];
  let i = start;

  while (i < lines.length) {
    const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
    if (!m) {
      // A blank line inside a list is tolerated only if a list item follows.
      if (!lines[i].trim() && i + 1 < lines.length && /^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i + 1])) {
        i++;
        continue;
      }
      break;
    }
    const indent = m[1].length;
    if (indent < baseIndent) break;
    if (indent > baseIndent) {
      // Deeper marker → nested list, rendered inside the previous <li>.
      const nested = [];
      i = parseList(lines, i, nested);
      if (items.length) items[items.length - 1] += nested.join('');
      else items.push(nested.join(''));
      continue;
    }
    // Same level: this item's text, plus any lazy continuation lines.
    let body = m[3];
    i++;
    while (
      i < lines.length &&
      lines[i].trim() &&
      !/^(\s*)([-*+]|\d+[.)])\s+/.test(lines[i]) &&
      !/^\s*(```|>|#{1,6}\s)/.test(lines[i]) &&
      lines[i].search(/\S/) > baseIndent
    ) {
      body += '\n' + lines[i].trim();
      i++;
    }
    items.push(joinOpenCode(body.split('\n')).map((l) => inline(l)).join('<br />'));
  }

  const tag = ordered ? 'ol' : 'ul';
  out.push(`<${tag}>${items.map((it) => `<li>${it}</li>`).join('')}</${tag}>`);
  return i;
}

/**
 * Copy buttons on code blocks. The blocks come from {@html}, so there are no
 * Svelte handlers to bind — one delegated listener on the container (chat
 * pane, Files preview) covers every block, including ones that arrive later.
 */
export async function copyCodeClick(e) {
  const btn = e.target.closest?.('.copy-btn');
  if (!btn) return;
  const code = btn.closest('.code-wrap')?.querySelector('code');
  if (!code) return;
  const text = code.textContent ?? '';

  let ok = false;
  try {
    // navigator.clipboard needs a secure context (https:// or localhost).
    // http://127.0.0.1 both qualify; a plain-http LAN IP does NOT, hence the
    // execCommand fallback rather than a silent no-op.
    if (navigator.clipboard?.writeText) {
      await navigator.clipboard.writeText(text);
      ok = true;
    } else {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:-1000px;opacity:0';
      document.body.appendChild(ta);
      ta.select();
      ok = document.execCommand('copy');
      document.body.removeChild(ta);
    }
  } catch (err) {
    console.log('[md] copy failed', err?.message ?? err);
  }

  btn.textContent = ok ? 'copied' : 'failed';
  btn.classList.toggle('ok', ok);
  btn.classList.toggle('bad', !ok);
  clearTimeout(btn._copyTimer);
  btn._copyTimer = setTimeout(() => {
    btn.textContent = 'copy';
    btn.classList.remove('ok', 'bad');
  }, 1600);
}

/**
 * YAML front matter ("---" … "---" at the very top) split from the body, so a
 * preview shows it as one block, not raw lines between two rules.
 * @param {string} src
 * @returns {{front: string, body: string}}
 */
export function splitFrontMatter(src) {
  const m = /^---\r?\n([\s\S]*?)\r?\n---[ \t]*(?:\r?\n|$)/.exec(String(src ?? ''));
  return m ? { front: m[1], body: String(src).slice(m[0].length) } : { front: '', body: String(src ?? '') };
}
