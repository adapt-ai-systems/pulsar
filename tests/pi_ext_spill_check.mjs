// Node check for pi_ext/spill.ts:  node pi_ext_spill_check.mjs <spill.ts>
// Run with cwd = <tmp>/cli; harness-overrides.json sits in <tmp>.
import { readFileSync, existsSync } from "node:fs";
import { join } from "node:path";

const fails = [];
const check = (name, ok, got) => { if (!ok) fails.push(`${name}: ${JSON.stringify(got)}`); };
const mod = await import(process.argv[2]);

const big = Array.from({ length: 400 }, (_, i) => `line ${i}`).join("\n");
const p = join(process.cwd(), ".pi", "spill", "x.txt");
check("fits", mod.spillText("short", 500, p) === null, null);
const out = mod.spillText(big, 500, p);
check("file whole", readFileSync(p, "utf8") === big, null);
check("names path", out.includes(p), out);
check("head", out.includes("line 0\n"), out);
check("tail", out.endsWith("line 399"), out);
check("small", out.length < 900, out.length);

let handler;
mod.default({ on: (name, fn) => { if (name === "tool_result") handler = fn; } });
check("registered", typeof handler === "function", null);
const ev = (toolName, text) => ({ toolName, content: [{ type: "text", text }] });
check("read untouched", (await handler(ev("read", big))) === undefined, null);
check("small untouched", (await handler(ev("bash", "ok"))) === undefined, null);
const r = await handler(ev("bash", big));
const f = join(process.cwd(), ".pi", "spill", "1.txt");
check("bash spilled", r.content[0].text.includes(f) && existsSync(f), r);
check("image untouched", (await handler({ toolName: "bash", content: [{ type: "image" }] })) === undefined, null);

if (fails.length) { console.log(fails.join("\n")); process.exit(1); }
console.log("OK");
