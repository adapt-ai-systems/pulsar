"""Offline tests for patch install/upgrade and actual JavaScript request budgets."""
import contextlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

PATH = Path(__file__).with_name("context-fit-max-tokens.py")
spec = importlib.util.spec_from_file_location("context_fit", PATH)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


def source(helper=""):
    call = mod.PATCHED_CALL if helper else mod.STOCK_CALL
    return helper + mod.STOCK_EXPORT + call + "};\n"


class InstallTests(unittest.TestCase):
    def run_patch(self, content, mode="apply"):
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory) / "provider.js"
            target.write_text(content)
            with patch.object(mod, "TARGET", target), patch.object(sys, "argv", [str(PATH), mode]), contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
                result = mod.main()
            return result, target.read_text()

    def test_stock_install(self):
        result, installed = self.run_patch(source())
        self.assertEqual(result, 0)
        self.assertEqual(installed, source(mod.HELPER))

    def test_legacy_upgrade(self):
        result, installed = self.run_patch(source(mod.LEGACY_HELPER))
        self.assertEqual(result, 0)
        self.assertEqual(installed, source(mod.HELPER))
        self.assertNotIn("pulsarEstimatePromptTokens", installed)

    def test_idempotent(self):
        self.assertEqual(self.run_patch(source(mod.HELPER)), (0, source(mod.HELPER)))

    def test_check_rejects_legacy_and_stock_without_writes(self):
        for content in (source(), source(mod.LEGACY_HELPER)):
            with self.subTest(content=content[:30]):
                self.assertEqual(self.run_patch(content, "check"), (1, content))

    def test_check_accepts_v2(self):
        self.assertEqual(self.run_patch(source(mod.HELPER), "check"), (0, source(mod.HELPER)))

    def test_unknown_version_not_overwritten(self):
        content = source(mod.LEGACY_HELPER.replace("const PULSAR_CTX_MARGIN = 64", "const PULSAR_CTX_MARGIN = 99"))
        self.assertEqual(self.run_patch(content), (3, content))

    def test_missing_or_duplicate_anchor_not_overwritten(self):
        for content in ("upstream changed\n", source() + mod.STOCK_CALL):
            self.assertEqual(self.run_patch(content), (3, content))


