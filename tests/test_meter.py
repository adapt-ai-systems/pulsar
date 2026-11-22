from pulsar.runner.meter import CallMeter


class Clock:
    def __init__(self):
        self.t = 100.0

    def __call__(self):
        return self.t


def drive(m, c, steps):
    """Run (kind, arg) steps; return every metrics event in order."""
    out = []
    for kind, arg in steps:
        if kind == "t":
            c.t += arg
        elif kind == "delta":
            r = m.delta(arg)
            if r: out.append(r)
        elif kind == "start":
            r = m.start()
            if r: out.append(r)
        else:
            r = m.observe(arg)
            if r: out.append(r)
    return out


def test_claude_usage_at_message_start_before_output():
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None),
        ("ev", {"type": "usage", "in": 41000, "out": 3}),   # message_start stub
        ("t", 0.9), ("delta", "hello "), ("t", 2.0), ("delta", "x" * 394),
        ("t", 0.1), ("ev", {"type": "tool_call"}),
        ("t", 30.0), ("ev", {"type": "tool_result"}),      # tool time must not count
        ("ev", {"type": "usage", "in": 42000, "out": 3}),   # NEXT call's message_start
        ("t", 0.5), ("delta", "done"),
        ("ev", {"type": "done"}),
    ])
    assert len(got) == 2
    a, b = got
    assert a["ttft_s"] == 0.9 and a["gen_s"] == 2.1 and a["call_s"] == 3.0
    assert a["out_tok"] == 100 and a["out_est"] is True
    # the next call's message_start usage landed on the previous call's "in"
    # (it arrived before any output) — off by one tool result, acceptable.
    assert a["in_tok"] == 42000
    assert b["ttft_s"] == 0.5 and b["out_chars"] == 4


def test_pi_usage_after_tool_result_goes_to_the_finished_call():
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None),
        ("t", 4.2), ("delta", "hello!"), ("t", 0.7),
        ("ev", {"type": "tool_call"}),
        ("ev", {"type": "tool_result"}),
        ("ev", {"type": "usage", "in": 11966, "out": 46}),  # trails the tool result
        ("t", 1.0), ("delta", "done"),
        ("ev", {"type": "usage", "in": 12030, "out": 3}),
        ("ev", {"type": "done"}),
    ])
    assert len(got) == 2
    a, b = got
    assert a["in_tok"] == 11966 and a["out_tok"] == 46 and a["out_est"] is False
    assert a["ttft_s"] == 4.2 and a["call_s"] == 4.9
    assert b["in_tok"] == 12030 and b["out_est"] is True and b["ttft_s"] == 1.0


def test_codex_usage_trailing_after_done_and_close_at_next_start():
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None), ("t", 4.0),
        ("ev", {"type": "done"}),                            # nothing yet: held
        ("ev", {"type": "usage", "in": 9000, "out": 250}),   # trailing usage closes it
        ("ev", {"type": "usage", "in": 1, "out": 999}),      # nothing open: ignored
    ])
    assert len(got) == 1
    assert got[0]["ttft_s"] is None and got[0]["gen_s"] == 4.0 and got[0]["out_tok"] == 250
    # held call with content closes at the next start()
    got = drive(m, c, [("start", None), ("t", 1.0), ("delta", "hi"), ("ev", {"type": "done"})])
    assert len(got) == 1 and got[0]["out_chars"] == 2


def test_empty_spans_are_silent():
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None), ("t", 2.0), ("delta", "a" * 40),
        ("ev", {"type": "tool_call"}), ("ev", {"type": "tool_call"}),  # two calls, one message
        ("ev", {"type": "tool_result"}),
        ("ev", {"type": "tool_result"}),                                # empty span in between
        ("t", 0.5), ("delta", "ok"), ("ev", {"type": "done"}),
    ])
    assert [g["out_chars"] for g in got] == [40, 2]


def test_hidden_thinking_deltas_start_the_generation_clock():
    """30s of hidden thinking followed by 10s of visible text must time the
    full 40s span, not just the visible text. Thinking tokens arrive as
    deltas too and must open the span."""
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None),
        ("t", 3.0), ("delta", "thinking " * 100),   # hidden, 30s worth
        ("t", 30.0), ("delta", "visible"), ("t", 10.0),
        ("ev", {"type": "tool_call"}),
        ("ev", {"type": "tool_result"}),
        ("ev", {"type": "usage", "in": 63000, "out": 805}),
    ])
    assert len(got) == 1
    a = got[0]
    assert a["ttft_s"] == 3.0 and a["gen_s"] == 40.0 and a["out_tok"] == 805


def test_pi_parallel_batch_ends_at_the_last_result():
    """G9 (pi tuning plan step 0): pi runs both tool calls of one message at
    once and sends the step's usage after the LAST result. The next call
    starts there; the 100 s tool is not a model call and the usage lands on
    the call that made the tool calls (in_tok was None, DS off R1 28/160)."""
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None),
        ("t", 2.0), ("delta", "x" * 40), ("t", 1.0),
        ("ev", {"type": "tool_call", "id": "a"}),
        ("ev", {"type": "tool_call", "id": "b"}),
        ("t", 1.0), ("ev", {"type": "tool_result", "id": "b"}),
        ("t", 100.0), ("ev", {"type": "tool_result", "id": "a"}),
        ("ev", {"type": "usage", "in": 25089, "out": 46}),
        ("t", 0.5), ("delta", "done"), ("t", 0.5),
        ("ev", {"type": "usage", "in": 25400, "out": 12}),
        ("ev", {"type": "done"}),
    ])
    assert len(got) == 2
    a, b = got
    assert a["call_s"] == 3.0 and a["ttft_s"] == 2.0
    assert a["in_tok"] == 25089 and a["out_tok"] == 46 and a["out_est"] is False
    assert b["call_s"] == 1.0 and b["ttft_s"] == 0.5 and b["in_tok"] == 25400


def test_a_result_that_never_came_does_not_merge_the_next_batch():
    """A tool_call whose result never arrives (killed tool) must not hold
    every later batch open: the next message's tool_call starts a new batch."""
    c = Clock(); m = CallMeter(clock=c)
    got = drive(m, c, [
        ("start", None), ("t", 1.0), ("delta", "a" * 8),
        ("ev", {"type": "tool_call", "id": "x"}), ("ev", {"type": "tool_call", "id": "y"}),
        ("ev", {"type": "tool_result", "id": "y"}),            # x never returns
        ("t", 1.0), ("delta", "b" * 8),
        ("ev", {"type": "tool_call", "id": "z"}),
        ("t", 2.0), ("ev", {"type": "tool_result", "id": "z"}),
        ("t", 1.0), ("delta", "c" * 8), ("ev", {"type": "done"}),
    ])
    assert [g["out_chars"] for g in got] == [16, 8]
