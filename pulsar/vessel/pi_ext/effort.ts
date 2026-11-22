/**
 * pi extension: numeric reasoning effort (DeepSeek dial "80-70-50-30-none")
 * + GLM-5.3 thinking-off rewrite.
 *
 * 1) The DS41 server takes reasoning_effort as a JSON integer 1..100 and
 * refuses the string "80". pi's thinkingLevelMap holds strings only, so
 * the provider map says "80" and this hook turns a numeric-string effort
 * into a number in the outgoing body. It looks at the three places pi
 * puts an effort (chat_template_kwargs.reasoning_effort, reasoning.effort,
 * reasoning_effort). Any other value is left alone.
 *
 * 2) GLM-5.3 and GLM-5.3-Flash have no non-thinking branch in their chat
 * template (only low/high/max, always opening <think>). Sending raw
 * chat_template_kwargs.enable_thinking = false with no reasoning_effort
 * renders Max effort and either disables <think> extraction (vLLM) or
 * closes <think></think> out-of-distribution (legacy alt), leaking raw
 * reasoning tokens straight into chat content. Rewriting GLM-5.3's
 * enable_thinking = false to enable_thinking = true + reasoning_effort =
 * "low" keeps <think>...</think> extraction active on every backend while
 * PiVessel / PiRpcVessel hides reasoning_delta on --thinking off sessions.
 *
 * Loaded by pulsar/vessel/pi.py (`-e <this file>`) for every pi launch.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

const NUMERIC = /^\d{1,3}$/;
const GLM53_MODEL = /^glm-?5\.?3/i;

function toNumber(obj: any, key: string): void {
	if (obj && typeof obj === "object" && typeof obj[key] === "string" && NUMERIC.test(obj[key])) {
		obj[key] = Number(obj[key]);
	}
}

function rewriteGlmThinkingOff(body: any): void {
	if (typeof body.model !== "string" || !GLM53_MODEL.test(body.model)) return;
	const kwargs = body.chat_template_kwargs;
	if (!kwargs || typeof kwargs !== "object") return;
	if (kwargs.enable_thinking === false) {
		kwargs.enable_thinking = true;
		if (kwargs.reasoning_effort === undefined || kwargs.reasoning_effort === "none") {
			kwargs.reasoning_effort = "low";
		}
	}
}

export default function (pi: ExtensionAPI) {
	pi.on("before_provider_request", (event) => {
		const body: any = event.payload;
		if (!body || typeof body !== "object") return undefined;
		rewriteGlmThinkingOff(body);
		toNumber(body.chat_template_kwargs, "reasoning_effort");
		toNumber(body.reasoning, "effort");
		toNumber(body, "reasoning_effort");
		return body;
	});
}
