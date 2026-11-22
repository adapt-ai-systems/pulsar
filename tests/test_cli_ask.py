"""Golden transcripts for pulsar-ask. It accepts any 2xx reply.

Recorded from the bash version against a fake daemon; the Python port must pass them
unchanged. Regenerate (only for an intentional output change, in the same commit):
    pytest tests/test_cli_ask.py --update-goldens
"""
import pytest

from tests.cli_golden import REFUSED, Case, goldens_fixture, run_case

NAME = "ask"
goldens = goldens_fixture(NAME)
ENV = {"PULSE_ID": "envid"}
POSTED = (200, {"ok": True, "value": {"id": "q1"}})
LISTED = (200, {"ok": True, "value": [
    {"id": "q1", "created_at": "Mon 01:02", "text": "Ship it?"},
    {"id": "q2", "created_at": "Mon 04:05", "text": "Which colour?"}]})

CASES = [
    Case("no-args-usage-exit-2", []),
    Case("help-flag", ["--help"]),
    Case("help-short", ["-h"]),
    Case("help-not-first-is-unknown-arg", ["Q", "--help"], env=ENV),
    Case("no-id", ["Q"]),
    Case("no-id-list", ["--list"]),
    Case("ok-free-text", ["Ship it?"], reply=POSTED, env=ENV),
    Case("ok-cwd-id", ["Ship it?"], reply=POSTED, cwd="pulse"),
    Case("ok-cwd-nested", ["Ship it?"], reply=POSTED, cwd="nested"),
    Case("ok-cwd-symlink", ["Ship it?"], reply=POSTED, cwd="link"),
    Case("env-beats-cwd", ["Ship it?"], reply=POSTED, env=ENV, cwd="pulse"),
    Case("choices", ["Which?", "--choice", "A", "--choice", "B"], reply=POSTED, env=ENV),
    Case("choices-default-context", ["Which?", "--choice", "A", "--choice", "B", "--default", "A",
                                     "--context", "why it matters"], reply=POSTED, env=ENV),
    Case("empty-default-and-context-omitted", ["Q", "--default", "", "--context", ""], reply=POSTED, env=ENV),
    Case("text-looks-like-flag", ["--choice", "--choice", "A"], reply=POSTED, env=ENV),
    Case("unicode-and-quotes", ["Say \"hi\" — café $HOME `id`", "--choice", "O'K"], reply=POSTED, env=ENV),
    Case("choice-missing-value", ["Q", "--choice"], env=ENV),
    Case("default-missing-value", ["Q", "--default"], env=ENV),
    Case("context-missing-value", ["Q", "--context"], env=ENV),
    Case("unknown-arg", ["Q", "--wat"], env=ENV),
    Case("second-positional-is-unknown", ["Q", "extra"], env=ENV),
    Case("reply-201", ["Q"], reply=(201, {"ok": True, "value": {"id": "q7"}}), env=ENV),
    Case("reply-204-empty", ["Q"], reply=(204, b""), env=ENV),
    Case("reply-200-bare-object", ["Q"], reply=(200, {"id": "q8"}), env=ENV),
    Case("reply-200-no-id", ["Q"], reply=(200, {"ok": True, "value": {}}), env=ENV),
    Case("reply-200-not-json", ["Q"], reply=(200, "not json"), env=ENV),
    Case("reply-200-list", ["Q"], reply=(200, []), env=ENV),
    Case("reply-404-envelope", ["Q"], reply=(404, {"ok": False, "error": {"message": "no"}}), env=ENV),
    Case("reply-500-text-trailing-newlines", ["Q"], reply=(500, "boom\n\n"), env=ENV),
    Case("reply-500-empty", ["Q"], reply=(500, b""), env=ENV),
    Case("daemon-refused", ["Q"], reply=REFUSED, env=ENV),
    Case("list-entries", ["--list"], reply=LISTED, env=ENV),
    Case("list-empty", ["--list"], reply=(200, {"ok": True, "value": []}), env=ENV),
    Case("list-no-value-key", ["--list"], reply=(200, {"ok": True}), env=ENV),
    Case("list-bare-list-is-empty", ["--list"], reply=(200, [{"id": "q"}]), env=ENV),
    Case("list-cwd-id", ["--list"], reply=LISTED, cwd="pulse"),
    Case("list-extra-args-ignored", ["--list", "--wat"], reply=LISTED, env=ENV),
    Case("list-404-json-still-parsed", ["--list"], reply=(404, {"ok": False}), env=ENV),
    Case("list-500-text", ["--list"], reply=(500, "boom"), env=ENV),
    Case("list-200-not-json", ["--list"], reply=(200, "not json"), env=ENV),
    Case("list-entry-missing-field", ["--list"], reply=(200, {"value": [{"id": "q1"}]}), env=ENV),
    Case("list-refused", ["--list"], reply=REFUSED, env=ENV),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_transcript(case, tmp_path, goldens):
    record, _ = run_case(NAME, case, tmp_path)
    goldens.check(case.id, record)
