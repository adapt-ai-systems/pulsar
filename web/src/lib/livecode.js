// Live code card: turns the argument JSON of the tool call being generated —
// code.json, streamed by the runner — into what the pane shows: the file path and the code as it is
// typed, the command for a shell call, the raw JSON for anything else.
// The JSON is almost always INCOMPLETE (that is the point), so this reads it
// with a tolerant scanner instead of JSON.parse.

const ESC = { n: '\n', t: '\t', r: '\r', b: '\b', f: '\f', '"': '"', '\\': '\\', '/': '/' };

/**
 * Read a JSON string starting just after its opening quote. Returns the
 * decoded value and the index after the closing quote (or text.length when
 * the string is still open). A dangling escape at the end is dropped.
 * @param {string} text
 * @param {number} i
 */
function readString(text, i) {
  let out = '';
  while (i < text.length) {
    const c = text[i];
    if (c === '"') return { value: out, end: i + 1, closed: true };
    if (c === '\\') {
      const e = text[i + 1];
      if (e === undefined) break;
      if (e === 'u') {
        const hex = text.slice(i + 2, i + 6);
        if (hex.length < 4) break;
        out += String.fromCharCode(parseInt(hex, 16) || 0);
        i += 6;
        continue;
      }
      out += ESC[e] ?? e;
      i += 2;
      continue;
    }
    out += c;
    i++;
  }
  return { value: out, end: text.length, closed: false };
}

/** Skip a non-string JSON value (number, object, array, literal); returns its end. */
function skipValue(text, i) {
  let depth = 0;
  while (i < text.length) {
    const c = text[i];
    if (c === '"') {
      i = readString(text, i + 1).end;
      continue;
    }
    if (c === '{' || c === '[') depth++;
    else if (c === '}' || c === ']') {
      if (depth === 0) return i;
      depth--;
    } else if (c === ',' && depth === 0) return i;
    i++;
  }
  return i;
}

/**
 * The top-level fields of a possibly-truncated JSON object. String values
 * are decoded (the last one may be partial); other values are their raw text.
 * @param {string} text
 * @returns {Record<string, string>}
 */
export function partialFields(text) {
  /** @type {Record<string, string>} */
  const out = {};
  if (typeof text !== 'string') return out;
  let i = text.indexOf('{');
  if (i < 0) return out;
  i++;
  while (i < text.length) {
    while (i < text.length && /[\s,]/.test(text[i])) i++;
    if (text[i] !== '"') break;
    const key = readString(text, i + 1);
    if (!key.closed) break;
    i = key.end;
    while (i < text.length && /\s/.test(text[i])) i++;
    if (text[i] !== ':') break;
    i++;
    while (i < text.length && /\s/.test(text[i])) i++;
    if (i >= text.length) break;
    if (text[i] === '"') {
      const val = readString(text, i + 1);
      out[key.value] = val.value;
      if (!val.closed) break;
      i = val.end;
    } else {
      const end = skipValue(text, i);
      out[key.value] = text.slice(i, end).trim();
      i = end;
    }
  }
  return out;
}

const pick = (f, ...keys) => {
  for (const k of keys) if (typeof f[k] === 'string') return f[k];
  return null;
};

/**
 * What the live code card shows for one streaming tool call.
 * `lines` is set for an edit: each line signed '-' (old) or '+' (new).
 * @param {{name?: string|null, text?: string}|null|undefined} code
 * @returns {{tool: string, path: string|null, body: string, lines: {sign: string, text: string}[]|null}|null}
 */
export function liveCodeView(code) {
  if (!code || typeof code.text !== 'string') return null;
  const tool = code.name || 'tool';
  const f = partialFields(code.text);
  const path = pick(f, 'file_path', 'path', 'notebook_path');
  const low = tool.toLowerCase();
  if (low === 'write' || low === 'notebookedit' || low === 'create') {
    const body = pick(f, 'content', 'new_source', 'text');
    if (body !== null) return { tool, path, body, lines: null };
  }
  if (low === 'edit') {
    const oldS = pick(f, 'old_string', 'oldText', 'old_text');
    const newS = pick(f, 'new_string', 'newText', 'new_text');
    if (oldS !== null || newS !== null) {
      const lines = [];
      if (oldS) for (const t of oldS.split('\n')) lines.push({ sign: '-', text: t });
      if (newS) for (const t of newS.split('\n')) lines.push({ sign: '+', text: t });
      return { tool, path, body: '', lines };
    }
  }
  if (low === 'bash' || low === 'shell') {
    const cmd = pick(f, 'command', 'cmd');
    if (cmd !== null) return { tool, path: pick(f, 'description'), body: cmd, lines: null };
  }
  return { tool, path, body: code.text, lines: null };
}
