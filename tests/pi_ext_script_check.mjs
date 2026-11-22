// Node check for the script tool (programmatic tool calling).
//   node pi_ext_script_check.mjs <script_runtime.ts> [<script.ts> <pi package dir>]
// Part 1 runs script_runtime.ts with fake tools. Part 2 (when a pi package
// dir is given) loads script.ts against pi's real tool definitions in a temp
// dir and drives write/read/grep/ls/edit/bash from one program.
import { mkdtempSync, readFileSync, existsSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { register } from "node:module";
import { pathToFileURL } from "node:url";

const fails = [];
const check = (name, ok, got) => { if (!ok) fails.push(`${name}: ${JSON.stringify(got)}`); };

const rt = await import(process.argv[2]);
const dir = mkdtempSync(join(tmpdir(), "pi-script-"));
const seen = [];
const tools = {
	read: async ([p]) => { seen.push(p); return `content of ${p}`; },
	grep: async ([pat]) => `a.py:1:${pat}\nb.py:2:${pat}`,
	bash: async ([cmd]) => ({ output: `ran ${cmd}`, code: cmd === "false" ? 1 : 0 }),
};

// 1. loops + Promise.all + text + return; only printed output comes back
let r = await rt.runScript(`
  const hits = (await grep("TODO")).split("\\n").map(l => l.split(":")[0]);
  const bodies = await Promise.all(hits.map(f => read(f)));
  text("files", hits);
  const b = await bash("false");
  text("code", b.code);
  return bodies.length;`, tools, { logPath: join(dir, "log.jsonl") });
check("loop text", r.text.includes('"a.py"') && r.text.includes("code 1") && r.text.includes("\n2\n"), r.text);
check("loop counts", r.totalCalls === 4 && r.calls.read === 2 && r.calls.grep === 1 && r.calls.bash === 1, r.calls);
check("footer", /\[script: 4 tool calls \(grep 1, read 2, bash 1\) in [\d.]+ s\]$/.test(r.text), r.text);
check("raw results stay out", !r.text.includes("content of a.py"), r.text);
check("log file", existsSync(join(dir, "log.jsonl")) && readFileSync(join(dir, "log.jsonl"), "utf8").includes("content of a.py"), null);
check("not error", !r.isError, r);

// 2. a throw is an error, output so far is kept
r = await rt.runScript(`text("before"); throw new Error("boom");`, tools);
check("throw", r.isError && r.text.includes("before") && r.text.includes("boom"), r.text);

// 3. syntax error
r = await rt.runScript(`this is not js`, tools);
check("syntax", r.isError && r.text.includes("[script error]"), r.text);

// 4. no way out of the sandbox
r = await rt.runScript(`text(typeof require, typeof process, typeof fetch); try { eval("1") } catch (e) { text("eval blocked") }`, tools);
check("sandbox", r.text.startsWith("undefined undefined undefined") && r.text.includes("eval blocked"), r.text);

// 5. async timeout keeps partial output
r = await rt.runScript(`text("started"); await sleep(5000); text("never");`, tools, { timeoutS: 1 });
check("timeout", r.timedOut && !r.isError && r.text.includes("started") && !r.text.includes("never"), r.text);

// 6. a synchronous infinite loop is stopped
r = await rt.runScript(`while (true) {}`, tools);
check("sync loop", r.isError, r.text);

// 7. output cap
r = await rt.runScript(`text("x".repeat(50000))`, tools, { maxOutput: 1000, logPath: "/tmp/L" });
check("cap", r.truncated && r.text.length < 1300 && r.text.includes("chars of script output cut") && r.text.includes("/tmp/L"), r.text.length);

// Part 2: real pi tools through script.ts
if (process.argv[3] && process.argv[4]) {
	const piIndex = pathToFileURL(join(process.argv[4], "dist", "index.js")).href;
	register("data:text/javascript," + encodeURIComponent(`
		export async function resolve(spec, ctx, next) {
			if (spec === "@mariozechner/pi-coding-agent") return { url: ${JSON.stringify(piIndex)}, shortCircuit: true };
			return next(spec, ctx);
		}`));
	const mod = await import(process.argv[3]);
	let tool;
	let hook;
	mod.default({ on: (ev, h) => { if (ev === "before_agent_start") hook = h; }, registerTool: (t) => { tool = t; }, getActiveTools: () => ["read", "bash", "edit", "write", "grep", "ls", "script"] });
	check("registered", tool?.name === "script", tool?.name);
	const ws = mkdtempSync(join(tmpdir(), "pi-script-ws-"));
	const res = await tool.execute("t1", { code: `
		await write("a.txt", "alpha\\nbeta\\n");
		text((await read("a.txt")).trim());
		text("grep:", (await grep("beta", { literal: true })).trim());
		await edit("a.txt", [{ oldText: "beta", newText: "gamma" }]);
		const ok = await bash("grep -c gamma a.txt");
		const bad = await bash("exit 7");
		text("bash", ok.code, ok.output.trim(), bad.code);
		text(typeof find);
		try { await read("missing.txt") } catch (e) { text("caught missing") }
	` }, undefined, undefined, { cwd: ws });
	const t = res.content[0].text;
	check("pi read", t.includes("alpha\nbeta"), t);
	check("pi grep", /grep: a\.txt:2: ?beta/.test(t), t);
	check("pi edit+bash", t.includes("bash 0 1 7"), t);
	const added = await hook({ systemPrompt: "BASE" });
	check("prompt section", added.systemPrompt.startsWith("BASE") && added.systemPrompt.includes("# Working with script"), added);
	check("inactive tool hidden", t.includes("\nundefined\n"), t);
	check("pi throw caught", t.includes("caught missing"), t);
	check("pi details", res.details.totalCalls === 7, res.details);
	let threw = false;
	try { await tool.execute("t2", { code: "throw new Error('nope')" }, undefined, undefined, { cwd: ws }); } catch (e) { threw = String(e.message).includes("nope"); }
	check("pi error throws", threw, threw);
}

if (fails.length) { console.error("FAIL\n" + fails.join("\n")); process.exit(1); }
const raw = await rt.runScript(`text(await bash("x")); text(await bash("false")); text({a: 1});`, tools);
check("bash result prints raw", raw.text.includes("ran x\nran false\n[exit 1]\n{\n  \"a\": 1\n}"), raw.text);

console.log("OK");
