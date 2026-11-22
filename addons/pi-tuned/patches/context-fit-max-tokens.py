#!/usr/bin/env python3
"""Pulsar patch: make max_tokens fit the model's context window.

vLLM rejects any request where prompt + max_tokens exceeds max_model_len
(400 "This model's maximum context length is N tokens. However, you
requested M output tokens and your prompt contains at least P input
tokens"). With models.json maxTokens 131072 on a 200K context model, that
capped the usable prompt at ~69K and later turns failed on 400.

Patch (openai-completions.js, streamOpenAICompletions):
  Send the requested output budget unchanged. If the server answers the
  vLLM context-length 400, parse its exact counts, set max_tokens to
  context - input - 64, and retry (or halve on repeated truncated-count
  400s down to the inclusive floor of 256).

  v2 removes the chars/3 pre-clamp. That estimate can overcount a long
  prompt and silently leave only a few hundred output tokens, even when
  the actual prompt plus requested output fits the server window. That
  can cut off long reasoning turns with no reply. The server's token
  counts, not a character heuristic or stale local contextWindow, must
  decide whether a smaller output budget is necessary.

Same contract as the other patches here: idempotent, marker-checked,
run by postinstall.  Modes: apply (default) | check (exit 1 when missing)
"""

from __future__ import annotations

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE.parent / "node_modules" / "@mariozechner" / "pi-ai" / "dist" / "providers" / "openai-completions.js"

MARKER = "pulsar: context-fit max_tokens"

STOCK_CALL = """            const { data: openaiStream, response } = await client.chat.completions
                .create(params, requestOptions)
                .withResponse();
"""

PATCHED_CALL = """            // pulsar: context-fit max_tokens — see patches/context-fit-max-tokens.py
            const { data: openaiStream, response } = await pulsarCreateWithContextFit(client, model, params, requestOptions);
"""

STOCK_EXPORT = "export const streamOpenAICompletions = (model, context, options) => {\n"

LEGACY_HELPER = r'''
// pulsar: context-fit max_tokens — vLLM rejects prompt + max_tokens > max_model_len.
// v2: only server-reported token counts may reduce the output budget.
const PULSAR_CTX_FLOOR = 256;
const PULSAR_CTX_MARGIN = 64;
function pulsarMaxTokensField(params) {
    if (typeof params.max_tokens === "number") return "max_tokens";
    if (typeof params.max_completion_tokens === "number") return "max_completion_tokens";
    return null;
}
function pulsarParseContextError(error) {
    const text = String(error?.message ?? error ?? "");
    const m = text.match(/maximum context length is (\d+) tokens[\s\S]*?requested (\d+) output tokens[\s\S]*?at least (\d+) input tokens/);
    if (!m) return null;
    return { contextWindow: Number(m[1]), requested: Number(m[2]), input: Number(m[3]) };
}
async function pulsarCreateWithContextFit(client, model, params, requestOptions) {
    const field = pulsarMaxTokensField(params);
    try {
        return await client.chat.completions.create(params, requestOptions).withResponse();
    }
    catch (error) {
        const parsed = field ? pulsarParseContextError(error) : null;
        if (!parsed) throw error;
        const fit = parsed.contextWindow - parsed.input - PULSAR_CTX_MARGIN;
        if (fit < PULSAR_CTX_FLOOR || fit >= params[field]) throw error;
        console.error(`[pulsar context-fit] server 400: window ${parsed.contextWindow}, prompt ${parsed.input}, requested ${parsed.requested}; retrying with ${field}=${fit}`);
        params[field] = fit;
        return await client.chat.completions.create(params, requestOptions).withResponse();
    }
}'''


