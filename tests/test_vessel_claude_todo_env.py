"""Claude pulses get the task tools (TaskCreate/TaskUpdate) that feed the pane's TODO card."""
from pulsar.vessel.claude import _child_env


def test_child_env_turns_on_the_todo_tools(monkeypatch):
    monkeypatch.setenv("CLAUDE_CODE_ENABLE_TODO_TOOLS", "0")  # a parent value is stripped with the prefix
    monkeypatch.setenv("CLAUDE_CODE_SOMETHING", "x")
    env = _child_env(None)
    assert env["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "1"
    assert "CLAUDE_CODE_SOMETHING" not in env


def test_extra_env_can_still_override(monkeypatch):
    env = _child_env({"CLAUDE_CODE_ENABLE_TODO_TOOLS": "0"})
    assert env["CLAUDE_CODE_ENABLE_TODO_TOOLS"] == "0"