class RequestTests(unittest.TestCase):
    def run_js(self, js, helper=None):
        result = subprocess.run(["node", "--input-type=module"], input=(helper or mod.HELPER) + '\n' + JS_MOCK + js, text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return json.loads(result.stdout)

    def test_legacy_reproduces_469_and_374_token_caps(self):
        for budget in (469, 374):
            # Serialized messages length is exactly 3 * estimated prompt.
            js = f'''
const messages = [{{role:"user",content:""}}];
messages[0].content = "x".repeat(3 * (200000 - 64 - {budget}) - JSON.stringify(messages).length);
const c = mock([]);
await pulsarCreateWithContextFit(c.client, {{contextWindow:200000}}, {{messages, max_tokens:32768}}, {{}});
console.log(JSON.stringify(c.calls[0].max_tokens));
'''
            # LEGACY is the installed-v2 snapshot (upgrade source of record).
            # v2 dropped the char/3 pre-clamp the original legacy build had,
            # so both v2 and v3 send the full budget untouched.
            self.assertEqual(self.run_js(js, mod.LEGACY_HELPER), 32768)
            self.assertEqual(self.run_js(js), 32768)

    def test_oversized_text_tools_and_images_do_not_guess_token_counts(self):
        for field in ("max_tokens", "max_completion_tokens"):
            js = f'''
const c = mock([]);
const params = {{messages:[{{role:"user",content:"x".repeat(900000)}}], tools:[{{description:"x".repeat(900000)}}], {field}:32768}};
await pulsarCreateWithContextFit(c.client, {{contextWindow:200000}}, params, {{}});
console.log(JSON.stringify(c.calls.map(p => p.{field})));
'''
            self.assertEqual(self.run_js(js), [32768])

    def test_exact_server_counts_retry_both_fields(self):
        for field in ("max_tokens", "max_completion_tokens"):
            js = f'''
const c = mock([contextError(200000,131072,148174)]);
const options = {{timeout:1234}};
await pulsarCreateWithContextFit(c.client, {{contextWindow:999999}}, {{{field}:131072}}, options);
assert.equal(c.options[0].timeout, options.timeout);
assert.equal(c.options[1].timeout, options.timeout);
if (!c.options[1].headers || !c.options[1].headers["x-client-request-id"]) throw new Error("retry must mint a fresh request id header");
console.log(JSON.stringify(c.calls.map(p => p.{field})));
'''
            self.assertEqual(self.run_js(js), [131072, 51762])

    def test_no_retry_for_unrelated_errors_or_missing_budget(self):
        for error, params in (("new Error('429 rate limit')", "{max_tokens:32768}"), ("contextError(200000,131072,148174)", "{}")):
            js = f'''
const error = {error}; const c = mock([error]);
await assert.rejects(() => pulsarCreateWithContextFit(c.client, {{}}, {params}, {{}}), e => e === error);
console.log(JSON.stringify(c.calls.length));
'''
            self.assertEqual(self.run_js(js), 1)

    def test_retry_floor_and_never_increase_budget(self):
        # (1000,700): fit 236 < floor 256 -> throw, 1 call.
        # (1000,680): fit EXACTLY 256 (v3 floor is inclusive) -> retry, 2 calls.
        # (200000,148174): naive fit 51762 > budget 32768 -> v3 halves to
        #   16384 and retries (2 calls); v2 threw at 1.
        for context, prompt, expected_calls in ((1000, 700, 1), (1000, 680, 2), (200000, 148174, 2)):
            js = f'''
const c = mock([contextError({context},32768,{prompt})]);
try {{ await pulsarCreateWithContextFit(c.client, {{}}, {{max_tokens:32768}}, {{}}); }} catch {{}}
console.log(JSON.stringify(c.calls.length));
'''
            self.assertEqual(self.run_js(js), expected_calls)

    def test_repeat_400_halves_until_floor_then_throws(self):
        # v3: the server-reported input count is TRUNCATED, so a second 400
        # with the same numbers is EXPECTED. While the reported fit still
        # exceeds the current budget the budget halves each round
        # (32768 -> 16384 -> ... -> 512 -> 256), then the next halving lands
        # under the floor and the original error throws. Bounded ladder.
        js = """
const ctx = 200000, reported = 190000;
const msg = `This model's maximum context length is ${ctx} tokens. However, you requested 32768 output tokens and your prompt contains at least ${reported} input tokens`;
const c = mock(Array.from({length: 30}, () => new Error(msg)));
let thrown = null;
try { await pulsarCreateWithContextFit(c.client, {}, {max_tokens:32768}, {}); } catch (e) { thrown = e; }
assert.ok(thrown, "must eventually throw");
const budgets = c.calls.map(p => p.max_tokens);
console.log(JSON.stringify(budgets));
"""
        # fit0 = 200000-190000-64 = 9936 (< 32768) -> retried at 9936;
        # then 9936 >= 9936 -> halve: 4968, 2484, 1242, 621, 310, 155 < 256
        # -> throw. calls: 32768, 9936, 4968, 2484, 1242, 621, 310 = 7.
        self.assertEqual(self.run_js(js),
                         [32768, 9936, 4968, 2484, 1242, 621, 310])


JS_MOCK = '''
import assert from "node:assert/strict";
function contextError(ctx, requested, input) {
    return new Error(`This model's maximum context length is ${ctx} tokens. However, you requested ${requested} output tokens and your prompt contains at least ${input} input tokens`);
}
function mock(errors) {
    const calls = [], options = [];
    return {calls, options, client:{chat:{completions:{create(params, opts) {
        calls.push(structuredClone(params));
        options.push(JSON.parse(JSON.stringify({ ...opts, headers: opts && opts.headers })));
        const error = errors.shift();
        return {async withResponse() { if (error) throw error; return {data:[],response:{}}; }};
    }}}}};
}
'''

if __name__ == "__main__":
    unittest.main()
