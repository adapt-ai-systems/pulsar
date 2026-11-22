/**
 * pi extension: todo_write — the pulse's own task list.
 *
 * grok, codex, and qwen already emit a `todo_write` tool call with this exact
 * shape; pi has no built-in todo tool, so this registers one. The call reaches
 * Pulsar through pi's ordinary tool_execution_start event and the pane folds
 * the newest call into the TODO card next to the idle/working strip — nothing
 * else in the pipeline knows this tool exists.
 *
 * Loaded by pulsar/vessel/pi.py (`-e <this file>`) for every pi launch.
 */

import type { ExtensionAPI } from "@mariozechner/pi-coding-agent";

interface Todo {
	id: string;
	content: string;
	status: "pending" | "in_progress" | "completed" | "cancelled";
	group?: string;
}

export default function todoExtension(pi: ExtensionAPI) {
	let current: Todo[] = [];

	pi.registerTool({
		name: "todo_write",
		label: "todo",
		description:
			"Maintain your task list for the current job. Call it with the FULL list every time " +
			"anything changes: when you plan the work (all items pending), when you start an item " +
			"(exactly one in_progress), and the moment an item is done (completed). The user watches " +
			"this list live, so keep it current — do not batch updates until the end. Optional `group` " +
			"puts items under a heading (e.g. \"Core\", \"Tests\", \"Docs\").",
		parameters: {
			type: "object",
			properties: {
				todos: {
					type: "array",
					description: "The complete, updated task list (replaces the previous one).",
					items: {
						type: "object",
						properties: {
							id: { type: "string", description: "Stable short id, e.g. \"1\", \"t3\"." },
							content: { type: "string", description: "Imperative, one line." },
							status: { type: "string", enum: ["pending", "in_progress", "completed", "cancelled"] },
							group: { type: "string", description: "Optional section heading." },
						},
						required: ["id", "content", "status"],
					},
				},
			},
			required: ["todos"],
		},
		async execute(_toolCallId: string, params: any) {
			const todos: Todo[] = Array.isArray(params?.todos) ? params.todos : [];
			current = todos;
			const done = todos.filter((t) => t.status === "completed").length;
			const active = todos.find((t) => t.status === "in_progress");
			const lines = todos.map((t) => {
				const box = t.status === "completed" ? "[x]" : t.status === "in_progress" ? "[>]" : t.status === "cancelled" ? "[-]" : "[ ]";
				return `${box} ${t.id}: ${t.content}`;
			});
			const summary = `todo: ${done}/${todos.length} done${active ? ` — now: ${active.content}` : ""}`;
			return {
				content: [{ type: "text", text: [summary, ...lines].join("\n") }],
				details: { count: todos.length, done },
			};
		},
	});
}
