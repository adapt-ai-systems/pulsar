"""Golden transcripts for pulsar-msg (message another pulse; JSON built by the CLI)."""
import pytest

from tests.cli_golden import REFUSED, Case, goldens_fixture, run_case

NAME = "msg"
goldens = goldens_fixture(NAME)
TARGET = "ffffffff6666"
TRICKY = "it's (done); \"quoted\" & $PATH `x`"

CASES = [
    Case("help-flag", ["--help"]),
    Case("no-args", []),
    Case("from-cwd", [TARGET, "hello", "there"], cwd="pulse"),
    Case("tricky-text", [TARGET, TRICKY], cwd="pulse"),
    Case("from-env", [TARGET, "hi"], env={"PULSAR_PULSE_ID": "envpulse42"}),
    Case("from-flag", ["--from", "flagpulse7", TARGET, "hi"]),
    Case("no-sender", [TARGET, "hi"]),
    Case("self-target", ["--from", TARGET, TARGET, "hi"]),
    Case("empty-text", [TARGET, "  "], cwd="pulse"),
    Case("daemon-202", [TARGET, "hi"], reply=(202, {"ok": True, "value": {"queued": "x.json"}}),
         cwd="pulse"),
    Case("daemon-404", [TARGET, "hi"], reply=(404, {"ok": False, "error": {"message": "no such pulse"}}),
         cwd="pulse"),
    Case("daemon-down", [TARGET, "hi"], reply=REFUSED, cwd="pulse"),
]


@pytest.mark.parametrize("case", CASES, ids=[c.id for c in CASES])
def test_golden(case, goldens, tmp_path):
    record, requests = run_case(NAME, case, tmp_path)
    goldens.check(case.id, record)
    if case.id in ("from-cwd", "tricky-text", "from-env", "from-flag"):
        assert record["exit"] == 0
        body = requests[0]["body"]
        assert requests[0]["path"] == f"/pulses/{TARGET}/messages"
        assert body["from_pulse_id"] == {"from-cwd": "pid123abc", "tricky-text": "pid123abc",
                                         "from-env": "envpulse42", "from-flag": "flagpulse7"}[case.id]
        if case.id == "tricky-text":
            assert body["text"] == TRICKY
    elif case.id == "daemon-202":
        assert record["exit"] == 0 and "queued" in record["stdout"]
    elif case.id == "help-flag":
        assert record["exit"] == 0 and not requests
    else:
        assert record["exit"] != 0
        assert not requests or record["exit"] == 1

