/**
 * pi extension: the `script` tool — programmatic tool calling.
 *
 * The model sends one JavaScript program; inside it pi's own read, bash,
 * edit, write, grep, find and ls are async functions. Many tool calls run in
 * one model request and only the program's printed output comes back.
 * script_runtime.ts holds the sandbox; this file maps pi's tool definitions
 * onto it.
 *
 * Only the built-ins that are active for this launch (the --tools
 * allowlist) are exposed. When guard.ts is loaded, inner bash commands pass
 * the same pulsar-guard check, 120 s default timeout and cd persistence as a
 * direct bash call (guard.ts publishes globalThis.__pulsarBashPrepare).
 *
 * Off by default. Loaded by pulsar/vessel/pi.py (`-e <this file>`) when the
 * harness override `ptc` is true, and by pi sessions directly.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import {
	createBashToolDefinition,
	createEditToolDefinition,
	createFindToolDefinition,
	createGrepToolDefinition,
	createLsToolDefinition,
	createReadToolDefinition,
	createWriteToolDefinition,
} from "@mariozechner/pi-coding-agent";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { DEFAULT_TIMEOUT_S, MAX_TIMEOUT_S, runScript, type ScriptTool } from "./script_runtime.ts";

const BUILTINS = ["read", "bash", "edit", "write", "grep", "find", "ls"] as const;

const DESCRIPTION =
	"Run a JavaScript program that calls your tools as async functions, so many tool calls happen in ONE step. " +
	"Write the body of an async function: use await, loops, if, try/catch and Promise.all. " +
	"Only what you print with text(...) (or return) comes back to you, so filter and summarize inside the program " +
	"instead of printing whole files you do not need.\n" +
	"Functions (each returns a string unless noted):\n" +
	"- read(path, {offset, limit}?)\n" +
	"- grep(pattern, {path, glob, ignoreCase, literal, context, limit}?)\n" +
	"- find(globPattern, {path, limit}?)\n" +
	"- ls(path?, {limit}?)\n" +
	"- bash(command, {timeout}?) -> {output, code}; a non-zero exit does NOT throw, check code\n" +
	"- edit(path, [{oldText, newText}, ...])  (same rules as the edit tool)\n" +
	"- write(path, content)\n" +
	"- text(...values) prints; a bash result prints as its output (plus [exit N] on failure), other objects as JSON. sleep(ms).\n" +
	"A failed read/grep/edit/write throws; catch it to keep going. There is no require, fs, process or network: " +
	"use the functions above. Use script whenever you would otherwise make two or more tool calls in a row whose " +
	"inputs you already know, e.g. read several files, grep then read the hits, apply edits then run the tests. " +
	"Use a direct tool call for a single step.\n" +
	"Example:\n" +
	"const files = (await find('*.py', {path: 'src'})).split('\\n').filter(Boolean);\n" +
	"for (const f of files) { const t = await read(f); if (t.includes('TODO')) text(f); }\n" +
	"const r = await bash('pytest -q 2>&1 | tail -5'); text(r.code, r.output);";

/** Added to the system prompt on every run: when to reach for script. */
const PROMPT_SECTION = `

# Working with script

Everything you print stays in the conversation and is sent again with every later request, so each direct tool call and each whole file you print costs you many times over. Plan the next several steps, then do them in one script:

- Gathering context: read the files, grep and read the hits in one script. Print only the lines you need (filter, slice, regex), not whole files.
- Making changes: apply every edit you have decided on and run the check in the same script. Print the end of the test output and any failures.
- Repeated work: loop over the files that need the same edit or check.

Use a direct tool call only for one step whose result you need to see before you can decide anything else. Do not put a single command in a script. Several commands in one bash call do not replace a script when you print all of their output.`;

function textOf(result: any): string {
	const parts = Array.isArray(result?.content) ? result.content : [];
	return parts.filter((p: any) => p?.type === "text").map((p: any) => p.text).join("\n");
}

