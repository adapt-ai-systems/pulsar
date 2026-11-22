"""Focused regression tests for pi's live assistant stream."""

from pulsar.vessel.pi import classify_record


def test_message_update_text_delta_reaches_runner_partial_stream():
    events = classify_record({
        "type": "message_update",
        "message": {"role": "assistant"},
        "assistantMessageEvent": {
            "type": "text_delta",
            "contentIndex": 0,
            "delta": "still working",
        },
    })

    assert len(events) == 1
    assert events[0].kind == "assistant_delta"
    assert events[0].payload == {"text": "still working"}


def test_message_update_thinking_delta_is_reasoning_toolcall_is_hidden():
    """Thinking becomes the visible reasoning trace; tool-call JSON stays
    meter-only. Both are generated tokens, so both feed the speed meter."""
    events = classify_record({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": "hmm"},
    })
    assert [e.kind for e in events] == ["reasoning_delta"]
    assert events[0].payload == {"text": "hmm"}

    events = classify_record({
        "type": "message_update",
        "assistantMessageEvent": {"type": "toolcall_delta", "delta": "{\"a\":"},
    })
    assert [e.kind for e in events] == ["code_delta"]
    assert events[0].payload == {"text": "{\"a\":", "name": None, "id": None}

    events = classify_record({
        "type": "message_update",
        "assistantMessageEvent": {
            "type": "toolcall_delta", "delta": "x", "contentIndex": 1,
            "partial": {"content": [
                {"type": "text", "text": "hi"},
                {"type": "toolCall", "id": "call_1", "name": "write",
                 "arguments": {}}]}},
    })
    assert events[0].payload == {"text": "x", "name": "write", "id": "call_1"}


def test_message_update_ignores_empty_deltas():
    assert classify_record({
        "type": "message_update",
        "assistantMessageEvent": {"type": "thinking_delta", "delta": ""},
    }) == []
    assert classify_record({
        "type": "message_update",
        "assistantMessageEvent": {"type": "text_delta", "delta": ""},
    }) == []
