"""Golden transcripts for pulsar-roster.

Recorded from the bash version against a fake daemon; the Python port must pass them
unchanged. Regenerate (only for an intentional output change, in the same commit):
    pytest tests/test_cli_roster.py --update-goldens
"""
import pytest

from tests.cli_golden import REFUSED, Case, goldens_fixture, run_case

NAME = "roster"
goldens = goldens_fixture(NAME)
PULSES = [
    {"id": "aaaaaaaa1111", "state": "running", "vessel": "claude", "model": "opus", "title": "Refactor lead"},
    {"id": "bbbbbbbb2222", "state": "stopped", "vessel": "pi", "model": "glm", "title": "Old Helper",
     "settings": {"role": "helper", "lead_id": "aaaaaaaa1111"}},
    {"id": "cccccccc3333", "state": "running", "vessel": "codex", "model": "gpt", "title": "Spawned helper",
     "settings": {"role": "helper", "spawned_by": "dddddddd4444eeee"}},
    {"id": "dddddddd4444", "state": "stopped", "vessel": "claude", "model": "sonnet", "title": "Archived one",
     "archived_at": "2026-10-01T00:00:00"},
    {"id": "eeeeeeee5555", "state": None, "title": None},
    {"id": "ffffffff6666", "state": "waiting", "vessel": "pi", "model": "qwen", "title": "café Helper-ish",
     "settings": {"role": "helper"}},
    {"id": "gggggggg7777", "state": "running", "vessel": "claude", "model": "opus", "title": "Plain (x)",
     "settings": {"role": "lead"}},
]
OK = (200, {"ok": True, "value": PULSES})

CASES = [
    Case("help-flag", ["--help"]),
    Case("help-short", ["-h"]),
    Case("help-word", ["help"]),
    Case("help-wins-over-earlier-flags", ["--json", "--help"]),
    Case("unknown-flag", ["--wat"]),
    Case("positional-is-unknown", ["x"]),
    Case("grep-missing-regex", ["--grep"]),
    Case("default", [], reply=OK),
    Case("all", ["--all"], reply=OK),
    Case("running", ["--running"], reply=OK),
    Case("running-all", ["--running", "--all"], reply=OK),
    Case("grep-case-insensitive", ["--grep", "helper"], reply=OK),
    Case("grep-regex-anchor", ["--grep", "^plain"], reply=OK),
    Case("grep-no-match", ["--grep", "zzz"], reply=OK),
    Case("grep-empty-regex-matches-all", ["--grep", ""], reply=OK),
    Case("grep-with-all", ["--grep", "arch", "--all"], reply=OK),
    Case("grep-unicode", ["--grep", "CAFÉ"], reply=OK),
    Case("json", ["--json"], reply=OK),
    Case("json-all-running-grep", ["--json", "--all", "--running", "--grep", "e"], reply=OK),
    Case("bad-regex", ["--grep", "("], reply=OK),
    Case("bad-regex-still-requests-first", ["--grep", "[a"], reply=OK),
    Case("bare-list-response", [], reply=(200, PULSES[:2])),
    Case("envelope-value-list", ["--json"], reply=(200, {"value": PULSES[:2]})),
    Case("empty-list", [], reply=(200, {"ok": True, "value": []})),
    Case("empty-list-json", ["--json"], reply=(200, [])),
    Case("reply-201-accepted", [], reply=(201, PULSES[:1])),
    Case("reply-204-empty-is-bad-response", [], reply=(204, b"")),
    Case("reply-200-not-json", [], reply=(200, "<html>nope</html>")),
    Case("reply-200-dict-without-value", [], reply=(200, {"ok": True})),
    Case("reply-200-value-not-list", [], reply=(200, {"value": "x"})),
    Case("reply-200-string-json", [], reply=(200, "\"x\"")),
    Case("reply-200-non-dict-pulse", [], reply=(200, [1])),
    Case("reply-404", [], reply=(404, {"ok": False, "error": {"message": "no"}})),
    Case("reply-500-text", [], reply=(500, "boom")),
    Case("daemon-refused", [], reply=REFUSED),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_transcript(case, tmp_path, goldens):
    record, _ = run_case(NAME, case, tmp_path)
    goldens.check(case.id, record)