/** Positional or object arguments -> the tool's params object. */
function params(name: string, args: any[]): Record<string, unknown> {
	const [a, b, c] = args;
	const opts = (o: unknown) => (o && typeof o === "object" && !Array.isArray(o) ? (o as Record<string, unknown>) : {});
	if (a && typeof a === "object" && !Array.isArray(a)) return a;
	switch (name) {
		case "read":
			return { path: a, ...opts(b) };
		case "grep":
		case "find":
			return { pattern: a, ...opts(b) };
		case "ls":
			return { ...(a === undefined ? {} : { path: a }), ...opts(b) };
		case "bash":
			return { command: a, ...opts(b) };
		case "edit":
			if (typeof b === "string") return { path: a, edits: [{ oldText: b, newText: c }] };
			return { path: a, edits: Array.isArray(b) ? b : [b] };
		case "write":
			return { path: a, content: b };
	}
	return {};
}

const EXIT_RE = /\n*Command exited with code (\d+)\s*$/;

export default function scriptExtension(pi: ExtensionAPI) {
	pi.on("before_agent_start", async (event: any) => ({ systemPrompt: event.systemPrompt + PROMPT_SECTION }));
	pi.registerTool({
		name: "script",
		label: "script",
		description: DESCRIPTION,
		promptSnippet: "Run a JavaScript program that calls the other tools as async functions (many calls in one step)",
		promptGuidelines: [
			"Batch work with script: when you know the next several tool calls, run them in one script and print only what you need.",
		],
		parameters: {
			type: "object",
			properties: {
				code: { type: "string", description: "Body of an async JavaScript function." },
				timeout: {
					type: "number",
					description: `Seconds to wait for the program (default ${DEFAULT_TIMEOUT_S}, max ${MAX_TIMEOUT_S}).`,
				},
			},
			required: ["code"],
		} as any,
		async execute(toolCallId: string, p: any, signal: AbortSignal | undefined, _onUpdate: any, ctx: any) {
			const cwd: string = ctx?.cwd ?? process.cwd();
			let active: string[] = [];
			try {
				active = pi.getActiveTools();
			} catch {
				active = [...BUILTINS];
			}
			const defs: Record<string, any> = {
				read: () => createReadToolDefinition(cwd),
				bash: () => createBashToolDefinition(cwd),
				edit: () => createEditToolDefinition(cwd),
				write: () => createWriteToolDefinition(cwd),
				grep: () => createGrepToolDefinition(cwd),
				find: () => createFindToolDefinition(cwd),
				ls: () => createLsToolDefinition(cwd),
			};
			const tools: Record<string, ScriptTool> = {};
			let n = 0;
			for (const name of BUILTINS) {
				if (!active.includes(name)) continue;
				const def = defs[name]();
				const call = (args: Record<string, unknown>, sig: AbortSignal) =>
					def.execute(`${toolCallId}.${++n}`, args, sig, undefined, ctx);
				if (name === "bash") {
					tools.bash = async (args, sig) => {
						const input: any = params("bash", args);
						const prepare = (globalThis as any).__pulsarBashPrepare;
						if (typeof prepare === "function") {
							const verdict = await prepare(input);
							if (verdict?.block) return { output: `[blocked] ${verdict.reason}`, code: 2 };
						}
						try {
							return { output: textOf(await call(input, sig)), code: 0 };
						} catch (e: any) {
							const msg = String(e?.message ?? e);
							const m = msg.match(EXIT_RE);
							if (m) return { output: msg.replace(EXIT_RE, ""), code: Number(m[1]) };
							return { output: msg, code: -1 };
						}
					};
				} else {
					tools[name] = async (args, sig) => textOf(await call(params(name, args), sig));
				}
			}
			const res = await runScript(String(p?.code ?? ""), tools, {
				timeoutS: p?.timeout,
				signal,
				logPath: join(tmpdir(), "pi-script", `${process.pid}`, `${toolCallId}.jsonl`),
			});
			// pi marks a tool result as an error only when execute throws.
			if (res.isError) throw new Error(res.text);
			return {
				content: [{ type: "text", text: res.text }],
				details: { calls: res.calls, totalCalls: res.totalCalls, elapsedMs: res.elapsedMs,
					timedOut: res.timedOut, truncated: res.truncated, logPath: res.logPath },
			} as any;
		},
	});
}
