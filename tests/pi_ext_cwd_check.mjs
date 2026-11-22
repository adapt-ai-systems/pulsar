// Node check for guard.ts cwd persistence: load the extension with a fake pi,
// run two bash calls through its tool_call hook, and confirm the second one
// starts in the directory the first one cd'd to.
import { mkdtempSync, mkdirSync, writeFileSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { execFileSync } from "node:child_process";
const cli = mkdtempSync(join(tmpdir(), "pi-cwd-"));
mkdirSync(join(cli, ".pi"), { recursive: true });
mkdirSync(join(cli, "sub", "deep"), { recursive: true });
process.env.PULSE_CLI_DIR = cli;
process.env.PULSAR_GUARD_BIN = "/bin/true";
const mod = await import(process.argv[2]);
const handlers = {};
mod.default({ on: (name, fn) => { handlers[name] = fn; } });
async function call(command) {
  const input = { command };
  await handlers.tool_call({ toolName: "bash", toolCallId: String(Math.random()), input });
  return execFileSync("bash", ["-c", input.command], { cwd: cli, encoding: "utf8" }).trim();
}
const a = await call("cd sub/deep && pwd");
const b = await call("pwd");
const c = await call("cd 'it''s here' 2>/dev/null; pwd; exit 3").catch((e) => `rc=${e.status} ${e.stdout.trim()}`);
const d = await call("cat <<'EOT'\nheredoc ok\nEOT");
console.log(JSON.stringify({ a, b, c, d, cli }));
if (a !== join(cli, "sub", "deep") || b !== a || !c.startsWith("rc=3") || d !== "heredoc ok") { console.error("FAIL"); process.exit(1); }
console.log("OK");
