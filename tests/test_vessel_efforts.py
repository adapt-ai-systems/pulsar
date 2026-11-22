from pulsar.vessel import efforts_for


def test_qwen38_pi_effort_override():
    assert efforts_for("pi-qwen38-flash", "pi") == (
        "off", "low", "medium", "xhigh")
