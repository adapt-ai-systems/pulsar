/**
 * pi extension: spill big tool outputs to a file. A bash/grep/find/ls result longer than
 * `spill_chars` is written whole to <cli>/.pi/spill/<n>.txt; the model gets
 * the head and tail plus the path, and reads or greps the file for the part
 * it needs. `read` results are left alone: the file is already on disk and
 * read has offset/limit.
 *
 * Loaded by pulsar/vessel/pi.py (`-e <this file>`) only when the harness
 * override `spill_chars` is set; the limit is read from
 * <pulse home>/harness-overrides.json.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { mkdirSync, readFileSync, writeFileSync } from "node:fs";
import { dirname, join } from "node:path";

const TOOLS = new Set(["bash", "grep", "find", "ls"]);
/** Used when the file is loaded by hand (pi-args.json) with no override. */
const DEFAULT_LIMIT = 2000;

function cliDir(): string {
	return process.env.PULSE_CLI_DIR || process.cwd();
}

function limitFromOverrides(): number {
	try {
		const v = JSON.parse(readFileSync(join(dirname(cliDir()), "harness-overrides.json"), "utf8")).spill_chars;
		return Number.isInteger(v) && v >= 500 ? v : DEFAULT_LIMIT;
	} catch {
		return DEFAULT_LIMIT;
	}
}

/** The text the model sees for an output over `limit`; null when it fits. */
export function spillText(text: string, limit: number, path: string): string | null {
	if (text.length <= limit) return null;
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, text);
	const head = Math.floor(limit * 0.7);
	const tail = limit - head;
	const lines = text.split("\n").length;
	return (
		`[Output was ${text.length} chars, ${lines} lines. Full output saved to ${path}. ` +
		`Below: the first ${head} and last ${tail} chars. To see more, grep that file ` +
		`or read it with offset/limit. Do not cat the whole file.]\n` +
		text.slice(0, head) +
		`\n[... ${text.length - limit} chars omitted ...]\n` +
		text.slice(-tail)
	);
}

export default function spillExtension(pi: ExtensionAPI) {
	const limit = limitFromOverrides();
	let n = 0;
	pi.on("tool_result", async (event) => {
		if (!TOOLS.has(event.toolName)) return;
		const content = event.content ?? [];
		if (content.some((c) => c.type !== "text")) return;
		const text = content.map((c: any) => c.text).join("\n");
		const out = spillText(text, limit, join(cliDir(), ".pi", "spill", `${n + 1}.txt`));
		if (out === null) return;
		n++;
		return { content: [{ type: "text" as const, text: out }] };
	});
}
