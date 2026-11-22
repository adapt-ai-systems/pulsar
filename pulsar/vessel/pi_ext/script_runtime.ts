/**
 * The runtime behind pi_ext/script.ts: programmatic tool calling.
 *
 * The model writes the body of an async JavaScript function. It runs in a
 * fresh node:vm context whose only way out is the tool functions passed in
 * (read, bash, grep, ...), so one model request can make many tool calls,
 * in loops and with Promise.all. Only what the program prints with text()
 * (and its return value) goes back to the model; the full result of every
 * inner call is written to a log file the model can read later.
 *
 * No pi import here, so tests can run it under plain node with fake tools.
 */

import vm from "node:vm";
import { appendFileSync, mkdirSync } from "node:fs";
import { dirname } from "node:path";

export const DEFAULT_TIMEOUT_S = 300;
export const MAX_TIMEOUT_S = 600;
export const MAX_OUTPUT_CHARS = 30_000;
/** Cap on the synchronous part of the program (a `while(true){}` with no await). */
const SYNC_LIMIT_MS = 5_000;

/** A tool as the program sees it: async, takes the call's arguments. */
export type ScriptTool = (args: any[], signal: AbortSignal) => Promise<unknown>;

export interface ScriptResult {
	text: string;
	isError: boolean;
	calls: Record<string, number>;
	totalCalls: number;
	elapsedMs: number;
	timedOut: boolean;
	truncated: boolean;
	logPath?: string;
}

function show(v: unknown): string {
	if (typeof v === "string") return v;
	if (v === undefined) return "undefined";
	// A bash() result prints as plain text: JSON would escape every newline.
	const r = v as { output?: unknown; code?: unknown } | null;
	if (r && typeof r.output === "string" && typeof r.code === "number") {
		return r.code === 0 ? r.output : `${r.output}\n[exit ${r.code}]`;
	}
	try {
		return JSON.stringify(v, null, 2) ?? String(v);
	} catch {
		return String(v);
	}
}

/** Head + tail of an over-long output, with a marker naming the log. */
export function capOutput(text: string, max: number = MAX_OUTPUT_CHARS, logPath?: string): [string, boolean] {
	if (text.length <= max) return [text, false];
	const head = Math.floor(max * 0.8);
	const tail = max - head;
	const cut = text.length - head - tail;
	const where = logPath ? ` Full tool results: ${logPath}` : "";
	return [
		text.slice(0, head) + `\n\n[... ${cut} chars of script output cut ...${where}]\n\n` + text.slice(text.length - tail),
		true,
	];
}

export async function runScript(
	code: string,
	tools: Record<string, ScriptTool>,
	opts: { timeoutS?: number; logPath?: string; signal?: AbortSignal; maxOutput?: number } = {},
): Promise<ScriptResult> {
	const t0 = Date.now();
	const timeoutS = Math.min(Math.max(Number(opts.timeoutS) || DEFAULT_TIMEOUT_S, 1), MAX_TIMEOUT_S);
	const out: string[] = [];
	const calls: Record<string, number> = {};
	let totalCalls = 0;
	const abort = new AbortController();
	const onOuterAbort = () => abort.abort();
	opts.signal?.addEventListener("abort", onOuterAbort);

	const log = (entry: Record<string, unknown>) => {
		if (!opts.logPath) return;
		try {
			mkdirSync(dirname(opts.logPath), { recursive: true });
			appendFileSync(opts.logPath, JSON.stringify(entry) + "\n");
		} catch {
			// logging must never break the program
		}
	};

	const text = (...vals: unknown[]) => {
		out.push(vals.map(show).join(" "));
	};
	const sandbox: Record<string, unknown> = {
		text,
		console: { log: text, error: text, warn: text, info: text },
		sleep: (ms: number) => new Promise((r) => setTimeout(r, Math.max(0, Number(ms) || 0))),
	};
	for (const [name, fn] of Object.entries(tools)) {
		sandbox[name] = async (...args: any[]) => {
			if (abort.signal.aborted) throw new Error(`${name}: script was stopped`);
			calls[name] = (calls[name] ?? 0) + 1;
			totalCalls += 1;
			const n = totalCalls;
			const started = Date.now();
			try {
				const res = await fn(args, abort.signal);
				log({ n, tool: name, args, ms: Date.now() - started, result: res });
				return res;
			} catch (e: any) {
				log({ n, tool: name, args, ms: Date.now() - started, error: String(e?.message ?? e) });
				throw e;
			}
		};
	}

	let isError = false;
	let timedOut = false;
	let timer: ReturnType<typeof setTimeout> | undefined;
	try {
		const context = vm.createContext(sandbox, { codeGeneration: { strings: false, wasm: false } });
		const program = new vm.Script(`(async () => {\n${code}\n})()`, { filename: "script.js" });
		const running = program.runInContext(context, { timeout: SYNC_LIMIT_MS }) as Promise<unknown>;
		const deadline = new Promise<"timeout">((r) => {
			timer = setTimeout(() => r("timeout"), timeoutS * 1000);
		});
		const outcome = await Promise.race([running.then((v) => ({ v })), deadline]);
		if (outcome === "timeout") {
			timedOut = true;
			abort.abort();
			out.push(`[script stopped after ${timeoutS} s; output so far is above. Tool calls still running were abandoned.]`);
		} else if (outcome.v !== undefined) {
			out.push(show(outcome.v));
		}
	} catch (e: any) {
		isError = true;
		const msg = e?.stack && typeof e.stack === "string" ? e.stack.split("\n").slice(0, 4).join("\n") : String(e?.message ?? e);
		out.push(`[script error] ${msg}`);
	} finally {
		if (timer) clearTimeout(timer);
		opts.signal?.removeEventListener("abort", onOuterAbort);
	}

	const elapsedMs = Date.now() - t0;
	const parts = Object.entries(calls).map(([k, v]) => `${k} ${v}`).join(", ");
	const footer = `[script: ${totalCalls} tool call${totalCalls === 1 ? "" : "s"}${parts ? ` (${parts})` : ""} in ${(elapsedMs / 1000).toFixed(1)} s]`;
	const [body, truncated] = capOutput(out.join("\n"), opts.maxOutput ?? MAX_OUTPUT_CHARS, opts.logPath);
	return {
		text: (body ? body + "\n" : "") + footer,
		isError,
		calls,
		totalCalls,
		elapsedMs,
		timedOut,
		truncated,
		logPath: opts.logPath,
	};
}