HELPER = r'''
// pulsar: context-fit max_tokens — vLLM rejects prompt + max_tokens > max_model_len.
// v2: only server-reported token counts may reduce the output budget.
// v3:
// (a) THE FLOOR GUARD WAS EXCLUSIVE. A fit landing EXACTLY on the floor was
//     thrown instead of retried; the floor is now inclusive.
// (b) THE RETRY MUST NOT REPLAY THE REQUEST ID. pi-ai stamps a constant
//     `x-client-request-id: <sessionId>` header on every request. Servers
//     that track admitted ids answer a replay with 400
//     `Request id '...' has already been executed` — a sub-second rejection,
//     never a compute, which can auto-retry pulses into a wedge. The fresh id
//     MUST be set in requestOptions.headers, not params: pi-ai sets
//     params-level headers AFTER onPayload, so a params mutation is silently
//     overwritten.
// (c) THE 400's "at least N input tokens" is vLLM's TRUNCATED prompt length,
//     not the true one — the real prompt can sit many k over the window, so
//     a single window-N-margin retry can still 400. When the reported fit no
//     longer fits under the current budget the budget halves each round
//     (bounded ladder), so an over-grown pulse degrades instead of dying at
//     the ceiling. A first in-range 400 is retried without consuming a slot
//     (one retry per fresh overflow report); repeat 400s and id rejections
//     share the bounded PULSAR_CTX_MAX_RETRIES ladder.
const PULSAR_CTX_FLOOR = 256;
const PULSAR_CTX_MARGIN = 64;
const PULSAR_CTX_MAX_RETRIES = 6;
function pulsarMaxTokensField(params) {
    if (typeof params.max_tokens === "number") return "max_tokens";
    if (typeof params.max_completion_tokens === "number") return "max_completion_tokens";
    return null;
}
function pulsarParseContextError(error) {
    const text = String(error?.message ?? error ?? "");
    const m = text.match(/maximum context length is (\d+) tokens[\s\S]*?requested (\d+) output tokens[\s\S]*?at least (\d+) input tokens/);
    if (!m) return null;
    return { contextWindow: Number(m[1]), requested: Number(m[2]), input: Number(m[3]) };
}
function pulsarIsDuplicateIdError(error) {
    return /has already been executed/i.test(String(error?.message ?? error ?? ""));
}
async function pulsarCreateWithContextFit(client, model, params, requestOptions) {
    const field = pulsarMaxTokensField(params);
    let fresh = `pulsar-${Date.now()}-${Math.random().toString(36).slice(2, 10)}`;
    let retries = 0;
    requestOptions = {
        ...requestOptions,
        headers: { ...(requestOptions?.headers || {}), "x-client-request-id": fresh },
    };
    for (;;) {
        const options = requestOptions;
        try {
            return await client.chat.completions.create(params, options).withResponse();
        }
        catch (error) {
            if (field && pulsarIsDuplicateIdError(error)) {
                if (++retries > PULSAR_CTX_MAX_RETRIES) throw error;
                fresh = `pulsar-r${retries}-${Date.now().toString(36)}`;
                requestOptions = { ...requestOptions, headers: { ...requestOptions.headers, "x-client-request-id": fresh } };
                console.error(`[pulsar context-fit] server rejected a replayed request id; retrying with a fresh id`);
                continue;
            }
            const parsed = field ? pulsarParseContextError(error) : null;
            if (!parsed) throw error;
            // One retry PER reported overflow: a repeat 400 with the same
            // numbers means the halving ladder, not fresh information, so
            // only an over-ceiling fit (or the ladder) consumes a retry slot.
            const fit0 = parsed.contextWindow - parsed.input - PULSAR_CTX_MARGIN;
            const over = fit0 >= params[field];
            if (over) retries += 1;
            if (retries > PULSAR_CTX_MAX_RETRIES) throw error;
            let fit = fit0;
            if (over) {
                // Server's count says the budget already fits: the true
                // prompt is larger than the truncated count it reported.
                // Halve so every retry makes progress.
                fit = Math.floor(params[field] / 2);
            }
            if (fit < PULSAR_CTX_FLOOR) throw error;
            console.error(`[pulsar context-fit] server 400: window ${parsed.contextWindow}, prompt ${parsed.input}, requested ${parsed.requested}; retrying with ${field}=${fit}`);
            params[field] = fit;
        }
    }
}'''


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "apply"
    try:
        source = TARGET.read_text(encoding="utf-8")
    except OSError as e:
        print(f"pi context-fit patch: cannot read {TARGET}: {e}", file=sys.stderr)
        return 2

    if HELPER in source and source.count(PATCHED_CALL) == 1:
        if mode == "check":
            print("pi context-fit patch: v3 present")
        return 0
    if MARKER in source:
        # An older installed build. If it is exactly the recorded v2 bytes,
        # upgrade in place; check mode reports MISSING and never writes.
        if source.count(LEGACY_HELPER) == 1 and source.count(PATCHED_CALL) == 1:
            if mode == "check":
                print(f"pi context-fit patch: v3 MISSING from {TARGET} (v2 installed, would upgrade)",
                      file=sys.stderr)
                return 1
            TARGET.write_text(source.replace(LEGACY_HELPER, HELPER, 1), encoding="utf-8")
            print(f"pi context-fit patch: upgraded to v3 in {TARGET}")
            return 0
        print(f"pi context-fit patch: unknown installed version in {TARGET} — re-derive",
              file=sys.stderr)
        return 3
    if mode == "check":
        print(f"pi context-fit patch: v3 MISSING from {TARGET}", file=sys.stderr)
        return 1

    for anchor, name in ((STOCK_CALL, "create call"), (STOCK_EXPORT, "export")):
        if source.count(anchor) != 1:
            print(f"pi context-fit patch: {name} anchor found {source.count(anchor)}x (want 1)"
                  " — pi-ai updated past this patch; re-derive", file=sys.stderr)
            return 3
    source = source.replace(STOCK_CALL, PATCHED_CALL, 1)
    source = source.replace(STOCK_EXPORT, HELPER + STOCK_EXPORT, 1)
    TARGET.write_text(source, encoding="utf-8")
    print(f"pi context-fit patch: applied to {TARGET}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
