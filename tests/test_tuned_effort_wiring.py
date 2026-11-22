from pulsar.vessel.pi import pi_thinking_level


def test_tuned_effort_dial_reaches_pi():
    # Unknown pi models are forced off; Tuned must take the Qwen3.8 dial.
    assert pi_thinking_level("pi-tuned-flash", None) == "low"  # registry default
    assert pi_thinking_level("pi-tuned-flash", "off") == "off"
    assert pi_thinking_level("pi-tuned-flash", "medium") == "medium"
    assert pi_thinking_level("tunedlocal/tuned", "xhigh") == "xhigh"


def test_tuned_dual_takes_the_same_dial():
    from pulsar.vessel.pi import pi_thinking_level
    assert pi_thinking_level("pi-tuned-flash-dual", None) == "low"
    assert pi_thinking_level("pi-tuned-flash-dual", "off") == "off"
    assert pi_thinking_level("tunedlocal/tuned-dual", "medium") == "medium"


def test_tuned_abliterated_effort_wiring():
    assert pi_thinking_level("pi-tuned-flash-abliterated", None) == "low"
    assert pi_thinking_level("pi-tuned-flash-abliterated", "off") == "off"
