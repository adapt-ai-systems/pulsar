/**
 * pi extension: the delete guard.
 *
 * Every bash tool call is handed to `pulsar-guard check` BEFORE it runs; a
 * non-zero exit blocks the call and the reason goes back to the model as the
 * tool error. Rules live in pulsar/cli/guard.py — this file only asks.
 *
 * It also gives a bash call with no `timeout` a 120 s one (pi's bash tool
 * has no default; claude-code uses 120 s). An explicit timeout is kept.
 *
 * And it names an abort_bash kill: PiRpcVessel.abort_bash writes
 * <cwd>/.pi/abort-bash.stamp, then SIGKILLs the running bash calls. A
 * signal exit has no exit code, so pi would report "(no output)" as a
 * success; a bash result that ends after a stamp newer than its start is
 * returned as an error with ABORT_BASH_NOTE instead.
 *
 * And it makes `cd` persist across bash calls (pi runs every call in a fresh
 * shell at the pulse cli dir). After the guard passes, the
 * command is wrapped: enter the directory remembered in <cli>/.pi/cwd, run
 * the command, remember `pwd`, exit with the command's status. The stamp is
 * cleared when this extension loads, so every spawn starts in the cli dir
 * as the context file says. A command that calls `exit` itself, or fails
 * under `set -e`, keeps the previous directory.
 *
 * Loaded by pulsar/vessel/pi.py (`-e <this file>`) for every pi launch.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";
import { execFile } from "node:child_process";
import { existsSync, readFileSync, statSync, unlinkSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { dirname, join, resolve } from "node:path";

function guardBin(): string {
	if (process.env.PULSAR_GUARD_BIN) return process.env.PULSAR_GUARD_BIN;
	try {
		// <repo>/pulsar/vessel/pi_ext/guard.ts -> <repo>/ops/bin/pulsar-guard
		return resolve(dirname(fileURLToPath(import.meta.url)), "../../../ops/bin/pulsar-guard");
	} catch {
		return "pulsar-guard";
	}
}

export const DEFAULT_BASH_TIMEOUT_S = 120;
export const ABORT_BASH_NOTE =
	"[pulsar: this command was killed by abort_bash: it ran silent while a new message waited. It did NOT finish.]";

function cliDir(): string {
	return process.env.PULSE_CLI_DIR || process.cwd();
}

function cwdStamp(): string {
	return join(cliDir(), ".pi", "cwd");
}

/** The directory the previous bash call ended in, when it still exists. */
export function rememberedCwd(): string | undefined {
	try {
		const dir = readFileSync(cwdStamp(), "utf8").trim();
		if (dir && existsSync(dir) && statSync(dir).isDirectory()) return dir;
	} catch {
		// no stamp yet
	}
	return undefined;
}

function shq(s: string): string {
	return "'" + s.replace(/'/g, "'\\''") + "'";
}

/** The command as pi should run it so that `cd` outlives the call. */
export function persistCwd(command: string, stamp: string = cwdStamp()): string {
	const q = shq(stamp);
	return (
		`cd -- "$(cat ${q} 2>/dev/null || pwd)" 2>/dev/null || true\n` +
		`${command}\n` +
		`__pulsar_rc=$?\n` +
		`pwd > ${q} 2>/dev/null\n` +
		`exit $__pulsar_rc`
	);
}

function abortStampMs(): number {
	try {
		return statSync(join(process.cwd(), ".pi", "abort-bash.stamp")).mtimeMs;
	} catch {
		return 0;
	}
}

export default function guardExtension(pi: ExtensionAPI) {
	const started = new Map<string, number>();
	try {
		unlinkSync(cwdStamp()); // a fresh pi process starts in the cli dir
	} catch {
		// nothing remembered
	}
	pi.on("tool_result", async (event) => {
		if (event.toolName !== "bash") return;
		const t0 = started.get(event.toolCallId);
		started.delete(event.toolCallId);
		if (t0 === undefined || abortStampMs() < t0) return;
		return { isError: true, content: [...(event.content ?? []), { type: "text" as const, text: ABORT_BASH_NOTE }] };
	});
	// The script tool (script.ts) runs bash inside a program, past this hook;
	// it calls the same check through this global.
	(globalThis as any).__pulsarBashPrepare = prepareBash;
	pi.on("tool_call", async (event) => {
		if (event.toolName !== "bash") return;
		started.set(event.toolCallId, Date.now());
		// pi passes this same input object to the tool (types.d.ts: "Mutate it in place").
		return await prepareBash(event.input as any);
	});
}

/** Default timeout, pulsar-guard check, cd persistence — mutates `input`. */
export async function prepareBash(input: any): Promise<{ block?: boolean; reason?: string } | undefined> {
	if (input && input.timeout == null) input.timeout = DEFAULT_BASH_TIMEOUT_S;
	const command = String(input?.command ?? "");
	if (!command.trim()) return;
	const cwd = rememberedCwd() ?? cliDir();
	return await new Promise<{ block?: boolean; reason?: string } | undefined>((done) => {
		execFile(
			guardBin(),
			["check", "--cwd", cwd, "--", command],
			{ cwd, maxBuffer: 256 * 1024, timeout: 10_000 },
			(err, stdout, stderr) => {
				const code = err && typeof (err as any).code === "number" ? (err as any).code : err ? 1 : 0;
				if (code === 0) {
					input.command = persistCwd(command);
					return done(undefined);
				}
				const text = (stdout + stderr).trim();
				if (code === 2) return done({ block: true, reason: text || "pulsar-guard refused this command" });
				// The guard itself failed (missing binary, crash): fail CLOSED for deletes only.
				if (/\b(rm|rmdir|rmtree|-delete)\b/.test(command)) {
					return done({ block: true, reason: `pulsar-guard could not run (exit ${code}): ${text}. Delete refused.` });
				}
				input.command = persistCwd(command);
				done(undefined);
			},
		);
	});
}
