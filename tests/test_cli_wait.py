"""Golden transcripts for pulsar-wait. Wait accepts any 2xx reply.

Recorded from the bash version against a fake daemon; the Python port must pass them
unchanged. Regenerate (only for an intentional output change, in the same commit):
    pytest tests/test_cli_wait.py --update-goldens
Timestamps (pulsar-wait in) are normalised to <TS>; their offset is asserted separately.
"""
import datetime
import json

import pytest

from tests.cli_golden import REFUSED, Case, goldens_fixture, run_case

NAME = "wait"
goldens = goldens_fixture(NAME)
ENV = {"PULSE_ID": "envid"}
W1 = (200, {"ok": True, "value": {"id": "w1"}})
LONG = "echo " + "x" * 130
# 119 ASCII bytes then a 2-byte character: head -c 120 cuts the character in half.
CUT = "a" * 119 + "étail"


def C(id, argv, reply=W1, env=ENV, cwd="plain"):
    return Case(id, argv, reply, env, cwd)


CASES = [
    # usage and dispatch
    C("no-args-usage-exit-1", [], env={}),
    C("help-flag", ["--help"]),
    C("help-short", ["-h"]),
    C("help-word", ["help"]),
    C("unknown-verb", ["wat"]),
    C("pulse-flag-too-short", ["--pulse", "x"]),
    C("pulse-flag-alone", ["--pulse"]),
    C("pulse-flag-then-help", ["--pulse", "x", "-h"]),
    C("pulse-flag-file", ["--pulse", "flagid", "file", "/tmp/f"], env={}),
    C("pulse-flag-beats-env", ["--pulse", "flagid", "file", "/tmp/f"]),
    C("empty-pulse-flag-falls-to-cwd", ["--pulse", "", "file", "/tmp/f"], cwd="pulse"),
    C("empty-pulse-flag-no-id", ["--pulse", "", "file", "/tmp/f"]),
    # pulse id sources
    C("file-id-from-cwd", ["file", "/tmp/f"], env={}, cwd="pulse"),
    C("file-id-from-nested-cwd", ["file", "/tmp/f"], env={}, cwd="nested"),
    C("file-id-from-symlink-cwd", ["file", "/tmp/f"], env={}, cwd="link"),
    C("file-env-beats-cwd", ["file", "/tmp/f"], cwd="pulse"),
    C("file-no-id", ["file", "/tmp/f"], env={}),
    C("log-no-id", ["log", "/tmp/f", "x"], env={}),
    C("http-no-id", ["http", "http://x"], env={}),
    C("cmd-no-id", ["cmd", "true"], env={}),
    C("pulse-no-id", ["pulse", "t"], env={}),
    C("list-no-id", ["list"], env={}),
    C("in-no-id", ["in", "5m"], env={}),
    # file
    C("file", ["file", "/tmp/done.flag"]),
    C("file-missing-path", ["file"]),
    C("file-extra-arg", ["file", "/tmp/f", "more"]),
    C("file-all-common-options", ["file", "/tmp/f", "--edge", "--every", "45", "--note", "my note"]),
    C("file-options-first", ["file", "--note", "n", "--every", "30", "/tmp/f"]),
    C("file-timeout-ignored", ["file", "/tmp/f", "--timeout", "abc"]),
    C("file-max-ctx-ignored", ["file", "/tmp/f", "--max-ctx", "abc"]),
    C("file-idle-sets-edge", ["file", "/tmp/f", "--idle"]),
    C("file-empty-note-falls-back", ["file", "/tmp/f", "--note", ""]),
    C("file-unicode-path", ["file", "/tmp/café \"q\" $x"]),
    C("every-not-a-number-posts-empty-body", ["file", "/tmp/f", "--every", "abc"]),
    C("timeout-not-a-number-http", ["http", "http://x", "--timeout", "x"]),
    C("max-ctx-not-a-number", ["pulse", "t", "--max-ctx", "x"]),
    C("every-missing-value", ["file", "/tmp/f", "--every"]),
    C("timeout-missing-value", ["file", "/tmp/f", "--timeout"]),
    C("note-missing-value", ["file", "/tmp/f", "--note"]),
    C("max-ctx-missing-value", ["file", "/tmp/f", "--max-ctx"]),
    # log
    C("log", ["log", "/tmp/gate.log", "exit [0-9]+"]),
    C("log-missing-regex", ["log", "/tmp/gate.log"]),
    C("log-missing-all", ["log"]),
    C("log-extra-arg", ["log", "/tmp/g", "x", "y"]),
    C("log-options", ["log", "/tmp/g", "x", "--edge", "--note", "n"]),
    # http
    C("http", ["http", "http://localhost:8726/health"]),
    C("http-contains", ["http", "http://x", "--contains", "ok"]),
    C("http-up", ["http", "http://x", "--up"]),
    C("http-up-contains", ["http", "http://x", "--up", "--contains", "ok"]),
    C("http-timeout", ["http", "http://x", "--timeout", "5", "--every", "30"]),
    C("http-empty-contains", ["http", "http://x", "--contains", ""]),
    C("http-missing-url", ["http"]),
    C("http-unknown-flag", ["http", "http://x", "--wat"]),
    C("http-contains-missing-value", ["http", "http://x", "--contains"]),
    C("http-second-positional-unknown", ["http", "http://x", "y"]),
    # cmd
    C("cmd", ["cmd", "test -f /tmp/x"]),
    C("cmd-ok", ["cmd", "test -f /tmp/x", "--ok"]),
    C("cmd-timeout", ["cmd", "true", "--timeout", "10"]),
    C("cmd-long-note-cut-at-120", ["cmd", LONG]),
    C("cmd-long-note-cut-at-120-ok", ["cmd", LONG, "--ok"]),
    C("cmd-note-cut-mid-character", ["cmd", CUT]),
    C("cmd-trailing-newline-in-command", ["cmd", "echo hi\n\n"]),
    C("cmd-missing", ["cmd"]),
    C("cmd-unknown-flag", ["cmd", "true", "--wat"]),
    C("cmd-two-positionals", ["cmd", "true", "false"]),
    # pulse
    C("pulse", ["pulse", "target1"]),
    C("pulse-idle", ["pulse", "target1", "--idle"]),
    C("pulse-max-ctx", ["pulse", "target1", "--max-ctx", "100000"]),
    C("pulse-idle-max-ctx-note", ["pulse", "target1", "--idle", "--max-ctx", "5", "--note", "n"]),
    C("pulse-flags-before-target", ["pulse", "--idle", "target1"]),
    C("pulse-missing-target", ["pulse"]),
    C("pulse-extra-arg", ["pulse", "t", "u"]),
    C("pulse-idle-edge-every", ["pulse", "t", "--idle", "--every", "30"]),
    # list
    C("list-watches-key", ["list"], reply=(200, {"ok": True, "value": {"watches": [
        {"id": "w1", "kind": "file", "mode": "once", "note": "pulsar-wait: file /tmp/f"},
        {"id": "w2", "kind": "pulse_state", "mode": "edge", "note": "n2"},
        {"id": "w3"}]}})),
    C("list-value-list", ["list"], reply=(200, {"value": [{"id": "w1", "kind": "log", "mode": "edge", "note": "x"}]})),
    C("list-bare-list", ["list"], reply=(200, [{"id": "w9", "kind": "http", "mode": "once", "note": None}])),
    C("list-empty-value", ["list"], reply=(200, {"ok": True, "value": []})),
    C("list-empty-watches", ["list"], reply=(200, {"ok": True, "value": {"watches": []}})),
    C("list-empty-object", ["list"], reply=(200, {})),
    C("list-cwd-id", ["list"], reply=(200, {"value": []}), env={}, cwd="pulse"),
    C("list-extra-args-ignored", ["list", "--wat"], reply=(200, {"value": []})),
    C("list-404-error-envelope", ["list"], reply=(404, {"ok": False, "error": {"message": "no"}})),
    C("list-500-text", ["list"], reply=(500, "boom")),
    C("list-200-not-json", ["list"], reply=(200, "nope")),
    C("list-refused", ["list"], reply=REFUSED),
    # rm
    C("rm", ["rm", "w1"], reply=(200, {"ok": True})),
    C("rm-204", ["rm", "w1"], reply=(204, b"")),
    C("rm-201", ["rm", "w1"], reply=(201, {})),
    C("rm-404", ["rm", "w1"], reply=(404, {"ok": False})),
    C("rm-500", ["rm", "w1"], reply=(500, "boom")),
    C("rm-refused", ["rm", "w1"], reply=REFUSED),
    C("rm-missing-id", ["rm"]),
    C("rm-needs-no-pulse-id", ["rm", "w1"], reply=(200, {}), env={}),
    # in
    C("in-seconds", ["in", "90s"]),
    C("in-minutes", ["in", "10m", "check the gate"]),
    C("in-hours", ["in", "2h"]),
    C("in-bare-number", ["in", "45"]),
    C("in-note-words-joined", ["in", "5m", "check", "the", "build"]),
    C("in-empty-note-is-default", ["in", "5m", ""]),
    C("in-no-args", ["in"]),
    C("in-bad-duration", ["in", "soon"]),
    C("in-bad-seconds-suffix", ["in", "abcs"]),
    C("in-bad-bare", ["in", "5x"]),
    C("in-negative", ["in", "-5"]),
    C("in-zero", ["in", "0"]),
    C("in-zero-minutes", ["in", "0m"]),
    C("in-note-looks-like-flag", ["in", "5m", "--wat"]),
    C("in-pulse-flag", ["--pulse", "flagid", "in", "1m", "hi"], env={}),
    C("in-id-from-cwd", ["in", "1m"], env={}, cwd="pulse"),
    # register replies
    C("reply-watch-key", ["file", "/tmp/f"], reply=(200, {"ok": True, "value": {"watch": {"id": "w5"}}})),
    C("reply-schedule-key", ["in", "5m"], reply=(200, {"ok": True, "value": {"schedule": {"id": "s7"}}})),
    C("reply-bare-object", ["file", "/tmp/f"], reply=(200, {"id": "w6"})),
    C("reply-no-id", ["file", "/tmp/f"], reply=(200, {"ok": True, "value": {}})),
    C("reply-null-id", ["file", "/tmp/f"], reply=(200, {"value": {"id": None}})),
    C("reply-numeric-id", ["file", "/tmp/f"], reply=(200, {"value": {"id": 12}})),
    C("reply-empty-watch-falls-to-value", ["file", "/tmp/f"], reply=(200, {"value": {"watch": {}, "id": "w8"}})),
    C("reply-201", ["file", "/tmp/f"], reply=(201, {"ok": True, "value": {"id": "w2"}})),
    C("reply-204", ["file", "/tmp/f"], reply=(204, b"")),
    C("reply-200-not-json", ["file", "/tmp/f"], reply=(200, "not json")),
    C("reply-200-list", ["file", "/tmp/f"], reply=(200, [1])),
    C("reply-200-value-list", ["file", "/tmp/f"], reply=(200, {"value": [1]})),
    C("reply-404-envelope", ["file", "/tmp/f"], reply=(404, {"ok": False, "error": {"message": "no"}})),
    C("reply-500-text-trailing-newlines", ["file", "/tmp/f"], reply=(500, "boom\n\n")),
    C("reply-500-empty", ["file", "/tmp/f"], reply=(500, b"")),
    C("reply-refused", ["file", "/tmp/f"], reply=REFUSED),
    C("in-reply-refused", ["in", "5m"], reply=REFUSED),
]


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.id)
def test_transcript(case, tmp_path, goldens):
    record, _ = run_case(NAME, case, tmp_path)
    goldens.check(case.id, record)


@pytest.mark.parametrize("arg, seconds", [("90s", 90), ("10m", 600), ("2h", 7200), ("45", 45)])
def test_in_schedules_at_now_plus_duration(arg, seconds, tmp_path):
    before = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    _, requests = run_case(NAME, C("t", ["in", arg]), tmp_path)
    after = datetime.datetime.now(datetime.timezone.utc).replace(tzinfo=None)
    at = datetime.datetime.fromisoformat(requests[0]["body"]["at_iso"])
    assert before + datetime.timedelta(seconds=seconds - 1) <= at <= after + datetime.timedelta(seconds=seconds + 1)
